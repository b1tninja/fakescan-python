"""In-process fakescan server (native binding) with the lab defaults filled in."""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import _paths
from ._native import Server as _NativeServer
from .client import Client
from .manager import Manager

DEFAULT_DEVICE_ID = "default"


@dataclass(frozen=True)
class Device:
    """Description of one emulated scanner: ``Device("tray5", profile="feeder-tray5")``."""

    id: str
    profile: str = "brother-wsd"
    device_uuid: str | None = None
    friendly_name: str | None = None


class DeviceHandle:
    """A running device of a :class:`Server` (see ``Server.devices``)."""

    def __init__(self, server: Server, id: str, uuid: str, profile: str, friendly_name: str) -> None:
        self._server = server
        self.id = id
        self.uuid = uuid
        self.profile = profile
        self.friendly_name = friendly_name

    @property
    def _prefix(self) -> str:
        # The default (first) device keeps the legacy, un-prefixed WSD paths.
        return "" if self.id == self._server._default_id else f"/d/{self.id}"

    @property
    def scan_endpoint(self) -> str:
        return f"{self._server.base_url}{self._prefix}/ScannerService"

    @property
    def device_endpoint(self) -> str:
        return f"{self._server.base_url}{self._prefix}/WebServices/Device"

    def client(self, **kwargs: Any) -> Client:
        """A control-plane ``Client`` scoped to this device (``/d/{id}/v1/...``)."""
        return Client(self._server.base_url, device=self.id, **kwargs)

    def __repr__(self) -> str:
        return f"DeviceHandle({self.id!r}, profile={self.profile!r}, uuid={self.uuid})"


class Server:
    """The fakescan server running in this process, on an ephemeral port by default.

    >>> with Server(devices=[Device("tray5", "feeder-tray5")]) as srv:  # doctest: +SKIP
    ...     srv.devices["tray5"].client().set_pages(5)

    With no ``devices`` there is one ``default`` device using ``profile`` (and ``device_uuid``).
    ``discovery`` stays off unless asked: an embedded server must not advertise a second
    scanner on the LAN. ``wsd=True`` still serves ``ScannerService`` at ``scan_endpoint``.
    Devices can be added and removed while running (``add_device`` / ``remove_device``, over
    the same ``/v1/devices`` CRUD the standalone binary exposes); ``devices`` stays in sync.

    ``srv.manager`` is a :class:`fakescan.Manager` bound to this server (add/remove/reconfigure/
    installed/reconcile/forget over HTTP). Embedded servers keep install state in MEMORY only:
    there is no registry file, nothing persists after ``stop()``, and discovery is off.
    """

    def __init__(
        self,
        devices: list[Device] | None = None,
        *,
        profile: str = "brother-wsd",
        profiles: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        device_uuid: str | None = None,
        wsd: bool = True,
        discovery: bool = False,
        advertise: str | None = None,
    ) -> None:
        if devices and device_uuid is not None:
            raise ValueError("device_uuid applies to the implicit default device; set it on a Device instead")
        specs = None
        if devices:
            specs = [
                (d.id, d.profile, d.device_uuid or str(_uuid.uuid4()), d.friendly_name) for d in devices
            ]
        self._native = _NativeServer(
            profiles=str(profiles if profiles is not None else _paths.profiles_dir()),
            profile=profile,
            host=host,
            port=port,
            device_uuid=device_uuid,
            wsd=wsd,
            discovery=discovery,
            advertise=advertise,
            devices=specs,
        )
        self._manager: Manager | None = None
        self._default_id = DEFAULT_DEVICE_ID if not devices else devices[0].id
        self.devices: dict[str, DeviceHandle] = {
            id_: DeviceHandle(self, id_, uuid, prof, name) for id_, uuid, prof, name in self._native.devices
        }
        if self._default_id not in self.devices:  # first reported device is the default
            self._default_id = next(iter(self.devices))

    port = property(lambda self: self._native.port)
    base_url = property(lambda self: self._native.base_url)
    running = property(lambda self: self._native.running)

    @property
    def device_uuid(self) -> str:
        """UUID of the default device."""
        return self.devices[self._default_id].uuid

    @property
    def scan_endpoint(self) -> str:
        """The default device's ScannerService endpoint (legacy path)."""
        return self.devices[self._default_id].scan_endpoint

    @property
    def manager(self) -> Manager:
        """A :class:`Manager` for this server (created lazily; closed by ``stop()``)."""
        if self._manager is None:
            self._manager = Manager(self.base_url)
        return self._manager

    def open_ui(self, opener: Callable[[str], Any] | None = None) -> str:
        """Open this server's ``/ui`` page (the same router as the daemon's; needs a fakescan
        build that has the route) with ``webbrowser.open`` unless ``opener`` is given. Returns the URL."""
        return self.manager.open_ui(opener)

    def client(self, **kwargs: Any) -> Client:
        """A control-plane ``Client`` for the default device (legacy ``/v1/*`` paths)."""
        return Client(self.base_url, **kwargs)

    def add_device(self, device: Device) -> DeviceHandle:
        """Add a scanner while the server runs. Duplicate id/uuid or bad profile raise
        ``fakescan.client.ApiError`` (an ``httpx.HTTPStatusError``) with the server's message."""
        with self.client() as c:
            row = c.add_device(
                device.id,
                device.profile,
                device_uuid=device.device_uuid,
                friendly_name=device.friendly_name,
            )
        handle = DeviceHandle(
            self, row["id"], str(row["uuid"]), row["profile"], row.get("friendly_name") or device.id
        )
        self.devices[handle.id] = handle
        return handle

    def remove_device(self, id: str) -> None:
        """Stop and remove a scanner while the server runs."""
        with self.client() as c:
            c.remove_device(id)
        self.devices.pop(id, None)

    def stop(self) -> None:
        if self._manager is not None:
            self._manager.close()
            self._manager = None
        self._native.stop()

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        return f"Server({self.base_url}, devices={list(self.devices)})"
