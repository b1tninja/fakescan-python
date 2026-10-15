"""The guard over the real pdfcraft / fakeprint bindings (skipped when they are not installed).

Safety of this test itself: every native entry point that could reach a device is replaced by a recording stub
BEFORE the guard is installed, so a broken guard would hit the stub, never a scanner, a spooler queue or the LAN.
Network-shaped calls only use TEST-NET (192.0.2.0/24) addresses. No listing of real devices is made.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("pdfcraft._native", reason="pdfcraft bindings not installed")

from fakescan import safety  # noqa: E402
from fakescan.safety import DeviceNotAllowed, Policy  # noqa: E402

FAKE = "00000000-0000-4000-8000-00000000abcd"
REAL = "e3248000-80ce-11db-8000-3c2af435905f"
NATIVE_CALLS: list[str] = []


@pytest.fixture()
def bindings(monkeypatch):
    import pdfcraft._native as n
    import pdfcraft.scan as sc
    import pdfcraft.scanning as sg

    NATIVE_CALLS.clear()
    rows = {"scanners": [
        {"id": "wia:{11111111-2222-3333-4444-555555555555}\\0000", "name": "A", "port": f"urn:uuid:{FAKE}/x", "connection": "wsd"},
        {"id": "wia:{66666666-2222-3333-4444-555555555555}\\0000", "name": "B", "port": f"urn:uuid:{REAL}/x", "connection": "wsd"},
    ], "errors": []}

    def stub(name, result=None):
        def f(*a, **k):
            NATIVE_CALLS.append(name)
            return result() if callable(result) else result
        return f

    for holder, attr, name, res in (
        (n, "list_scanners", "list_scanners", lambda: {**rows, "scanners": [dict(r) for r in rows["scanners"]]}),
        (n, "scanner_details", "scanner_details", lambda: {"id": "x", "name": "x", "port": None, "details": "loaded"}),
        (n, "scanner_status", "scanner_status", None),
        (n, "scanner_dump", "scanner_dump", ""),
        (n, "run_attempt", "run_attempt", None),
        (n, "acquire", "acquire", None),
        (n, "acquire_pages", "acquire_pages", None),
        (sc, "_list_scanners", "list_scanners", lambda: {**rows, "scanners": [dict(r) for r in rows["scanners"]]}),
        (sc, "_scanner_details", "scanner_details", lambda: {"id": "x", "name": "x", "port": None, "details": "loaded"}),
        (sc, "_scanner_status", "scanner_status", None),
        (sc, "_scanner_dump", "scanner_dump", ""),
        (sc, "_acquire", "acquire", None),
        (sc, "_acquire_pages", "acquire_pages", None),
    ):
        monkeypatch.setattr(holder, attr, stub(name, res))
    monkeypatch.setattr(sg, "_n", _Proxy(sg._n, {"run_attempt": stub("run_attempt"), "list_scanners": n.list_scanners,
                                                 "scanner_details": n.scanner_details, "scanner_status": n.scanner_status,
                                                 "scanner_dump": n.scanner_dump}))
    safety.set_policy(Policy(queue_resolver=None))
    inst = safety.install(modules=["pdfcraft.wsdprint", "pdfcraft.printing", "pdfcraft.scan", "pdfcraft.scanning"])
    yield inst
    inst.uninstall()
    safety.set_policy(None)


class _Proxy:
    def __init__(self, real, over):
        self._real, self._over = real, over

    def __getattr__(self, name):
        return self._over[name] if name in self._over else getattr(self._real, name)


def test_every_table_entry_of_the_installed_libraries_is_wrapped(bindings):
    assert bindings.patched
    import importlib

    for s in safety.SINKS:
        if not s.module.startswith("pdfcraft."):
            continue
        mod = importlib.import_module(s.module)
        holder = getattr(mod, s.owner) if s.owner else mod
        fn = holder.__dict__.get(s.name) if s.owner else getattr(mod, s.name)
        assert getattr(fn, "__fakescan_safety__", False), f"{s.module}.{s.owner}.{s.name} is not guarded"
        for a in s.aliases:
            assert getattr(holder.__dict__.get(a), "__fakescan_safety__", False), a


def test_wsdprint_refuses_non_loopback_before_the_network(bindings):
    from pdfcraft import wsdprint as w

    for fn, args in ((w.printer_info, ()), (w.list_jobs, ()), (w.job_elements, (1,)), (w.cancel_job, (1,))):
        with pytest.raises(DeviceNotAllowed):
            fn("http://192.0.2.1:5357/p", *args, timeout=0.2)
    with pytest.raises(DeviceNotAllowed):
        w.print_document("http://192.0.2.1:5357/p", b"%PDF-1.4", format="application/pdf", user_confirmed=True, timeout=0.2)
    with pytest.raises(DeviceNotAllowed):
        w.print_document("", b"x", user_confirmed=True)


def test_registry_refuses_before_registering_or_sending(bindings):
    from pdfcraft import printing as p

    r = p.Registry()
    with pytest.raises(DeviceNotAllowed):
        r.register_wsd(["http://192.0.2.1:5357/p"])
    with pytest.raises(DeviceNotAllowed):
        r.register_ipp(["ipp://192.0.2.1:631/p"])
    with pytest.raises(DeviceNotAllowed):
        r.add_wsd_printer("http://192.0.2.1:5357/p")
    with pytest.raises(DeviceNotAllowed):
        r.submit(b"%PDF-1.4", printer="wsd:http://192.0.2.1:5357/p", user_confirmed=True)
    with pytest.raises(DeviceNotAllowed):
        r.submit(b"%PDF-1.4", user_confirmed=True)  # no printer named: the default may be a real one
    with pytest.raises(DeviceNotAllowed):
        r.submit_job(b"%PDF-1.4", job=p.Job(printer="wsd:http://192.0.2.1:5357/p"), user_confirmed=True)
    with pytest.raises(DeviceNotAllowed):
        r.printer_details("wsd:http://192.0.2.1:5357/p")
    with pytest.raises(DeviceNotAllowed):
        r.register_system_spooler()  # no way to prove a queue here (resolver None)
    with pytest.raises(DeviceNotAllowed):
        p.PrintJob(r, b"%PDF-1.4", user_confirmed=True)  # no job.printer
    with pytest.raises(DeviceNotAllowed):
        p.PrintJob(r, b"%PDF-1.4", job=p.Job(printer="wsd:http://192.0.2.1:5357/p"), user_confirmed=True)
    assert r.backend_names() == ()  # nothing was registered
    assert r.register_wsd(["http://127.0.0.1:5357/p"]) == ("wsd:http://127.0.0.1:5357/p",)  # loopback is a fake


def test_scanning_listing_filters_and_opens_only_fakes(bindings):
    import pdfcraft.scan as sc
    import pdfcraft.scanning as sg

    out = sg.list_scanners()  # a listing shows every device
    assert [r.name for r in out.items] == ["A", "B"]
    NATIVE_CALLS.clear()
    out = sg.list_scanners(details=True)
    assert [r.name for r in out] == ["x", "B"]  # the stub's detail row replaced the fake's row; B is listed, never opened
    assert NATIVE_CALLS.count("scanner_details") == 1
    flat = sc.list_scanners()
    assert [r["name"] for r in flat["scanners"]] == ["A", "B"]
    rows, errors = sc.scanners_with_errors()
    assert [r["name"] for r in rows] == ["A", "B"] and errors == []
    marked = sg.list_scanners(include_excluded=True)
    assert [m.excluded for m in marked] == [False, True]


def test_scan_entry_points_refuse_unproven_devices_before_native(bindings):
    import pdfcraft.scan as sc
    import pdfcraft.scanning as sg

    NATIVE_CALLS.clear()
    for dev in ("", "wia:{abc}\\0000", REAL):
        with pytest.raises(DeviceNotAllowed):
            sc.acquire(device=dev)
        with pytest.raises(DeviceNotAllowed):
            sc.acquire_pages(device=dev)
        with pytest.raises(DeviceNotAllowed):
            sc.start(device=dev)
        with pytest.raises(DeviceNotAllowed):
            sc.ScanJob(device=dev)
        with pytest.raises(DeviceNotAllowed):
            sg.acquire(sg.ScanSettings(device=dev))
        job = sg.ScanJob(sg.ScanSettings(device=dev))
        with pytest.raises(DeviceNotAllowed):
            sg.run_job(job)
        plan = job.begin_attempt()
        with pytest.raises(DeviceNotAllowed):
            plan.acquire()
        with pytest.raises(DeviceNotAllowed):
            plan.start()
        with pytest.raises(DeviceNotAllowed):
            job.start_attempt(plan)
        with pytest.raises(DeviceNotAllowed):
            sg.RunningAttempt(plan)
    for id_ in ("wia:{abc}\\0000", REAL, ""):
        with pytest.raises(DeviceNotAllowed):
            sg.scanner_details(id_)
        with pytest.raises(DeviceNotAllowed):
            sg.scanner_status(id_)
        with pytest.raises(DeviceNotAllowed):
            sg.device_view(id_)
        with pytest.raises(DeviceNotAllowed):
            sc.scanner_details(id_)
        with pytest.raises(DeviceNotAllowed):
            sc.scanner_status(id_)
    for f in ("", "b", "nomatch"):
        with pytest.raises(DeviceNotAllowed):
            sg.diagnostic_dump(f)
        with pytest.raises(DeviceNotAllowed):
            sc.scanner_dump(f)
    assert NATIVE_CALLS.count("run_attempt") + NATIVE_CALLS.count("acquire") + NATIVE_CALLS.count("acquire_pages") == 0
    assert "scanner_details" not in NATIVE_CALLS and "scanner_dump" not in NATIVE_CALLS
    # the listed fake's bare id becomes usable (proven once through its port uuid)
    sg.list_scanners()
    sg.scanner_details("wia:{11111111-2222-3333-4444-555555555555}\\0000")
    assert NATIVE_CALLS.count("scanner_details") == 1


def test_scanning_with_a_fake_device_id_reaches_native(bindings):
    import pdfcraft.scanning as sg

    plan = sg.ScanJob(sg.ScanSettings(device=f"urn:uuid:{FAKE}")).begin_attempt()
    with pytest.raises(AttributeError):  # the guard let it through; the recording stub returns nothing usable
        plan.acquire()
    assert NATIVE_CALLS.count("run_attempt") == 1


def test_scan_job_stand_in_keeps_isinstance(bindings):
    import pdfcraft._native as n
    import pdfcraft.scan as sc

    j = sc.ScanJob.__wrapped_original__
    assert j is not None
    assert inspect.isclass(j)


def test_guarded_proxies_over_real_modules_do_not_patch(monkeypatch):
    import pdfcraft.wsdprint as w

    orig = w.print_document
    g = safety.guarded(w)
    with pytest.raises(DeviceNotAllowed):
        g.print_document("http://192.0.2.1:5357/p", b"x", user_confirmed=True, timeout=0.2)
    assert w.print_document is orig
    from pdfcraft import printing as p

    reg = safety.guarded(p.Registry())
    with pytest.raises(DeviceNotAllowed):
        reg.register_wsd(["http://192.0.2.1:5357/p"])
