"""Test-harness device protection: the harness may only touch devices that are PROVABLY fakes.

Pure Python, standard library only. Nothing in here lives in (or is read by) any Rust crate: this is the whole
guard that keeps the Python test harnesses (pdfcraft-fakeprint, pdfcraft-fakescan, fakeprint-python's tests, ...)
away from real scanners and printers. It is not a product feature and not a security boundary against a hostile
program; it prevents a harness from addressing the owner's real devices by mistake.

Model
-----
* Identity is a UUID in the fake namespace (:data:`FAKE_UUID_PREFIX`), never a name. A reference
  (a string, a dict, an object with ``.id`` / ``.uuid`` / ``.port`` / ``.url`` ..., the bindings' ``ScannerRef`` /
  ``PrinterRef``) is *provably fake* when
    - an identity field carries a fake UUID, or
    - an identity URL points at a loopback address (the embedded servers; ``Policy.loopback_is_fake``), or
    - it was proven earlier and remembered (:meth:`Policy.remember`; a WIA id is a random guid, the device UUID
      is in its port, so the harness proves the row once and the bare id is then known), or
    - a queue name resolves, through the registry, to a fake WSD device UUID (Windows spooler queues).
  and nothing contradicts it (a non-fake UUID in the same identity field, a non-loopback host without a fake
  UUID, a deny pattern).
* ``fakes-only`` (default): anything not provably fake is excluded. An absent device ("use the default device")
  is excluded too: the default device can be a real one.
* ``deny=<patterns>`` adds substring patterns (id / name / port / url, case and punctuation insensitive) that
  exclude a device even when it looks fake.
* Excluded devices are refused with :class:`DeviceNotAllowed`, whose text never contains an id, an address or a
  name, BEFORE any call that would open, address or print on the device is made.

Configuration (environment)
---------------------------
``PDFCRAFT_TEST_DEVICE_POLICY``: ``fakes-only`` | ``deny=a,b;c`` | ``fakes-only,deny=a,b``. ``deny=`` is additive
(it never loosens ``fakes-only``); the explicit token ``deny-only`` selects the loose mode (everything not denied
passes) and exists for non-harness use only. An unknown token raises :class:`PolicyError` (fail closed).
The legacy names ``CRAFT_SCAN_DENY``, ``PDFCRAFT_SCAN_DENY``, ``CRAFT_PRINT_DENY``, ``PDFCRAFT_PRINT_DENY``
(comma or semicolon separated patterns) are read as deny patterns too.

Use
---
``install()`` patches the pdfcraft / fakeprint entry points in place (what the pytest plugin
``fakescan.safety_pytest`` does); ``guarded(x)`` returns a guarded proxy of one module / object; ``guard_open``,
``guard_submit`` and ``guard()`` wrap your own code. See ``docs`` in the pdfcraft-notes repo
(``python-device-safety.md``) for the call-site inventory.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import importlib
import inspect
import ipaddress
import os
import re
import sys
import threading
import types
from collections.abc import Callable, Iterable, Mapping
from typing import Any

__all__ = [
    "ALLOW_REAL_ENV",
    "DeviceNotAllowed",
    "FAKE_UUID_PREFIX",
    "GUARDED",
    "Installation",
    "LEGACY_DENY_ENVS",
    "Marked",
    "MarkedListing",
    "POLICY_ENV",
    "Policy",
    "PolicyError",
    "PURE",
    "SafetyConfigError",
    "Verdict",
    "allowed",
    "assert_fake",
    "assert_fake_queue",
    "current_policy",
    "filter_refs",
    "guard",
    "guard_open",
    "guard_submit",
    "guarded",
    "install",
    "is_fake",
    "normalise_uuid",
    "queue_ref",
    "self_check",
    "set_policy",
    "use_policy",
    "uuids_in",
]

#: The fakes' identity convention (fakescan / fakeprint device UUIDs all start with it).
FAKE_UUID_PREFIX = "00000000-0000-4000-8000-"
POLICY_ENV = "PDFCRAFT_TEST_DEVICE_POLICY"
LEGACY_DENY_ENVS = ("CRAFT_SCAN_DENY", "PDFCRAFT_SCAN_DENY", "CRAFT_PRINT_DENY", "PDFCRAFT_PRINT_DENY")
ALLOW_REAL_ENV = "PDFCRAFT_ALLOW_REAL_DEVICE_TESTS"


class PolicyError(ValueError):
    """The policy text (``PDFCRAFT_TEST_DEVICE_POLICY``) cannot be understood: refuse to run, never loosen."""


class SafetyConfigError(RuntimeError):
    """The guard table no longer matches the code it guards (a renamed parameter): fail loudly, never skip."""


class DeviceNotAllowed(PermissionError):
    """A device is excluded by the test device policy. The text is fixed: it never carries the device's
    uuid, address, port or name (those are what the guard exists to keep out of logs)."""

    def __init__(self, action: str = "use", reason: str = "not-provably-fake") -> None:
        self.action = action
        self.reason = reason
        super().__init__(
            f"{action}: the device is excluded by the test device policy ({reason}); "
            "the test harness only touches provably fake devices (fakescan.safety)"
        )

    def __reduce__(self):  # keeps pickling (pytest-xdist) free of any other state
        return (type(self), (self.action, self.reason))


# --------------------------------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------------------------------
_UUID_RE = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})(?![0-9a-f])"
)
_URL_RE = re.compile(r"[a-z][a-z0-9+.\-]*://([^/\s?#\\]*)", re.IGNORECASE)


def uuids_in(text: object) -> list[str]:
    """Every UUID found in ``text`` (any of the shapes ``{...}\\0000``, ``urn:uuid:...``, ``uuid:...``,
    ``WSD-...``, a URL containing it, 32 hex digits without dashes), lowercase, dashed, in order."""
    if not isinstance(text, str):
        return []
    return ["-".join(m.groups()) for m in _UUID_RE.finditer(text.lower())]


def normalise_uuid(text: object) -> str:
    """The first UUID in ``text`` in canonical form (lowercase, dashed, no braces, no ``urn:uuid:`` / ``uuid:``
    prefix); when there is none, ``text`` lowercased and stripped of braces / prefixes (``""`` for a non-string)."""
    if not isinstance(text, str):
        return ""
    found = uuids_in(text)
    if found:
        return found[0]
    v = text.strip().lower()
    for pre in ("urn:uuid:", "uuid:"):
        if v.startswith(pre):
            v = v[len(pre):]
            break
    return v.strip("{}").strip()


def _is_fake_uuid(u: str) -> bool:
    return u.startswith(FAKE_UUID_PREFIX)


def _squash(text: object) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text).lower())


def _hosts(text: str) -> list[str]:
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        h = m.group(1).rsplit("@", 1)[-1]
        if h.startswith("["):
            h = h[1:h.find("]")] if "]" in h else h[1:]
        elif h.count(":") == 1:
            h = h.split(":", 1)[0]
        out.append(h.strip().lower())
    return out


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------------------------------
# reference fields
# --------------------------------------------------------------------------------------------------
# P: fields that carry the device's own identity (uuid, WSD port string, endpoints); I: ids that may be a local
# handle (a WIA id is a random guid: no proof, no contradiction); N: names (deny matching only).
_P_ATTRS = ("uuid", "device_uuid", "port", "url", "uri", "endpoint", "service_endpoint", "device_endpoint",
            "ipp_endpoint", "scan_endpoint", "address", "wsd_url")
_I_ATTRS = ("id", "wia_id", "craft_id", "device_id", "printer_id", "printer", "device", "queue", "path")
_N_ATTRS = ("name", "friendly_name", "display_name", "model", "description", "manufacturer", "make_and_model")


def _get(ref: Any, attr: str) -> Any:
    if isinstance(ref, Mapping):
        return ref.get(attr)
    try:
        return getattr(ref, attr, None)
    except Exception:  # noqa: BLE001 - a property that raises is "not there"
        return None


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple, set, frozenset)):
        return [s for x in v for s in _strs(x)]
    return []


def _fields(ref: Any) -> tuple[list[str], list[str], list[str]]:
    """``(p, i, n)`` field texts of a reference. A bare string counts as ``p`` (strictest)."""
    if isinstance(ref, str):
        return ([ref] if ref.strip() else []), [], []
    p = [s for a in _P_ATTRS for s in _strs(_get(ref, a))]
    i = [s for a in _I_ATTRS for s in _strs(_get(ref, a))]
    n = [s for a in _N_ATTRS for s in _strs(_get(ref, a))]
    return p, i, n


@dataclasses.dataclass(frozen=True)
class Verdict:
    """The policy's answer for one reference. ``reason`` is a short code, never device text."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclasses.dataclass(frozen=True)
class Marked:
    """A reference with its verdict (what ``include_excluded=True`` listings return)."""

    ref: Any
    excluded: bool
    reason: str


@dataclasses.dataclass(frozen=True)
class MarkedListing:
    """A listing with the excluded devices kept and marked. ``items`` has every device as :class:`Marked`."""

    items: tuple[Marked, ...]
    errors: tuple[Any, ...] = ()

    @property
    def allowed(self) -> tuple[Any, ...]:
        return tuple(m.ref for m in self.items if not m.excluded)

    @property
    def excluded(self) -> tuple[Any, ...]:
        return tuple(m.ref for m in self.items if m.excluded)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


# --------------------------------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------------------------------
def _split_patterns(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,;]", text) if t.strip()]


def _parse_policy(text: str) -> tuple[str, list[str]]:
    mode, deny, in_deny = "fakes-only", [], False
    for tok in _split_patterns(text):
        low = tok.lower()
        if low == "fakes-only":
            continue
        if low == "deny-only":
            mode = "deny-only"
        elif low.startswith("deny="):
            in_deny = True
            if tok[5:].strip():
                deny.append(tok[5:].strip())
        elif in_deny:
            deny.append(tok)
        else:
            raise PolicyError(f"{POLICY_ENV}: unknown token (expected fakes-only, deny-only or deny=<patterns>)")
    return mode, deny


class _Auto:
    def __repr__(self) -> str:
        return "AUTO"


AUTO = _Auto()


def _default_queue_resolver(queue: str) -> str | None:
    """Windows spooler queue -> WSD device UUID, through fakeprint.winprint (read-only registry / spooler reads,
    the original unwrapped functions). ``None`` when it cannot be proven (not Windows, not installed, not a WSD
    port, any error)."""
    try:
        if sys.platform != "win32":
            return None
        wp = importlib.import_module("fakeprint.winprint")
        lister = getattr(wp.list_printers, "__wrapped_original__", wp.list_printers)
        port_uuid = getattr(wp.wsd_port_uuid, "__wrapped_original__", wp.wsd_port_uuid)
        for q in lister():
            if q.name == queue:
                return port_uuid(q.port)
    except Exception:  # noqa: BLE001
        return None
    return None


class Policy:
    """Which devices the harness may touch. See the module docstring."""

    def __init__(
        self,
        *,
        mode: str = "fakes-only",
        deny: Iterable[str] = (),
        extra: Iterable[Any] = (),
        loopback_is_fake: bool = True,
        queue_resolver: Callable[[str], str | None] | None | _Auto = AUTO,
        allow_names: Iterable[str] = (),
    ) -> None:
        if mode not in ("fakes-only", "deny-only"):
            raise PolicyError("mode must be 'fakes-only' or 'deny-only'")
        self.mode = mode
        self.deny = tuple(p for p in (str(d).strip() for d in deny) if _squash(p))
        self._deny_sq = tuple(_squash(p) for p in self.deny)
        self.loopback_is_fake = loopback_is_fake
        self.queue_resolver = queue_resolver
        self.allow_names = tuple(_squash(n) for n in allow_names if _squash(n))
        self._known: set[str] = set()
        self._lock = threading.Lock()
        for e in extra:
            self.remember_text(e)

    # -- construction -------------------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> "Policy":
        env = os.environ if env is None else env
        mode, deny = _parse_policy(env.get(POLICY_ENV, ""))
        for name in LEGACY_DENY_ENVS:
            deny.extend(_split_patterns(env.get(name, "")))
        return cls(mode=mode, deny=deny, **kw)

    def with_allowed_names(self, *names: str) -> "Policy":
        """A copy that also lets devices named exactly ``names`` through (the ``real_device_ok`` marker). Deny
        patterns still win."""
        p = Policy(mode=self.mode, deny=self.deny, loopback_is_fake=self.loopback_is_fake,
                   queue_resolver=self.queue_resolver, allow_names=(*self.allow_names, *names))
        p._known = set(self._known)
        return p

    # -- known (proven) ids ---------------------------------------------------------------------------
    @staticmethod
    def _known_keys(text: str) -> set[str]:
        keys = {_squash(text)}
        m = re.match(r"^[a-z][a-z0-9]*:(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
        if m and "://" not in text.split(":", 1)[0]:
            keys.add(_squash(m.group(1)))
        keys.discard("")
        return keys

    def remember_text(self, text: Any) -> None:
        """Trust this exact id/text from now on (use only for something already proven or deliberately named)."""
        if isinstance(text, str) and text.strip():
            with self._lock:
                self._known |= self._known_keys(text)

    def remember(self, ref: Any) -> bool:
        """Remember every identity text of ``ref`` when ``ref`` is allowed (a proof through its port / uuid makes
        its bare WIA id usable later). Returns whether it was allowed; a refused ref is never remembered."""
        if not self.evaluate(ref).allowed:
            return False
        p, i, _ = _fields(ref)
        for t in (*i, *p):
            self.remember_text(t)
        return True

    def _is_known(self, text: str) -> bool:
        keys = self._known_keys(text)
        with self._lock:
            return any(k in self._known for k in keys)

    # -- decisions -----------------------------------------------------------------------------------
    def _resolve_queue(self, queue: str) -> str | None:
        r = self.queue_resolver
        if r is AUTO:
            r = _default_queue_resolver
        if r is None:
            return None
        try:
            return r(queue)
        except Exception:  # noqa: BLE001 - cannot prove it: excluded
            return None

    def can_resolve_queues(self) -> bool:
        if self.queue_resolver is AUTO:
            if sys.platform != "win32":
                return False
            try:
                importlib.import_module("fakeprint.winprint")
                return True
            except Exception:  # noqa: BLE001
                return False
        return self.queue_resolver is not None

    def evaluate(self, ref: Any) -> Verdict:
        """The verdict for ``ref``; never raises, never returns device text."""
        try:
            return self._evaluate(ref)
        except Exception:  # noqa: BLE001 - fail closed on anything unforeseen
            return Verdict(False, "not-provably-fake")

    def _evaluate(self, ref: Any) -> Verdict:
        p, i, n = _fields(ref)
        fields = [*p, *i, *n]
        if not fields:
            return Verdict(False, "no-device")
        sq = [_squash(f) for f in fields]
        for pat in self._deny_sq:
            if any(pat in s for s in sq):
                return Verdict(False, "denied")
        if self.mode == "deny-only":
            return Verdict(True, "deny-only")
        ident = [*p, *i]
        # spooler / cups queues have no uuid in their name: the registry must prove them
        queues = [t for t in ident if re.match(r"^(spooler|cups):", t.strip(), re.IGNORECASE)]
        if queues:
            for q in queues:
                u = self._resolve_queue(q.split(":", 1)[1])
                if u is None:
                    return Verdict(False, "unresolved")
                found = uuids_in(u)
                if not found or not all(_is_fake_uuid(x) for x in found):
                    return Verdict(False, "not-provably-fake")
            return Verdict(True, "fake-queue")
        if isinstance(ref, str) and self._is_known(ref):  # a bare id proven earlier (a WIA id is a random guid)
            return Verdict(True, "known")
        proof = False
        for f in p:  # device-identity fields: a real uuid contradicts, a fake one proves
            us = uuids_in(f)
            if any(not _is_fake_uuid(u) for u in us):
                return Verdict(False, "not-provably-fake")
            proof = proof or bool(us)
        if not isinstance(ref, str) and any(self._is_known(t) for t in i):
            return Verdict(True, "known")
        for f in i:  # id-like fields: a fake uuid proves, a plain guid is a local handle (neutral)
            proof = proof or any(_is_fake_uuid(u) for u in uuids_in(f))
        hosts: list[tuple[str, bool]] = []
        for f in ident:
            has_fake = any(_is_fake_uuid(u) for u in uuids_in(f))
            for h in _hosts(f):
                if not _is_loopback_host(h) and not has_fake:
                    return Verdict(False, "not-provably-fake")
                hosts.append((h, _is_loopback_host(h)))
        if self.loopback_is_fake and hosts and all(lo for _, lo in hosts):
            proof = True
        if proof:
            return Verdict(True, "fake")
        if self.allow_names and any(s in self.allow_names for s in (_squash(x) for x in n)):
            return Verdict(True, "named")
        return Verdict(False, "not-provably-fake")

    def allowed(self, ref: Any) -> bool:
        return self.evaluate(ref).allowed

    def assert_allowed(self, ref: Any, action: str = "use") -> None:
        v = self.evaluate(ref)
        if not v.allowed:
            raise DeviceNotAllowed(action, v.reason)

    assert_fake = assert_allowed

    # -- listings --------------------------------------------------------------------------------------
    def filter_refs(self, refs: Any, include_excluded: bool = False) -> Any:
        """Only the allowed refs of ``refs`` (a list, a dict with ``scanners`` / ``items``, or a bindings
        ``Listing``). ``include_excluded=True`` returns a :class:`MarkedListing` with every ref marked."""
        items, errors, rebuild = _unpack_listing(refs)
        marks = []
        for r in items:
            v = self.evaluate(r)
            marks.append(Marked(r, not v.allowed, v.reason))
        if include_excluded:
            return MarkedListing(tuple(marks), tuple(errors))
        return rebuild([m.ref for m in marks if not m.excluded])

    def self_check(self, targets: Iterable[Any]) -> None:
        """Fail (``DeviceNotAllowed``) when any target is not allowed: run it on the devices a session is about
        to use, so a non-fake (or a deny-listed marker) stops the whole session before any test runs."""
        for t in targets:
            self.assert_allowed(t, "session self-check")


def _unpack_listing(refs: Any):
    if isinstance(refs, Mapping):
        key = "scanners" if "scanners" in refs else "items"
        items = list(refs.get(key, ()))
        return items, list(refs.get("errors", ())), lambda kept: {**refs, key: kept}
    if dataclasses.is_dataclass(refs) and hasattr(refs, "items") and not callable(refs.items):
        return list(refs.items), list(getattr(refs, "errors", ())), lambda kept: dataclasses.replace(refs, items=tuple(kept))
    items = list(refs)
    return items, [], lambda kept: list(kept)


# --------------------------------------------------------------------------------------------------
# active policy
# --------------------------------------------------------------------------------------------------
_active: Policy | None = None
_active_lock = threading.Lock()


def current_policy() -> Policy:
    """The active policy (the one :func:`set_policy` / the pytest plugin set); otherwise one built from the
    environment, which is ``fakes-only`` unless the environment says more."""
    global _active
    with _active_lock:
        if _active is None:
            _active = Policy.from_env()
        return _active


def set_policy(policy: Policy | None) -> Policy | None:
    """Make ``policy`` the active one (``None``: rebuild from the environment next time). Returns the old one."""
    global _active
    with _active_lock:
        old, _active = _active, policy
        return old


@contextlib.contextmanager
def use_policy(policy: Policy):
    old = set_policy(policy)
    try:
        yield policy
    finally:
        set_policy(old)


def is_fake(ref: Any, *, extra: Iterable[Any] = (), policy: Policy | None = None) -> bool:
    """Whether ``ref`` is provably fake (module-level convenience over the active policy; ``extra`` are ids the
    caller has proven)."""
    base = policy or current_policy()
    extra = tuple(extra)
    if not extra:
        return base.allowed(ref)
    pol = Policy(mode=base.mode, deny=base.deny, extra=extra, loopback_is_fake=base.loopback_is_fake,
                 queue_resolver=base.queue_resolver, allow_names=base.allow_names)
    with base._lock:
        pol._known |= base._known
    return pol.allowed(ref)


def allowed(ref: Any, policy: Policy | None = None) -> bool:
    return (policy or current_policy()).allowed(ref)


def assert_fake(ref: Any, action: str = "use", policy: Policy | None = None) -> None:
    (policy or current_policy()).assert_allowed(ref, action)


def filter_refs(refs: Any, include_excluded: bool = False, policy: Policy | None = None) -> Any:
    return (policy or current_policy()).filter_refs(refs, include_excluded)


def self_check(targets: Iterable[Any], policy: Policy | None = None) -> None:
    (policy or current_policy()).self_check(targets)


# --------------------------------------------------------------------------------------------------
# guards for your own code
# --------------------------------------------------------------------------------------------------
def _path(bound: Mapping[str, Any], path: str) -> Any:
    """``a.b.c`` through arguments, attributes and mappings; ``_MISSING`` when any step is absent."""
    cur: Any = bound
    for step in path.split("."):
        if isinstance(cur, Mapping):
            if step not in cur:
                return _MISSING
            cur = cur[step]
        else:
            try:
                cur = getattr(cur, step)
            except Exception:  # noqa: BLE001
                return _MISSING
    return cur


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


_MISSING = _Missing()


def _flat(v: Any, as_text: bool) -> list[Any]:
    if v is _MISSING or v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        return [x for e in v for x in _flat(e, as_text)]
    if hasattr(v, "printers") and not isinstance(v, (str, Mapping)):  # an IppBackend configuration
        return _flat(tuple(v.printers), as_text)
    if as_text and not isinstance(v, (str, Mapping)):
        return [str(v)]
    return [v]


def _check_targets(policy: Policy, bound: Mapping[str, Any], targets: tuple[str, ...], action: str,
                   *, as_text: bool = False, any_required: bool = False) -> None:
    refs: list[Any] = []
    for t in targets:
        found = _flat(_path(bound, t), as_text)
        if not found and not any_required:
            raise DeviceNotAllowed(action, "no-device")  # "the default device" is never provably fake
        refs.extend(found)
    if not refs:
        raise DeviceNotAllowed(action, "no-device")
    for r in refs:
        policy.assert_allowed(r, action)


def guard_open(*targets: str, action: str = "open", policy: Policy | None = None, as_text: bool = False,
               any_required: bool = False, exempt: tuple[str, ...] = ()) -> Callable[[Callable], Callable]:
    """Decorator: before the wrapped function runs, every ``targets`` argument (a parameter name or a dotted path
    such as ``settings.device``; the first parameter when none is given) must be an allowed device, else
    :class:`DeviceNotAllowed` is raised and the function is NOT called. A missing / empty target is refused.
    ``exempt`` names arguments whose presence skips the check (a scripted in-memory registry)."""

    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        names = targets or (next(iter(sig.parameters)),)

        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any):
            bound = _bind(sig, a, k)
            if not any(_path(bound, e) not in (_MISSING, None) for e in exempt):
                _check_targets(policy or current_policy(), bound, names, action, as_text=as_text, any_required=any_required)
            return fn(*a, **k)

        wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
        return wrapper

    return deco


def guard_submit(*targets: str, policy: Policy | None = None, **kw: Any) -> Callable[[Callable], Callable]:
    """:func:`guard_open` for a call that sends a job to a device (``action='submit'``)."""
    return guard_open(*targets, action="submit", policy=policy, **kw)


@contextlib.contextmanager
def guard(*refs: Any, action: str = "open", policy: Policy | None = None):
    """``with guard(ref): ...``: raises before the block when a ref is excluded."""
    pol = policy or current_policy()
    if not refs:
        raise DeviceNotAllowed(action, "no-device")
    for r in refs:
        pol.assert_allowed(r, action)
    yield pol


def _bind(sig: inspect.Signature, a: tuple, k: dict) -> dict[str, Any]:
    try:
        b = sig.bind(*a, **k)
    except TypeError:
        raise  # let the real function's own TypeError message surface unchanged
    return dict(b.arguments)


# --------------------------------------------------------------------------------------------------
# the guard table: every entry point of the bindings / harness libraries that opens, addresses or prints
# --------------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Sink:
    module: str
    owner: str | None  # class name, or None for a module-level function
    name: str
    action: str
    targets: tuple[str, ...] = ()
    exempt: tuple[str, ...] = ()  # arguments whose presence means "in-memory test registry, no device"
    as_text: bool = False
    any_required: bool = False
    aliases: tuple[str, ...] = ()
    custom: str | None = None  # a hand-written wrapper (see _CUSTOM)
    remember_result: bool = False  # remember the returned job handle (it belongs to a vetted printer)


_S = Sink
SINKS: tuple[Sink, ...] = (
    # --- pdfcraft.wsdprint (WS-Print client over the Rust network layer): the URL is the device
    _S("pdfcraft.wsdprint", None, "printer_info", "open", ("url",)),
    _S("pdfcraft.wsdprint", None, "print_document", "submit", ("url",)),
    _S("pdfcraft.wsdprint", None, "list_jobs", "open", ("url",)),
    _S("pdfcraft.wsdprint", None, "job_elements", "open", ("url",)),
    _S("pdfcraft.wsdprint", None, "cancel_job", "open", ("url",)),
    # --- pdfcraft.printing
    _S("pdfcraft.printing", "Registry", "register_wsd", "register", ("urls",)),
    _S("pdfcraft.printing", "Registry", "register_ipp", "register", ("urls",)),
    _S("pdfcraft.printing", "Registry", "add_wsd_printer", "register", ("url",)),
    _S("pdfcraft.printing", "Registry", "add_ipp_printer", "register", ("uri",)),
    _S("pdfcraft.printing", "Registry", "register_system_spooler", "register", custom="spooler"),
    _S("pdfcraft.printing", "Registry", "list_printers", "list", custom="list_printers"),
    _S("pdfcraft.printing", "Registry", "printer_details", "open", ("printer_id",)),
    _S("pdfcraft.printing", "Registry", "submit", "submit", ("printer", "job.printer"), any_required=True,
       aliases=("submit_job",), remember_result=True),
    # job_status / cancel_job take a job handle of a printer that was registered (and so checked) already; the system
    # spooler gives no handles. They are PURE below: refusing a handle would only break "unknown job" error paths.
    _S("pdfcraft.printing", "Registry", "list_jobs", "open", ("printer_id",)),
    _S("pdfcraft.printing", "Registry", "find_job", "open", ("printer_id",)),
    _S("pdfcraft.printing", "PrintJob", "__init__", "submit", ("job.printer",)),
    # --- pdfcraft.scan (flat API) and pdfcraft.scanning (typed API): the device is the scanner
    _S("pdfcraft.scan", None, "list_scanners", "list", custom="list_scanners"),
    _S("pdfcraft.scan", None, "scanners_with_errors", "list", custom="scanners_with_errors"),
    _S("pdfcraft.scan", None, "scanner_status", "open", ("id",)),
    _S("pdfcraft.scan", None, "scanner_details", "open", ("id",)),
    _S("pdfcraft.scan", None, "scanner_dump", "open", custom="dump"),
    _S("pdfcraft.scan", None, "acquire", "scan", ("device",)),
    _S("pdfcraft.scan", None, "acquire_pages", "scan", ("device",)),
    _S("pdfcraft.scan", None, "start", "scan", ("device",)),
    _S("pdfcraft.scan", None, "ScanJob", "scan", custom="native_ctor"),
    _S("pdfcraft.scanning", None, "list_scanners", "list", custom="list_scanners"),
    _S("pdfcraft.scanning", None, "scanner_status", "open", ("id",)),
    _S("pdfcraft.scanning", None, "scanner_details", "open", ("id",)),
    _S("pdfcraft.scanning", None, "diagnostic_dump", "open", custom="dump"),
    _S("pdfcraft.scanning", None, "device_view", "open", ("id",), exempt=("_registry",)),
    _S("pdfcraft.scanning", None, "acquire", "scan", ("settings.device",), exempt=("job_kw.registry",)),
    _S("pdfcraft.scanning", None, "run_job", "scan", ("job.settings.device",), exempt=("registry", "job._registry")),
    _S("pdfcraft.scanning", "AttemptPlan", "acquire", "scan", ("self.settings.device",), exempt=("registry",)),
    _S("pdfcraft.scanning", "AttemptPlan", "start", "scan", ("self.settings.device",), exempt=("registry",)),
    _S("pdfcraft.scanning", "RunningAttempt", "__init__", "scan", ("plan.settings.device",), exempt=("registry",)),
    _S("pdfcraft.scanning", "ScanJob", "start_attempt", "scan", ("plan.settings.device",), exempt=("self._registry",)),
    # --- fakescan's WSD scan client (it can be pointed at any address): the base url is the device
    _S("fakescan.soap", None, "create_scan_job", "scan", ("base_url",)),
    _S("fakescan.soap", None, "get_scanner_elements", "open", ("base_url",)),
    _S("fakescan.soap", None, "retrieve_image_bytes", "scan", ("base_url",)),
    _S("fakescan.soap", None, "acquire_pages", "scan", ("base_url",)),
    _S("fakescan.soap", None, "acquire_jfif_pages", "scan", ("base_url",)),
    _S("fakescan.soap", None, "acquire_dib_platen", "scan", ("base_url",)),
    # --- fakeprint (the harness' own clients)
    _S("fakeprint.soap", "PrintClient", "__init__", "open", ("endpoint",)),
    _S("fakeprint.winprint", None, "print_raw", "submit", custom="winprint"),
    _S("fakeprint.winprint", None, "print_image", "submit", custom="winprint"),
    _S("fakeprint.winprint", None, "job_status", "open", custom="winprint"),
    _S("fakeprint.winprint", None, "wait_for_job", "open", custom="winprint"),
    _S("fakeprint.winprint", None, "list_printers", "list", custom="winprint_list"),
)

#: Public names of the guarded libraries that need no guard (pure data, local state, or already behind a guarded
#: call), as ``module[.Class].name``. The harness' coverage test requires every public callable of the guarded
#: modules to be in :data:`GUARDED` or here, so a new entry point cannot appear unreviewed.
PURE: frozenset[str] = frozenset("""
pdfcraft.wsdprint.*
pdfcraft.printing.choose_resolution pdfcraft.printing.capability_view pdfcraft.printing.ipp_job_id
pdfcraft.printing.parse_ipp_job_id
pdfcraft.printing.Registry.__init__ pdfcraft.printing.Registry.job_status pdfcraft.printing.Registry.cancel_job
pdfcraft.printing.Registry.backend_info pdfcraft.printing.Registry.backend_names pdfcraft.printing.Registry.clear_converter
pdfcraft.printing.Registry.events_status pdfcraft.printing.Registry.has_converter pdfcraft.printing.Registry.ipp_version_of
pdfcraft.printing.Registry.maintain_events pdfcraft.printing.Registry.set_converter pdfcraft.printing.Registry.shutdown_events
pdfcraft.printing.PrintJob.*
pdfcraft.scan.backend_names pdfcraft.scan.create_document pdfcraft.scan.interrupt_kind pdfcraft.scan.pages_to_pdf
pdfcraft.scan.scan_supported
pdfcraft.scanning.backend_names pdfcraft.scanning.error_from_failure pdfcraft.scanning.supported
pdfcraft.scanning.ScanJob.*
pdfcraft.scanning.AttemptPlan.*
pdfcraft.scanning.RunningAttempt.*
fakeprint.winprint.describe_printers fakeprint.winprint.wsd_port_uuid
fakeprint.soap.PrintClient.*
fakescan.soap.get_scanner_status fakescan.soap.format_magic_ok fakeprint.soap.parse_fault
""".split())
# ``Class.*`` / ``module.*`` entries are prefixes: the sinks listed in SINKS take precedence over them.

GUARDED: frozenset[str] = frozenset(
    ".".join(x for x in (s.module, s.owner, s.name) if x) for s in SINKS
) | frozenset(f"{s.module}.{s.owner}.{a}" for s in SINKS for a in s.aliases)


def is_covered(qualified: str) -> bool:
    """Whether ``module[.Class].name`` is in :data:`GUARDED` or :data:`PURE` (exactly or through a ``.*`` prefix)."""
    if qualified in GUARDED or qualified in PURE:
        return True
    parts = qualified.split(".")
    return any(".".join(parts[:n]) + ".*" in PURE for n in range(1, len(parts)))


# --------------------------------------------------------------------------------------------------
# wrappers
# --------------------------------------------------------------------------------------------------
PolicyGetter = Callable[[], Policy]


def _simple(fn: Callable, sink: Sink, getp: PolicyGetter) -> Callable:
    sig = inspect.signature(fn)
    for t in (*sink.targets, *sink.exempt):
        root = t.split(".")[0]
        if root not in sig.parameters and not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            raise SafetyConfigError(f"{sink.module}.{sink.name} has no parameter {root!r}: the safety table is stale")

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any):
        bound = _bind(sig, a, k)
        if not any(_path(bound, e) not in (_MISSING, None) for e in sink.exempt):
            _check_targets(getp(), bound, sink.targets, sink.action, as_text=sink.as_text, any_required=sink.any_required)
        out = fn(*a, **k)
        if sink.remember_result:
            job = getattr(out, "job", None)
            if job is not None:
                getp().remember_text(str(job))
        return out

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = fn  # type: ignore[attr-defined]
    return wrapper


def _row_text_match(row: Any, device: str | None) -> bool:
    if not device:
        return True
    want = _squash(device)
    p, i, n = _fields(row)
    return any(want in _squash(f) for f in (*p, *i, *n))


def _wrap_list_scanners(orig: Callable, getp: PolicyGetter, *, tuple_form: bool = False) -> Callable:
    """Listing wrapper (flat dict API and typed API). The enumeration itself opens nothing (names only); devices
    are then opened one by one through ``orig`` only when allowed, so ``details=True`` / ``device=...`` can never
    open an excluded device."""

    @functools.wraps(orig)
    def wrapper(*a: Any, include_excluded: bool = False, device: str | None = None, details: bool = False, **k: Any):
        pol = getp()
        if a:
            raise TypeError("list_scanners takes keyword arguments only")
        base = orig(device=None, details=False, **k)  # reads the device list; nothing is opened
        pay_rows, errors, rebuild = _unpack_listing(base if not tuple_form else {"scanners": base[0], "errors": base[1]})
        marks = []
        for r in pay_rows:
            v = pol.evaluate(r)
            marks.append(Marked(r, not v.allowed, v.reason))
        if device is not None and not _squash(device):
            raise ValueError("an empty device filter is refused")
        opener = _opener_for(orig)
        kept: list[Any] = []
        for m in marks:
            if m.excluded or not _row_text_match(m.ref, device):
                continue
            pol.remember(m.ref)  # proven through its port / uuid: its bare WIA id is usable from now on
            kept.append(opener(m.ref) if (details or device) else m.ref)
        if include_excluded:
            full = [Marked(x, False, "ok") for x in kept] + [m for m in marks if m.excluded]
            return MarkedListing(tuple(full), tuple(errors))
        out = rebuild(kept)
        return (list(out["scanners"]), list(out.get("errors", ()))) if tuple_form else out

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = orig  # type: ignore[attr-defined]
    return wrapper


def _opener_for(listing_fn: Callable) -> Callable[[Any], Any]:
    """The single-device opener that matches the lister's row type (typed rows or dicts)."""
    mod = importlib.import_module(listing_fn.__module__)
    details = getattr(mod, "scanner_details")
    orig = getattr(details, "__wrapped_original__", details)

    def open_one(row: Any) -> Any:
        rid = row["id"] if isinstance(row, Mapping) else row.id
        return orig(rid)

    return open_one


def _wrap_dump(orig: Callable, getp: PolicyGetter) -> Callable:
    """``scanner_dump(filter)`` opens every device the substring matches: refuse unless the filter is non-empty,
    selects at least one device, and every device it selects is allowed."""
    mod = importlib.import_module(orig.__module__)
    lister = getattr(mod, "list_scanners", None) or importlib.import_module("pdfcraft.scan").list_scanners
    lister = getattr(lister, "__wrapped_original__", lister)

    @functools.wraps(orig)
    def wrapper(filter: str):  # noqa: A002
        pol = getp()
        if not isinstance(filter, str) or not _squash(filter):
            raise DeviceNotAllowed("open", "no-device")
        base = lister(device=None, details=False)
        rows, _, _ = _unpack_listing(base)
        matched = [r for r in rows if _row_text_match(r, filter)]
        if not matched:
            raise DeviceNotAllowed("open", "no-device")
        for r in matched:
            pol.assert_allowed(r, "open")
        return orig(filter)

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = orig  # type: ignore[attr-defined]
    return wrapper


class _NativeCtor:
    """Stands in for the (non-subclassable) native ``ScanJob``: checks ``device`` first, then delegates;
    ``isinstance(x, ScanJob)`` keeps working."""

    def __init__(self, native: Any, getp: PolicyGetter) -> None:
        self._native, self._getp = native, getp
        self.__fakescan_safety__ = True
        self.__wrapped_original__ = native

    def __call__(self, *a: Any, **k: Any):
        dev = k.get("device", a[0] if a else "")
        _check_targets(self._getp(), {"device": dev}, ("device",), "scan")
        return self._native(*a, **k)

    def __instancecheck__(self, inst: Any) -> bool:
        return isinstance(inst, self._native)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native, name)


def _wrap_list_printers(orig: Callable, getp: PolicyGetter) -> Callable:
    @functools.wraps(orig)
    def wrapper(self: Any, *a: Any, include_excluded: bool = False, **k: Any):
        pol = getp()
        listing = orig(self, *a, **k)
        out = pol.filter_refs(listing, include_excluded)
        if not include_excluded:
            for r in _unpack_listing(out)[0]:
                pol.remember(r)
        return out

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = orig  # type: ignore[attr-defined]
    return wrapper


def _wrap_spooler(orig: Callable, getp: PolicyGetter) -> Callable:
    @functools.wraps(orig)
    def wrapper(self: Any, *a: Any, **k: Any):
        if not getp().can_resolve_queues():  # no way to prove a queue is fake here: do not even register it
            raise DeviceNotAllowed("register", "unresolved")
        return orig(self, *a, **k)

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = orig  # type: ignore[attr-defined]
    return wrapper


def _winprint_ref(mod: Any, printer: Any) -> dict[str, Any]:
    """The queue's identity from the registry (its WSD device UUID), via the original (unwrapped) functions."""
    ident: dict[str, Any] = {"name": printer if isinstance(printer, str) else ""}
    lister = getattr(mod.list_printers, "__wrapped_original__", mod.list_printers)
    port_uuid = getattr(mod.wsd_port_uuid, "__wrapped_original__", mod.wsd_port_uuid)
    try:
        for q in lister():
            if q.name == printer:
                ident["port"] = q.port
                ident["uuid"] = port_uuid(q.port)
                break
    except Exception:  # noqa: BLE001 - cannot prove: no uuid, so excluded
        pass
    if not ident.get("uuid"):
        ident["uuid"] = None
    return ident


def queue_ref(name: Any, module: Any = None) -> dict[str, Any]:
    """The policy reference of the Windows queue called exactly ``name``: ``{"name", "uuid"}`` where ``uuid`` is the WSD
    device UUID Windows recorded for the queue's port (read-only registry / spooler reads through
    ``fakeprint.winprint``'s original functions), or ``None`` when it cannot be read (not Windows, not WSD, any
    error). The queue's name proves nothing; the port string (``WSD-<random guid>``) is not the device uuid."""
    try:
        mod = module or importlib.import_module("fakeprint.winprint")
    except Exception:  # noqa: BLE001
        return {"name": name if isinstance(name, str) else "", "uuid": None}
    return _winprint_ref(mod, name)


def assert_fake_queue(name: Any, action: str = "open", policy: Policy | None = None, module: Any = None) -> None:
    """Raise :class:`DeviceNotAllowed` unless the Windows queue ``name`` is bound to a fake device (its WSD port UUID is
    in the fake namespace and no deny pattern matches). For harness code that talks to a queue through ctypes
    (``OpenPrinter``, ``CreateDC``, ``DeviceCapabilities``) instead of ``fakeprint.winprint``."""
    if not isinstance(name, str) or not name.strip():
        raise DeviceNotAllowed(action, "no-device")
    ref = queue_ref(name, module)
    (policy or current_policy()).assert_allowed({"uuid": ref["uuid"], "name": ref["name"]} if ref["uuid"] else {"name": ref["name"]}, action)


def _wrap_winprint(orig: Callable, getp: PolicyGetter, sink: Sink) -> Callable:
    @functools.wraps(orig)
    def wrapper(printer: Any = None, *a: Any, **k: Any):
        if not isinstance(printer, str) or not printer.strip():
            raise DeviceNotAllowed(sink.action, "no-device")
        mod = importlib.import_module(orig.__module__)
        ref = _winprint_ref(mod, printer)
        pol = getp()
        # the port string is "WSD-<random guid>" (not the device uuid): the proof is the registry uuid
        pol.assert_allowed({"uuid": ref["uuid"], "name": ref["name"]} if ref["uuid"] else {"name": ref["name"]}, sink.action)
        return orig(printer, *a, **k)

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = orig  # type: ignore[attr-defined]
    return wrapper


def _wrap_winprint_list(orig: Callable, getp: PolicyGetter) -> Callable:
    @functools.wraps(orig)
    def wrapper(*a: Any, include_excluded: bool = False, **k: Any):
        pol = getp()
        mod = importlib.import_module(orig.__module__)
        port_uuid = getattr(mod.wsd_port_uuid, "__wrapped_original__", mod.wsd_port_uuid)
        rows = list(orig(*a, **k))
        marks = []
        for q in rows:
            try:
                u = port_uuid(q.port)
            except Exception:  # noqa: BLE001
                u = None
            v = pol.evaluate({"uuid": u, "name": q.name}) if u else Verdict(False, "unresolved")
            if v.allowed and any(_squash(d) in _squash(q.name + str(q.port)) for d in pol.deny):
                v = Verdict(False, "denied")
            marks.append(Marked(q, not v.allowed, v.reason))
        if include_excluded:
            return MarkedListing(tuple(marks))
        return [m.ref for m in marks if not m.excluded]

    wrapper.__fakescan_safety__ = True  # type: ignore[attr-defined]
    wrapper.__wrapped_original__ = orig  # type: ignore[attr-defined]
    return wrapper


def _build(fn: Callable, sink: Sink, getp: PolicyGetter) -> Callable:
    c = sink.custom
    if c is None:
        return _simple(fn, sink, getp)
    if c == "list_scanners":
        return _wrap_list_scanners(fn, getp)
    if c == "scanners_with_errors":
        return _wrap_list_scanners(fn, getp, tuple_form=True)
    if c == "dump":
        return _wrap_dump(fn, getp)
    if c == "native_ctor":
        return _NativeCtor(fn, getp)  # type: ignore[return-value]
    if c == "list_printers":
        return _wrap_list_printers(fn, getp)
    if c == "spooler":
        return _wrap_spooler(fn, getp)
    if c == "winprint":
        return _wrap_winprint(fn, getp, sink)
    if c == "winprint_list":
        return _wrap_winprint_list(fn, getp)
    raise SafetyConfigError(f"unknown custom wrapper {c!r}")


# --------------------------------------------------------------------------------------------------
# install (patch in place) and guarded() proxies
# --------------------------------------------------------------------------------------------------
@dataclasses.dataclass
class Installation:
    """What :func:`install` patched. ``uninstall()`` restores the originals."""

    patched: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)
    _undo: list[tuple[Any, str, Any]] = dataclasses.field(default_factory=list, repr=False)

    def uninstall(self) -> None:
        for holder, name, original in reversed(self._undo):
            setattr(holder, name, original)
        self._undo.clear()
        self.patched.clear()

    def __enter__(self) -> "Installation":
        return self

    def __exit__(self, *exc: object) -> None:
        self.uninstall()


_install_lock = threading.Lock()


def install(policy: Policy | None = None, *, modules: Iterable[str] | None = None, load: bool = True) -> Installation:
    """Replace every guarded entry point (:data:`SINKS`) of the importable libraries with its guarded version,
    in place, so code that calls ``pdfcraft.wsdprint.print_document(...)`` or ``Registry().submit(...)`` is
    guarded without being edited. ``policy`` becomes the active policy; the wrappers read the active policy at
    call time. Idempotent; a library that cannot be imported (``fakeprint.winprint`` off Windows) is skipped
    and listed in ``.skipped``. Code that bound an entry point BEFORE install (``from m import f`` earlier) is not
    reached: install at the very start (a conftest does)."""
    if policy is not None:
        set_policy(policy)
    getp: PolicyGetter = current_policy
    inst = Installation()
    mods = set(modules) if modules is not None else None
    with _install_lock:
        try:
            for s in SINKS:
                if mods is None or s.module in mods:
                    _install_one(s, inst, getp, load)
        except BaseException:
            inst.uninstall()  # a stale table leaves nothing half patched
            raise
    return inst


def _install_one(s: Sink, inst: Installation, getp: PolicyGetter, load: bool) -> None:
    q = ".".join(x for x in (s.module, s.owner, s.name) if x)
    try:
        mod = sys.modules.get(s.module) or (importlib.import_module(s.module) if load else None)
    except Exception:  # noqa: BLE001 - not importable here
        mod = None
    if mod is None:
        inst.skipped.append(q)
        return
    holder = getattr(mod, s.owner) if s.owner else mod
    if not hasattr(holder, s.name):
        raise SafetyConfigError(f"{q} does not exist: the safety table is stale")
    current = holder.__dict__.get(s.name) if s.owner else getattr(mod, s.name)
    if current is None:
        current = getattr(holder, s.name)
    if getattr(current, "__fakescan_safety__", False):
        inst.skipped.append(q + " (already)")
        return
    new = _build(current, s, getp)
    setattr(holder, s.name, new)
    inst._undo.append((holder, s.name, current))
    inst.patched.append(q)
    for alias in s.aliases:
        old = holder.__dict__.get(alias)
        if old is not None and not getattr(old, "__fakescan_safety__", False):
            setattr(holder, alias, new)
            inst._undo.append((holder, alias, old))


def _sinks_for(module: str, owner: str | None) -> dict[str, Sink]:
    out: dict[str, Sink] = {}
    for s in SINKS:
        if s.module == module and s.owner == owner:
            out[s.name] = s
            for a in s.aliases:
                out[a] = s
    return out


class _GuardedObject:
    """Proxy over one bindings object (a ``Registry``, a ``PrintJob``, an ``AttemptPlan`` ...): its guarded
    methods check first; objects they return that are themselves guarded types come back wrapped."""

    def __init__(self, obj: Any, getp: PolicyGetter) -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_getp", getp)
        t = type(obj)
        object.__setattr__(self, "_sinks", _sinks_for(t.__module__, t.__qualname__))

    def __getattr__(self, name: str) -> Any:
        obj, sinks = object.__getattribute__(self, "_obj"), object.__getattribute__(self, "_sinks")
        getp = object.__getattribute__(self, "_getp")
        attr = getattr(obj, name)
        if not callable(attr) or inspect.isclass(attr):
            return attr
        sink = sinks.get(name)
        if sink is not None:
            raw = getattr(type(obj), name, None)
            if raw is not None and not getattr(raw, "__fakescan_safety__", False):
                bound_fn = _build(raw, sink, getp)
                attr = functools.partial(bound_fn, obj)
            # already patched class: the method is guarded by install(), use it as is

        @functools.wraps(attr)
        def call(*a: Any, **k: Any):
            out = attr(*a, **k)
            return _maybe_wrap(out, getp)

        return call

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_obj"), name, value)

    def __repr__(self) -> str:
        return f"guarded({object.__getattribute__(self, '_obj')!r})"


def _maybe_wrap(out: Any, getp: PolicyGetter) -> Any:
    t = type(out)
    if _sinks_for(t.__module__, t.__qualname__):
        return _GuardedObject(out, getp)
    return out


class _GuardedModule:
    def __init__(self, mod: types.ModuleType, getp: PolicyGetter) -> None:
        self._mod, self._getp = mod, getp
        self._sinks = _sinks_for(mod.__name__, None)
        self._cache: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        attr = getattr(self._mod, name)
        sink = self._sinks.get(name)
        if sink is not None:
            raw = getattr(attr, "__wrapped_original__", attr)
            new = attr if getattr(attr, "__fakescan_safety__", False) else _build(raw, sink, self._getp)
            self._cache[name] = new
            return new
        if inspect.isclass(attr) and _sinks_for(self._mod.__name__, attr.__qualname__):
            def factory(*a: Any, __cls: Any = attr, **k: Any):
                return _GuardedObject(__cls(*a, **k), self._getp)

            self._cache[name] = factory
            return factory
        return attr


def guarded(target: Any, policy: Policy | None = None) -> Any:
    """A guarded view of ``target``:

    * a bindings module (``pdfcraft.scanning``, ``pdfcraft.scan``, ``pdfcraft.wsdprint``, ``pdfcraft.printing``,
      ``fakeprint.winprint``): its guarded functions check before calling, listings are filtered (pass
      ``include_excluded=True`` for a :class:`MarkedListing`); classes with guarded methods come back as factories
      of guarded objects;
    * an instance of a guarded type (``Registry``, ``PrintJob``, ``AttemptPlan``, ``PrintClient``): a proxy whose
      guarded methods check first (a ``PrintClient`` is checked once, on its endpoint);
    * anything else: ``TypeError`` (an object that is not known to be guarded is not wrapped silently).

    The active policy is read at call time unless ``policy`` is given."""
    getp: PolicyGetter = (lambda: policy) if policy is not None else current_policy
    if isinstance(target, types.ModuleType):
        return _GuardedModule(target, getp)
    t = type(target)
    sinks = _sinks_for(t.__module__, t.__qualname__)
    if sinks:
        if t.__qualname__ == "PrintClient":
            ep = getattr(target, "endpoint", None) or getattr(target, "_endpoint", None)
            _check_targets(getp(), {"endpoint": ep}, ("endpoint",), "open")
            return target
        return _GuardedObject(target, getp)
    raise TypeError("guarded(): not a module or an object type with guarded entry points")
