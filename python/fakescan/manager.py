"""``fakescan.Manager``: the device-manager HTTP surface (add/remove/reconfigure/install tracking).

Every mutation goes to the daemon, which owns the registry file (Rust file lock + atomic writer).
Python never writes ``devices.json``. Errors are :class:`fakescan.ApiError` (400 bad input or an
``installed`` whose port lacks the UUID, 404 unknown device, 409 duplicate id/uuid).
"""

from __future__ import annotations

import ipaddress
import os
import time
import webbrowser
from urllib.parse import urlsplit
from typing import Any, Callable, Iterable, Mapping

import httpx

from . import _paths
from .client import ApiError, Client, _check
from .ui import (
    ProfileInfo,
    StatusRow,
    faults_from_status,
    profiles_from_files,
    summary_level as _summary_level,
)
from .registry import (
    ManagedDevice,
    ReconcileResult,
    Registry,
    Wia,
    match_wia_to_devices,
    port_uuids,
)


def is_loopback_url(url: str) -> bool:
    """True when ``url``'s host is ``localhost`` or a loopback IP."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Manager:
    """Typed client for the manager endpoints of a fakescan server (daemon or embedded).

    ``Manager("http://127.0.0.1:3920")``, ``Manager(client)`` or ``Manager(server)`` (anything with
    a ``base_url``, e.g. :class:`fakescan.Server`, whose ``.manager`` is exactly this).
    """

    def __init__(self, target: str | Client | Any, *, timeout: float = 30.0) -> None:
        if isinstance(target, Client):
            self._client = Client(target.server_url, client=target._http)
        else:
            url = target if isinstance(target, str) else target.base_url
            self._client = Client(url, timeout=timeout)
        self.base_url = self._client.server_url

    # -- lifecycle -------------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Manager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Manager({self.base_url})"

    # -- reads -----------------------------------------------------------------------------
    def devices(self) -> list[ManagedDevice]:
        """GET /v1/devices with install state, config and ``needs_readd`` (+ reason)."""
        return [ManagedDevice.parse(r) for r in self._client.devices()]

    def device(self, id: str) -> ManagedDevice:
        for d in self.devices():
            if d.id == id:
                return d
        raise KeyError(f"unknown device {id!r}")

    def registry(self) -> Registry:
        """GET /v1/registry: the document the daemon holds (in memory for embedded servers)."""
        return Registry.parse(self._client._json("GET", "/v1/registry"))

    # -- device CRUD -----------------------------------------------------------------------
    def add(
        self,
        id: str,
        profile: str,
        device_uuid: str | None = None,
        friendly_name: str | None = None,
    ) -> ManagedDevice:
        """POST /v1/devices. Without ``device_uuid`` the daemon reuses the row's stored UUID, else
        derives one. Adding an installed device again keeps its identity."""
        self._client.add_device(id, profile, device_uuid=device_uuid, friendly_name=friendly_name)
        return self.device(id)

    def remove(self, id: str, forget: bool = False) -> None:
        """DELETE /v1/devices/{id}[?forget=true]: stop it; keep the registry row unless ``forget``."""
        r = self._client._http.delete(
            self._client._server_url(f"/v1/devices/{id}"), params={"forget": "true"} if forget else None
        )
        _check(r)

    def reconfigure(self, id: str, profile: str) -> ManagedDevice:
        """Swap the profile. An installed device becomes ``stale`` (needs_readd) only when the
        PUBLISHED capabilities change (e.g. media sizes), not for capacity-only changes."""
        return ManagedDevice.parse(
            self._client._json("POST", f"/v1/devices/{id}/reconfigure", {"profile": profile})
        )

    # -- install tracking ------------------------------------------------------------------
    def installed(self, id: str, wia_id: str, wia_name: str, wia_port: str) -> ManagedDevice:
        """Record that Windows has this device. The daemon refuses (ApiError 400) unless
        ``wia_port`` contains the device's UUID."""
        return ManagedDevice.parse(
            self._client._json(
                "POST",
                f"/v1/devices/{id}/installed",
                {"wia_id": wia_id, "wia_name": wia_name, "wia_port": wia_port},
            )
        )

    def forget(self, id: str) -> ManagedDevice:
        """Drop install info, keep the row."""
        return ManagedDevice.parse(self._client._json("POST", f"/v1/devices/{id}/forget", {}))

    def reconcile(self, scanners: Iterable[Mapping[str, Any] | Wia]) -> ReconcileResult:
        """POST /v1/reconcile with the Windows scanner list (``id``/``name``/``port`` each).

        The match rule is the contract's (exactly one UUID in ``port``, equal to a device's). The
        daemon's own check is a substring test, so rows with several UUIDs are withheld here and
        reported as foreign: they can never be installed through this call.
        """
        rows = [Wia.parse(s) for s in scanners]
        ambiguous = [w for w in rows if len(port_uuids(w.port)) > 1]
        sendable = [w for w in rows if len(port_uuids(w.port)) <= 1]
        body = {"scanners": [w.to_dict() for w in sendable]}
        res = ReconcileResult.parse(self._client._json("POST", "/v1/reconcile", body))
        res.foreign.extend(ambiguous)
        return res

    def match(self, scanners: Iterable[Mapping[str, Any] | Wia]):
        """Pure local classification (no request is changed): see ``registry.match_wia_to_devices``."""
        return match_wia_to_devices(self.devices(), scanners)

    def wait_installed(self, id: str, timeout: float = 60.0, interval: float = 0.5) -> ManagedDevice:
        """Poll /v1/devices until ``id`` is ``installed``; ``TimeoutError`` otherwise."""
        deadline = time.monotonic() + timeout
        while True:
            dev = self.device(id)
            if dev.state == "installed":
                return dev
            if time.monotonic() >= deadline:
                raise TimeoutError(f"device {id!r} still {dev.state!r} after {timeout}s")
            time.sleep(interval)

    def reconcile_from_windows(
        self, list_scanners: Callable[[], Any] | None = None
    ) -> ReconcileResult:
        """List WIA scanners (read-only) and reconcile. Uses ``pdfcraft.scan.list_scanners`` unless
        ``list_scanners`` is given (a callable returning ``{"scanners": [...]}`` or a list of dicts
        with ``id``/``name``/``port``). Changes nothing on Windows."""
        if list_scanners is None:
            try:
                from pdfcraft.scan import list_scanners  # type: ignore[import-not-found]
            except ImportError as e:
                raise RuntimeError(
                    "pdfcraft.scan is not importable; pass list_scanners=... to reconcile_from_windows"
                ) from e
        listing = list_scanners()
        if isinstance(listing, Mapping):
            listing = listing.get("scanners") or []
        return self.reconcile([s for s in listing if isinstance(s, (Mapping, Wia))])

    # -- UI / tray parity ------------------------------------------------------------------
    def profiles(self) -> list[ProfileInfo]:
        """GET /v1/profiles as typed :class:`~fakescan.ui.ProfileInfo`.

        FALLBACK (best effort): a daemon built before the UI work answers 404; then the ``*.json``
        files of the profiles directory this package knows (``FAKESCAN_PROFILES``, else the sibling
        fakescan repo's ``profiles/``) are read locally instead. Those rows have
        ``source == "files"``; they describe the local checkout, which may differ from what a
        remote daemon actually has.
        """
        try:
            data = self._client._json("GET", "/v1/profiles")
        except ApiError as e:
            if e.status_code not in (404, 405):
                raise
            return profiles_from_files(_paths.profiles_dir())
        if isinstance(data, dict):
            data = data.get("profiles", [])
        if not isinstance(data, list):
            return profiles_from_files(_paths.profiles_dir())
        return [ProfileInfo.parse(x) for x in data if isinstance(x, dict)]

    @property
    def ui_url(self) -> str:
        return f"{self.base_url}/ui"

    def open_ui(self, opener: Callable[[str], Any] | None = None) -> str:
        """Open the daemon's ``/ui`` page in the default browser (``webbrowser.open`` unless
        ``opener`` is given) and return the URL. The page is loopback-only on the server side."""
        url = self.ui_url
        (opener or webbrowser.open)(url)
        return url

    def open_add_device_settings(
        self, opener: Callable[[str], Any] | None = None, *, use_daemon: bool = True
    ) -> str:
        """Open Windows' printers/scanners settings page (where a scanner is Added).

        Returns the path used: ``"daemon"`` (a loopback daemon opened it in its own session via
        ``POST /v1/ctl open_settings``), ``"startfile"`` (``opener('ms-settings:printers')``, default
        ``os.startfile``, Windows only) or ``"unavailable"``. Never raises for a missing route or
        an unreachable daemon.
        """
        if use_daemon and is_loopback_url(self.base_url):
            try:
                res = self._client._json("POST", "/v1/ctl", {"cmd": "open_settings", "page": "printers"})
            except (httpx.HTTPError, ValueError):
                res = None
            if isinstance(res, dict):
                inner = res.get("result") if isinstance(res.get("result"), dict) else res
                if inner.get("opened") is True:
                    return "daemon"
        if opener is None and os.name == "nt":
            opener = os.startfile  # type: ignore[attr-defined]
        if opener is None:
            return "unavailable"
        opener("ms-settings:printers")
        return "startfile"

    def status_table(self) -> list[StatusRow]:
        """One :class:`~fakescan.ui.StatusRow` per device: GET /v1/devices joined with each
        device's ``/d/{id}/v1/status``. Tolerant: a missing field or a device whose status call
        fails leaves that column ``None``/empty. Raises if /v1/devices itself fails."""
        rows: list[StatusRow] = []
        for d in self.devices():
            st: dict[str, Any] = {}
            try:
                r = self._client._http.get(self._client._server_url(f"/d/{d.id}/v1/status"))
                if r.status_code == 200 and isinstance(r.json(), dict):
                    st = r.json()
            except (httpx.HTTPError, ValueError):
                pass
            cap = st.get("feeder_capacity")
            if cap is None and d.config is not None:
                cap = d.config.feeder.capacity
            pages = st.get("pages_remaining")
            inst = d.install
            rows.append(
                StatusRow(
                    id=d.id,
                    friendly_name=d.friendly_name,
                    uuid=d.uuid,
                    profile=d.profile,
                    install_state=d.state,
                    needs_readd=d.needs_readd,
                    reason=d.needs_readd_reason or (inst.reason if inst and d.needs_readd else None),
                    pages_remaining=int(pages) if isinstance(pages, (int, float)) else None,
                    feeder_capacity=int(cap) if isinstance(cap, (int, float)) else None,
                    faults=faults_from_status(st),
                    scan_endpoint=d.scan_endpoint,
                    device_endpoint=d.device_endpoint,
                    last_peer=inst.last_peer if inst else None,
                )
            )
        return rows

    def summary_level(self) -> str:
        """``green`` / ``amber`` / ``red`` as the tray icon shows it; ``red`` when the daemon is
        unreachable or answers with an error."""
        try:
            rows: list[StatusRow] | None = self.status_table()
        except (httpx.HTTPError, ValueError):
            rows = None
        return _summary_level(rows)
