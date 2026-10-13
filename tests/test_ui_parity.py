"""Device-manager UI/tray parity: profiles, status table, open_ui, settings opener, tray, ctl.

Nothing here opens a browser or the Windows settings page (all openers are injected) and nothing
spawns a daemon. Tests that need a route the fakescan checkout may not have yet (``GET /v1/profiles``,
``GET /ui``) probe for it and skip with a reason; they run unchanged once it lands.
"""

from __future__ import annotations

import os

import httpx
import pytest

from fakescan import (
    ApiError,
    Client,
    Device,
    Manager,
    ProfileInfo,
    Server,
    StatusRow,
    ctl,
    format_status_table,
    summary_level,
    tray,
)

pytestmark = pytest.mark.embedded


def _port(uuid: str) -> str:
    return f"urn:uuid:{uuid}/uri:{uuid}/ScannerService"


@pytest.fixture
def srv():
    with Server(
        devices=[
            Device("tray5", profile="feeder-tray5"),
            Device("brother", profile="brother-wsd"),
            Device("duplex", profile="feeder-duplex30", friendly_name="Duplexer"),
        ]
    ) as s:
        yield s


def _route_exists(srv, path: str) -> bool:
    return httpx.get(srv.base_url + path, timeout=5).status_code != 404


@pytest.fixture
def needs_profiles_route(srv):
    if not _route_exists(srv, "/v1/profiles"):
        pytest.skip("this fakescan build has no GET /v1/profiles yet")


@pytest.fixture
def needs_ui_route(srv):
    if not _route_exists(srv, "/ui"):
        pytest.skip("this fakescan build has no GET /ui yet")


def _mock_manager(handler, base="http://127.0.0.1:3920") -> Manager:
    return Manager(Client(base, client=httpx.Client(transport=httpx.MockTransport(handler))))


# -- profiles -------------------------------------------------------------------------------
def test_profiles_typed_and_feeder_shapes(srv):
    # Works with or without the route (file fallback has the same shape).
    ps = {p.id: p for p in srv.manager.profiles()}
    assert {"feeder-tray5", "brother-wsd", "feeder-duplex30", "glass-wsd"} <= set(ps)
    t5 = ps["feeder-tray5"]
    assert isinstance(t5, ProfileInfo) and t5.feeder.capacity == 5
    assert t5.feeder.media_sizes == ["letter"] and t5.feeder.duplex is False
    assert t5.connection == "wsd" and "jfif" in t5.formats
    assert ps["brother-wsd"].feeder.capacity is None
    assert ps["feeder-duplex30"].feeder.duplex is True


def test_profiles_come_from_daemon_route_when_present(srv, needs_profiles_route):
    assert all(p.source == "daemon" for p in srv.manager.profiles())


def test_profiles_file_fallback_when_route_404s(tmp_path, monkeypatch):
    (tmp_path / "p1.json").write_text(
        '{"id":"p1","label":"One","connection":"usb","formats":["jfif"],'
        '"feeder":{"capacity":7,"media_sizes":["a4"],"duplex":true}}'
    )
    (tmp_path / "broken.json").write_text("{nope")
    monkeypatch.setenv("FAKESCAN_PROFILES", str(tmp_path))
    m = _mock_manager(lambda req: httpx.Response(404, json={"error": "not found"}))
    (p,) = m.profiles()
    assert p.source == "files" and p.id == "p1" and p.description == "One"
    assert p.feeder.capacity == 7 and p.feeder.duplex and p.feeder.media_sizes == ["a4"]


def test_profiles_parse_daemon_payload():
    body = [{"id": "x", "connection": "wsd", "formats": ["jfif"],
             "feeder": {"capacity": None, "media_sizes": [], "duplex": False}, "description": "d"}]
    m = _mock_manager(lambda req: httpx.Response(200, json=body))
    (p,) = m.profiles()
    assert p == ProfileInfo("x", "wsd", ["jfif"], p.feeder, "d") and p.source == "daemon"
    assert p.feeder.capacity is None


def test_profiles_other_errors_propagate():
    m = _mock_manager(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(ApiError):
        m.profiles()


# -- open_ui ----------------------------------------------------------------------------------
def test_open_ui_uses_injected_opener(srv):
    seen: list[str] = []
    assert srv.manager.open_ui(seen.append) == f"{srv.base_url}/ui"
    assert srv.open_ui(seen.append) == f"{srv.base_url}/ui"
    assert seen == [f"{srv.base_url}/ui"] * 2


def test_open_ui_defaults_to_webbrowser(monkeypatch):
    import webbrowser

    seen: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: seen.append(url))
    assert Manager("http://127.0.0.1:3920/").open_ui() == "http://127.0.0.1:3920/ui"
    assert seen == ["http://127.0.0.1:3920/ui"]


def test_ui_page_served_and_loopback(srv, needs_ui_route):
    r = httpx.get(f"{srv.base_url}/ui")
    assert r.status_code == 200 and "<html" in r.text.lower()


# -- open_add_device_settings ----------------------------------------------------------------
def test_settings_via_daemon_when_it_opens():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.content))
        return httpx.Response(200, json={"opened": True})

    opened: list[str] = []
    assert _mock_manager(handler).open_add_device_settings(opened.append) == "daemon"
    assert opened == []
    assert calls[0][:2] == ("POST", "/v1/ctl")
    assert b"open_settings" in calls[0][2] and b"printers" in calls[0][2]


def test_settings_accepts_wrapped_result():
    m = _mock_manager(lambda r: httpx.Response(200, json={"ok": True, "result": {"opened": True}}))
    assert m.open_add_device_settings(lambda u: pytest.fail("fallback used")) == "daemon"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": "no"}),
        httpx.Response(400, json={"error": "unknown cmd"}),
        httpx.Response(200, json={"opened": False}),
        httpx.Response(200, text="not json"),
    ],
)
def test_settings_falls_back_to_startfile(response):
    opened: list[str] = []
    m = _mock_manager(lambda r: response)
    assert m.open_add_device_settings(opened.append) == "startfile"
    assert opened == ["ms-settings:printers"]


def test_settings_falls_back_when_daemon_unreachable():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    opened: list[str] = []
    assert _mock_manager(handler).open_add_device_settings(opened.append) == "startfile"
    assert opened == ["ms-settings:printers"]


def test_settings_never_asks_a_remote_daemon():
    opened: list[str] = []
    m = _mock_manager(lambda r: pytest.fail("remote daemon must not be contacted"), base="http://192.0.2.7:3920")
    assert m.open_add_device_settings(opened.append) == "startfile"
    assert opened == ["ms-settings:printers"]


def test_settings_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    m = _mock_manager(lambda r: httpx.Response(404))
    assert m.open_add_device_settings() == "unavailable"


def test_settings_default_opener_is_startfile_on_windows(monkeypatch):
    if os.name != "nt":
        pytest.skip("os.startfile is Windows-only")
    seen: list[str] = []
    monkeypatch.setattr(os, "startfile", seen.append)
    assert _mock_manager(lambda r: httpx.Response(404)).open_add_device_settings() == "startfile"
    assert seen == ["ms-settings:printers"]


def test_settings_use_daemon_false_skips_post():
    opened: list[str] = []
    m = _mock_manager(lambda r: pytest.fail("no request expected"))
    assert m.open_add_device_settings(opened.append, use_daemon=False) == "startfile"


# -- status table -----------------------------------------------------------------------------
def test_status_table_three_devices_with_pages(srv):
    srv.devices["tray5"].client().set_pages(5)
    srv.devices["duplex"].client().set_pages(12)
    srv.devices["duplex"].client().set_fault(jam=True)
    rows = {r.id: r for r in srv.manager.status_table()}
    assert set(rows) == {"tray5", "brother", "duplex"}
    t5 = rows["tray5"]
    assert isinstance(t5, StatusRow)
    assert (t5.pages_remaining, t5.feeder_capacity, t5.profile) == (5, 5, "feeder-tray5")
    assert t5.uuid == srv.devices["tray5"].uuid and t5.install_state == "new" and not t5.needs_readd
    assert t5.scan_endpoint == srv.devices["tray5"].scan_endpoint
    assert t5.device_endpoint.endswith("/WebServices/Device") and t5.faults == []
    assert rows["brother"].pages_remaining == 0 and rows["brother"].feeder_capacity is None
    assert rows["duplex"].pages_remaining == 12 and rows["duplex"].faults == ["jam"]
    assert rows["duplex"].friendly_name == "Duplexer"


def test_status_table_reflects_install_and_stale(srv):
    m = srv.manager
    u = srv.devices["tray5"].uuid
    m.installed("tray5", "{w}", "fakescan tray5", _port(u))
    rows = {r.id: r for r in m.status_table()}
    assert rows["tray5"].install_state == "installed" and not rows["tray5"].needs_readd
    m.reconfigure("tray5", "feeder-tray50")  # published change => needs a re-Add
    row = {r.id: r for r in m.status_table()}["tray5"]
    assert row.install_state == "stale" and row.needs_readd


def test_status_table_tolerates_missing_fields_and_failed_status():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/devices":
            return httpx.Response(200, json=[{"id": "a"}, {"id": "b", "profile": "p", "install": {"state": "probed", "last_peer": "10.0.0.2"}}])
        if req.url.path == "/d/a/v1/status":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"pages_remaining": 3, "jam": True, "busy": False})

    a, b = _mock_manager(handler).status_table()
    assert a.id == "a" and a.pages_remaining is None and a.faults == [] and a.install_state == "new"
    assert b.pages_remaining == 3 and b.faults == ["jam"] and b.last_peer == "10.0.0.2"


def _row(state="installed", readd=False, **kw) -> StatusRow:
    return StatusRow(id=kw.pop("id", "d"), install_state=state, needs_readd=readd, **kw)


def test_summary_level_cases():
    assert summary_level([]) == "green"
    assert summary_level([_row(), _row(id="e")]) == "green"
    for state in ("new", "probed", "stale"):
        assert summary_level([_row(), _row(state)]) == "amber"
    assert summary_level([_row(readd=True)]) == "amber"
    assert summary_level(None) == "red"


def test_manager_summary_level_green_amber_red(srv):
    m = srv.manager
    assert m.summary_level() == "amber"  # all new
    for d in srv.devices.values():
        m.installed(d.id, "{x}", d.id, _port(d.uuid))
    assert m.summary_level() == "green"
    m.reconfigure("tray5", "feeder-tray50")
    assert m.summary_level() == "amber"
    dead = Manager("http://127.0.0.1:1", timeout=1)
    assert dead.summary_level() == "red"


def test_format_status_table_plain_and_width_aware():
    rows = [
        _row(id="tray5", friendly_name="fakescan tray5", profile="feeder-tray5", pages_remaining=5,
             feeder_capacity=5, uuid="cdf3017c-e591", last_peer="192.168.1.40"),
        _row("stale", True, id="long", friendly_name="N" * 40, profile="P" * 40, reason="media sizes changed",
             pages_remaining=0, faults=["jam", "cover_open"], uuid="abcdef0123"),
    ]
    wide = format_status_table(rows, 200)
    lines = wide.splitlines()
    assert lines[0].split()[:3] == ["ID", "NAME", "STATE"] and "PEER" in lines[0]
    assert "5/5" in wide and "jam,cover_open" in wide and "stale!" in wide
    assert "needs re-Add" in wide and "media sizes changed" in wide and "192.168.1.40" in wide
    narrow = format_status_table(rows, 60)
    assert all(len(l) <= 60 for l in narrow.splitlines())
    assert "tray5" in narrow and "stale!" in narrow and "PEER" not in narrow
    assert format_status_table([], 80) == "(no devices)"


# -- tray.ensure_daemon ---------------------------------------------------------------------
class _Spawner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))


def test_ensure_daemon_attached_spawns_nothing():
    sp = _Spawner()
    assert tray.ensure_daemon("http://127.0.0.1:3920", opener=sp, health_check=lambda u: True) == "attached"
    assert sp.calls == []


def test_ensure_daemon_started(tmp_path):
    exe = tmp_path / "fakescan.exe"
    exe.write_text("")
    sp = _Spawner()
    answers = iter([False, False, True])  # initial probe, one wait poll, then healthy
    r = tray.ensure_daemon(
        "http://127.0.0.1:4000", exe=exe, args=("--no-tray",), opener=sp,
        health_check=lambda u: next(answers), sleep=lambda s: None,
    )
    assert r == "started"
    ((cmd, kw),) = sp.calls
    assert cmd[:3] == [str(exe), "--manage", "--no-tray"] and cmd[-2:] == ["--listen", "127.0.0.1:4000"]
    assert kw["stdin"] is not None and kw["stdout"] is not None
    if os.name == "nt":
        import subprocess

        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kw["creationflags"] & subprocess.DETACHED_PROCESS


def test_ensure_daemon_default_port_adds_no_listen(tmp_path):
    exe = tmp_path / "f.exe"
    exe.write_text("")
    sp = _Spawner()
    calls = iter([False, True])
    tray.ensure_daemon("http://127.0.0.1:3920", exe=exe, opener=sp, health_check=lambda u: next(calls))
    assert sp.calls[0][0] == [str(exe), "--manage"]


def test_ensure_daemon_errors(tmp_path, monkeypatch):
    sp = _Spawner()
    with pytest.raises(FileNotFoundError):
        tray.ensure_daemon("http://127.0.0.1:3920", exe=tmp_path / "missing.exe", opener=sp, health_check=lambda u: False)
    with pytest.raises(ValueError):
        tray.ensure_daemon("http://192.0.2.7:3920", exe=tmp_path, opener=sp, health_check=lambda u: False)
    exe = tmp_path / "f.exe"
    exe.write_text("")
    with pytest.raises(TimeoutError):
        tray.ensure_daemon("http://127.0.0.1:3920", exe=exe, opener=sp, timeout=0.05,
                           health_check=lambda u: False, sleep=lambda s: None)
    assert len(sp.calls) == 1  # spawned once, never retried or killed
    # nothing found anywhere
    monkeypatch.setattr(tray, "find_daemon_exe", lambda exe=None: None)
    with pytest.raises(FileNotFoundError):
        tray.ensure_daemon("http://127.0.0.1:3920", opener=sp, health_check=lambda u: False)


def test_find_daemon_exe_order(tmp_path, monkeypatch):
    env_exe = tmp_path / "env.exe"
    env_exe.write_text("")
    monkeypatch.setenv("FAKESCAN_EXE", str(env_exe))
    assert tray.find_daemon_exe() == env_exe
    explicit = tmp_path / "x.exe"
    explicit.write_text("")
    assert tray.find_daemon_exe(explicit) == explicit
    monkeypatch.delenv("FAKESCAN_EXE")
    monkeypatch.setattr(tray.shutil, "which", lambda n: None)
    monkeypatch.setattr(tray, "KNOWN_DAEMON_DIR", tmp_path / "none")
    repo = tmp_path / "repo"
    (repo / "target" / "debug").mkdir(parents=True)
    built = repo / "target" / "debug" / tray._exe_name()
    built.write_text("")
    monkeypatch.setenv("FAKESCAN_REPO", str(repo))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    assert tray.find_daemon_exe() == built


def test_embedded_server_has_no_tray_module_side_effects(srv):
    assert tray.health(srv.base_url) is True  # health probe only; Server never spawns a tray


# -- ctl: parse level with an injected Manager (no network) ---------------------------------
class FakeManager:
    def __init__(self):
        self.calls = []
        self.fail = None

    def _rec(self, *a):
        self.calls.append(a)
        if self.fail:
            raise self.fail

    def status_table(self):
        self.calls.append(("status_table",))
        return [_row("new", id="t", friendly_name="T", profile="p")]

    def open_ui(self, opener=None):
        self.calls.append(("open_ui", opener))
        if opener:
            opener("http://x/ui")
        return "http://x/ui"

    def open_add_device_settings(self, opener=None):
        self.calls.append(("settings",))
        return "startfile"

    def profiles(self):
        self.calls.append(("profiles",))
        return [ProfileInfo("feeder-tray5", "wsd", ["jfif"], ProfileInfo.parse({"feeder": {"capacity": 5, "media_sizes": ["letter"]}}).feeder)]

    def add(self, id, profile, device_uuid=None, friendly_name=None):
        self._rec("add", id, profile, device_uuid, friendly_name)
        from fakescan.registry import ManagedDevice

        return ManagedDevice(id=id, uuid="u-1", profile=profile)

    def remove(self, id, forget=False):
        self._rec("remove", id, forget)


def test_ctl_parse_level(capsys):
    fm = FakeManager()
    opened = []
    assert ctl.main(["devices", "--width", "80"], manager=fm) == 0
    out = capsys.readouterr().out
    assert "summary: amber" in out and "T" in out
    assert ctl.main(["status"], manager=fm) == 0
    assert ctl.main(["ui"], manager=fm, opener=opened.append) == 0
    assert opened == ["http://x/ui"] and "http://x/ui" in capsys.readouterr().out.splitlines()[-1]
    assert ctl.main(["open-settings"], manager=fm) == 0 and "startfile" in capsys.readouterr().out
    assert ctl.main(["profiles"], manager=fm) == 0 and "capacity=5" in capsys.readouterr().out
    assert ctl.main(["add", "x1", "--profile", "feeder-tray5", "--uuid", "U", "--name", "Nm"], manager=fm) == 0
    assert ("add", "x1", "feeder-tray5", "U", "Nm") in fm.calls
    assert ctl.main(["add", "x2"], manager=fm) == 0 and ("add", "x2", "brother-wsd", None, None) in fm.calls
    assert ctl.main(["remove", "x1", "--forget"], manager=fm) == 0 and ("remove", "x1", True) in fm.calls
    assert ctl.main(["remove", "x2"], manager=fm) == 0 and ("remove", "x2", False) in fm.calls


def test_ctl_argument_errors():
    with pytest.raises(SystemExit):
        ctl.main(["add"], manager=FakeManager())
    with pytest.raises(SystemExit):
        ctl.main(["remove"], manager=FakeManager())
    with pytest.raises(SystemExit):
        ctl.main(["add", "x", "--bogus"], manager=FakeManager())


def test_ctl_mutating_error_text_and_exit_code(capsys):
    def handler(req):
        return httpx.Response(409, json={"error": "device id already exists: x"})

    resp = httpx.Response(409, json={"error": "device id already exists: x"}, request=httpx.Request("POST", "http://h/v1/devices"))
    fm = FakeManager()
    fm.fail = ApiError(resp)
    assert ctl.main(["add", "x", "--profile", "p"], manager=fm) == 1
    err = capsys.readouterr().err
    assert "device id already exists: x" in err and "409" in err
    assert ctl.main(["remove", "x"], manager=fm) == 1
    assert "already exists" in capsys.readouterr().err


# -- ctl: end to end against an embedded server -------------------------------------------
def test_ctl_end_to_end(srv, capsys):
    base = ["--base-url", srv.base_url]
    srv.devices["tray5"].client().set_pages(4)
    assert ctl.main(base + ["devices", "--width", "120"]) == 0
    out = capsys.readouterr().out
    assert "tray5" in out and "4/5" in out and "brother" in out and "summary: amber" in out
    assert ctl.main(base + ["status", "--width", "120"]) == 0
    assert "feeder-duplex30" in capsys.readouterr().out

    assert ctl.main(base + ["add", "extra", "--profile", "feeder-a4only", "--name", "Extra A4"]) == 0
    assert "added: extra" in capsys.readouterr().out
    dev = srv.manager.device("extra")
    assert dev.friendly_name == "Extra A4" and dev.config.feeder.media_sizes == ["a4"]
    assert ctl.main(base + ["add", "extra", "--profile", "feeder-a4only"]) == 1  # duplicate
    assert "extra" in capsys.readouterr().err
    assert ctl.main(base + ["add", "bad", "--profile", "no-such"]) == 1
    assert capsys.readouterr().err.startswith("error:")

    assert ctl.main(base + ["remove", "extra", "--forget"]) == 0
    assert "extra" not in {d.id for d in srv.manager.devices()}
    assert srv.manager.registry().find("extra") is None
    assert ctl.main(base + ["remove", "extra"]) == 1  # 404 now
    assert "unknown device" in capsys.readouterr().err.lower()

    assert ctl.main(base + ["profiles"]) == 0
    ptxt = capsys.readouterr().out
    assert "feeder-tray5" in ptxt and "capacity=5" in ptxt and "brother-wsd" in ptxt

    seen: list[str] = []
    assert ctl.main(base + ["ui"], opener=seen.append) == 0
    assert seen == [f"{srv.base_url}/ui"]


def test_ctl_unreachable_server_exits_nonzero(capsys):
    assert ctl.main(["--base-url", "http://127.0.0.1:1", "devices"]) == 1
    assert "cannot connect" in capsys.readouterr().err


def test_ctl_legacy_status_raw_and_registry_forms_still_parse(srv, capsys, tmp_path, monkeypatch):
    assert ctl.main(["--base-url", srv.base_url, "status", "--raw"]) == 0
    assert "ctl info:" in capsys.readouterr().out
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # legacy forms edit a throwaway registry
    monkeypatch.setattr(ctl, "registry_path", lambda: tmp_path / "devices.json")
    assert ctl.main(["add", "--name", "legacy", "--listen", "0.0.0.0:3922"]) == 0
    assert "added: legacy" in capsys.readouterr().out
    assert ctl.main(["list"]) == 0 and "legacy" in capsys.readouterr().out
    assert ctl.main(["remove", "--name", "legacy"]) == 0
    assert "removed: legacy" in capsys.readouterr().out
