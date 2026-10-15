"""initial_pages (tray preload on create/reset) and the explicit load_paper / empty_tray helpers."""

from __future__ import annotations

import pytest

from fakescan import ApiError, Device, Server
from fakescan.registry import DeviceRow, ManagedDevice

pytestmark = pytest.mark.embedded


def _state(c):
    s = c.status()
    return s.pages_remaining, s.feeder_ready, s.initial_pages


def test_default_feeder_stays_empty():
    with Server(devices=[Device("a", "brother-wsd"), Device("b", "feeder-tray5")]) as srv:
        for h in srv.devices.values():
            assert _state(h.client()) == (0, False, 0)
        c = srv.devices["b"].client()
        c.reset()
        assert _state(c) == (0, False, 0)
    with Server() as srv:  # implicit default device
        assert _state(srv.client()) == (0, False, 0)


def test_device_initial_pages_applies_on_creation_and_after_reset():
    with Server(devices=[Device("t", "feeder-tray5", initial_pages=3), Device("e", "feeder-tray5")]) as srv:
        c = srv.devices["t"].client()
        assert _state(c) == (3, True, 3)
        c.set_pages(1)  # a run-time set_pages still overrides it ...
        assert c.status().pages_remaining == 1
        c.reset()
        assert _state(c) == (3, True, 3)  # ... until the next reset
        assert _state(srv.devices["e"].client()) == (0, False, 0)  # neighbours unaffected
        row = {d["id"]: d for d in c.devices()}
        assert row["t"]["initial_pages"] == 3 and row["e"]["initial_pages"] == 0


def test_server_default_own_value_wins_and_capacity_clamp():
    with Server(
        devices=[
            Device("default", "feeder-tray5"),
            Device("own", "feeder-tray50", initial_pages=2),
            Device("zero", "feeder-tray50", initial_pages=0),
            Device("big", "feeder-tray50"),
        ],
        initial_pages=9,
    ) as srv:
        assert _state(srv.client()) == (5, True, 5)  # 9 clamped to the 5-sheet tray
        assert _state(srv.devices["own"].client()) == (2, True, 2)
        assert _state(srv.devices["zero"].client()) == (0, False, 0)  # explicit 0 = empty
        assert _state(srv.devices["big"].client()) == (9, True, 9)
    with Server(initial_pages=4) as srv:  # implicit default device (brother-wsd: no tray limit)
        assert _state(srv.client()) == (4, True, 4)


def test_runtime_add_device_takes_initial_pages():
    with Server(devices=[Device("a", "brother-wsd")]) as srv:
        h = srv.add_device(Device("late", "feeder-tray50", initial_pages=6))
        assert _state(h.client()) == (6, True, 6)
        row = srv.manager.device("late")
        assert row.initial_pages == 6
        assert srv.manager.add("late2", "feeder-tray5", initial_pages=2).initial_pages == 2
        assert _state(srv.devices["a"].client()) == (0, False, 0)


def test_initial_pages_does_not_change_the_fingerprint():
    with Server(devices=[Device("plain", "feeder-tray5"), Device("loaded", "feeder-tray5", initial_pages=5)]) as srv:
        a, b = srv.manager.device("plain"), srv.manager.device("loaded")
        assert a.config.fingerprint == b.config.fingerprint
        assert a.config.published_fingerprint == b.config.published_fingerprint
        assert a.state == b.state == "new" and not b.needs_readd


def test_load_paper_and_empty_tray_are_explicit():
    with Server(devices=[Device("t", "feeder-tray5"), Device("b", "brother-wsd")]) as srv:
        c = srv.devices["b"].client()
        assert _state(c)[:2] == (0, False)
        st = c.load_paper()  # default 6
        assert (st.pages_remaining, st.feeder_ready) == (6, True)
        st = c.load_paper(2)
        assert (st.pages_remaining, st.feeder_ready) == (2, True)
        st = c.empty_tray()
        assert (st.pages_remaining, st.feeder_ready) == (0, False)
        # The tray limit still applies: 6 sheets do not fit the 5-sheet tray.
        t = srv.devices["t"].client()
        with pytest.raises(ApiError):
            t.load_paper(6)
        assert t.load_paper(5).pages_remaining == 5


def test_registry_models_parse_initial_pages():
    assert DeviceRow.parse({"name": "x", "initial_pages": 4}).initial_pages == 4
    assert DeviceRow.parse({"name": "x"}).initial_pages is None
    assert "initial_pages" not in DeviceRow.parse({"name": "x", "initial_pages": 4}).extra
    md = ManagedDevice.parse({"id": "x", "uuid": "u", "profile": "p", "initial_pages": 6})
    assert md.initial_pages == 6 and "initial_pages" not in md.extra
    assert ManagedDevice.parse({"id": "x"}).initial_pages == 0
