from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from backend.importers import import_document


@pytest.mark.parametrize(
    ("mode", "pixels", "transparency", "expected"),
    [
        pytest.param(
            "P", [0, 1, 0], 0,
            [(255, 255, 255), (0, 0, 0), (255, 255, 255)],
            id="palette-transparent-index",
        ),
        pytest.param(
            "P", [0, 1, 2], bytes([0, 255, 128]),
            [(255, 255, 255), (0, 0, 0), (127, 127, 127)],
            id="palette-alpha-table",
        ),
        pytest.param(
            "RGB", [(10, 20, 30), (0, 0, 0), (10, 20, 30)], (10, 20, 30),
            [(255, 255, 255), (0, 0, 0), (255, 255, 255)],
            id="rgb-transparent-color",
        ),
        pytest.param(
            "L", [128, 0, 128], 128,
            [(255, 255, 255), (0, 0, 0), (255, 255, 255)],
            id="grayscale-transparent-color",
        ),
        pytest.param(
            "RGBA", [(0, 0, 0, 0), (0, 0, 0, 255), (0, 0, 0, 128)], None,
            [(255, 255, 255), (0, 0, 0), (127, 127, 127)],
            id="rgba-alpha-channel",
        ),
        pytest.param(
            "LA", [(0, 0), (0, 255), (0, 128)], None,
            [(255, 255, 255), (0, 0, 0), (127, 127, 127)],
            id="grayscale-alpha-channel",
        ),
        pytest.param(
            "RGB", [(10, 20, 30), (0, 0, 0), (255, 255, 255)], None,
            [(10, 20, 30), (0, 0, 0), (255, 255, 255)],
            id="opaque-rgb",
        ),
    ],
)
def test_png_import_composites_transparency_on_white(
    tmp_path: Path, mode: str, pixels: list, transparency: object,
    expected: list[tuple[int, int, int]],
) -> None:
    source = Image.new(mode, (3, 1))
    if mode == "P":
        # Equal RGB values with different alpha values must remain distinguishable.
        source.putpalette([0, 0, 0] * 256)
    source.putdata(pixels)
    buffer = BytesIO()
    options = {"transparency": transparency} if transparency is not None else {}
    source.save(buffer, format="PNG", **options)
    source_bytes = buffer.getvalue()

    pages = import_document(source_bytes, "source.png", tmp_path)

    assert pages == [(1, 3, 1, "page-0001.png")]
    assert (tmp_path / "source.png").read_bytes() == source_bytes
    with Image.open(tmp_path / "page-0001.png") as imported:
        assert imported.mode == "RGB"
        assert "transparency" not in imported.info
        assert [imported.getpixel((x, 0)) for x in range(3)] == expected
