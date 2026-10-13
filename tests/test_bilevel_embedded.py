"""BlackAndWhite1 tickets return real 1-bit pages; `bilevel_polarity` flips the bit sense only."""

from __future__ import annotations

import struct
from io import BytesIO

import httpx
import pytest
from PIL import Image

from fakescan import Server
from fakescan.soap import create_scan_job, retrieve_image_bytes

pytestmark = pytest.mark.embedded


def _scan(srv: Server, fmt: str, color: str) -> bytes:
    srv.client().set_pages(1)
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(
            http, srv.base_url, format=fmt, pages=1, dpi=100, input_source="ADF", color=color
        )
        return retrieve_image_bytes(http, srv.base_url, job)


def _visual(data: bytes) -> bytes:
    """Gray pixels of the top 70% of the page.

    The bottom-right QR carries ``client=<peer ip:port>``, which differs per
    connection, so it is excluded from visual comparisons.
    """
    img = Image.open(BytesIO(data)).convert("L")
    return img.crop((0, 0, img.width, int(img.height * 0.7))).tobytes()


def _tiff_tags(data: bytes) -> dict[int, int]:
    assert data[:4] == b"II*\x00"
    ifd = struct.unpack_from("<I", data, 4)[0]
    (n,) = struct.unpack_from("<H", data, ifd)
    tags: dict[int, int] = {}
    for i in range(n):
        tag, typ, _count = struct.unpack_from("<HHI", data, ifd + 2 + i * 12)
        at = ifd + 2 + i * 12 + 8
        tags[tag] = struct.unpack_from("<H" if typ == 3 else "<I", data, at)[0]
    return tags


def _bmp(data: bytes):
    assert data[:2] == b"BM"
    off, hdr, w, h, _planes, bpp = struct.unpack_from("<IIiiHH", data, 10)
    assert hdr == 40
    palette = [data[54 + i * 4] for i in range((off - 54) // 4)]
    return w, h, bpp, palette


def test_bw1_tiff_is_one_bit_bilevel():
    with Server(profile="brother-wsd") as srv:
        data = _scan(srv, "tiff-single-uncompressed", "BlackAndWhite1")
    tags = _tiff_tags(data)
    assert tags[258] == 1 and tags[277] == 1  # BitsPerSample, SamplesPerPixel
    assert tags[262] == 1  # MinIsBlack by default
    img = Image.open(BytesIO(data))
    assert img.mode == "1"
    assert tags[279] == ((tags[256] + 7) // 8) * tags[257]  # strip byte count


def test_gray8_tiff_is_eight_bit():
    with Server(profile="brother-wsd") as srv:
        data = _scan(srv, "tiff-single-uncompressed", "Grayscale8")
    tags = _tiff_tags(data)
    assert tags[258] == 8 and tags[277] == 1
    assert Image.open(BytesIO(data)).mode == "L"


def test_polarity_flips_photometric_not_the_visual():
    visuals = {}
    photometric = {}
    for pol in ("black_is_zero", "white_is_zero"):
        with Server(profile="brother-wsd") as srv:
            srv.client().set_effects(bilevel_polarity=pol)
            data = _scan(srv, "tiff-single-uncompressed", "BlackAndWhite1")
        photometric[pol] = _tiff_tags(data)[262]
        visuals[pol] = _visual(data)
    assert photometric == {"black_is_zero": 1, "white_is_zero": 0}
    assert visuals["black_is_zero"] == visuals["white_is_zero"]
    dark = sum(1 for b in visuals["black_is_zero"] if b < 128)
    assert 0 < dark < len(visuals["black_is_zero"]) // 2, "mostly light with some dark"


def test_bmp_palette_order_on_a_dib_profile():
    seen = {}
    for pol in ("black_is_zero", "white_is_zero"):
        with Server(profile="wia-rich") as srv:
            srv.client().set_effects(bilevel_polarity=pol)
            data = _scan(srv, "dib", "BlackAndWhite1")
        _w, _h, bpp, palette = _bmp(data)
        assert bpp == 1
        seen[pol] = (palette, _visual(data))
    assert seen["black_is_zero"][0] == [0, 255]
    assert seen["white_is_zero"][0] == [255, 0]
    assert seen["black_is_zero"][1] == seen["white_is_zero"][1]


def test_jfif_bw_stays_a_valid_bilevel_looking_jpeg():
    with Server(profile="brother-wsd") as srv:
        data = _scan(srv, "jfif", "BlackAndWhite1")
    img = Image.open(BytesIO(data)).convert("L")
    px = list(img.tobytes())
    extreme = sum(1 for p in px if p <= 40 or p >= 215)
    assert extreme >= len(px) * 0.97


def test_polarity_rejects_unknown_value():
    from fakescan import ApiError

    with Server(profile="brother-wsd") as srv:
        with pytest.raises(ApiError):
            srv.client().set_effects(bilevel_polarity="sideways")


def _jpeg_components(data: bytes) -> int:
    i = data.index(b"\xff\xc0")
    return data[i + 9]  # SOF0: marker(2) len(2) P(1) Y(2) X(2) Nf(1)


def test_jfif_gray_effect_switches_component_count():
    with Server(profile="brother-wsd") as srv:
        assert srv.client().status().raw["effects"]["jfif_gray"] == "gray8"
        data = _scan(srv, "jfif", "Grayscale8")
        assert _jpeg_components(data) == 1
        assert Image.open(BytesIO(data)).mode == "L"
        srv.client().set_effects(jfif_gray="rgb24")  # legacy coercion, Windows rejects it
        assert srv.client().status().raw["effects"]["jfif_gray"] == "rgb24"
        assert _jpeg_components(_scan(srv, "jfif", "Grayscale8")) == 3
        srv.client().set_effects(clear=True)
        assert srv.client().status().raw["effects"]["jfif_gray"] == "gray8"
        with pytest.raises(Exception):
            srv.client().set_effects(jfif_gray="purple")
