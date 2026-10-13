"""Manager + registry against the real `fakescan --manage` daemon (no discovery, tmp registry)."""

from __future__ import annotations

import pytest
from conftest import Daemon

from fakescan import ApiError, Manager, registry

pytestmark = pytest.mark.local

HW = "11111111-2222-4333-8444-555555555555"


def _port(u: str) -> str:
    return f"urn:uuid:{u}/uri:{u}/ScannerService"


def test_runtime_add_persists_and_reload_keeps_uuids(fakescan_binary, tmp_path):
    reg_file = tmp_path / "devices.json"
    d1 = Daemon(fakescan_binary, reg_file, tmp_path / "d1.log").start()
    try:
        with Manager(d1.base_url) as m:
            assert m.devices() == []
            a = m.add("tray5", "feeder-tray5", friendly_name="Five")
            b = m.add("duplex", "feeder-duplex30")
            m.installed("tray5", "{w5}", "Five", _port(a.uuid))
            row = registry.load(reg_file).find("tray5")  # read-only loader sees the daemon's write
            assert row.device_uuid == a.uuid and row.profile == "feeder-tray5" and row.friendly_name == "Five"
            assert row.listen == f"127.0.0.1:{d1.port}"
            assert row.install.state == "installed" and row.install.wia.id == "{w5}"
            assert row.config.published_fingerprint == a.config.published_fingerprint
            assert registry.from_server(m).find("duplex").device_uuid == b.uuid
            m.remove("tray5")  # remove keeps the row and its install state
            assert "tray5" not in {x.id for x in m.devices()}
            kept = registry.load(reg_file).find("tray5")
            assert kept is not None and kept.install.state == "installed"
            m.add("tray5", "feeder-tray5")  # same identity comes back
            assert m.device("tray5").uuid == a.uuid
            assert m.forget("tray5").state == "new"  # forget drops install info, keeps the row
            row = registry.load(reg_file).find("tray5")
            assert row is not None and row.install.state == "new" and row.install.wia is None
            m.remove("duplex", forget=True)
            assert registry.load(reg_file).find("duplex") is None
        assert {r.name: r.device_uuid for r in registry.load(reg_file).devices} == {"tray5": a.uuid}
    finally:
        d1.stop()

    # a second process on the SAME listen reloads the rows with the same UUIDs
    d2 = Daemon(fakescan_binary, reg_file, tmp_path / "d2.log", port=d1.port).start()
    try:
        with Manager(d2.base_url) as m:
            devs = {x.id: x for x in m.devices()}
            assert set(devs) == {"tray5"}
            assert devs["tray5"].uuid == a.uuid and devs["tray5"].profile == "feeder-tray5"
    finally:
        d2.stop()
    assert {r.name: r.device_uuid for r in registry.load(reg_file).devices} == {"tray5": a.uuid}


def test_daemon_installed_refusal_and_stale_persist(fakescan_binary, tmp_path):
    reg_file = tmp_path / "devices.json"
    d = Daemon(fakescan_binary, reg_file, tmp_path / "d.log").start()
    try:
        with Manager(d.base_url) as m:
            t = m.add("tray5", "feeder-tray5")
            with pytest.raises(ApiError):
                m.installed("tray5", "{1}", "n", "USB001")
            assert registry.load(reg_file).find("tray5").install.state == "new"
            m.installed("tray5", "{1}", "n", _port(t.uuid))
            m.reconfigure("tray5", "feeder-tray50")
            row = registry.load(reg_file).find("tray5")
            assert row.install.state == "stale" and row.needs_readd and row.profile == "feeder-tray50"
            r = m.reconcile([{"id": "{hw}", "name": "Brother", "port": _port(HW)}])
            assert [w.id for w in r.foreign] == ["{hw}"]
            assert registry.load(reg_file).find("tray5").install.state == "stale"
    finally:
        d.stop()
