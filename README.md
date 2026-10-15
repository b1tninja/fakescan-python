# fakescan-python

Python package **`fakescan`**: bindings for [fakescan](https://github.com/b1tninja/fakescan), the fake WSD scanner. It carries two things:

- **`fakescan.Server`** (native, PyO3): the lab server running *in your process* on an ephemeral port. No `cargo run`, no port 3920, no stale instance. It runs the same `fakescan::server::serve` the `fakescan` binary runs.
- **The control-plane client and SOAP helpers** (`Client`, `acquire_pages`, `body_problems`, `fakescan-ctl`, `fakescan-lab`, …) that used to live in `fakescan/python`.

| Repo | Role |
|------|------|
| **fakescan** | Rust: fake WSD scanner, `/v1` control plane, `fakescan::server` |
| **fakescan-python** | This repo: PyO3 bindings + Python client |
| **pdfcraft-fakescan** | Harness: arms fakescan, drives pdfcraft, asserts outcomes |

## Layout

Clone **fakescan** beside this repository (the build uses it as a path dependency, and tests read its profiles and WSD schema):

```text
parent/
  fakescan/          # Rust server (path dependency)
  fakescan-python/   # this repo
```

`FAKESCAN_REPO` overrides where the sibling checkout is; `FAKESCAN_PROFILES` overrides the profiles directory.

## Install (development)

```powershell
cd D:\code\fakescan-python
python -m venv .venv
.venv\Scripts\activate
pip install -U pip maturin pytest pillow httpx
maturin develop
pytest -m "not live"
```

## Run the server in-process

```python
from fakescan import Server
from fakescan.soap import acquire_jfif_pages

with Server(profile="brother-wsd") as srv:      # 127.0.0.1, ephemeral port
    srv.client().set_pages(3)                   # the existing HTTP Client, bound to this server
    pages = acquire_jfif_pages(srv.base_url, page_count=3)
    assert len(pages) == 3
```

`Server(devices=None, *, profile, profiles, host, port, device_uuid, wsd, discovery, advertise, initial_pages)`: `port=0` picks one; `wsd` serves `ScannerService`; `discovery` (WS-Discovery announce) is **off** by default so a test server never advertises a scanner on the LAN. With no `devices` there is one `default` device (`profile`, `device_uuid`).

## Several scanners in one server

One server emulates N scanners, each with its own profile, UUID, feeder/tray and state:

```python
from fakescan import Server, Device
from fakescan.soap import acquire_pages

with Server(devices=[Device("tray5", profile="feeder-tray5"),
                     Device("tray50", profile="feeder-tray50", friendly_name="Big tray")]) as srv:
    h = srv.devices["tray5"]                 # DeviceHandle: id, uuid, profile, friendly_name,
    h.client().set_pages(5)                  #   scan_endpoint, device_endpoint, client()
    pages = acquire_pages(h.client().base_url, page_count=5)   # scoped base -> /d/tray5/ScannerService
    h.client().set_pages(6)                  # ApiError (HTTP 409): the tray holds 5 sheets
```

- `Device(id, profile, device_uuid=None, friendly_name=None, initial_pages=None)`; the first device is the default: `srv.client()` and the legacy `/v1/*`, `/ScannerService` paths address it. Other devices live under `/d/{id}/...`.
- Paper. Every fake scanner starts with an EMPTY feeder (`status().pages_remaining == 0`), so a feeder scan fails until paper is loaded. Be explicit in tests: `c.load_paper(6)` (= `set_pages(6)` + `set_feeder(True)`) and `c.empty_tray()` (= `set_pages(0)`). Or start loaded: `Device("t", "feeder-tray5", initial_pages=5)` / `Server(initial_pages=6)` (a device's own value wins; clamped to the tray capacity). The initial stack is loaded when the device is created and again on every `c.reset()`, `set_pages` still overrides it at run time, and `c.status().initial_pages` reports it. It is runtime state: it never changes the capability fingerprint or marks an installed device stale. `Client.add_device(..., initial_pages=)` and `Manager.add(..., initial_pages=)` take it too.
- `Client(base_url, device="tray5")` scopes every control call to `/d/tray5/v1/*`, and its `base_url` is `{base}/d/tray5`, so the `fakescan.soap` helpers work unchanged. `Client.devices()`, `add_device(id, profile, ...)`, `remove_device(id)` manage the fleet of any server.
- Faults: `Client.set_fault(jam=, cover_open=, interlock_open=, ..., cover_open_after_n=N, jam_after_n=N, ...)` for the classic toggles and counters. For interruptions at a chosen moment use the fault plan: `Client.inject_fault(kind, when, sticky=, one_shot=)` with `when` = `"now"` (also hits a running job), `after_pages(N)` (between pages), `mid_page(P, fraction=0.5)` or `mid_page(P, bytes=B)` (page P's stream is cut, that page is not delivered), or `after_ms(T)`; `Client.fault_plan()` lists entries (fired or pending), `Client.clear_fault_plan()` drops them. Kinds: jam, cover_open, interlock_open, multi_feed, empty, offline, lamp_error, busy, abort. Semantics: fakescan `docs/defects.md`.
- HTTP failures raise `fakescan.ApiError` (an `httpx.HTTPStatusError` carrying `status_code` and the server's message): 409 for a tray over capacity or a duplicate device id/UUID, 400 for an unknown profile, 404 for an unknown or removed device.

### Manager-style runtime add/remove

fakescan is a device manager: devices come and go without restarting the server (a restart breaks Windows' PnP association). In process:

```python
h = srv.add_device(Device("duplex", profile="feeder-duplex30"))   # POST /v1/devices
...
srv.remove_device("duplex")                                       # DELETE /v1/devices/duplex
```

`srv.devices` stays in sync. Against an external server use `Client(url).add_device(...)` / `remove_device(...)`.

## Manager and the device registry

`fakescan.Manager(base_url | Client | Server)` is the typed client for the daemon's manager surface
(`fakescan --manage`, or any in-process `Server`, whose `srv.manager` is one):

```python
from fakescan import Manager
m = Manager("http://127.0.0.1:3920")
m.devices()                                  # [ManagedDevice]: id, uuid, profile, install, config, needs_readd(+_reason)
m.add("tray5", "feeder-tray5")               # POST /v1/devices (device_uuid=, friendly_name= optional)
m.remove("tray5", forget=False)              # keeps the registry row unless forget=True
m.reconfigure("tray5", "feeder-tray50")      # installed + published capabilities changed => stale / needs_readd
m.installed("tray5", wia_id, wia_name, wia_port)   # refused (ApiError 400) unless wia_port contains the UUID
m.forget("tray5")                            # drop install info, keep the row
m.reconcile(wia_scanners)                    # -> ReconcileResult(installed, stale, foreign)
m.reconcile_from_windows()                   # lists WIA scanners read-only via pdfcraft.scan.list_scanners
m.registry(); m.wait_installed("tray5", timeout=60)
```

Errors are `fakescan.ApiError` (400 bad profile / port without the UUID, 404 unknown device, 409 duplicate).
A WIA scanner is matched to a device **only** by the UUID in its `port` string (exactly one distinct UUID,
case/brace/`urn:uuid:` tolerant); names are never used. Fake devices are recognised by the UUID prefix `fakescan.FAKE_UUID_PREFIX` (`00000000-0000-4000-8000-`; `fakescan.is_fake_uuid(value)`, case/brace/`urn:uuid:` tolerant): the server (`POST /v1/devices` and `Device(device_uuid=...)`) refuses a UUID outside it with a 400, and a device without an explicit UUID gets a random one inside it. Anything else is `foreign` and is never touched
(`fakescan.match_wia_to_devices(devices, wia_scanners)` is the pure version of that rule; rows with
several UUIDs are withheld from the daemon by `Manager.reconcile` and returned as foreign).
`reconcile_from_windows` never modifies anything on Windows and accepts an injected `list_scanners=` callable.

`fakescan.registry` has typed, leniently parsed dataclasses (`Registry`, `DeviceRow`, `Install`, `Config`,
`Wia`, ...; unknown fields stay in `.extra`; legacy `added.pnp` rows load as `installed` with note
`legacy added.pnp`), `load(path=None)` (reads `devices.json` directly, read-only, retries transient
errors, never takes the `.lock`), and `from_server(client)` (`GET /v1/registry`). The default path follows
`registry.rs`: `$FAKESCAN_REGISTRY`, else `%LOCALAPPDATA%akescan\devices.json` on Windows,
`~/.local/share/fakescan/devices.json` elsewhere.

**Python never writes the registry file.** All mutation goes through the daemon's HTTP API so the Rust
file lock and atomic writer remain the single writer. Embedded `Server`s keep install state in memory
only (no registry file, no persistence after `stop()`, no discovery).

An in-process server is for SOAP and control-plane tests. Windows WIA needs a device *Added in Imaging*, which means the long-lived `fakescan` binary with discovery on and its stable lab UUID. Keep using that for `pdfcraft-fakescan`'s `pdfcraft_wia` tests.

## Device manager UI and tray (Python parity)

The GUI itself is **the daemon's**: a web page at `GET /ui` (loopback-only) and, on Windows, a tray
icon hosted inside `fakescan --manage` (cargo feature `tray`; `--no-tray` disables it). Python ships no
GUI of its own; it gives you the same data and actions:

```python
from fakescan import Manager, format_status_table, summary_level, tray
m = Manager("http://127.0.0.1:3920")
m.profiles()                    # [ProfileInfo(id, connection, formats, feeder=FeederInfo(capacity, media_sizes, duplex), description)]
rows = m.status_table()         # [StatusRow(id, friendly_name, uuid, profile, install_state, needs_readd, reason,
                                #            pages_remaining, feeder_capacity, faults, scan_endpoint,
                                #            device_endpoint, last_peer)]
print(format_status_table(rows, width=100)); summary_level(rows)   # 'green' | 'amber' | 'red'
m.summary_level()               # same, but 'red' when the daemon is unreachable
m.open_ui()                     # webbrowser.open(base_url + "/ui"); returns the URL (opener injectable)
m.open_add_device_settings()    # 'daemon' | 'startfile' | 'unavailable' (never raises for a missing route)
srv.open_ui()                   # embedded Server: same router, same /ui
tray.ensure_daemon()            # 'attached' | 'started'
```

* `profiles()` reads `GET /v1/profiles`; a daemon without that route (404) falls back, best effort, to
  the `*.json` in `FAKESCAN_PROFILES` / the sibling repo's `profiles/` (`ProfileInfo.source == "files"`).
* `open_add_device_settings()` asks a *loopback* daemon to open `ms-settings:printers` in its own
  session (`POST /v1/ctl {cmd:"open_settings",page:"printers"}`); otherwise, on Windows, it calls the
  injectable `os.startfile('ms-settings:printers')`.
* Levels follow the tray: green = every device installed, amber = something is new/probed/stale or
  needs a re-Add, red = the daemon is unreachable or errors.
* `fakescan.tray.ensure_daemon(base_url, exe=None, args=(), opener=subprocess.Popen)`: the tray lives
  in the daemon, so this ensures `fakescan --manage` is running: `GET /health` first (attach-first,
  idempotent, never kills anything), else it spawns the exe (explicit arg, `FAKESCAN_EXE`, PATH,
  `D:\temp\fakescan-daemon`, `$CARGO_TARGET_DIR` / `$FAKESCAN_REPO/target/{debug,release}`) detached with
  no console window and waits for `/health`. Embedded `Server`s never show a tray.
* `fakescan-ctl` subcommands (each takes `--base-url`): `ui`, `devices` / `status [--raw]` (table),
  `add ID --profile P [--uuid U] [--name FRIENDLY]`, `remove ID [--forget]`, `profiles`,
  `open-settings`. Errors print the server's message and exit 1. The older registry-row forms
  `add --name N ...` / `remove --name N` (no positional ID) and `status --raw` behave as before.

## Tests

```powershell
pytest -m embedded          # native Server, no external process
pytest -m "not live"        # + local matrix (builds and starts the fakescan binary)
pytest -m live              # needs a running fakescan on FAKESCAN_BASE_URL (default :3920)
```

The local matrix honors `CARGO_TARGET_DIR`.

## Related

- **pdfcraft-fakescan**: `pytest -m pdfcraft_wia` (pdfcraft acquire through WIA)
- fakescan docs: `docs/lab-harness.md`, `docs/test-matrix.md`

## License

MIT OR Apache-2.0.


## The `diag` test page: layout, rendering, analyzer

`fakescan.diag` exposes the ground-truth layout of the `diag` page (layout v2: print marks, rulers, resolution, colour and
geometry targets; sizes and expected values in mm), renders it, and analyzes a scanned or printed-back raster per element
(scale x/y, rotation, shear, margin offsets and clipped frame edges, ruler dpi, colour error, hue error, last resolvable
pitch, Sierpinski level, ...):

```python
from fakescan import diag
layout = diag.diag_page_layout(100, paper="A4")        # layout v3, any sheet size (paper="LETTER", "210x297", ...)
report = diag.analyze_diag_page(scan_image, layout)   # needs Pillow
vector = diag.diag_page_primitives(layout)            # mm primitives for a vector PDF
layout_v2 = diag.diag_page_layout(100, version=2)     # the fixed-size layout (blind pattern analyzer)
```

Layout and report format: `docs/diag-page-v3.md` (default, parametric) and `docs/diag-page-v2.md` in the fakescan repo. Needs the native module built with
`maturin develop`.
