"""Fault plan end to end: Client -> embedded Server -> SOAP acquire (no WIA)."""

from __future__ import annotations

import httpx
import pytest

from fakescan import Server, after_ms, after_pages, mid_page
from fakescan.soap import (
    SoapFault,
    acquire_jfif_pages,
    create_scan_job,
    get_scanner_status,
    retrieve_image_bytes,
)

pytestmark = pytest.mark.embedded


def _pages_until_fault(base_url: str, n_max: int) -> tuple[list[bytes], Exception | None]:
    got: list[bytes] = []
    with httpx.Client(timeout=30.0) as http:
        job = create_scan_job(http, base_url, format="jfif", pages=0, dpi=100, input_source="ADF")
        for _ in range(n_max):
            try:
                got.append(retrieve_image_bytes(http, base_url, job))
            except Exception as exc:  # noqa: BLE001 - the caller inspects the type
                return got, exc
    return got, None


def test_set_fault_cover_open_after_n_serves_exactly_n_pages_then_faults():
    with Server() as srv:
        c = srv.client()
        c.set_pages(4)
        st = c.set_fault(cover_open_after_n=2)
        assert st.cover_open_after_n == 2
        pages, err = _pages_until_fault(srv.base_url, 4)
        assert len(pages) == 2
        assert all(p[:2] == b"\xff\xd8" for p in pages)
        assert isinstance(err, SoapFault)
        assert err.subcode == "wscn:ClientErrorCoverOpen", err
        st = c.status()
        assert st.cover_open and st.cover_open_after_n == 0
        with httpx.Client() as http:
            xml = get_scanner_status(http, srv.base_url)
        assert "<wscn:ScannerStateReason>CoverOpen</wscn:ScannerStateReason>" in xml
        assert "<wscn:ScannerState>Stopped</wscn:ScannerState>" in xml
        c.reset()
        assert not c.status().cover_open


def test_set_fault_accepts_interlock_open():
    with Server() as srv:
        c = srv.client()
        st = c.set_fault(interlock_open=True)
        assert st.interlock_open
        c.reset()
        assert not c.status().interlock_open


def test_inject_fault_between_pages_and_listing():
    with Server() as srv:
        c = srv.client()
        c.set_pages(4)
        c.inject_fault("jam", after_pages(1))
        plan = c.fault_plan()
        assert [(e["kind"], e["fired"]) for e in plan] == [("jam", False)]
        assert c.status().fault_plan[0]["trigger"] == "after 1 pages"
        pages, err = _pages_until_fault(srv.base_url, 4)
        assert len(pages) == 1
        assert isinstance(err, SoapFault) and err.subcode == "wscn:ClientErrorMediaJam"
        assert c.fault_plan()[0]["fired"] is True
        assert c.status().jam
        c.clear_fault_plan()
        assert c.fault_plan() == []
        assert c.status().jam, "clearing the plan leaves raised conditions"
        c.reset()


def test_inject_fault_mid_page_cuts_the_stream_and_keeps_earlier_pages():
    with Server() as srv:
        c = srv.client()
        c.set_pages(4)
        c.inject_fault("cover_open", mid_page(2, fraction=0.5))
        pages, err = _pages_until_fault(srv.base_url, 4)
        assert len(pages) == 1, "page 1 intact"
        assert pages[0][:2] == b"\xff\xd8"
        assert isinstance(err, httpx.RemoteProtocolError | httpx.ReadError), repr(err)
        st = c.status()
        assert st.cover_open
        assert c.fault_plan()[0]["fired"] is True
        jobs = c.jobs()
        assert jobs[0].page_index == 1, "the cut page is not counted"


def test_inject_fault_now_on_running_job():
    with Server() as srv:
        c = srv.client()
        c.set_pages(3)
        with httpx.Client(timeout=30.0) as http:
            job = create_scan_job(http, srv.base_url, format="jfif", pages=0, dpi=100, input_source="ADF")
            assert retrieve_image_bytes(http, srv.base_url, job)[:2] == b"\xff\xd8"
            c.inject_fault("cover_open", "now")
            assert c.status().cover_open
            with pytest.raises(SoapFault) as ei:
                retrieve_image_bytes(http, srv.base_url, job)
            assert ei.value.subcode == "wscn:ClientErrorCoverOpen"


def test_inject_fault_timed_shows_in_status_without_retrieve():
    with Server() as srv:
        c = srv.client()
        c.set_pages(2)
        with httpx.Client(timeout=30.0) as http:
            create_scan_job(http, srv.base_url, format="jfif", pages=0, dpi=100, input_source="ADF")
        c.inject_fault("cover_open", after_ms(150))
        assert not c.status().cover_open
        import time

        time.sleep(0.4)
        assert c.status().cover_open, "a status poll evaluates the timer"


def test_inject_fault_rejects_bad_when():
    with Server() as srv:
        c = srv.client()
        with pytest.raises(Exception):
            c.inject_fault("jam", {"page": 1})
        assert c.fault_plan() == []


def test_mid_page_needs_exactly_one_cut():
    with pytest.raises(ValueError):
        mid_page(2)
    with pytest.raises(ValueError):
        mid_page(2, fraction=0.5, bytes=10)


def test_plain_acquire_still_works_with_empty_plan():
    with Server() as srv:
        srv.client().set_pages(2)
        assert len(acquire_jfif_pages(srv.base_url, page_count=2)) == 2
