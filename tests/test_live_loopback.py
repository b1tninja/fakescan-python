"""Live tests against a running fakescan (pytest -m live)."""

from __future__ import annotations

import io
import os

import pytest

from fakescan import Client
from fakescan.soap import acquire_jfif_pages

pytestmark = pytest.mark.live

BASE = os.environ.get("FAKESCAN_BASE_URL", "http://127.0.0.1:3920")


@pytest.fixture
def client():
    c = Client(BASE)
    if not c.health():
        pytest.skip(f"fakescan not up at {BASE}")
    c.reset()
    c.set_profile("brother-wsd")
    yield c
    c.reset()
    c.close()


def _tiny_jpeg(label: int) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        pytest.skip("pillow required for live loopback tests (pip install -e '.[dev]')")
    img = Image.new("RGB", (64, 64), (label * 40 % 255, 80, 160))
    d = ImageDraw.Draw(img)
    d.text((8, 24), f"P{label}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_loopback_soap_exact_echo(client: Client):
    pages = [_tiny_jpeg(0), _tiny_jpeg(1)]
    info = client.set_loopback_bytes(pages)
    assert info.active
    assert info.count == 2
    assert client.status().pages_remaining == 2

    got = acquire_jfif_pages(BASE, page_count=2, dpi=100)
    assert len(got) == 2
    assert got[0] == pages[0]
    assert got[1] == pages[1]


def test_fault_jam_after_n_status(client: Client):
    st = client.set_fault(jam_after_n=2)
    assert st.jam_after_n == 2
    client.set_pages(4)
    assert client.status().pages_remaining == 4


def test_pages_side_order(client: Client):
    st = client.set_pages(3, side="back", order="reverse")
    assert st.page_side == "back"
    assert st.page_order == "reverse"
    assert st.image_pattern == "duplex_pair"
