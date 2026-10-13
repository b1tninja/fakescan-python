"""Offline: registry dataclass parsing and the WIA matching rule. No server, no registry file."""

from __future__ import annotations

import json

import pytest

from fakescan import registry
from fakescan.registry import DeviceRow, match_wia_to_devices

pytestmark = pytest.mark.embedded

U5 = "00000000-0000-4000-8000-0000000000a5"
U50 = "00000000-0000-4000-8000-0000000000b0"
BROTHER = "11111111-2222-4333-8444-555555555555"


def _devs():
    return [DeviceRow(name="tray5", device_uuid=U5), DeviceRow(name="tray50", device_uuid=U50)]


def test_full_row_parses_and_keeps_unknown_fields():
    doc = {
        "advertise_default": "10.0.0.2",
        "port_policy": {"base": 3920, "step": 2},
        "future_top": {"x": 1},
        "devices": [{
            "name": "tray5", "listen": "0.0.0.0:3920", "advertise": "", "device_uuid": U5,
            "profile": "feeder-tray5", "friendly_name": "fakescan tray5", "mystery": [1],
            "config": {"fingerprint": "sha256:a", "published_fingerprint": "sha256:b",
                       "feeder": {"capacity": 5, "media_sizes": ["letter"], "duplex": False, "z": 1},
                       "new_cfg": True},
            "install": {"state": "stale", "reason": "profile changed", "installed_at": "2026-01-01T00:00:00Z",
                        "wia": {"id": "{1}\0003", "name": "n", "port": f"urn:uuid:{U5}/x", "q": 1},
                        "published_fingerprint_at_install": "sha256:c", "hello": "world"},
        }],
    }
    reg = registry.parse(doc)
    assert reg.advertise_default == "10.0.0.2" and reg.extra == {"future_top": {"x": 1}}
    row = reg.find("tray5")
    assert row.extra == {"mystery": [1]}
    assert row.config.feeder.capacity == 5 and row.config.feeder.extra == {"z": 1}
    assert row.config.extra == {"new_cfg": True}
    assert row.install.state == "stale" and row.install.extra == {"hello": "world"}
    assert row.install.wia.port.endswith("/x")
    assert row.needs_readd and row.needs_readd_reason == "profile changed"


def test_legacy_added_pnp_row_loads_as_installed():
    old = {"devices": [{"name": "fake", "listen": "0.0.0.0:3920", "advertise": "1.2.3.4",
                        "device_uuid": U5, "added": {"pnp": True}},
                       {"name": "fresh", "listen": "0.0.0.0:3922", "advertise": "", "device_uuid": U50}]}
    reg = registry.parse(old)
    fake, fresh = reg.devices
    assert fake.install.state == "installed" and fake.install.note == "legacy added.pnp"
    assert fake.profile == "brother-wsd" and fake.config is None and fake.added_pnp
    assert fresh.install.state == "new" and not fresh.needs_readd


def test_load_missing_empty_and_file(tmp_path):
    assert registry.load(tmp_path / "nope.json").devices == []
    (tmp_path / "e.json").write_text("")
    assert registry.load(tmp_path / "e.json").devices == []
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"devices": [{"name": "a", "device_uuid": U5}]}), encoding="utf-8")
    assert registry.load(p).find("a").device_uuid == U5
    p.write_text("{not json")
    with pytest.raises(OSError):
        registry.load(p, retries=2, delay=0.001)


def test_registry_path_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKESCAN_REGISTRY", str(tmp_path / "x.json"))
    assert registry.registry_path() == tmp_path / "x.json"
    monkeypatch.delenv("FAKESCAN_REGISTRY")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = registry.registry_path()
    if registry.os.name == "nt":
        assert p == tmp_path / "lad" / "fakescan" / "devices.json"
    else:
        assert p == tmp_path / "home" / ".local" / "share" / "fakescan" / "devices.json"


def test_matching_by_port_uuid_only():
    wia = [{"id": "1", "name": "fakescan tray5", "port": f"urn:uuid:{U5}/uri:{U5}/ScannerService"}]
    m = match_wia_to_devices(_devs(), wia)
    assert list(m.matched) == ["tray5"] and not m.foreign


@pytest.mark.parametrize("port", [
    U5.upper(), "{" + U5 + "}", f"URN:UUID:{U5.upper()}/uri:{{{U5}}}/ScannerService", f"http://h/{U5}",
])
def test_matching_case_brace_prefix_variants(port):
    m = match_wia_to_devices(_devs(), [{"id": "x", "name": "whatever", "port": port}])
    assert list(m.matched) == ["tray5"]


def test_brother_row_is_foreign_even_with_a_fakescan_like_name():
    brother = {"id": "b", "name": "fakescan tray5",  # a name guess must never match
               "port": f"urn:uuid:{BROTHER}/uri:{BROTHER}/ScannerService"}
    m = match_wia_to_devices(_devs(), [brother])
    assert m.matched == {} and [w.id for w in m.foreign] == ["b"]


def test_two_uuids_in_port_is_foreign_and_empty_port_is_foreign():
    two = {"id": "2", "name": "n", "port": f"urn:uuid:{U5}/uri:{U50}/ScannerService"}
    none = {"id": "3", "name": "fakescan tray5", "port": "USB001"}
    m = match_wia_to_devices(_devs(), [two, none])
    assert m.matched == {} and [w.id for w in m.foreign] == ["2", "3"]


def test_duplicate_listing_of_one_device_is_not_foreign():
    a = {"id": "1", "name": "n", "port": U5}
    b = {"id": "2", "name": "n", "port": "{" + U5 + "}"}
    m = match_wia_to_devices(_devs(), [a, b])
    assert m.matched["tray5"].id == "1" and [w.id for w in m.duplicates] == ["2"] and not m.foreign
