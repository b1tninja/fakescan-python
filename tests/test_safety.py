"""Unit tests of fakescan.safety. No device, no network, no spooler: recording fakes only."""

from __future__ import annotations

import pickle
import sys
import types
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakescan import safety  # noqa: E402
from fakescan.safety import DeviceNotAllowed, Policy, PolicyError, Sink  # noqa: E402

import _safety_fakelib as lib  # noqa: E402

FAKE = lib.FAKE
REAL = lib.REAL
LIB = "_safety_fakelib"


@pytest.fixture(autouse=True)
def _reset():
    lib.reset()
    old = safety.set_policy(None)
    yield
    safety.set_policy(old)


def pol(**kw):
    kw.setdefault("queue_resolver", None)
    return Policy(**kw)


# ---- normalisation -----------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    FAKE, FAKE.upper(), "{" + FAKE + "}", "urn:uuid:" + FAKE, "uuid:" + FAKE, "{" + FAKE + "}\\0000",
    "wia:{" + FAKE.upper() + "}\\0000", "http://192.0.2.1:3921/" + FAKE + "/ScannerService",
    "urn:uuid:" + FAKE + "/uri:" + FAKE + "/ScannerService", "WSD-" + FAKE, FAKE.replace("-", ""),
    "  " + FAKE + "  ",
])
def test_normalise_uuid_forms(text):
    assert safety.normalise_uuid(text) == FAKE
    assert safety.uuids_in(text)[0] == FAKE


def test_normalise_without_uuid_and_non_strings():
    assert safety.normalise_uuid("  {Not-A-Uuid}  ") == "not-a-uuid"
    assert safety.normalise_uuid("urn:uuid:abc") == "abc"
    assert safety.normalise_uuid(None) == "" and safety.normalise_uuid(5) == ""
    assert safety.uuids_in(None) == []
    assert safety.uuids_in("0" * 40) == []  # a longer hex run is not a uuid (no accidental slice)


def test_constant():
    assert safety.FAKE_UUID_PREFIX == "00000000-0000-4000-8000-"


# ---- fakes-only --------------------------------------------------------------------------------
@pytest.mark.parametrize("ref", [
    FAKE, "{" + FAKE + "}\\0000", "urn:uuid:" + FAKE, "http://192.0.2.1:3921/" + FAKE,
    "http://127.0.0.1:5000/p", "wsd:http://127.0.0.1:5357/p", "ipp:ipp://localhost:631/p", "http://[::1]:80/x",
    "http://127.9.9.9/x", {"uuid": FAKE, "name": "anything"}, {"url": "http://127.0.0.1:1/"},
])
def test_provably_fake(ref):
    assert pol().allowed(ref)


@pytest.mark.parametrize("ref", [
    REAL, "", None, 5, {}, object(), "wia:{11111111-2222-3333-4444-555555555555}\\0000",
    "http://192.168.1.50/x", "http://printer.local/x", "wsd:http://203.0.113.9/p", {"name": "Fake Scanner"},
    {"name": "fakescan default", "id": "default"}, {"port": "\\\\.\\Usbscan0", "name": "x"},
    {"uuid": REAL}, {"uuid": FAKE, "port": f"urn:uuid:{REAL}"},        # a real uuid contradicts a fake one
    {"url": "http://127.0.0.1/x", "endpoint": "http://192.168.1.2/x"},  # one non-loopback host
    "00000000-0000-4000-8001-000000000001",                              # next to the prefix, not in it
    "http://192.0.2.1/x?u=" + REAL,
])
def test_not_provably_fake(ref):
    v = pol().evaluate(ref)
    assert not v.allowed and not v


def test_wia_row_is_proven_by_its_port_not_its_id():
    row = lib.ROWS[0]
    assert pol().allowed(row)
    assert not pol().allowed(lib.ROWS[1])  # real device uuid in the port
    assert not pol().allowed(lib.ROWS[2])  # the USB twin: no uuid at all
    assert not pol().allowed(row["id"])    # a bare WIA id proves nothing by itself


def test_objects_with_attributes():
    @dataclass
    class Ref:
        id: str
        name: str = ""
        port: str | None = None

    assert pol().allowed(Ref("x", "n", f"urn:uuid:{FAKE}/uri"))
    assert not pol().allowed(Ref("x", "fake looking", None))


def test_remember_makes_bare_id_usable_only_after_proof():
    p = pol()
    bare = lib.ROWS[0]["id"]
    assert not p.allowed(bare)
    assert not p.remember(lib.ROWS[1]) and not p.allowed(lib.ROWS[1]["id"])  # refused rows are never remembered
    assert p.remember(lib.ROWS[0])
    assert p.allowed(bare) and p.allowed(bare.removeprefix("wia:"))
    assert not p.allowed(lib.ROWS[1]["id"])


def test_extra_and_is_fake_helper():
    p = pol()
    assert not safety.is_fake("wia:{abc}\\0001", policy=p)
    assert safety.is_fake("wia:{abc}\\0001", extra=["wia:{abc}\\0001"], policy=p)
    safety.set_policy(pol(extra=["spooler-known"]))
    assert safety.is_fake("spooler-known") and not safety.is_fake("other")


def test_loopback_proof_can_be_switched_off():
    assert not pol(loopback_is_fake=False).allowed("http://127.0.0.1:1/p")
    assert pol(loopback_is_fake=False).allowed(f"http://127.0.0.1:1/{FAKE}")


# ---- deny ----------------------------------------------------------------------------------------
def test_deny_wins_over_fake_and_matches_loosely():
    p = pol(deny=["Brother", "E3248000-80CE"])
    assert not p.allowed({"uuid": FAKE, "name": "My BROTHER MFC"})
    assert p.evaluate({"uuid": FAKE, "name": "My BROTHER MFC"}).reason == "denied"
    assert not p.allowed({"uuid": FAKE, "id": "e3248000_80ce"})
    assert not p.allowed({"url": "http://127.0.0.1/brother-9330"})
    assert p.allowed({"uuid": FAKE, "name": "fakeprint inkjet"})


def test_deny_only_mode_is_loose_but_still_denies():
    p = pol(mode="deny-only", deny=["office"])
    assert p.allowed("http://192.168.1.2/x")
    assert not p.allowed({"name": "Office MFC", "url": "http://192.168.1.2/x"})


def test_env_forms():
    def e(**kw):
        return Policy.from_env(kw, queue_resolver=None)

    p = e()
    assert p.mode == "fakes-only" and p.deny == ()
    assert e(PDFCRAFT_TEST_DEVICE_POLICY="fakes-only").deny == ()
    p = e(PDFCRAFT_TEST_DEVICE_POLICY="deny=a,b;c")
    assert p.mode == "fakes-only" and p.deny == ("a", "b", "c")  # additive: never loosens
    p = e(PDFCRAFT_TEST_DEVICE_POLICY="fakes-only,deny=x, y")
    assert p.mode == "fakes-only" and p.deny == ("x", "y")
    p = e(PDFCRAFT_TEST_DEVICE_POLICY="deny=x,fakes-only,z")
    assert p.deny == ("x", "z")
    assert e(PDFCRAFT_TEST_DEVICE_POLICY="deny-only,deny=q").mode == "deny-only"
    with pytest.raises(PolicyError):
        e(PDFCRAFT_TEST_DEVICE_POLICY="allow-everything")
    with pytest.raises(PolicyError):
        e(PDFCRAFT_TEST_DEVICE_POLICY="x=y")


def test_legacy_env_names_are_deny_patterns():
    env = {
        "CRAFT_SCAN_DENY": "one,two",
        "PDFCRAFT_SCAN_DENY": "three;four",
        "CRAFT_PRINT_DENY": "five",
        "PDFCRAFT_PRINT_DENY": " six , ",
        "PDFCRAFT_TEST_DEVICE_POLICY": "deny=seven",
    }
    p = Policy.from_env(env, queue_resolver=None)
    assert set(p.deny) == {"one", "two", "three", "four", "five", "six", "seven"}
    assert p.mode == "fakes-only"
    for word in p.deny:
        assert not p.allowed({"uuid": FAKE, "name": f"dev {word} x"})


def test_from_env_takes_extra_deny_patterns():
    p = Policy.from_env({"PDFCRAFT_SCAN_DENY": "a"}, deny=["b"], queue_resolver=None)
    assert p.deny == ("a", "b")


def test_from_env_reads_the_process_environment(monkeypatch):
    for n in safety.LEGACY_DENY_ENVS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("PDFCRAFT_SCAN_DENY", "secret")
    monkeypatch.delenv("PDFCRAFT_TEST_DEVICE_POLICY", raising=False)
    assert not safety.current_policy().allowed({"uuid": FAKE, "name": "secret"})


# ---- queues ---------------------------------------------------------------------------------------
def test_spooler_queue_needs_the_registry_proof():
    names = {"fake q": FAKE, "real q": REAL, "weird": "not a uuid"}
    p = Policy(queue_resolver=names.get)
    assert p.allowed("spooler:fake q")
    assert not p.allowed("spooler:real q") and not p.allowed("spooler:weird") and not p.allowed("spooler:ghost")
    assert p.evaluate("spooler:ghost").reason == "unresolved"
    assert not pol().allowed("spooler:fake q")  # no resolver: cannot prove
    assert not pol().allowed("cups:anything")

    def boom(_):
        raise RuntimeError("registry unreadable")

    assert not Policy(queue_resolver=boom).allowed("spooler:fake q")


# ---- listings --------------------------------------------------------------------------------------
def test_filter_refs_dict_list_and_listing():
    p = pol()
    payload = {"scanners": [dict(r) for r in lib.ROWS], "errors": [{"backend": "b", "error": "e"}]}
    out = p.filter_refs(payload)  # a listing shows every device
    assert [r["name"] for r in out["scanners"]] == ["Fake Scanner", "Office MFC", "Office MFC"] and out["errors"] == payload["errors"]
    only = p.filter_refs(payload, allowed_only=True)
    assert [r["name"] for r in only["scanners"]] == ["Fake Scanner"]
    listing = lib.Listing(tuple(lib.PRINTERS), ())
    assert [r.name for r in p.filter_refs(listing).items] == ["fakeprint inkjet", "Office"]
    assert [r.name for r in p.filter_refs(listing, allowed_only=True).items] == ["fakeprint inkjet"]
    assert p.filter_refs([]) == []
    marked = p.filter_refs(listing, include_excluded=True)
    assert [m.excluded for m in marked] == [False, True] and len(marked) == 2
    assert [r.name for r in marked.allowed] == ["fakeprint inkjet"] and [r.name for r in marked.excluded] == ["Office"]
    assert all(m.reason for m in marked)


# ---- exceptions never leak ---------------------------------------------------------------------------
def test_exception_text_leaks_nothing():
    secrets = [REAL, "192.168.77.5", "Secret Office Printer", "e3248000", "Usbscan0"]
    ref = {"uuid": REAL, "url": "http://192.168.77.5/x", "name": "Secret Office Printer", "port": "\\\\.\\Usbscan0"}
    with pytest.raises(DeviceNotAllowed) as ei:
        pol().assert_allowed(ref, "submit")
    e = ei.value
    blob = " ".join([str(e), repr(e), repr(e.args), e.action, e.reason])
    for s in secrets:
        assert s.lower() not in blob.lower()
    assert "excluded by the test device policy" in str(e)
    e2 = pickle.loads(pickle.dumps(e))
    assert str(e2) == str(e) and isinstance(e2, PermissionError)
    with pytest.raises(DeviceNotAllowed) as ei:
        pol(deny=["secret"]).assert_allowed({"uuid": FAKE, "name": "Secret Office Printer"})
    assert "secret" not in str(ei.value).lower()


# ---- guard_open / guard_submit / guard() ------------------------------------------------------------
def test_guard_open_refuses_before_the_call():
    calls = []

    @safety.guard_open("url", policy=pol())
    def op(url, n=1):
        calls.append(url)
        return n

    assert op("http://127.0.0.1:1/p", n=3) == 3
    for bad in ("http://192.168.1.2/p", "", None, REAL):
        with pytest.raises(DeviceNotAllowed):
            op(bad)
    with pytest.raises(DeviceNotAllowed):
        op(url="http://192.168.1.2/p")
    assert calls == ["http://127.0.0.1:1/p"]


def test_guard_defaults_dotted_paths_exempt_and_submit():
    calls = []

    @safety.guard_submit("settings.device", exempt=("registry",), policy=pol())
    def go(settings=None, registry=None):
        calls.append(1)

    class S:
        device = ""

    with pytest.raises(DeviceNotAllowed) as ei:
        go(S())
    assert ei.value.action == "submit" and ei.value.reason == "no-device"  # "the default device" is excluded
    with pytest.raises(DeviceNotAllowed):
        go(None)
    S.device = f"urn:uuid:{FAKE}"
    go(S())
    S.device = ""
    go(S(), registry=object())  # an in-memory scripted registry touches no device
    assert len(calls) == 2

    @safety.guard_open(policy=pol())  # first parameter by default
    def first(x):
        calls.append(x)

    with pytest.raises(DeviceNotAllowed):
        first("nope")
    assert len(calls) == 2


def test_guard_context_manager():
    ran = []
    with safety.guard(FAKE, policy=pol()):
        ran.append(1)
    with pytest.raises(DeviceNotAllowed):
        with safety.guard(FAKE, REAL, policy=pol()):
            ran.append(2)
    with pytest.raises(DeviceNotAllowed):
        with safety.guard(policy=pol()):
            ran.append(3)
    assert ran == [1]


def test_self_check():
    p = pol(deny=["marker"])
    p.self_check([FAKE, "http://127.0.0.1:1/"])
    with pytest.raises(DeviceNotAllowed):
        p.self_check([FAKE, REAL])
    with pytest.raises(DeviceNotAllowed):
        p.self_check([{"uuid": FAKE, "name": "has MARKER"}])
    with pytest.raises(DeviceNotAllowed):
        safety.self_check(["http://10.0.0.1/"], policy=p)


# ---- install / guarded on a recording library ---------------------------------------------------------
TABLE = (
    Sink(LIB, None, "open_device", "open", ("url",)),
    Sink(LIB, None, "scan", "scan", ("device",), exempt=("registry",)),
    Sink(LIB, None, "list_scanners", "list", custom="list_scanners"),
    Sink(LIB, None, "scanner_details", "open", ("id",)),
    Sink(LIB, None, "scanner_dump", "open", custom="dump"),
    Sink(LIB, "Registry", "register_wsd", "register", ("urls",)),
    Sink(LIB, "Registry", "submit", "submit", ("printer", "job.printer"), any_required=True, aliases=("submit_job",),
         remember_result=True),
    Sink(LIB, "Registry", "list_printers", "list", custom="list_printers"),
    Sink(LIB, "Registry", "job_status", "open", ("job_id",), as_text=True),
)


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setattr(safety, "SINKS", TABLE)
    return TABLE


@pytest.fixture()
def installed(table):
    safety.set_policy(pol())
    inst = safety.install(modules=[LIB])
    yield lib
    inst.uninstall()


def test_install_refuses_before_any_call_and_uninstall_restores(table):
    m = lib
    orig = m.open_device
    safety.set_policy(pol())
    inst = safety.install(modules=[LIB])
    assert m.open_device is not orig and inst.patched
    assert m.open_device("http://127.0.0.1:9/p") == "opened"
    for bad in ("http://192.168.1.2/p", "", None, REAL):
        with pytest.raises(DeviceNotAllowed):
            m.open_device(bad)
    assert lib.CALLS == [("open_device", "http://127.0.0.1:9/p")]
    again = safety.install(modules=[LIB])
    assert not again.patched and again.skipped  # idempotent
    inst.uninstall()
    assert m.open_device is orig


def test_scan_default_device_and_registry_exemption(installed):
    m = installed
    for bad in ("", REAL, "wia:{abc}\\0000"):
        with pytest.raises(DeviceNotAllowed):
            m.scan(device=bad)
    with pytest.raises(DeviceNotAllowed):
        m.scan()
    assert lib.CALLS == []
    assert m.scan(device=FAKE) == "scanned"
    assert m.scan(registry=object()) == "scanned"  # scripted registry, no device


def test_listing_never_opens_excluded_devices(installed):
    m = installed
    out = m.list_scanners()  # a listing shows every device
    assert [r["name"] for r in out["scanners"]] == ["Fake Scanner", "Office MFC", "Office MFC"]
    assert lib.CALLS == [("list_scanners", None, False)]  # names only
    lib.reset()
    out = m.list_scanners(details=True)
    assert [r.get("details") for r in out["scanners"]] == ["loaded", None, None]  # excluded rows listed, never opened
    opened = [c for c in lib.CALLS if c[0] == "scanner_details"]
    assert len(opened) == 1 and "11111111" in opened[0][1]  # only the fake was opened
    lib.reset()
    out = m.list_scanners(device="mfc", details=False)  # names the excluded rows: listed, nothing opened
    assert [r["name"] for r in out["scanners"]] == ["Office MFC", "Office MFC"] and not [c for c in lib.CALLS if c[0] == "scanner_details"]
    with pytest.raises(ValueError):
        m.list_scanners(device="  ")
    marked = m.list_scanners(include_excluded=True)
    assert [x.excluded for x in marked] == [False, True, True] and len(marked.allowed) == 1


def test_proven_row_makes_its_bare_id_usable(installed):
    m = installed
    fake_id, real_id = lib.ROWS[0]["id"], lib.ROWS[1]["id"]
    with pytest.raises(DeviceNotAllowed):
        m.scanner_details(fake_id)  # not listed yet: unproven
    m.list_scanners()
    lib.reset()
    assert m.scanner_details(fake_id)["details"] == "loaded"
    with pytest.raises(DeviceNotAllowed):
        m.scanner_details(real_id)
    assert [c[0] for c in lib.CALLS] == ["scanner_details"]


def test_dump_filter(installed):
    m = installed
    with pytest.raises(DeviceNotAllowed):
        m.scanner_dump("")
    with pytest.raises(DeviceNotAllowed):
        m.scanner_dump("mfc")       # selects excluded devices
    with pytest.raises(DeviceNotAllowed):
        m.scanner_dump("nothing")   # selects nothing
    assert not [c for c in lib.CALLS if c[0] == "scanner_dump"]
    assert m.scanner_dump("fake scanner") == "dump"  # selects only the fake
    assert [c for c in lib.CALLS if c[0] == "scanner_dump"] == [("scanner_dump", "fake scanner")]


def test_registry_methods(installed):
    m = installed
    r = m.Registry()
    assert r.register_wsd(["http://127.0.0.1:5357/p"])
    with pytest.raises(DeviceNotAllowed):
        r.register_wsd(["http://127.0.0.1:5357/p", "http://203.0.113.9/p"])  # one bad url poisons the call
    with pytest.raises(DeviceNotAllowed):
        r.submit(b"x")                                    # no printer: the default printer may be real
    with pytest.raises(DeviceNotAllowed):
        r.submit(b"x", printer="wsd:http://203.0.113.9/p")
    with pytest.raises(DeviceNotAllowed):
        r.submit_job(b"x", printer="wsd:http://203.0.113.9/p")  # the alias is guarded too

    class J:
        printer = "wsd:http://203.0.113.9/p"

    with pytest.raises(DeviceNotAllowed):
        r.submit(b"x", printer="wsd:http://127.0.0.1:5357/p", job=J())  # job.printer is checked as well
    assert [c[0] for c in lib.CALLS] == ["register_wsd"]
    sub = r.submit(b"x", printer="wsd:http://127.0.0.1:5357/p")
    assert sub.job  # the returned handle is remembered: its status can be read though its text has no proof
    assert r.job_status(sub.job) == "status"
    with pytest.raises(DeviceNotAllowed):
        r.job_status("wsd:http://203.0.113.9/p#7")
    items = r.list_printers().items
    assert [p.name for p in items] == ["fakeprint inkjet", "Office"]  # listed, not hidden
    assert len(r.list_printers(include_excluded=True)) == 2


def test_guarded_module_and_instance_proxies(table):
    m = lib
    safety.set_policy(pol())
    orig = m.open_device
    g = safety.guarded(m)
    assert m.open_device is orig  # nothing was patched
    assert g.open_device("http://127.0.0.1:1/") == "opened"
    with pytest.raises(DeviceNotAllowed):
        g.open_device("http://192.168.1.2/")
    assert len([c for c in lib.CALLS if c[0] == "open_device"]) == 1
    reg = g.Registry()  # classes with guarded methods come back as factories of guarded objects
    with pytest.raises(DeviceNotAllowed):
        reg.submit(b"x", printer="wsd:http://203.0.113.9/p")
    assert [p.name for p in reg.list_printers().items] == ["fakeprint inkjet", "Office"]  # listed, not hidden
    reg2 = safety.guarded(m.Registry())
    with pytest.raises(DeviceNotAllowed):
        reg2.register_wsd(["http://203.0.113.9/p"])
    with pytest.raises(TypeError):
        safety.guarded(object())
    assert g.FAKE == FAKE  # data passes through
    assert not [c for c in lib.CALLS if c[0] in ("submit", "register_wsd")]


def test_stale_table_fails_loudly_and_leaves_nothing_patched(monkeypatch):
    m = lib
    orig = m.open_device
    monkeypatch.setattr(safety, "SINKS", (Sink(LIB, None, "open_device", "open", ("renamed_param",)),))
    with pytest.raises(safety.SafetyConfigError):
        safety.install(modules=[LIB])
    monkeypatch.setattr(safety, "SINKS", (
        Sink(LIB, None, "open_device", "open", ("url",)),
        Sink(LIB, None, "does_not_exist", "open", ("x",)),
    ))
    with pytest.raises(safety.SafetyConfigError):
        safety.install(modules=[LIB])
    assert m.open_device is orig  # the first sink was rolled back


def test_unimportable_library_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(safety, "SINKS", (Sink("no_such_library_xyz", None, "f", "open", ("x",)),))
    inst = safety.install(modules=["no_such_library_xyz"])
    assert inst.skipped and not inst.patched


# ---- the Windows spooler helpers (a fake winprint module: no spooler is touched) ----------------------
def _fake_winprint():
    PI = namedtuple("PI", "name port")
    calls = []
    uuids = {"WSD-1": FAKE, "WSD-2": REAL}
    mod = types.ModuleType("_fake_winprint")
    queues = [PI("fake inkjet", "WSD-1"), PI("Office", "WSD-2"), PI("Usb", "USB001")]
    mod.list_printers = lambda: list(queues)
    mod.wsd_port_uuid = lambda port: uuids.get(port)
    mod.print_image = lambda printer, rgb=None: calls.append(("print_image", printer)) or 7
    mod.print_raw = lambda printer, data=b"": calls.append(("print_raw", printer)) or 8
    mod.job_status = lambda printer, job_id=1: calls.append(("job_status", printer)) or "st"
    mod.wait_for_job = lambda printer, job_id=1: calls.append(("wait_for_job", printer)) or "w"
    for f in ("list_printers", "wsd_port_uuid", "print_image", "print_raw", "job_status", "wait_for_job"):
        getattr(mod, f).__module__ = "_fake_winprint"
    sys.modules["_fake_winprint"] = mod
    return mod, calls


def test_winprint_only_prints_to_queues_proven_by_their_registry_uuid(monkeypatch):
    mod, calls = _fake_winprint()
    try:
        monkeypatch.setattr(safety, "SINKS", tuple(
            Sink("_fake_winprint", None, n, "submit", custom="winprint")
            for n in ("print_image", "print_raw", "job_status", "wait_for_job")
        ) + (Sink("_fake_winprint", None, "list_printers", "list", custom="winprint_list"),))
        safety.set_policy(pol())
        inst = safety.install(modules=["_fake_winprint"])
        assert [q.name for q in mod.list_printers()] == ["fake inkjet"]  # the others are never listed
        assert [m.excluded for m in mod.list_printers(include_excluded=True)] == [False, True, True]
        assert mod.print_image("fake inkjet") == 7
        for bad in ("Office", "Usb", "ghost", "", None, "fake INKJET"):
            for fn in (mod.print_image, mod.print_raw, mod.job_status, mod.wait_for_job):
                with pytest.raises(DeviceNotAllowed):
                    fn(bad)
        assert calls == [("print_image", "fake inkjet")]
        safety.set_policy(pol(deny=["inkjet"]))
        with pytest.raises(DeviceNotAllowed):
            mod.print_raw("fake inkjet")
        assert mod.list_printers() == []
        inst.uninstall()
    finally:
        sys.modules.pop("_fake_winprint", None)


def test_assert_fake_queue_uses_the_registry_uuid_not_the_name():
    mod, calls = _fake_winprint()
    try:
        p = pol()
        safety.assert_fake_queue("fake inkjet", policy=p, module=mod)
        assert safety.queue_ref("fake inkjet", mod)["uuid"] == FAKE
        for bad in ("Office", "Usb", "ghost", "", None):
            with pytest.raises(DeviceNotAllowed):
                safety.assert_fake_queue(bad, policy=p, module=mod)
        with pytest.raises(DeviceNotAllowed):
            safety.assert_fake_queue("fake inkjet", policy=pol(deny=["inkjet"]), module=mod)
        assert calls == []
    finally:
        sys.modules.pop("_fake_winprint", None)


# ---- the table's own shape --------------------------------------------------------------------------
def test_coverage_helper():
    assert safety.is_covered("pdfcraft.wsdprint.print_document")
    assert safety.is_covered("pdfcraft.printing.Registry.submit_job")
    assert safety.is_covered("pdfcraft.printing.PrintJob.advance")  # PrintJob.* is pure (its ctor is guarded)
    assert not safety.is_covered("pdfcraft.printing.Registry.brand_new_method")
    assert not safety.is_covered("pdfcraft.scanning.brand_new_function")


def test_allow_names_for_real_device_marker():
    p = pol(deny=["secret"]).with_allowed_names("My Named Device")
    assert p.allowed({"name": "My Named Device"})
    assert not p.allowed({"name": "Other Device"})
    assert not pol(deny=["named"]).with_allowed_names("My Named Device").allowed({"name": "My Named Device"})
