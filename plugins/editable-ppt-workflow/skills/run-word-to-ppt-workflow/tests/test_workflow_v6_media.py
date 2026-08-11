"""Security and fidelity contracts for V6 reference media normalization."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]
MEDIA_PATH = ROOT / "scripts" / "workflow_v6_media.py"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_media():
    spec = importlib.util.spec_from_file_location("workflow_v6_media_under_test", MEDIA_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def save_photo(path: Path, *, size: tuple[int, int] = (120, 60), exif: Image.Exif | None = None) -> None:
    image = Image.new("RGB", size, "#336699")
    image.save(path, format="JPEG", exif=exif or Image.Exif())


def png_with_dimensions(width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + b"IHDR" + header + struct.pack(">I", zlib.crc32(b"IHDR" + header) & 0xffffffff) + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xffffffff)


def test_normalize_uses_decoded_magic_not_misleading_extension_and_preserves_original_bytes(tmp_path: Path):
    media = load_media()
    source = tmp_path / "misleading.jpg"
    Image.new("RGB", (16, 8), "red").save(source, format="PNG")
    before = source.read_bytes()

    result = media.normalize_reference(tmp_path / "project", source, reference_id="ref-1", kind="screenshot")

    assert result.mime_type == "image/png"
    assert (tmp_path / "project" / result.original_path).read_bytes() == before
    with Image.open(tmp_path / "project" / result.model_input_path) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (16, 8)


def test_normalize_rejects_corrupt_magic_and_decoded_image_bombs(tmp_path: Path):
    media = load_media()
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\nnot-an-image")
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(png_with_dimensions(16_384, 16_384))

    with pytest.raises(ValueError, match="decode|image"):
        media.normalize_reference(tmp_path / "project", corrupt, reference_id="corrupt", kind="screenshot")
    with pytest.raises(ValueError, match="pixel|dimension|limit"):
        media.normalize_reference(tmp_path / "project", bomb, reference_id="bomb", kind="screenshot")


def test_photo_normalization_applies_orientation_strips_gps_and_preserves_ratio(tmp_path: Path):
    media = load_media()
    source = tmp_path / "portrait.jpg"
    exif = Image.Exif()
    exif[274] = 6
    exif[37510] = b"sensitive-location-metadata"
    save_photo(source, size=(120, 60), exif=exif)

    result = media.normalize_reference(tmp_path / "project", source, reference_id="portrait", kind="photo")

    with Image.open(tmp_path / "project" / result.model_input_path) as normalized:
        assert normalized.size == (60, 120)
        assert normalized.getexif().get(37510) is None
        assert normalized.getexif().get(274) is None
    assert result.width == 60 and result.height == 120


def test_screenshot_model_input_is_lossless_png_and_svg_logo_is_safe_raster_without_stretch(tmp_path: Path):
    media = load_media()
    screenshot = tmp_path / "shot.png"
    source_image = Image.new("RGBA", (96, 48), "#11aa77")
    source_image.save(screenshot, format="PNG")
    screenshot_result = media.normalize_reference(tmp_path / "project", screenshot, reference_id="shot", kind="screenshot")
    with Image.open(tmp_path / "project" / screenshot_result.model_input_path) as actual:
        assert actual.format == "PNG"
        assert ImageChops.difference(source_image, actual.convert("RGBA")).getbbox() is None

    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="#224488"/></svg>', encoding="utf-8")
    logo_result = media.normalize_reference(tmp_path / "project", logo, reference_id="logo", kind="logo")
    with Image.open(tmp_path / "project" / logo_result.model_input_path) as actual:
        assert actual.format == "PNG"
        assert actual.size[0] == actual.size[1] * 2


def test_svg_with_script_or_external_reference_is_rejected(tmp_path: Path):
    media = load_media()
    for name, body in {
        "script": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        "external": '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.test/a.png"/></svg>',
    }.items():
        source = tmp_path / f"{name}.svg"
        source.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match="SVG|svg|external|script"):
            media.normalize_reference(tmp_path / "project", source, reference_id=name, kind="logo")


def test_project_media_resolution_rejects_traversal_and_symlink_escape(tmp_path: Path):
    media = load_media()
    project = tmp_path / "project"
    safe = project / "02_v6" / "reference_media" / "ref" / "thumbnail.png"
    safe.parent.mkdir(parents=True)
    safe.write_bytes(b"ok")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    escape = project / "02_v6" / "reference_media" / "ref" / "escape.png"
    try:
        escape.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert media.resolve_project_media(project, "02_v6/reference_media/ref/thumbnail.png", variant="thumbnail") == safe.resolve()
    with pytest.raises(ValueError, match="escape|project|path"):
        media.resolve_project_media(project, "../outside.png", variant="thumbnail")
    with pytest.raises(ValueError, match="escape|project|path"):
        media.resolve_project_media(project, "02_v6/reference_media/ref/escape.png", variant="thumbnail")


def test_project_media_resolution_accepts_jpeg_photo_model_input(tmp_path: Path):
    media = load_media()
    project = tmp_path / "project"
    model = project / "02_v6" / "reference_media" / "photo" / "model-input.jpg"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"ok")

    assert media.resolve_project_media(project, "02_v6/reference_media/photo/model-input.jpg", variant="model-input") == model.resolve()


def test_reference_import_keeps_acquisition_lifecycle_and_records_normalized_local_media(tmp_path: Path, monkeypatch):
    from workflow_v6_contract import new_page, new_project
    from workflow_v6_materials import new_page_materials
    from workflow_v6_source import import_reference
    from workflow_v6_state import create

    project = tmp_path / "project"
    create(project, new_project(
        word_source={"path": "00_source/source.docx", "sha256": "a" * 64},
        logo_source={"path": "00_source/logo.svg", "sha256": "b" * 64},
        pages=[new_page(1, title="Evidence")],
    ))
    materials = new_page_materials(page_number=1, fixed_page_title="Evidence", word_original="Evidence", effective_body="")
    (project / "02_v6/page_materials").mkdir(parents=True)
    (project / "02_v6/page_materials/page_001.json").write_text(__import__("json").dumps(materials), encoding="utf-8")
    receipt = {"artifact_version": "reference-materials-v6", "page_number": 1, "references": [], "search_requests": [], "reference_acquisitions": [{"request_id": "request-1", "page_number": 1, "purpose": "verified storefront image", "identity_evidence_need": "storefront", "status": "pending", "history": ["pending"]}]}
    (project / "02_v6/reference_materials").mkdir(parents=True)
    (project / "02_v6/reference_materials/page_001.json").write_text(__import__("json").dumps(receipt), encoding="utf-8")
    image = tmp_path / "candidate.png"
    Image.new("RGB", (20, 10), "#cc8844").save(image, format="PNG")
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: pytest.fail("source URL was fetched"))

    result = import_reference(project, page_number=1, request_id="request-1", image=image, source_url="https://example.test/candidate.png")

    candidate = result["candidate"]
    reference = candidate["reference"]
    assert result["status"] == "found"
    assert candidate["source_url"] == "https://example.test/candidate.png"
    assert candidate["local_path"] == reference["original_path"]
    assert reference["thumbnail_path"].endswith("thumbnail.png")
    assert Path(reference["model_input_path"]).stem == "model-input"
    assert (project / reference["original_path"]).read_bytes() == image.read_bytes()
