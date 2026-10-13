"""Manager against an in-process Server: the HTTP surface and the Rust install-state transitions.

Embedded servers keep the registry in memory only; no file is read or written.
"""

from __future__ import annotations

import pytest

from fakescan import ApiError, Device, Manager, Server, _paths

pytestmark = pytest.mark.embedded

HW = "11111111-2222-4333-8444-555555555555"  # stands in for a real, foreign scanner: never ours


@pytest.fixture
def srv():
    with Server(devices=[Device("tray5", profile="feeder-tray5"), Device("brother", profile="brother-wsd")]) as s:
        yield s


def _port(uuid: str) -> str:
    return f"urn:uuid:{uuid}/uri:{uuid}/ScannerService"


def test_manager_is_bound_to_the_server(srv):
    assert isinstance(srv.manager, Manager) and srv.manager is srv.manager
    assert Manager(srv).base_url == srv.base_url
    assert Manager(srv.client()).base_url == srv.base_url
    rows = {d.id: d for d in srv.manager.devices()}
    assert set(rows) == {"tray5", "brother"}
    d = rows["tray5"]
    assert d.uuid == srv.devices["tray5"].uuid and d.state == "new" and not d.needs_readd
    assert d.config.feeder.capacity == 5 and d.config.published_fingerprint.startswith("sha256:")
    assert {r.name for r in srv.manager.registry().devices} == {"tray5", "brother"}


def test_add_remove_forget_lifecycle(srv):
    m = srv.manager
    d = m.add("duplex", "feeder-duplex30", friendly_name="Duplexer")
    assert d.profile == "feeder-duplex30" and d.friendly_name == "Duplexer" and d.config.feeder.duplex
    assert m.registry().find("duplex") is not None
    with pytest.raises(ApiError) as e:
        m.add("duplex", "feeder-duplex30")
    assert e.value.status_code == 409
    with pytest.raises(ApiError) as e:
        m.add("bad", "no-such-profile")
    assert e.value.status_code == 400
    m.remove("duplex")  # keeps the row
    assert "duplex" not in {x.id for x in m.devices()}
    assert m.registry().find("duplex") is not None
    again = m.add("duplex", "feeder-duplex30")  # same identity comes back
    assert again.uuid == d.uuid
    m.remove("duplex", forget=True)
    assert m.registry().find("duplex") is None
    with pytest.raises(ApiError) as e:
        m.remove("duplex")
    assert e.value.status_code == 404


def test_installed_requires_uuid_in_port(srv):
    m = srv.manager
    uuid = m.device("tray5").uuid
    with pytest.raises(ApiError) as e:
        m.installed("tray5", "{1}\\0003", "fakescan tray5", "USB001")
    assert e.value.status_code == 400 and "UUID" in e.value.detail
    assert m.device("tray5").state == "new"
    with pytest.raises(ApiError):  # another device's UUID is not enough
        m.installed("tray5", "{1}", "n", _port(m.device("brother").uuid))
    with pytest.raises(ApiError) as e:
        m.installed("nope", "{1}", "n", _port(uuid))
    assert e.value.status_code == 404
    d = m.installed("tray5", "{1}\\0003", "fakescan tray5", _port(uuid.upper()))
    assert d.state == "installed" and d.install.installed_at and d.install.wia.name == "fakescan tray5"
    assert d.install.published_fingerprint_at_install == d.config.published_fingerprint
    assert m.wait_installed("tray5", timeout=1).state == "installed"
    f = m.forget("tray5")
    assert f.state == "new" and f.install.wia is None
    with pytest.raises(TimeoutError):
        m.wait_installed("tray5", timeout=0.3, interval=0.1)


def test_reconfigure_published_change_marks_stale(srv):
    m = srv.manager
    m.installed("tray5", "{1}", "n", _port(m.device("tray5").uuid))
    d = m.reconfigure("tray5", "feeder-tray50")  # media_sizes change ADFMaximumSize
    assert d.profile == "feeder-tray50" and d.state == "stale"
    assert d.needs_readd and d.needs_readd_reason
    assert m.device("tray5").needs_readd
    assert m.registry().find("tray5").needs_readd


def test_reconfigure_brother_to_glass_marks_stale(srv):
    m = srv.manager
    m.installed("brother", "{2}", "n", _port(m.device("brother").uuid))
    d = m.reconfigure("brother", "glass-wsd")
    assert d.state == "stale" and d.needs_readd


def test_reconfigure_uninstalled_stays_new_and_errors(srv):
    d = srv.manager.reconfigure("tray5", "feeder-tray50")
    assert d.state == "new" and not d.needs_readd
    with pytest.raises(ApiError) as e:
        srv.manager.reconfigure("tray5", "no-such-profile")
    assert e.value.status_code == 400
    with pytest.raises(ApiError) as e:
        srv.manager.reconfigure("ghost", "brother-wsd")
    assert e.value.status_code == 404


def _capacity_only_pair(m: Manager):
    """(a, b) profile ids with equal published fingerprints but different full fingerprints."""
    ids = sorted(p.stem for p in _paths.profiles_dir().glob("*.json"))
    seen = {}
    for pid in ids:
        try:
            m.add("probe-" + pid, pid)
        except ApiError:
            continue
        c = m.device("probe-" + pid).config
        m.remove("probe-" + pid, forget=True)
        seen[pid] = (c.published_fingerprint, c.fingerprint)
    for a in ids:
        for b in ids:
            if a < b and a in seen and b in seen and seen[a][0] == seen[b][0] and seen[a][1] != seen[b][1]:
                return a, b
    return None


def test_capacity_only_profile_variant_keeps_installed(srv):
    m = srv.manager
    pair = _capacity_only_pair(m)
    if pair is None:
        pytest.skip("no two shipped profiles differ only in capacity (same published fingerprint, different full one)")
    a, b = pair
    m.add("capdev", a)
    m.installed("capdev", "{3}", "n", _port(m.device("capdev").uuid))
    d = m.reconfigure("capdev", b)
    assert d.state == "installed" and not d.needs_readd


def test_reconcile_installs_marks_stale_and_leaves_foreign(srv):
    m = srv.manager
    t, b = m.device("tray5").uuid, m.device("brother").uuid
    m.installed("brother", "{b}", "brother", _port(b))
    scanners = [
        {"id": "{t}", "name": "anything", "port": _port(t)},
        {"id": "{hw}", "name": "Brother MFC", "port": _port(HW)},
        {"id": "{two}", "name": "x", "port": f"urn:uuid:{t}/uri:{b}/ScannerService"},
    ]
    r = m.reconcile(scanners)
    assert r.installed == ["tray5"]
    assert r.stale == ["brother"]  # was installed, no longer listed
    assert sorted(w.id for w in r.foreign) == ["{hw}", "{two}"]
    assert m.device("tray5").state == "installed"
    assert m.device("brother").state == "stale" and m.device("brother").needs_readd
    assert m.device("tray5").install.wia.id == "{t}"  # the foreign rows touched nothing
    assert [x.name for x in m.registry().devices] == ["tray5", "brother"]


def test_reconcile_from_windows_with_fake_listing(srv):
    m = srv.manager
    t = m.device("tray5").uuid
    calls = []

    def fake():
        calls.append(1)
        return {"count": 2, "scanners": [{"id": "{t}", "name": "n", "port": _port(t), "feeder": True},
                                         {"id": "{x}", "name": "n2", "port": "USB001"}]}

    r = m.reconcile_from_windows(fake)
    assert calls and r.installed == ["tray5"] and [w.id for w in r.foreign] == ["{x}"]
    assert m.reconcile_from_windows(lambda: []).stale == ["tray5"]


def test_reconcile_from_windows_without_pdfcraft(srv, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pdfcraft", None)
    monkeypatch.setitem(sys.modules, "pdfcraft.scan", None)
    with pytest.raises(RuntimeError, match="pdfcraft.scan"):
        srv.manager.reconcile_from_windows()


def test_embedded_server_never_touches_a_registry_file(tmp_path, monkeypatch):
    reg = tmp_path / "devices.json"
    monkeypatch.setenv("FAKESCAN_REGISTRY", str(reg))
    with Server(devices=[Device("a", "feeder-tray5")]) as s:
        s.manager.installed("a", "{1}", "n", _port(s.manager.device("a").uuid))
    assert not reg.exists()
