"""Bounded, safe local reference-media handling for V6 projects."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Final
from xml.etree import ElementTree

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError


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
_UNSAFE_SVG_TAGS = frozenset({"script", "image", "foreignobject", "iframe", "object", "embed", "link"})
_BROWSER_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)

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


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _safe_directory(project: Path, reference_id: str) -> Path:
    root = Path(project).resolve()
    for directory in (root, root / "02_v6", root / "02_v6" / "reference_media", root / "02_v6" / "reference_media" / reference_id):
        if directory.exists():
            if not directory.is_dir() or _is_link_or_reparse(directory):
                raise ValueError("reference media output parent is a link, reparse point, or non-directory")
            continue
        directory.mkdir()
        if _is_link_or_reparse(directory):
            raise ValueError("reference media output parent is a link or reparse point")
    return directory


def _write_new(path: Path, data: bytes) -> None:
    if _is_link_or_reparse(path) or path.exists():
        raise ValueError("reference media output already exists or is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _image_bytes(image: Image.Image, *, image_format: str, **options: object) -> bytes:
    output = BytesIO()
    image.save(output, format=image_format, **options)
    return output.getvalue()


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


def _svg_canvas_size(root: ElementTree.Element) -> tuple[int, int]:
    view_box = root.get("viewBox", "").replace(",", " ").split()
    view_width = _svg_dimension(view_box[2]) if len(view_box) == 4 else None
    view_height = _svg_dimension(view_box[3]) if len(view_box) == 4 else None
    width = _svg_dimension(root.get("width")) if not str(root.get("width", "")).endswith("%") else None
    height = _svg_dimension(root.get("height")) if not str(root.get("height", "")).endswith("%") else None
    width = width or view_width
    height = height or view_height
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("SVG requires numeric dimensions or a valid viewBox")
    _checked_size(round(width), round(height))
    return max(1, round(width)), max(1, round(height))


def _safe_svg_root(data: bytes) -> tuple[ElementTree.Element, int, int]:
    if len(data) > MAX_ENCODED_BYTES:
        raise ValueError("image encoded size exceeds the limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SVG must be UTF-8") from exc
    if re.search(r"<!DOCTYPE|<!ENTITY|<\s*script\b|\bon[a-z]+\s*=", text, re.IGNORECASE):
        raise ValueError("SVG script or external resource is not allowed")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("SVG is malformed") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError("SVG root is required")
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in _UNSAFE_SVG_TAGS or tag == "style":
            raise ValueError("SVG contains an unsafe or unsupported element")
        for attribute, value in element.attrib.items():
            local_name = attribute.rsplit("}", 1)[-1].lower()
            normalized = value.strip().lower()
            if local_name in {"href", "src"} and not normalized.startswith("#"):
                raise ValueError("SVG external resource is not allowed")
            for target in re.findall(r"url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)", normalized):
                if not target.startswith("#"):
                    raise ValueError("SVG external resource is not allowed")
    width, height = _svg_canvas_size(root)
    return root, width, height


def _browser_renderer() -> Path:
    executable = next((path for path in _BROWSER_PATHS if path.is_file()), None)
    if executable is None:
        raise ValueError("a safe SVG renderer is unavailable")
    return executable


def _safe_svg_raster(data: bytes, destination: Path) -> Image.Image:
    root, width, height = _safe_svg_root(data)
    ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
    scale = min(1.0, MODEL_MAX_EDGE / max(width, height))
    output_width, output_height = max(1, round(width * scale)), max(1, round(height * scale))
    root.set("width", str(output_width))
    root.set("height", str(output_height))
    html = destination / ".safe-render.html"
    output = destination / ".safe-render.png"
    profile = destination / ".safe-render-profile"
    if profile.exists() or _is_link_or_reparse(profile):
        raise ValueError("SVG renderer profile output is unsafe")
    profile.mkdir()
    markup = (
        "<!doctype html><meta charset=utf-8><style>html,body{margin:0;padding:0;overflow:hidden;}svg{display:block;}</style>"
        + ElementTree.tostring(root, encoding="unicode")
    ).encode("utf-8")
    _write_new(html, markup)
    try:
        completed = subprocess.run(
            [str(_browser_renderer()), "--headless=new", "--disable-gpu", "--disable-extensions", "--disable-background-networking", "--disable-component-update", "--disable-sync", "--hide-scrollbars", "--allow-file-access-from-files", "--run-all-compositor-stages-before-draw", "--force-device-scale-factor=1", f"--user-data-dir={profile}", f"--window-size={output_width},{output_height}", f"--screenshot={output}", html.resolve().as_uri()],
            check=False, capture_output=True, timeout=20,
        )
        if completed.returncode != 0 or not output.is_file() or _is_link_or_reparse(output):
            raise ValueError("SVG could not be safely rasterized")
        image, _mime_type = _open_raster(_read_file_limited(output))
        white = Image.new("RGB", image.size, "white")
        if image.size != (output_width, output_height) or ImageChops.difference(image.convert("RGB"), white).getbbox() is None:
            raise ValueError("SVG renderer produced an invalid or blank preview")
        return image
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("SVG could not be safely rasterized") from exc
    finally:
        for temporary in (html, output):
            temporary.unlink(missing_ok=True)
        if profile.exists() and not _is_link_or_reparse(profile):
            for child in sorted(profile.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir() and not _is_link_or_reparse(child):
                    child.rmdir()
            profile.rmdir()


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
    destination = _safe_directory(project, reference_id)
    if is_svg:
        if kind != "logo":
            raise ValueError("SVG is only accepted for a logo reference")
        image, mime_type = _safe_svg_raster(data, destination), "image/svg+xml"
        original_suffix = ".svg"
    else:
        image, mime_type = _open_raster(data)
        original_suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/bmp": ".bmp"}[mime_type]
    original = destination / f"original{original_suffix}"
    _write_new(original, data)
    if kind == "photo":
        image = ImageOps.exif_transpose(image)
        model = _resized(_rgb(image), MODEL_MAX_EDGE)
        model_path = destination / "model-input.jpg"
        _write_new(model_path, _image_bytes(model, image_format="JPEG", quality=90, optimize=True))
    else:
        model = _resized(image, MODEL_MAX_EDGE)
        model_path = destination / "model-input.png"
        _write_new(model_path, _image_bytes(model, image_format="PNG", optimize=False, compress_level=9))
    thumbnail = _resized(model, THUMBNAIL_MAX_EDGE)
    thumbnail_path = destination / "thumbnail.png"
    _write_new(thumbnail_path, _image_bytes(thumbnail, image_format="PNG", optimize=False, compress_level=9))
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
    _image, mime_type = _open_raster(_read_file_limited(Path(path)))
    return mime_type


def _read_file_limited(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        expected = path.lstat()
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(expected, actual):
                raise ValueError("media file changed or is unsafe")
            data = handle.read(MAX_ENCODED_BYTES + 1)
    except Exception:
        raise
    if len(data) > MAX_ENCODED_BYTES:
        raise ValueError("image encoded size exceeds the limit")
    return data


def read_validated_project_media(project: Path, relative_path: str, *, variant: str) -> tuple[bytes, str, Path]:
    """Read, validate, and return one immutable-in-memory media response buffer."""
    path = resolve_project_media(project, relative_path, variant=variant)
    data = _read_file_limited(path)
    _image, mime_type = _open_raster(data)
    return data, mime_type, path
