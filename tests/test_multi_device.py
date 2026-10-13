"""Multi-device in-process server: per-device profiles, tray capacity, isolation, runtime CRUD."""

from __future__ import annotations

import httpx
import pytest

from fakescan import ApiError, Client, Device, Server
from fakescan.soap import acquire_pages

pytestmark = pytest.mark.embedded

UUID_A = "00000000-0000-4000-8000-0000000000a1"


@pytest.fixture
def srv():
    with Server(
        devices=[
            Device("tray5", profile="feeder-tray5"),
            Device("tray50", profile="feeder-tray50"),
            Device("brother", profile="brother-wsd"),
        ]
    ) as s:
        yield s


def test_devices_have_distinct_profiles_and_uuids(srv):
    assert list(srv.devices) == ["tray5", "tray50", "brother"]
    assert len({d.uuid for d in srv.devices.values()}) == 3
    assert srv.devices["tray5"].client().status().profile_id == "feeder-tray5"
    assert srv.devices["tray50"].client().status().profile_id == "feeder-tray50"
    # first device is the default: legacy path + srv.client()
    assert srv.client().status().profile_id == "feeder-tray5"
    assert srv.devices["tray5"].scan_endpoint == f"{srv.base_url}/ScannerService"
    assert srv.devices["tray50"].scan_endpoint == f"{srv.base_url}/d/tray50/ScannerService"
    assert srv.device_uuid == srv.devices["tray5"].uuid
    listed = {d["id"]: d for d in srv.client().devices()}
    assert set(listed) == set(srv.devices)
    assert listed["tray50"]["uuid"] == srv.devices["tray50"].uuid


def test_explicit_uuid_and_name():
    with Server(devices=[Device("x", "glass-wsd", device_uuid=UUID_A, friendly_name="Bench")]) as s:
        assert s.devices["x"].uuid == UUID_A
        assert s.devices["x"].friendly_name == "Bench"


def test_tray_capacity_is_enforced(srv):
    c = srv.devices["tray5"].client()
    with pytest.raises(ApiError) as e:
        c.set_pages(6)
    assert e.value.status_code == 409
    assert isinstance(e.value, httpx.HTTPStatusError)
    assert str(e.value.detail) and "409" in str(e.value)
    assert c.status().pages_remaining == 0  # status unchanged
    assert c.set_pages(5).pages_remaining == 5


@pytest.mark.parametrize(
    "dev,fed", [("tray5", 5), ("tray50", 8), ("brother", 3)]
)
def test_soap_acquire_returns_exactly_the_fed_pages(srv, dev, fed):
    h = srv.devices[dev]
    h.client().set_pages(fed)
    pages = acquire_pages(h.client().base_url, page_count=fed)
    assert len(pages) == fed
    assert all(p[:2] == b"\xff\xd8" for p in pages)


def test_devices_are_isolated(srv):
    a, b = srv.devices["tray5"].client(), srv.devices["tray50"].client()
    a.set_pages(4)
    assert b.status().pages_remaining == 0
    b.set_pages(2)
    acquire_pages(srv.devices["tray50"].client().base_url, page_count=2)
    assert a.status().pages_remaining == 4
    assert len(a.jobs()) == 0
    assert len(b.jobs()) >= 1


def test_add_and_remove_at_runtime(srv):
    h = srv.add_device(Device("duplex", profile="feeder-duplex30"))
    assert srv.devices["duplex"] is h
    assert h.client().status().profile_id == "feeder-duplex30"
    assert h.scan_endpoint.endswith("/d/duplex/ScannerService")
    assert any(d["id"] == "duplex" for d in srv.client().devices())
    h.client().set_pages(2)
    assert len(acquire_pages(h.client().base_url, page_count=2)) == 2

    srv.remove_device("duplex")
    assert "duplex" not in srv.devices
    with pytest.raises(ApiError) as e:
        h.client().status()
    assert e.value.status_code == 404
    r = httpx.post(h.scan_endpoint, content=b"<x/>", headers={"Content-Type": "application/soap+xml"})
    assert r.status_code == 404
    # other devices untouched
    assert srv.devices["tray5"].client().status().profile_id == "feeder-tray5"


def test_duplicate_id_and_uuid_are_clear_errors(srv):
    with pytest.raises(ApiError, match="409") as e:
        srv.add_device(Device("tray5", profile="glass-wsd"))
    assert e.value.status_code == 409
    with pytest.raises(ApiError) as e:
        srv.add_device(Device("fresh", profile="glass-wsd", device_uuid=srv.devices["tray50"].uuid))
    assert e.value.status_code == 409
    assert "fresh" not in srv.devices
    with pytest.raises(ApiError) as e:
        srv.add_device(Device("bad", profile="no-such-profile"))
    assert e.value.status_code == 400
    with pytest.raises(ApiError):
        srv.remove_device("nope")


def test_startup_errors_surface():
    with pytest.raises(RuntimeError, match="duplicate device id"):
        Server(devices=[Device("a", "glass-wsd"), Device("a", "glass-wsd")])
    with pytest.raises(RuntimeError, match="failed to start"):
        Server(devices=[Device("a", "no-such-profile")])


def test_context_manager_stops_everything():
    with Server(devices=[Device("a", "glass-wsd"), Device("b", "feeder-tray5")]) as s:
        url = s.base_url
        assert httpx.get(f"{url}/health").status_code == 200
    assert not s.running
    for path in ("/health", "/d/b/v1/status"):
        with pytest.raises(httpx.TransportError):
            httpx.get(url + path, timeout=1.0)


def test_client_device_scoping_against_any_server(srv):
    c = Client(srv.base_url, device="tray50")
    assert c.base_url == f"{srv.base_url}/d/tray50"
    assert c.status().profile_id == "feeder-tray50"
    assert {d["id"] for d in c.devices()} == set(srv.devices)
    c.add_device("late", "glass-wsd")
    assert Client(srv.base_url, device="late").status().profile_id == "glass-wsd"
    c.remove_device("late")
    with pytest.raises(ApiError):
        Client(srv.base_url, device="late").status()
