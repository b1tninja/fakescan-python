"""Live SOAP fault tests against a running fakescan (pytest -m live).

These exercise mid-job jam / empty and ScannerStatus without WIA or pdfcraft.
"""

from __future__ import annotations

import os

import pytest

from fakescan import Client
from fakescan.soap import (
    SoapFault,
    create_scan_job,
    get_scanner_status,
    retrieve_image_bytes,
)

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


def test_scanner_status_jam_and_cover(client: Client):
    client.set_fault(jam=True, cover_open=True)
    import httpx

    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, BASE)
    assert "MediaJam" in xml
    assert "CoverOpen" in xml
    assert "Stopped" in xml


def test_jam_after_n_trips_on_third_retrieve(client: Client):
    client.set_pages(4)
    client.set_fault(jam_after_n=2)
    import httpx

    with httpx.Client(timeout=60.0) as http:
        job = create_scan_job(http, BASE, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, BASE, job)
        retrieve_image_bytes(http, BASE, job)
        with pytest.raises(SoapFault) as caught:
            retrieve_image_bytes(http, BASE, job)
    fault = caught.value
    assert "MediaJam" in (fault.subcode or "") or "MediaJam" in fault.body
    assert "ClientErrorMediaJam" in fault.body or "MediaJam" in fault.body
    st = client.status()
    assert st.jam is True


def test_empty_mid_job_ends_with_no_images(client: Client):
    client.set_pages(5)
    client.set_fault(empty_mid_job=2)
    import httpx

    with httpx.Client(timeout=60.0) as http:
        job = create_scan_job(http, BASE, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, BASE, job)
        retrieve_image_bytes(http, BASE, job)
        with pytest.raises(SoapFault) as caught:
            retrieve_image_bytes(http, BASE, job)
    assert "NoImagesAvailable" in (caught.value.subcode or caught.value.body)


def test_offline_blocks_create_with_attention_required(client: Client):
    client.set_fault(offline=True)
    import httpx

    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, BASE)
        assert "AttentionRequired" in xml
        assert "Stopped" in xml
        with pytest.raises(SoapFault) as blocked:
            create_scan_job(http, BASE, format="jfif", pages=1, dpi=100)
    assert "NotAcceptingJobs" in (blocked.value.subcode or blocked.value.body)


def test_offline_after_n_trips_then_stays_offline(client: Client):
    client.set_pages(4)
    client.set_fault(offline_after_n=1)
    import httpx

    with httpx.Client(timeout=60.0) as http:
        job = create_scan_job(http, BASE, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, BASE, job)
        with pytest.raises(SoapFault) as dropped:
            retrieve_image_bytes(http, BASE, job)
    assert "NotAcceptingJobs" in (dropped.value.subcode or dropped.value.body)
    assert client.status().offline is True
    with httpx.Client(timeout=30.0) as http:
        with pytest.raises(SoapFault) as blocked:
            create_scan_job(http, BASE, format="jfif", pages=1, dpi=100)
    assert "NotAcceptingJobs" in (blocked.value.subcode or blocked.value.body)


def test_multi_feed_shows_on_scanner_status(client: Client):
    client.set_fault(multi_feed=True)
    import httpx

    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, BASE)
    assert "MultipleFeedError" in xml
    assert "Stopped" in xml


def test_multi_feed_after_n_trips_on_second_retrieve(client: Client):
    client.set_pages(4)
    client.set_fault(multi_feed_after_n=1)
    import httpx

    with httpx.Client(timeout=60.0) as http:
        job = create_scan_job(http, BASE, format="jfif", pages=0, dpi=100)
        retrieve_image_bytes(http, BASE, job)
        with pytest.raises(SoapFault) as caught:
            retrieve_image_bytes(http, BASE, job)
    fault = caught.value
    assert "MultipleFeed" in (fault.subcode or "") or "MultipleFeed" in fault.body
    assert client.status().multi_feed is True


def test_lamp_error_blocks_create(client: Client):
    client.set_fault(lamp_error=True)
    import httpx

    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, BASE)
        assert "LampError" in xml
        assert "Stopped" in xml
        with pytest.raises(SoapFault) as blocked:
            create_scan_job(http, BASE, format="jfif", pages=1, dpi=100)
    assert "NotAcceptingJobs" in (blocked.value.subcode or blocked.value.body)


def test_busy_blocks_create_with_processing_status(client: Client):
    client.set_fault(busy=True)
    import httpx

    with httpx.Client(timeout=30.0) as http:
        xml = get_scanner_status(http, BASE)
        assert "Processing" in xml
        with pytest.raises(SoapFault) as blocked:
            create_scan_job(http, BASE, format="jfif", pages=1, dpi=100)
    assert "NotAcceptingJobs" in (blocked.value.subcode or blocked.value.body)
    assert "busy" in blocked.value.body.lower() or "NotAccepting" in blocked.value.body


def test_abort_retrieve_cuts_mtom_body(client: Client):
    client.set_pages(2)
    client.set_fault(abort_retrieve=True, abort_retrieve_bytes=128)
    import httpx

    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, BASE, format="jfif", pages=0, dpi=100)
        with pytest.raises(Exception) as caught:
            retrieve_image_bytes(http, BASE, job)
    # Stream abort surfaces as a transport / incomplete-read error, not a SOAP Fault.
    assert caught.value is not None
    assert client.status().abort_retrieve is True
