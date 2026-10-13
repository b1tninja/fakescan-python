"""Typed pieces behind the device-manager UI/tray parity (see ``Manager.status_table`` & co).

The GUI itself is the daemon's ``GET /ui`` page and its in-daemon tray; this module only gives
Python the same *data*: profile list, per-device status rows, the plain-text table and the
green/amber/red summary level the tray icon uses.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

# Boolean flags of GET /d/{id}/v1/status that count as an active fault.
FAULT_KEYS = (
    "jam", "cover_open", "multi_feed", "lamp_error", "offline", "busy", "storage_full", "interlock_open",
)


@dataclass
class FeederInfo:
    capacity: int | None = None
    media_sizes: list[str] = field(default_factory=list)
    duplex: bool = False

    @classmethod
    def parse(cls, raw: Any) -> FeederInfo:
        r = raw if isinstance(raw, Mapping) else {}
        cap = r.get("capacity")
        return cls(
            capacity=int(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) else None,
            media_sizes=[str(x) for x in (r.get("media_sizes") or [])],
            duplex=bool(r.get("duplex", False)),
        )


@dataclass
class ProfileInfo:
    """One element of ``GET /v1/profiles``. ``source`` is ``"daemon"`` or ``"files"`` (fallback)."""

    id: str
    connection: str = ""
    formats: list[str] = field(default_factory=list)
    feeder: FeederInfo = field(default_factory=FeederInfo)
    description: str = ""
    source: str = "daemon"

    @classmethod
    def parse(cls, raw: Mapping[str, Any], source: str = "daemon") -> ProfileInfo:
        return cls(
            id=str(raw.get("id") or ""),
            connection=str(raw.get("connection") or ""),
            formats=[str(x) for x in (raw.get("formats") or [])],
            feeder=FeederInfo.parse(raw.get("feeder")),
            description=str(raw.get("description") or raw.get("label") or ""),
            source=source,
        )


def profiles_from_files(directory: Path) -> list[ProfileInfo]:
    """Best-effort ``ProfileInfo`` list from ``*.json`` in ``directory`` (unreadable files skipped)."""
    out: list[ProfileInfo] = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            raw.setdefault("id", p.stem)
            out.append(ProfileInfo.parse(raw, source="files"))
    return out


@dataclass
class StatusRow:
    """One line of the device table (what the web UI and the tray summarise)."""

    id: str
    friendly_name: str = ""
    uuid: str = ""
    profile: str = ""
    install_state: str = "new"
    needs_readd: bool = False
    reason: str | None = None
    pages_remaining: int | None = None
    feeder_capacity: int | None = None
    faults: list[str] = field(default_factory=list)
    scan_endpoint: str | None = None
    device_endpoint: str | None = None
    last_peer: str | None = None


def faults_from_status(raw: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(raw, Mapping):
        return []
    return [k for k in FAULT_KEYS if raw.get(k) is True]


def summary_level(rows: Sequence[StatusRow] | None) -> str:
    """The tray's icon level: ``green`` (every device installed), ``amber`` (some device is new /
    probed / stale or needs a re-Add), ``red`` (``rows`` is ``None``: the daemon is unreachable or
    the manager reported an error). No devices at all is green (nothing needs an Add)."""
    if rows is None:
        return "red"
    for r in rows:
        if r.needs_readd or r.install_state != "installed":
            return "amber"
    return "green"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else (s[: max(n - 1, 0)] + "~" if n > 1 else s[:n])


def format_status_table(rows: Sequence[StatusRow], width: int | None = None) -> str:
    """Plain-text table, at most ``width`` columns wide (default: the terminal's, min 40).

    Columns: ID NAME STATE PROFILE PAGES FAULTS UUID PEER. When too wide, PEER, UUID and then
    NAME are dropped and the remaining flexible columns are clipped with an ellipsis. Devices that
    need a re-Add get a ``!`` after the state and a reason line underneath.
    """
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    width = max(width, 40)
    if not rows:
        return "(no devices)"

    def pages(r: StatusRow) -> str:
        if r.pages_remaining is None:
            return "-"
        return f"{r.pages_remaining}/{r.feeder_capacity}" if r.feeder_capacity is not None else str(r.pages_remaining)

    cols: list[tuple[str, list[str]]] = [
        ("ID", [r.id for r in rows]),
        ("NAME", [r.friendly_name or "-" for r in rows]),
        ("STATE", [r.install_state + ("!" if r.needs_readd else "") for r in rows]),
        ("PROFILE", [r.profile or "-" for r in rows]),
        ("PAGES", [pages(r) for r in rows]),
        ("FAULTS", [",".join(r.faults) or "-" for r in rows]),
        ("UUID", [r.uuid[:8] or "-" for r in rows]),
        ("PEER", [r.last_peer or "-" for r in rows]),
    ]
    flexible = {"NAME", "PROFILE", "FAULTS", "PEER"}

    def widths(cs: list[tuple[str, list[str]]]) -> list[int]:
        return [max(len(h), *(len(v) for v in vals)) for h, vals in cs]

    def total(ws: list[int]) -> int:
        return sum(ws) + 2 * (len(ws) - 1)

    for drop in ("PEER", "UUID", "NAME"):
        if total(widths(cols)) <= width:
            break
        cols = [c for c in cols if c[0] != drop]
    ws = widths(cols)
    # clip the widest flexible column until it fits
    while total(ws) > width:
        cand = [i for i, (h, _) in enumerate(cols) if h in flexible and ws[i] > 6]
        if not cand:
            break
        i = max(cand, key=lambda k: ws[k])
        ws[i] -= 1

    def line(cells: list[str]) -> str:
        return "  ".join(_clip(c, w).ljust(w) for c, w in zip(cells, ws)).rstrip()

    out = [line([h for h, _ in cols]), line(["-" * w for w in ws])]
    for i, r in enumerate(rows):
        out.append(line([vals[i] for _, vals in cols]))
        if r.needs_readd:
            out.append(_clip(f"  ! {r.id}: needs re-Add in Windows" + (f" ({r.reason})" if r.reason else ""), width))
    return "\n".join(out)
