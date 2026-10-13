"""fakescan-ctl — control-plane CLI and registry manager for fakescan.

Connects to a running fakescan instance via HTTP (POST /v1/ctl) and manages
the per-repo device registry (%LOCALAPPDATA%\\fakescan\\devices.json).

Usage:
    fakescan-ctl status [--raw] [--width N] [--base-url URL]   (table; --raw = ctl info + status JSON)
    fakescan-ctl devices [--width N]                            (same table)
    fakescan-ctl ui                                             (open <base-url>/ui in the browser)
    fakescan-ctl profiles                                       (GET /v1/profiles)
    fakescan-ctl add ID --profile P [--uuid U] [--name FRIENDLY]   (runtime add via the manager API)
    fakescan-ctl remove ID [--forget]                           (runtime remove via the manager API)
    fakescan-ctl open-settings                                  (open Windows printers settings)
    fakescan-ctl ctl <cmd> [--args JSON] [--base-url URL]
    fakescan-ctl pages <count> [--side front|back] [--order forward|reverse]
    fakescan-ctl pattern <id>
    fakescan-ctl scenario <id>
    fakescan-ctl fault [--jam BOOL] [--cover-open BOOL] [--offline BOOL] [--ms N]
                       [--jam-after-n N] [--offline-after-n N] [--empty-mid-job N]
    fakescan-ctl reset
    fakescan-ctl save [--name NAME]
    fakescan-ctl list
    fakescan-ctl add --name NAME [--listen HOST:PORT] [--advertise IP]
                    [--uuid UUID] [--profile PROFILE] [--exe PATH]
    fakescan-ctl remove --name NAME
    fakescan-ctl mark-added [--name NAME] [--pnp]
    fakescan-ctl ensure [--name NAME] [--all] [--base-url URL]
    fakescan-ctl pipe <cmd> [--args JSON] [--name NAME]   (Windows: named pipe)

Legacy registry-row forms keep working: ``add --name NAME ...`` / ``remove --name NAME`` (no
positional ID) edit devices.json directly; with a positional ID they talk to the running server.

Registry path: %LOCALAPPDATA%\\fakescan\\devices.json
Port policy  : even ports 3920, 3922, … (odd ports are fakeprint — never used here)
Siblings     : never touches %LOCALAPPDATA%\\fakeprint\\  or kills fakeprint processes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

from .client import DEFAULT_BASE_URL, ApiError
from .manager import Manager
from .ui import format_status_table, summary_level

# ── Registry ──────────────────────────────────────────────────────────────────

DEFAULT_UUID = "00000000-0000-4000-8000-0000000000f2"
DEFAULT_LISTEN = "0.0.0.0:3920"


def registry_path() -> Path:
    """
    %LOCALAPPDATA%\\fakescan\\devices.json on Windows;
    ~/.local/share/fakescan/devices.json elsewhere.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(base) / "fakescan" / "devices.json"
    return Path.home() / ".local" / "share" / "fakescan" / "devices.json"


def load_registry() -> dict[str, Any]:
    """Load devices.json; return empty registry structure if absent."""
    p = registry_path()
    if not p.exists():
        return {"port_policy": {"base": 3920, "step": 2}, "devices": []}
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def save_registry(reg: dict[str, Any]) -> None:
    """Write devices.json, creating the directory tree as needed."""
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def find_device(reg: dict[str, Any], name: str) -> dict[str, Any] | None:
    for d in reg.get("devices", []):
        if d.get("name") == name:
            return d
    return None


def next_port(reg: dict[str, Any]) -> int:
    """Return the next unused even port in the registry."""
    policy = reg.get("port_policy", {})
    base: int = policy.get("base", 3920)
    step: int = max(policy.get("step", 2), 2)
    used = {
        int(d.get("listen", "").split(":")[-1])
        for d in reg.get("devices", [])
        if ":" in d.get("listen", "")
    }
    p = base
    while p in used:
        p += step
    return p


def validate_port(port: int) -> None:
    if port % 2 != 0:
        print(
            f"error: fakescan uses even ports (3920, 3922, …); "
            f"{port} is odd — odd ports are fakeprint",
            file=sys.stderr,
        )
        sys.exit(1)


# ── HTTP ctl ──────────────────────────────────────────────────────────────────


def ctl_post(base_url: str, cmd: str, args: dict[str, Any] | None = None) -> Any:
    """POST /v1/ctl and return the JSON result (or raise on error)."""
    body: dict[str, Any] = {"cmd": cmd}
    if args:
        body["args"] = args
    with httpx.Client(timeout=15.0) as c:
        r = c.post(f"{base_url.rstrip('/')}/v1/ctl", json=body)
        r.raise_for_status()
        return r.json()


def ctl_get(base_url: str) -> Any:
    """GET /v1/ctl and return the JSON caps + identity."""
    with httpx.Client(timeout=5.0) as c:
        r = c.get(f"{base_url.rstrip('/')}/v1/ctl")
        r.raise_for_status()
        return r.json()


def is_running(base_url: str) -> bool:
    """True if fakescan answers /health at base_url."""
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{base_url.rstrip('/')}/health")
            return r.status_code == 200 and r.text.strip() == "ok"
    except Exception:
        return False


# ── Named pipe (Windows) ──────────────────────────────────────────────────────


def pipe_ctl(name: str, cmd: str, args: dict[str, Any] | None = None) -> Any:
    """Send a JSONL ctl command via \\\\.\pipe\\fakescan-<name> (Windows only)."""
    if os.name != "nt":
        print("error: named pipe is Windows-only; use --base-url HTTP instead", file=sys.stderr)
        sys.exit(1)
    import socket

    pipe_path = rf"\\.\pipe\fakescan-{name}"
    body: dict[str, Any] = {"cmd": cmd}
    if args:
        body["args"] = args
    line = json.dumps(body) + "\n"

    # On Windows, named pipes can be opened with open() or CreateFile via ctypes.
    # The simplest cross-version approach is the socket trick via subprocess,
    # but the recommended way is ctypes. We use a try/except fallback.
    try:
        import ctypes
        import ctypes.wintypes as wt

        GENERIC_READ_WRITE = 0xC0000000
        OPEN_EXISTING = 3
        FILE_FLAG_OVERLAPPED = 0x40000000

        handle = ctypes.windll.kernel32.CreateFileW(
            pipe_path,
            GENERIC_READ_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            None,
        )
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        if handle == INVALID_HANDLE_VALUE:
            err = ctypes.windll.kernel32.GetLastError()
            raise OSError(f"CreateFile failed: error {err}")

        buf = line.encode("utf-8")
        written = wt.DWORD(0)
        ctypes.windll.kernel32.WriteFile(handle, buf, len(buf), ctypes.byref(written), None)

        recv = ctypes.create_string_buffer(65536)
        read = wt.DWORD(0)
        ctypes.windll.kernel32.ReadFile(handle, recv, 65536, ctypes.byref(read), None)
        ctypes.windll.kernel32.CloseHandle(handle)

        resp = json.loads(recv.raw[: read.value].decode("utf-8").strip())
        if not resp.get("ok"):
            print(f"pipe error: {resp.get('error')}", file=sys.stderr)
            sys.exit(1)
        return resp.get("result")

    except Exception as e:
        print(f"pipe error: {e}", file=sys.stderr)
        sys.exit(1)


# ── ensure ────────────────────────────────────────────────────────────────────


def cmd_ensure(name: str, reg: dict[str, Any]) -> int:
    """Check if the named instance is running; start it if not."""
    row = find_device(reg, name)
    if row is None:
        print(f"error: no registry row named {name!r}; run: fakescan-ctl list", file=sys.stderr)
        return 1

    listen = row.get("listen", DEFAULT_LISTEN)
    port = int(listen.split(":")[-1]) if ":" in listen else 3920
    base_url = f"http://127.0.0.1:{port}"

    if is_running(base_url):
        print(f"fakescan/{name} already running on :{port} — attaching")
        info = ctl_get(base_url)
        print(json.dumps(info, indent=2))
        return 0

    exe = row.get("exe")
    if not exe:
        print(
            f"ensure: no exe in registry row {name!r}; "
            f"add it with: fakescan-ctl add --name {name} --exe PATH",
            file=sys.stderr,
        )
        return 1

    exe_path = Path(exe)
    if not exe_path.exists():
        print(f"ensure: exe not found: {exe_path}", file=sys.stderr)
        return 1

    # Build args from registry row.
    cmd_args = [str(exe_path)]
    cmd_args += ["--listen", listen]
    cmd_args += ["--device-uuid", row.get("device_uuid", DEFAULT_UUID)]
    cmd_args += ["--lab-name", name]
    if row.get("profile"):
        cmd_args += ["--profile", row["profile"]]
    if row.get("advertise"):
        cmd_args += ["--advertise", row["advertise"]]

    # Start without redirecting stdout (long-running server).
    if os.name == "nt":
        import subprocess
        subprocess.Popen(cmd_args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        import subprocess
        subprocess.Popen(cmd_args, start_new_session=True)

    print(f"started fakescan/{name} on :{port} — pid pending")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def _api_error_text(e: httpx.HTTPStatusError) -> str:
    try:
        return str(e.response.json().get("error") or e.response.text.strip())
    except Exception:
        return e.response.text.strip() or str(e)


def main(  # noqa: C901
    argv: list[str] | None = None,
    *,
    manager: Any = None,
    opener: Callable[[str], Any] | None = None,
) -> int:
    """CLI entry. ``manager`` (a Manager-like object) and ``opener`` (browser / settings opener)
    are injection points for tests; by default a :class:`fakescan.Manager` for ``--base-url``."""
    p = argparse.ArgumentParser(
        prog="fakescan-ctl",
        description="fakescan control-plane CLI + registry manager",
        epilog=(
            "Registry: %LOCALAPPDATA%\\fakescan\\devices.json  "
            "| Port band: even (3920, 3922, …)  "
            "| Never touches fakeprint"
        ),
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, metavar="URL",
                   help="fakescan HTTP base URL (default %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ── device commands ───────────────────────────────────────────────────────
    st_p = sub.add_parser("status", help="device status table (--raw: GET /v1/ctl + /v1/status JSON)")
    st_p.add_argument("--raw", action="store_true")
    st_p.add_argument("--width", type=int, default=None)
    dv_p = sub.add_parser("devices", help="device status table")
    dv_p.add_argument("--width", type=int, default=None)
    sub.add_parser("ui", help="open <base-url>/ui in the default browser")
    sub.add_parser("profiles", help="list profiles (GET /v1/profiles; local files if the route is absent)")
    sub.add_parser("open-settings", help="open the Windows printers settings page (Add device)")

    ctl_p = sub.add_parser("ctl", help="POST /v1/ctl {cmd, args}")
    ctl_p.add_argument("ctl_cmd", metavar="CMD", help="ctl command name")
    ctl_p.add_argument("--args", metavar="JSON", default=None,
                       help="args JSON object, e.g. '{\"count\":3}'")

    pages_p = sub.add_parser("pages", help="POST /v1/ctl pages")
    pages_p.add_argument("count", type=int)
    pages_p.add_argument("--side", choices=("front", "back"))
    pages_p.add_argument("--order", choices=("forward", "reverse"))

    pat_p = sub.add_parser("pattern", help="POST /v1/ctl pattern")
    pat_p.add_argument("id", choices=(
        "page_id", "wedges", "patches", "slant_f", "corners",
        "duplex_pair", "qr", "wia_plain",
    ))

    scen_p = sub.add_parser("scenario", help="POST /v1/ctl scenario")
    scen_p.add_argument("id", choices=("order", "qr", "tone", "color", "orient", "crop", "duplex"))

    fault_p = sub.add_parser("fault", help="POST /v1/ctl fault")
    fault_p.add_argument("--jam", type=_bool, default=None)
    fault_p.add_argument("--cover-open", type=_bool, default=None, dest="cover_open")
    fault_p.add_argument("--multi-feed", type=_bool, default=None, dest="multi_feed")
    fault_p.add_argument("--lamp-error", type=_bool, default=None, dest="lamp_error")
    fault_p.add_argument("--busy", type=_bool, default=None)
    fault_p.add_argument("--offline", type=_bool, default=None)
    fault_p.add_argument("--ms", type=int, default=None)
    fault_p.add_argument("--jam-after-n", type=int, default=None, dest="jam_after_n")
    fault_p.add_argument("--empty-mid-job", type=int, default=None, dest="empty_mid_job")
    fault_p.add_argument("--offline-after-n", type=int, default=None, dest="offline_after_n")
    fault_p.add_argument("--multi-feed-after-n", type=int, default=None, dest="multi_feed_after_n")
    fault_p.add_argument("--abort-retrieve", type=_bool, default=None, dest="abort_retrieve")
    fault_p.add_argument("--abort-retrieve-after-n", type=int, default=None, dest="abort_retrieve_after_n")
    fault_p.add_argument("--abort-retrieve-bytes", type=int, default=None, dest="abort_retrieve_bytes")
    fault_p.add_argument("--lamp-warmup-ms", type=int, default=None, dest="lamp_warmup_ms")

    sub.add_parser("reset", help="POST /v1/ctl reset")

    save_p = sub.add_parser("save", help="POST /v1/ctl save (write running state to registry)")
    save_p.add_argument("--name", default=None,
                        help="lab-name (use FAKESCAN_NAME env or --lab-name on the running process)")

    # ── named-pipe command ────────────────────────────────────────────────────
    pipe_p = sub.add_parser("pipe", help="Send JSONL ctl via \\\\.\pipe\\fakescan-<name> (Windows)")
    pipe_p.add_argument("ctl_cmd", metavar="CMD")
    pipe_p.add_argument("--args", metavar="JSON", default=None)
    pipe_p.add_argument("--name", default="default", metavar="NAME")

    # ── registry commands ─────────────────────────────────────────────────────
    sub.add_parser("list", help="List devices.json registry rows")

    add_p = sub.add_parser(
        "add", help="ADD ID --profile P: add a device to the running server; "
        "legacy: add --name NAME: write a registry row")
    add_p.add_argument("id", nargs="?", default=None, metavar="ID")
    add_p.add_argument("--name", default=None, metavar="NAME",
                       help="friendly name with ID; registry row name without ID (legacy)")
    add_p.add_argument("--listen", metavar="HOST:PORT")
    add_p.add_argument("--advertise", metavar="IP", default="")
    add_p.add_argument("--uuid", metavar="UUID", default=None)
    add_p.add_argument("--profile", default=None, help="profile id (default brother-wsd)")
    add_p.add_argument("--exe", default=None, metavar="PATH")
    add_p.add_argument("--autostart", action="store_true")

    rem_p = sub.add_parser(
        "remove", help="REMOVE ID [--forget]: stop a device on the running server; "
        "legacy: remove --name NAME: delete a registry row")
    rem_p.add_argument("id", nargs="?", default=None, metavar="ID")
    rem_p.add_argument("--forget", action="store_true", help="also drop the registry row")
    rem_p.add_argument("--name", default=None, metavar="NAME", help="legacy registry row name")

    ma_p = sub.add_parser("mark-added", help="Mark a registry row as PnP-added")
    ma_p.add_argument("--name", default="default", metavar="NAME")
    ma_p.add_argument("--pnp", dest="pnp", action="store_true", default=True)
    ma_p.add_argument("--no-pnp", dest="pnp", action="store_false")

    ens_p = sub.add_parser("ensure", help="Check if running; start from registry if not")
    ens_p.add_argument("--name", default="default", metavar="NAME")
    ens_p.add_argument("--all", dest="all_", action="store_true",
                       help="Ensure all registry rows with autostart=true")

    args = p.parse_args(argv)
    base = args.base_url.rstrip("/")

    def mgr() -> Any:
        return manager if manager is not None else Manager(base)

    try:
        # ── manager / UI commands ─────────────────────────────────────────────
        if args.cmd in ("devices", "status") and not getattr(args, "raw", False):
            rows = mgr().status_table()
            print(format_status_table(rows, args.width))
            print(f"summary: {summary_level(rows)}")
            return 0

        if args.cmd == "ui":
            print(mgr().open_ui(opener))
            return 0

        if args.cmd == "profiles":
            for pr in mgr().profiles():
                cap = pr.feeder.capacity
                feeder = "no feeder" if not (pr.feeder.media_sizes or cap or pr.feeder.duplex) else (
                    f"capacity={cap if cap is not None else '-'} media={','.join(pr.feeder.media_sizes) or '-'}"
                    f"{' duplex' if pr.feeder.duplex else ''}")
                print(f"{pr.id:<18} {pr.connection:<5} {','.join(pr.formats):<30} {feeder}")
            return 0

        if args.cmd == "open-settings":
            print(mgr().open_add_device_settings(opener))
            return 0

        if args.cmd == "add" and args.id is not None:
            dev = mgr().add(args.id, args.profile or "brother-wsd", device_uuid=args.uuid,
                            friendly_name=args.name)
            print(f"added: {dev.id} profile={dev.profile} uuid={dev.uuid}")
            return 0

        if args.cmd == "add" and args.name is None:
            p.error("add: give a device ID (runtime add) or --name NAME (legacy registry row)")

        if args.cmd == "remove" and args.id is not None:
            mgr().remove(args.id, forget=args.forget)
            print(f"removed: {args.id}" + (" (forgotten)" if args.forget else ""))
            return 0

        if args.cmd == "remove" and args.name is None:
            p.error("remove: give a device ID or --name NAME (legacy registry row)")

        # ── device commands ───────────────────────────────────────────────────
        if args.cmd == "status":
            info = ctl_get(base)
            with httpx.Client(timeout=5.0) as c:
                st = c.get(f"{base}/v1/status").json()
            print("ctl info:", json.dumps(info, indent=2))
            print("status:", json.dumps(st, indent=2))
            return 0

        if args.cmd == "ctl":
            ctl_args = json.loads(args.args) if args.args else None
            result = ctl_post(base, args.ctl_cmd, ctl_args)
            print(json.dumps(result, indent=2))
            return 0

        if args.cmd == "pages":
            a: dict[str, Any] = {"count": args.count}
            if args.side:
                a["side"] = args.side
            if args.order:
                a["order"] = args.order
            print(json.dumps(ctl_post(base, "pages", a), indent=2))
            return 0

        if args.cmd == "pattern":
            print(json.dumps(ctl_post(base, "pattern", {"id": args.id}), indent=2))
            return 0

        if args.cmd == "scenario":
            print(json.dumps(ctl_post(base, "scenario", {"id": args.id}), indent=2))
            return 0

        if args.cmd == "fault":
            fa: dict[str, Any] = {}
            if args.jam is not None:
                fa["jam"] = args.jam
            if args.cover_open is not None:
                fa["cover_open"] = args.cover_open
            if args.multi_feed is not None:
                fa["multi_feed"] = args.multi_feed
            if args.lamp_error is not None:
                fa["lamp_error"] = args.lamp_error
            if args.busy is not None:
                fa["busy"] = args.busy
            if args.offline is not None:
                fa["offline"] = args.offline
            if args.ms is not None:
                fa["ms"] = args.ms
            if args.jam_after_n is not None:
                fa["jam_after_n"] = args.jam_after_n
            if args.empty_mid_job is not None:
                fa["empty_mid_job"] = args.empty_mid_job
            if args.offline_after_n is not None:
                fa["offline_after_n"] = args.offline_after_n
            if args.multi_feed_after_n is not None:
                fa["multi_feed_after_n"] = args.multi_feed_after_n
            if args.abort_retrieve is not None:
                fa["abort_retrieve"] = args.abort_retrieve
            if args.abort_retrieve_after_n is not None:
                fa["abort_retrieve_after_n"] = args.abort_retrieve_after_n
            if args.abort_retrieve_bytes is not None:
                fa["abort_retrieve_bytes"] = args.abort_retrieve_bytes
            if args.lamp_warmup_ms is not None:
                fa["lamp_warmup_ms"] = args.lamp_warmup_ms
            print(json.dumps(ctl_post(base, "fault", fa), indent=2))
            return 0

        if args.cmd == "reset":
            print(json.dumps(ctl_post(base, "reset"), indent=2))
            return 0

        if args.cmd == "save":
            result = ctl_post(base, "save")
            print(json.dumps(result, indent=2))
            return 0

        if args.cmd == "pipe":
            pipe_args = json.loads(args.args) if args.args else None
            result = pipe_ctl(args.name, args.ctl_cmd, pipe_args)
            print(json.dumps(result, indent=2))
            return 0

        # ── registry commands ─────────────────────────────────────────────────
        if args.cmd == "list":
            reg = load_registry()
            devices = reg.get("devices", [])
            print(f"Registry: {registry_path()}")
            if not devices:
                print("  (no devices)")
            for d in devices:
                pnp = "added" if d.get("added", {}).get("pnp") else "not-added"
                print(
                    f"  {d['name']:<14} listen={d.get('listen','?'):<18} "
                    f"uuid={d.get('device_uuid','?')[-4:]}  "
                    f"profile={d.get('profile','?'):<12} {pnp}"
                )
            return 0

        if args.cmd == "add":
            reg = load_registry()
            listen = args.listen or f"0.0.0.0:{next_port(reg)}"
            port = int(listen.split(":")[-1]) if ":" in listen else 3920
            validate_port(port)
            uuid = args.uuid or DEFAULT_UUID
            args.profile = args.profile or "brother-wsd"
            row: dict[str, Any] = {
                "name": args.name,
                "listen": listen,
                "advertise": args.advertise,
                "device_uuid": uuid,
                "profile": args.profile,
                "autostart": args.autostart,
                "added": {"pnp": False},
            }
            if args.exe:
                row["exe"] = args.exe
            # Upsert
            devices = reg.get("devices", [])
            replaced = False
            for i, d in enumerate(devices):
                if d.get("name") == args.name:
                    devices[i] = row
                    replaced = True
                    break
            if not replaced:
                devices.append(row)
            reg["devices"] = devices
            save_registry(reg)
            print(f"{'updated' if replaced else 'added'}: {args.name} listen={listen}")
            return 0

        if args.cmd == "remove":
            reg = load_registry()
            before = len(reg.get("devices", []))
            reg["devices"] = [d for d in reg.get("devices", []) if d.get("name") != args.name]
            if len(reg["devices"]) < before:
                save_registry(reg)
                print(f"removed: {args.name}")
            else:
                print(f"not found: {args.name}")
            return 0

        if args.cmd == "mark-added":
            reg = load_registry()
            row = find_device(reg, args.name)
            if row is None:
                print(f"error: no registry row {args.name!r}", file=sys.stderr)
                return 1
            row.setdefault("added", {})["pnp"] = args.pnp
            save_registry(reg)
            print(f"mark-added: {args.name} pnp={args.pnp}")
            return 0

        if args.cmd == "ensure":
            reg = load_registry()
            if args.all_:
                names = [
                    d["name"]
                    for d in reg.get("devices", [])
                    if d.get("autostart")
                ]
                if not names:
                    print("no autostart rows in registry")
                    return 0
                rc = 0
                for n in names:
                    if cmd_ensure(n, reg) != 0:
                        rc = 1
                return rc
            return cmd_ensure(args.name, reg)

    except httpx.ConnectError:
        print(f"error: cannot connect to fakescan at {base}", file=sys.stderr)
        print("  check: fakescan-ctl list  (then ensure --name default)", file=sys.stderr)
        return 1
    except ApiError as e:
        print(f"error: {_api_error_text(e)} (HTTP {e.response.status_code})", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as e:
        print(f"error: HTTP {e.response.status_code}: {e.response.text}", file=sys.stderr)
        return 1

    return 0


def _bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected bool, got {s!r}")


if __name__ == "__main__":
    raise SystemExit(main())
