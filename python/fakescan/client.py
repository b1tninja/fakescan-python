"""HTTP client for fakescan `:3920` control plane.

The Rust binary owns WSD/WIA. This module only scripts `/v1/*` so pytest /
lab scripts can arm pages, faults, and hard loopback without PowerShell.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:3920"


@dataclass(frozen=True)
class LoopbackInfo:
    count: int
    digests: list[str]
    active: bool


@dataclass(frozen=True)
class Job:
    """One scan job from GET /v1/jobs. The job token is not included."""

    id: int
    state: str
    format: str
    source: str
    dpi: int
    color: str
    pages_left: int
    page_index: int
    width_px: int
    height_px: int


@dataclass(frozen=True)
class ActivityEvent:
    """One call from GET /v1/activity. `kind` is `wsd`, `job`, or `ctl`."""

    seq: int
    kind: str
    peer: str
    action: str
    outcome: str
    ms: int
    detail: str


@dataclass(frozen=True)
class Activity:
    """GET /v1/activity (and GET /v1/trace): high-water seq and newer events."""

    seq: int
    events: list[ActivityEvent]


@dataclass(frozen=True)
class Status:
    """Subset of GET /v1/status used by the harness."""

    raw: dict[str, Any]

    @property
    def profile_id(self) -> str:
        return str(self.raw.get("profile_id", ""))

    @property
    def formats(self) -> list[str]:
        return [str(x) for x in (self.raw.get("formats") or [])]

    @property
    def pages_remaining(self) -> int:
        return int(self.raw.get("pages_remaining", 0))

    @property
    def duplex(self) -> bool:
        """Hardware ADF duplex advertised by the active profile."""
        return bool(self.raw.get("duplex"))

    @property
    def feeder_ready(self) -> bool:
        return bool(self.raw.get("feeder_ready"))

    @property
    def sticky_ready(self) -> bool:
        return bool(self.raw.get("sticky_ready"))

    @property
    def jam(self) -> bool:
        return bool(self.raw.get("jam"))

    @property
    def cover_open(self) -> bool:
        return bool(self.raw.get("cover_open"))

    @property
    def multi_feed(self) -> bool:
        return bool(self.raw.get("multi_feed"))

    @property
    def lamp_error(self) -> bool:
        return bool(self.raw.get("lamp_error"))

    @property
    def busy(self) -> bool:
        return bool(self.raw.get("busy"))

    @property
    def offline(self) -> bool:
        return bool(self.raw.get("offline"))

    @property
    def jam_after_n(self) -> int:
        return int(self.raw.get("jam_after_n", 0))

    @property
    def empty_mid_job(self) -> int:
        return int(self.raw.get("empty_mid_job", 0))

    @property
    def offline_after_n(self) -> int:
        return int(self.raw.get("offline_after_n", 0))

    @property
    def multi_feed_after_n(self) -> int:
        return int(self.raw.get("multi_feed_after_n", 0))

    @property
    def cover_open_after_n(self) -> int:
        return int(self.raw.get("cover_open_after_n", 0))

    @property
    def interlock_open(self) -> bool:
        return bool(self.raw.get("interlock_open"))

    @property
    def fault_plan(self) -> list[dict[str, Any]]:
        """Scheduled faults (pending and fired), plus read-only rows for legacy ``*_after_n``."""
        return [dict(e) for e in (self.raw.get("fault_plan") or [])]

    @property
    def abort_retrieve(self) -> bool:
        return bool(self.raw.get("abort_retrieve"))

    @property
    def abort_retrieve_after_n(self) -> int:
        return int(self.raw.get("abort_retrieve_after_n", 0))

    @property
    def abort_retrieve_bytes(self) -> int:
        return int(self.raw.get("abort_retrieve_bytes", 0))

    @property
    def loopback_pages(self) -> int:
        return int(self.raw.get("loopback_pages", 0))

    @property
    def loopback_digests(self) -> list[str]:
        return list(self.raw.get("loopback_digests") or [])

    @property
    def page_side(self) -> str | None:
        return self.raw.get("page_side")

    @property
    def page_order(self) -> str:
        return str(self.raw.get("page_order", "forward"))

    @property
    def image_pattern(self) -> str:
        return str(self.raw.get("image_pattern", ""))

    @property
    def wsd_instance_id(self) -> int:
        """WS-Discovery AppSequence InstanceId (boot-time seconds; 0 if WSD off)."""
        return int(self.raw.get("wsd_instance_id", 0))

    @property
    def wsd_metadata_version(self) -> int:
        """Discovery MetadataVersion on Hello / ProbeMatch / ResolveMatch."""
        return int(self.raw.get("wsd_metadata_version", 0))


def after_pages(n: int) -> dict[str, Any]:
    """Trigger between pages: after ``n`` pages were delivered."""
    return {"after_pages": int(n)}


def mid_page(page: int, *, fraction: float | None = None, bytes: int | None = None) -> dict[str, Any]:
    """Trigger while page ``page`` (1-based) streams: cut after ``fraction`` (0..1) or ``bytes``."""
    if (fraction is None) == (bytes is None):
        raise ValueError("mid_page needs exactly one of fraction= or bytes=")
    return {"page": int(page), **({"fraction": float(fraction)} if bytes is None else {"bytes": int(bytes)})}


def after_ms(ms: int, *, from_: str = "create") -> dict[str, Any]:
    """Trigger ``ms`` after CreateScanJob (``from_="create"``) or the first RetrieveImage."""
    return {"after_ms": int(ms), "from": from_}


def _job(item: Mapping[str, Any]) -> Job:
    return Job(
        id=int(item["id"]),
        state=str(item["state"]),
        format=str(item["format"]),
        source=str(item["source"]),
        dpi=int(item["dpi"]),
        color=str(item["color"]),
        pages_left=int(item["pages_left"]),
        page_index=int(item["page_index"]),
        width_px=int(item["width_px"]),
        height_px=int(item["height_px"]),
    )


def _activity_event(item: Mapping[str, Any]) -> ActivityEvent:
    return ActivityEvent(
        seq=int(item["seq"]),
        kind=str(item["kind"]),
        peer=str(item["peer"]),
        action=str(item["action"]),
        outcome=str(item["outcome"]),
        ms=int(item["ms"]),
        detail=str(item["detail"]),
    )


def loopback_digest(data: bytes) -> str:
    """Match Rust `loopback_digest`: `len:first8hex`."""
    head = data[:8].hex()
    return f"{len(data)}:{head}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ApiError(httpx.HTTPStatusError):
    """An HTTP error from the control plane, with the server's own message attached."""

    def __init__(self, response: httpx.Response) -> None:
        detail = response.text.strip()
        try:
            detail = str(response.json().get("error", detail))
        except Exception:  # not JSON
            pass
        request = response.request
        super().__init__(
            f"{request.method} {request.url} -> {response.status_code}: {detail}",
            request=request,
            response=response,
        )
        self.status_code = response.status_code
        self.detail = detail


def _check(r: httpx.Response) -> None:
    if r.status_code >= 400:
        raise ApiError(r)


class Client:
    """Thin httpx wrapper around fakescan control API.

    ``device`` scopes the client to one emulated scanner of a multi-device server: every
    control path and ``base_url`` become ``{base}/d/{device}``, so ``fakescan.soap`` helpers
    can be given ``client.base_url`` and post to that device's ``ScannerService``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        device: str | None = None,
    ) -> None:
        self.server_url = base_url.rstrip("/")
        self.device = device
        self.base_url = f"{self.server_url}/d/{device}" if device else self.server_url
        self._owns = client is None
        self._http = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _server_url(self, path: str) -> str:
        return f"{self.server_url}{path}"

    def _json(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        r = self._http.request(method, self._url(path), json=body, params=params)
        _check(r)
        if not r.content:
            return None
        return r.json()

    def devices(self) -> list[dict[str, Any]]:
        """Every device on the server (GET /v1/devices), regardless of this client's scope."""
        r = self._http.get(self._server_url("/v1/devices"))
        _check(r)
        data = r.json()
        return data["devices"] if isinstance(data, dict) else data

    def add_device(
        self,
        id: str,
        profile: str,
        *,
        device_uuid: str | None = None,
        friendly_name: str | None = None,
    ) -> dict[str, Any]:
        """Add a device at runtime (POST /v1/devices). 409 on duplicate id/uuid -> ApiError."""
        body: dict[str, Any] = {"id": id, "profile": profile}
        if device_uuid is not None:
            body["device_uuid"] = str(device_uuid)
        if friendly_name is not None:
            body["friendly_name"] = friendly_name
        r = self._http.post(self._server_url("/v1/devices"), json=body)
        _check(r)
        return r.json()

    def remove_device(self, id: str) -> None:
        """Remove a device at runtime (DELETE /v1/devices/{id})."""
        _check(self._http.delete(self._server_url(f"/v1/devices/{id}")))

    def for_device(self, id: str) -> Client:
        """A client for another device of the same server, sharing this HTTP session."""
        return Client(self.server_url, client=self._http, device=id)

    def health(self) -> bool:
        r = self._http.get(self._server_url("/health"))
        return r.status_code == 200 and r.text.strip() == "ok"

    def status(self) -> Status:
        return Status(raw=self._json("GET", "/v1/status"))

    def jobs(self) -> list[Job]:
        """Scan jobs without tokens (GET /v1/jobs)."""
        data = self._json("GET", "/v1/jobs")
        return [_job(item) for item in data["jobs"]]

    def activity(self, after: int = 0) -> Activity:
        """Events with `seq` greater than `after`. GET /v1/trace returns the same JSON."""
        data = self._json("GET", "/v1/activity", params={"after": after})
        return Activity(
            seq=int(data["seq"]),
            events=[_activity_event(item) for item in data["events"]],
        )

    def log_text(self, after: int = 0) -> str:
        """Plain-text activity since `after` (GET /v1/log)."""
        r = self._http.get(self._url("/v1/log"), params={"after": after})
        _check(r)
        return r.text

    def reset(self) -> Status:
        return Status(raw=self._json("POST", "/v1/reset"))

    def set_profile(self, profile_id: str) -> Status:
        return Status(raw=self._json("POST", "/v1/profile", {"id": profile_id}))

    def set_feeder(self, ready: bool) -> Status:
        return Status(raw=self._json("POST", "/v1/feeder", {"ready": ready}))

    def set_pages(
        self,
        count: int,
        *,
        side: str | None = None,
        order: str | None = None,
    ) -> Status:
        body: dict[str, Any] = {"count": count}
        if side is not None:
            body["side"] = side
        if order is not None:
            body["order"] = order
        return Status(raw=self._json("POST", "/v1/pages", body))

    def set_pattern(self, pattern_id: str) -> Status:
        return Status(raw=self._json("POST", "/v1/pattern", {"id": pattern_id}))

    def set_scenario(self, scenario_id: str) -> Status:
        return Status(raw=self._json("POST", "/v1/scenario", {"id": scenario_id}))

    def set_slow(self, ms: int = 0, *, bps: int | None = None) -> Status:
        """Pre-RetrieveImage sleep (`ms`) and/or MTOM send throttle (`bps` bytes/sec)."""
        body: dict[str, Any] = {"ms": ms}
        if bps is not None:
            body["bps"] = bps
        return Status(raw=self._json("POST", "/v1/slow", body))

    def set_effects(self, **fields: Any) -> Status:
        """Merge image-defect knobs (POST /v1/effects): ``preset``, ``seed``, ``skew_deg``, ...

        ``bilevel_polarity="black_is_zero"|"white_is_zero"`` switches the bit sense of
        BlackAndWhite1 output (BMP palette order, TIFF PhotometricInterpretation) while
        the visual image stays identical. Daemons older than that effect ignore the key.

        ``jfif_gray="gray8"|"rgb24"|"gray8_rgb_stride"|"gray8_tiff"`` picks how a
        Grayscale8 + JFIF/JPEG/EXIF ticket is answered. ``gray8`` (default) is consistent gray
        (Grayscale8, BytesPerLine = width, 1-component JPEG; live-proven on Windows). ``rgb24`` is
        the legacy coercion (RGB24 FinalParameters, width*3 stride, 3-component JPEG), known to be
        rejected by the Windows inbox driver and kept for tests. ``gray8_rgb_stride`` is gray with
        a width*3 stride; ``gray8_tiff`` answers with an 8-bit gray TIFF (rejected live). It never
        changes the published ScannerConfiguration; ``clear=True`` and ``reset`` restore
        ``gray8``. Older daemons ignore the key (probe
        ``status().raw["effects"].get("jfif_gray")``).
        """
        return Status(raw=self._json("POST", "/v1/effects", fields))

    def set_jpeg_com(self, enabled: bool) -> Status:
        return Status(raw=self._json("POST", "/v1/jpeg-com", {"enabled": enabled}))

    def set_fault(
        self,
        *,
        jam: bool | None = None,
        cover_open: bool | None = None,
        multi_feed: bool | None = None,
        lamp_error: bool | None = None,
        busy: bool | None = None,
        offline: bool | None = None,
        interlock_open: bool | None = None,
        ms: int | None = None,
        jam_after_n: int | None = None,
        cover_open_after_n: int | None = None,
        empty_mid_job: int | None = None,
        offline_after_n: int | None = None,
        multi_feed_after_n: int | None = None,
        abort_retrieve: bool | None = None,
        abort_retrieve_after_n: int | None = None,
        abort_retrieve_bytes: int | None = None,
        lamp_warmup_ms: int | None = None,
    ) -> Status:
        body: dict[str, Any] = {}
        if jam is not None:
            body["jam"] = jam
        if cover_open is not None:
            body["cover_open"] = cover_open
        if multi_feed is not None:
            body["multi_feed"] = multi_feed
        if lamp_error is not None:
            body["lamp_error"] = lamp_error
        if busy is not None:
            body["busy"] = busy
        if offline is not None:
            body["offline"] = offline
        if interlock_open is not None:
            body["interlock_open"] = interlock_open
        if cover_open_after_n is not None:
            body["cover_open_after_n"] = cover_open_after_n
        if ms is not None:
            body["ms"] = ms
        if jam_after_n is not None:
            body["jam_after_n"] = jam_after_n
        if empty_mid_job is not None:
            body["empty_mid_job"] = empty_mid_job
        if offline_after_n is not None:
            body["offline_after_n"] = offline_after_n
        if multi_feed_after_n is not None:
            body["multi_feed_after_n"] = multi_feed_after_n
        if abort_retrieve is not None:
            body["abort_retrieve"] = abort_retrieve
        if abort_retrieve_after_n is not None:
            body["abort_retrieve_after_n"] = abort_retrieve_after_n
        if abort_retrieve_bytes is not None:
            body["abort_retrieve_bytes"] = abort_retrieve_bytes
        if lamp_warmup_ms is not None:
            body["lamp_warmup_ms"] = lamp_warmup_ms
        return Status(raw=self._json("POST", "/v1/fault", body))

    def inject_fault(
        self,
        kind: str,
        when: str | Mapping[str, Any] = "now",
        *,
        sticky: bool | None = None,
        one_shot: bool | None = None,
        job: int | None = None,
        pages: tuple[int, int] | None = None,
    ) -> Status:
        """Schedule a fault (``POST /v1/faults/plan``); returns the new status.

        ``kind``: jam, cover_open, interlock_open, multi_feed, empty, offline, lamp_error, busy,
        abort (transport cut only). ``when``: ``"now"`` (applies at once, also to a running job),
        ``{"after_pages": N}`` (before the N+1th RetrieveImage), ``{"page": P, "fraction": F}`` or
        ``{"page": P, "bytes": B}`` (the stream of page P is cut part-way; that page is not
        delivered), ``{"after_ms": T, "from": "create"|"first_retrieve"}``. See :func:`after_pages`,
        :func:`mid_page`, :func:`after_ms`.
        """
        entry: dict[str, Any] = {"kind": kind, "when": dict(when) if isinstance(when, Mapping) else when}
        if sticky is not None:
            entry["sticky"] = sticky
        if one_shot is not None:
            entry["one_shot"] = one_shot
        if job is not None:
            entry["job"] = job
        if pages is not None:
            entry["pages"] = [int(pages[0]), int(pages[1])]
        return Status(raw=self._json("POST", "/v1/faults/plan", {"faults": [entry]}))

    def fault_plan(self) -> list[dict[str, Any]]:
        """Scheduled faults: kind, when, trigger text, sticky, one_shot, fired, last_fired."""
        data = self._json("GET", "/v1/faults/plan")
        return [dict(e) for e in (data.get("fault_plan") or [])]

    def clear_fault_plan(self) -> Status:
        """Drop scheduled faults only. Conditions already raised stay until ``reset()``."""
        return Status(raw=self._json("DELETE", "/v1/faults/plan"))

    def loopback(self) -> LoopbackInfo:
        data = self._json("GET", "/v1/loopback")
        return LoopbackInfo(
            count=int(data["count"]),
            digests=list(data.get("digests") or []),
            active=bool(data.get("active")),
        )

    def set_loopback_bytes(self, pages: Sequence[bytes]) -> LoopbackInfo:
        """Upload raw page blobs (prefer JFIF). Empty clears the store."""
        payload = {
            "pages": [base64.b64encode(p).decode("ascii") for p in pages],
        }
        self._json("POST", "/v1/loopback", payload)
        return self.loopback()

    def set_loopback_files(self, paths: Sequence[str | Path]) -> LoopbackInfo:
        blobs = [Path(p).read_bytes() for p in paths]
        return self.set_loopback_bytes(blobs)

    def clear_loopback(self) -> Status:
        return Status(raw=self._json("DELETE", "/v1/loopback"))

    def expect_loopback_files(self, paths: Sequence[str | Path]) -> None:
        """Assert server digests match local files (ground truth)."""
        local = [loopback_digest(Path(p).read_bytes()) for p in paths]
        remote = self.loopback()
        if local != remote.digests:
            raise AssertionError(
                f"loopback digest mismatch: local={local!r} remote={remote.digests!r}"
            )

    def expect_acquired_jfif(self, acquired: bytes | Path, original: bytes | Path) -> None:
        """Hard loopback expect: acquired JFIF bytes equal the uploaded original."""
        a = Path(acquired).read_bytes() if not isinstance(acquired, bytes) else acquired
        o = Path(original).read_bytes() if not isinstance(original, bytes) else original
        if a != o:
            raise AssertionError(
                "acquired JFIF != loopback original "
                f"(sha256 acquired={sha256_hex(a)[:16]} original={sha256_hex(o)[:16]})"
            )
