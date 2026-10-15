"""Typed, read-only view of the fakescan device registry (``devices.json``).

Python NEVER writes the registry file. All mutation goes through the daemon's HTTP API
(:class:`fakescan.Manager`), so the Rust file lock + atomic writer stays the single writer.

* :func:`load` reads the file directly (read-only; tolerant of a concurrent atomic rewrite).
* :func:`from_server` reads ``GET /v1/registry``.
* :func:`match_wia_to_devices` is the contract's matching rule (UUID in the WIA ``port``).

Parsing is lenient: unknown fields are kept in ``.extra``; legacy rows that only have
``added.pnp`` load as ``installed`` with note ``"legacy added.pnp"``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

REGISTRY_ENV = "FAKESCAN_REGISTRY"
LEGACY_NOTE = "legacy added.pnp"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def registry_path() -> Path:
    """Same rules as ``registry.rs``: ``$FAKESCAN_REGISTRY``, else
    ``%LOCALAPPDATA%\\fakescan\\devices.json`` (Windows) or ``~/.local/share/fakescan/devices.json``."""
    env = os.environ.get(REGISTRY_ENV)
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or "C:\\Users\\Default\\AppData\\Local"
        return Path(base) / "fakescan" / "devices.json"
    home = os.environ.get("HOME") or "/tmp"
    return Path(home) / ".local" / "share" / "fakescan" / "devices.json"


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _split(raw: Mapping[str, Any], known: Iterable[str]) -> dict[str, Any]:
    k = set(known)
    return {key: val for key, val in raw.items() if key not in k}


@dataclass
class Wia:
    """A Windows (WIA) scanner as observed on the Windows side."""

    id: str = ""
    name: str = ""
    port: str = ""

    @classmethod
    def parse(cls, raw: Mapping[str, Any] | "Wia") -> Wia:
        if isinstance(raw, Wia):
            return raw
        return cls(id=str(raw.get("id") or ""), name=str(raw.get("name") or ""), port=str(raw.get("port") or ""))

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "port": self.port}


@dataclass
class Feeder:
    capacity: int | None = None
    media_sizes: list[str] = field(default_factory=list)
    duplex: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Feeder:
        r = _d(raw)
        cap = r.get("capacity")
        return cls(
            capacity=int(cap) if isinstance(cap, (int, float)) else None,
            media_sizes=[str(x) for x in (r.get("media_sizes") or [])],
            duplex=bool(r.get("duplex", False)),
            extra=_split(r, ("capacity", "media_sizes", "duplex")),
        )


@dataclass
class Config:
    """What a device was when last started (``config`` of a registry row)."""

    fingerprint: str = ""
    published_fingerprint: str = ""
    feeder: Feeder = field(default_factory=Feeder)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Config | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            fingerprint=str(raw.get("fingerprint") or ""),
            published_fingerprint=str(raw.get("published_fingerprint") or ""),
            feeder=Feeder.parse(raw.get("feeder")),
            extra=_split(raw, ("fingerprint", "published_fingerprint", "feeder")),
        )


_INSTALL_KEYS = (
    "state", "reason", "note", "first_probed_at", "last_seen_at", "last_peer", "installed_at",
    "verified_at", "wia", "published_fingerprint_at_install",
)


@dataclass
class Install:
    """Install lifecycle: ``new | probed | installed | stale``."""

    state: str = "new"
    reason: str | None = None
    note: str | None = None
    first_probed_at: str | None = None
    last_seen_at: str | None = None
    last_peer: str | None = None
    installed_at: str | None = None
    verified_at: str | None = None
    wia: Wia | None = None
    published_fingerprint_at_install: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Install:
        r = _d(raw)
        wia = r.get("wia")
        return cls(
            state=str(r.get("state") or "new"),
            reason=_s(r.get("reason")),
            note=_s(r.get("note")),
            first_probed_at=_s(r.get("first_probed_at")),
            last_seen_at=_s(r.get("last_seen_at")),
            last_peer=_s(r.get("last_peer")),
            installed_at=_s(r.get("installed_at")),
            verified_at=_s(r.get("verified_at")),
            wia=Wia.parse(wia) if isinstance(wia, dict) else None,
            published_fingerprint_at_install=_s(r.get("published_fingerprint_at_install")),
            extra=_split(r, _INSTALL_KEYS),
        )

    @property
    def installed(self) -> bool:
        return self.state == "installed"

    @property
    def stale(self) -> bool:
        return self.state == "stale"


def _needs_readd(install: Install) -> tuple[bool, str | None]:
    # Mirrors registry.rs: only a previously installed device that went stale needs a re-Add.
    if install.stale:
        return True, install.reason
    return False, None


@dataclass
class DeviceRow:
    """One registry row (one emulated device)."""

    name: str
    listen: str = ""
    advertise: str = ""
    device_uuid: str = ""
    profile: str = "brother-wsd"
    friendly_name: str | None = None
    initial_pages: int | None = None  # the device's own tray preload; None = inherit
    config: Config | None = None
    install: Install = field(default_factory=Install)
    exe: str | None = None
    autostart: bool = False
    harness: Any = None
    added_pnp: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> DeviceRow:
        install = Install.parse(raw.get("install"))
        pnp = bool(_d(raw.get("added")).get("pnp", False))
        if pnp and install.state == "new":  # old single-row format: only the boolean existed
            install.state = "installed"
            install.note = install.note or LEGACY_NOTE
        return cls(
            name=str(raw.get("name") or ""),
            listen=str(raw.get("listen") or ""),
            advertise=str(raw.get("advertise") or ""),
            device_uuid=str(raw.get("device_uuid") or ""),
            profile=str(raw.get("profile") or "brother-wsd"),
            friendly_name=_s(raw.get("friendly_name")),
            initial_pages=(
                int(raw["initial_pages"]) if isinstance(raw.get("initial_pages"), int) else None
            ),
            config=Config.parse(raw.get("config")),
            install=install,
            exe=_s(raw.get("exe")),
            autostart=bool(raw.get("autostart", False)),
            harness=raw.get("harness"),
            added_pnp=pnp,
            extra=_split(
                raw,
                ("name", "listen", "advertise", "device_uuid", "profile", "friendly_name",
                 "initial_pages", "config", "install", "exe", "autostart", "harness", "added"),
            ),
        )

    @property
    def uuid(self) -> str:
        return self.device_uuid

    @property
    def needs_readd(self) -> bool:
        return _needs_readd(self.install)[0]

    @property
    def needs_readd_reason(self) -> str | None:
        return _needs_readd(self.install)[1]


@dataclass
class Registry:
    """The registry document."""

    devices: list[DeviceRow] = field(default_factory=list)
    advertise_default: str | None = None
    port_policy: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Registry:
        r = _d(raw)
        rows = r.get("devices")
        return cls(
            devices=[DeviceRow.parse(x) for x in rows if isinstance(x, dict)] if isinstance(rows, list) else [],
            advertise_default=_s(r.get("advertise_default")),
            port_policy=_d(r.get("port_policy")),
            extra=_split(r, ("devices", "advertise_default", "port_policy")),
        )

    def find(self, name: str) -> DeviceRow | None:
        return next((d for d in self.devices if d.name == name), None)

    def rows_for_listen(self, listen: str) -> list[DeviceRow]:
        return [d for d in self.devices if d.listen == listen]


@dataclass
class ManagedDevice:
    """One element of ``GET /v1/devices`` (a running device plus its registry state)."""

    id: str
    uuid: str
    profile: str
    friendly_name: str = ""
    initial_pages: int = 0  # sheets a reset loads (0 = starts empty)
    scan_endpoint: str | None = None
    device_endpoint: str | None = None
    install: Install | None = None
    config: Config | None = None
    needs_readd: bool = False
    needs_readd_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ManagedDevice:
        inst = raw.get("install")
        return cls(
            id=str(raw.get("id") or ""),
            uuid=str(raw.get("uuid") or ""),
            profile=str(raw.get("profile") or ""),
            friendly_name=str(raw.get("friendly_name") or ""),
            initial_pages=int(raw.get("initial_pages") or 0),
            scan_endpoint=_s(raw.get("scan_endpoint")),
            device_endpoint=_s(raw.get("device_endpoint")),
            install=Install.parse(inst) if isinstance(inst, dict) else None,
            config=Config.parse(raw.get("config")),
            needs_readd=bool(raw.get("needs_readd", False)),
            needs_readd_reason=_s(raw.get("needs_readd_reason")),
            extra=_split(
                raw,
                ("id", "uuid", "profile", "friendly_name", "initial_pages", "scan_endpoint", "device_endpoint", "install",
                 "config", "needs_readd", "needs_readd_reason"),
            ),
        )

    @property
    def state(self) -> str:
        return self.install.state if self.install else "new"

    @property
    def device_uuid(self) -> str:
        return self.uuid


@dataclass
class ReconcileResult:
    installed: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    foreign: list[Wia] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ReconcileResult:
        return cls(
            installed=[str(x) for x in raw.get("installed") or []],
            stale=[str(x) for x in raw.get("stale") or []],
            foreign=[Wia.parse(x) for x in raw.get("foreign") or [] if isinstance(x, dict)],
        )


@dataclass
class WiaMatch:
    """Result of :func:`match_wia_to_devices`."""

    matched: dict[str, Wia] = field(default_factory=dict)   # device id/name -> its WIA row
    foreign: list[Wia] = field(default_factory=list)        # not provably a registered device
    duplicates: list[Wia] = field(default_factory=list)     # a second row for an already matched device


def port_uuids(port: str) -> set[str]:
    """Distinct lowercase UUIDs in a WIA port string (braces and ``urn:uuid:`` are irrelevant)."""
    return {m.group(0).lower() for m in _UUID_RE.finditer(port or "")}


def _norm_uuid(u: str) -> str:
    return str(u).strip().lower().replace("urn:uuid:", "").strip("{}")


def _dev_key(dev: Any) -> tuple[str, str]:
    if isinstance(dev, Mapping):
        key = str(dev.get("id") or dev.get("name") or "")
        uid = str(dev.get("uuid") or dev.get("device_uuid") or "")
    else:
        key = str(getattr(dev, "id", None) or getattr(dev, "name", None) or "")
        uid = str(getattr(dev, "uuid", None) or getattr(dev, "device_uuid", None) or "")
    return key, _norm_uuid(uid)


def match_wia_to_devices(devices: Iterable[Any], wia_scanners: Iterable[Mapping[str, Any] | Wia]) -> WiaMatch:
    """The contract rule: a WIA row matches a device only if EXACTLY ONE distinct UUID appears in
    its ``port`` string (case-insensitive, ``{}``/``urn:uuid:`` tolerant) and it equals a device's
    UUID. Everything else (no UUID, several UUIDs, an unknown UUID, a name-only resemblance) is
    foreign. ``devices`` may be DeviceRow, ManagedDevice, Device handles or dicts.
    """
    by_uuid = {}
    for dev in devices:
        key, uid = _dev_key(dev)
        if key and uid:
            by_uuid[uid] = key
    out = WiaMatch()
    for raw in wia_scanners:
        w = Wia.parse(raw)
        found = port_uuids(w.port)
        key = by_uuid.get(next(iter(found))) if len(found) == 1 else None
        if key is None:
            out.foreign.append(w)
        elif key in out.matched:
            out.duplicates.append(w)
        else:
            out.matched[key] = w
    return out


def parse(doc: Any) -> Registry:
    """Parse an already-decoded registry document (e.g. the body of ``GET /v1/registry``)."""
    return Registry.parse(doc)


def load(path: str | os.PathLike[str] | None = None, *, retries: int = 6, delay: float = 0.05) -> Registry:
    """Read the registry file (read-only). A missing file is an empty registry.

    Writers replace the file atomically, but on Windows a reader can hit a transient sharing
    violation or (rarely) a partial view, so transient OS/JSON errors are retried. The
    ``devices.json.lock`` file is never opened or held by readers.
    """
    p = Path(path) if path is not None else registry_path()
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            text = p.read_text(encoding="utf-8-sig")
            return Registry.parse(json.loads(text) if text.strip() else {})
        except FileNotFoundError:
            return Registry()
        except (OSError, ValueError) as e:  # PermissionError, sharing violation, torn JSON
            last = e
            time.sleep(delay * (attempt + 1))
    raise OSError(f"could not read registry {p}: {last}") from last


def from_server(client: Any) -> Registry:
    """Read the registry through a daemon: ``GET /v1/registry`` (``client``: Client, Manager, or URL)."""
    from .manager import Manager

    if isinstance(client, str):
        with Manager(client) as m:
            return m.registry()
    if isinstance(client, Manager):
        return client.registry()
    return Manager(client).registry()
