"""The native ``fakescan.Server``: the lab server in-process, no cargo build or manual start."""

from __future__ import annotations

import httpx
import pytest

from fakescan import Server
from fakescan.soap import acquire_jfif_pages

pytestmark = pytest.mark.embedded


def test_starts_on_an_ephemeral_port_and_stops():
    srv = Server()
    try:
        assert srv.port > 0
        assert srv.running
        assert httpx.get(f"{srv.base_url}/health").text.strip() == "ok"
    finally:
        srv.stop()
    assert not srv.running
    srv.stop()  # idempotent
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{srv.base_url}/health", timeout=1.0)


def test_context_manager_and_identity():
    uuid = "00000000-0000-4000-8000-0000000000aa"
    with Server(profile="brother-wsd", device_uuid=uuid) as srv:
        status = srv.client().status()
        assert status.profile_id == "brother-wsd"
        assert srv.device_uuid == uuid
        assert srv.scan_endpoint.endswith("/ScannerService")


def test_servers_are_independent():
    with Server() as a, Server() as b:
        assert a.port != b.port
        a.client().set_pages(3)
        b.client().set_pages(1)
        assert a.client().status().pages_remaining == 3
        assert b.client().status().pages_remaining == 1


def test_unknown_profile_fails_with_a_reason():
    with pytest.raises(RuntimeError, match="failed to start"):
        Server(profile="no-such-profile")


@pytest.mark.parametrize("fed", [1, 3])
def test_soap_feeder_acquire_returns_every_fed_page(fed):
    with Server() as srv:
        srv.client().set_pages(fed)
        pages = acquire_jfif_pages(srv.base_url, page_count=fed)
        assert len(pages) == fed
        assert all(p[:2] == b"\xff\xd8" for p in pages)
