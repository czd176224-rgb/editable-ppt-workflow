"""Bounded, safe local reference-media handling for V6 projects."""

from __future__ import annotations

import hashlib
import re
import shutil
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Final
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


MAX_ENCODED_BYTES: Final = 25 * 1024 * 1024
MAX_DECODED_PIXELS: Final = 80_000_000
MAX_EDGE: Final = 16_384
MODEL_MAX_EDGE: Final = 2_048
THUMBNAIL_MAX_EDGE: Final = 512
MEDIA_ROOT: Final = PurePosixPath("02_v6/reference_media")
_RASTER_FORMATS: Final = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "BMP": "image/bmp"}
_SAFE_VARIANTS: Final = {
    "original": "original",
    "thumbnail": "thumbnail.png",
    "model-input": "model-input.",
}
_REFERENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")

# Enforce the V6 boundary consistently for every Pillow decode in this process.
Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS


@dataclass(frozen=True)
class NormalizedReference:
    original_path: str
    thumbnail_path: str
    model_input_path: str
    original_sha256: str
    model_input_sha256: str
    mime_type: str
    width: int
    height: int


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checked_size(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise ValueError("image dimensions are invalid")
    if width > MAX_EDGE or height > MAX_EDGE or width * height > MAX_DECODED_PIXELS:
        raise ValueError("image dimensions exceed the decoded-image limit")


def _open_raster(data: bytes) -> tuple[Image.Image, str]:
    if not data or len(data) > MAX_ENCODED_BYTES:
        raise ValueError("image encoded size exceeds the limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as inspected:
                image_format = inspected.format or ""
                mime_type = _RASTER_FORMATS.get(image_format)
                if mime_type is None:
                    raise ValueError("unsupported image magic or MIME type")
                _checked_size(*inspected.size)
                inspected.verify()
            with Image.open(BytesIO(data)) as decoded:
                _checked_size(*decoded.size)
                decoded.load()
                return decoded.copy(), mime_type
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("image dimensions exceed the decoded-image limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("unsupported", "image dimensions", "image encoded")):
            raise
        raise ValueError("image magic or decoded content is invalid") from exc


def _contained(project: Path, candidate: Path) -> Path:
    root = Path(project).resolve()
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("media path escapes the project") from exc
    return resolved


def _relative(project: Path, path: Path) -> str:
    return path.relative_to(Path(project).resolve()).as_posix()


def _resized(image: Image.Image, maximum: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
    return copy


def _rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def _svg_dimension(value: str | None) -> float | None:
    if value is None:
        return None
    match = _NUMBER.match(value.strip())
    return float(match.group(0)) if match else None


def _svg_color(value: str | None) -> str | None:
    if not value or value.lower() in {"none", "transparent"}:
        return None
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        if len(value) == 4:
            return "#" + "".join(char * 2 for char in value[1:])
        return value[:7]
    if re.fullmatch(r"[A-Za-z]+", value):
        return value
    return None


def _safe_svg_raster(data: bytes) -> Image.Image:
    if len(data) > MAX_ENCODED_BYTES:
        raise ValueError("image encoded size exceeds the limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SVG must be UTF-8") from exc
    if re.search(r"<!DOCTYPE|<!ENTITY|<\s*script\b|\bon[a-z]+\s*=|\b(?:href|src)\s*=\s*['\"](?:https?:|//|file:|data:)", text, re.IGNORECASE):
        raise ValueError("SVG script or external resource is not allowed")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("SVG is malformed") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError("SVG root is required")
    width = _svg_dimension(root.get("width"))
    height = _svg_dimension(root.get("height"))
    view_box = root.get("viewBox", "").replace(",", " ").split()
    if (width is None or height is None) and len(view_box) == 4:
        width = width or _svg_dimension(view_box[2])
        height = height or _svg_dimension(view_box[3])
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("SVG dimensions are required")
    _checked_size(int(width), int(height))
    scale = min(1.0, MODEL_MAX_EDGE / max(width, height))
    canvas = Image.new("RGBA", (max(1, round(width * scale)), max(1, round(height * scale))), (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in {"svg", "g", "defs", "title", "desc"}:
            continue
        if tag in {"script", "image", "use", "foreignobject", "iframe", "object", "embed", "link", "style"}:
            raise ValueError("SVG contains an unsafe or unsupported element")
        fill = _svg_color(element.get("fill"))
        if tag == "rect" and fill:
            x, y = _svg_dimension(element.get("x")) or 0, _svg_dimension(element.get("y")) or 0
            w, h = _svg_dimension(element.get("width")), _svg_dimension(element.get("height"))
            if w is not None and h is not None and w >= 0 and h >= 0:
                draw.rectangle((x * scale, y * scale, (x + w) * scale, (y + h) * scale), fill=fill)
        elif tag == "circle" and fill:
            cx, cy, radius = _svg_dimension(element.get("cx")) or 0, _svg_dimension(element.get("cy")) or 0, _svg_dimension(element.get("r"))
            if radius is not None and radius >= 0:
                draw.ellipse(((cx - radius) * scale, (cy - radius) * scale, (cx + radius) * scale, (cy + radius) * scale), fill=fill)
        elif tag == "ellipse" and fill:
            cx, cy = _svg_dimension(element.get("cx")) or 0, _svg_dimension(element.get("cy")) or 0
            rx, ry = _svg_dimension(element.get("rx")), _svg_dimension(element.get("ry"))
            if rx is not None and ry is not None and rx >= 0 and ry >= 0:
                draw.ellipse(((cx - rx) * scale, (cy - ry) * scale, (cx + rx) * scale, (cy + ry) * scale), fill=fill)
    return canvas


def normalize_reference(project: Path, source: Path, *, reference_id: str, kind: str) -> NormalizedReference:
    """Preserve source bytes while producing bounded, safe derivatives."""
    project = Path(project).resolve()
    source = Path(source)
    if not _REFERENCE_ID.fullmatch(reference_id):
        raise ValueError("reference_id is invalid")
    if kind not in {"photo", "screenshot", "logo"}:
        raise ValueError("reference kind is invalid")
    if not source.is_file() or source.stat().st_size > MAX_ENCODED_BYTES:
        raise ValueError("reference image is unavailable or exceeds the encoded limit")
    data = source.read_bytes()
    is_svg = data.lstrip().startswith(b"<svg") or data.lstrip().startswith(b"<?xml") and b"<svg" in data[:1024]
    if is_svg:
        if kind != "logo":
            raise ValueError("SVG is only accepted for a logo reference")
        image, mime_type = _safe_svg_raster(data), "image/svg+xml"
        original_suffix = ".svg"
    else:
        image, mime_type = _open_raster(data)
        original_suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/bmp": ".bmp"}[mime_type]
    destination = project / Path(MEDIA_ROOT) / reference_id
    destination.mkdir(parents=True, exist_ok=True)
    original = destination / f"original{original_suffix}"
    shutil.copyfile(source, original)
    if kind == "photo":
        image = ImageOps.exif_transpose(image)
        model = _resized(_rgb(image), MODEL_MAX_EDGE)
        model_path = destination / "model-input.jpg"
        model.save(model_path, format="JPEG", quality=90, optimize=True)
    else:
        model = _resized(image, MODEL_MAX_EDGE)
        model_path = destination / "model-input.png"
        model.save(model_path, format="PNG", optimize=False, compress_level=9)
    thumbnail = _resized(model, THUMBNAIL_MAX_EDGE)
    thumbnail_path = destination / "thumbnail.png"
    thumbnail.save(thumbnail_path, format="PNG", optimize=False, compress_level=9)
    return NormalizedReference(
        original_path=_relative(project, original),
        thumbnail_path=_relative(project, thumbnail_path),
        model_input_path=_relative(project, model_path),
        original_sha256=_sha256(original),
        model_input_sha256=_sha256(model_path),
        mime_type=mime_type,
        width=model.width,
        height=model.height,
    )


def resolve_project_media(project: Path, relative_path: str, *, variant: str) -> Path:
    """Resolve only a declared V6 media derivative under the project root."""
    if variant not in _SAFE_VARIANTS or not isinstance(relative_path, str):
        raise ValueError("media variant or path is invalid")
    raw = PurePosixPath(relative_path.replace("\\", "/"))
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts) or not raw.is_relative_to(MEDIA_ROOT):
        raise ValueError("media path escapes the project")
    expected = _SAFE_VARIANTS[variant]
    if variant == "original" and not raw.name.startswith(expected + "."):
        raise ValueError("media variant does not match path")
    if variant == "model-input" and not raw.name.startswith(expected):
        raise ValueError("media variant does not match path")
    if variant == "thumbnail" and raw.name != expected:
        raise ValueError("media variant does not match path")
    return _contained(Path(project), Path(project).resolve().joinpath(*raw.parts))


def validated_media_mime(path: Path) -> str:
    """Return a safe HTTP MIME only after the stored raster decodes cleanly."""
    _image, mime_type = _open_raster(Path(path).read_bytes())
    return mime_type
