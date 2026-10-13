"""fakescan-mcp — attach-only MCP server for a running fakescan instance.

Connects to a running fakescan at --base-url (default http://127.0.0.1:3920)
and exposes MCP tools for controlling it.  This server **never** starts, stops,
or kills the fakescan binary.  If the device is not reachable, tools fail fast.

Install the optional dep first:
    pip install -e "./python[mcp]"

Then add to your Cursor / Claude MCP config (stdio):
    {
      "command": "fakescan-mcp",
      "args": ["--base-url", "http://192.168.1.10:3920"]
    }

Or start directly:
    fakescan-mcp --base-url http://127.0.0.1:3920
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Fail fast if mcp package is not installed.
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print(
        "error: mcp package not installed.\n"
        "  pip install -e './python[mcp]'\n"
        "  (or: pip install mcp)",
        file=sys.stderr,
    )
    sys.exit(1)

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:3920"

# ── helpers ───────────────────────────────────────────────────────────────────


def _ctl_post(base: str, cmd: str, args: dict[str, Any] | None = None) -> Any:
    body: dict[str, Any] = {"cmd": cmd}
    if args:
        body["args"] = args
    with httpx.Client(timeout=15.0) as c:
        r = c.post(f"{base}/v1/ctl", json=body)
        r.raise_for_status()
        return r.json()


def _ctl_get(base: str) -> Any:
    with httpx.Client(timeout=5.0) as c:
        r = c.get(f"{base}/v1/ctl")
        r.raise_for_status()
        return r.json()


def _check_alive(base: str) -> None:
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{base}/health")
            if r.status_code != 200 or r.text.strip() != "ok":
                raise RuntimeError(f"fakescan /health returned unexpected: {r.text!r}")
    except httpx.ConnectError:
        raise RuntimeError(
            f"fakescan not reachable at {base}\n"
            "  Start it first: cargo run -p fakescan -- --lab-name default\n"
            "  Or: fakescan-ctl ensure --name default"
        )


# ── server ────────────────────────────────────────────────────────────────────


def build_server(base_url: str) -> Server:
    base = base_url.rstrip("/")
    server = Server("fakescan-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="ctl_info",
                description=(
                    "GET /v1/ctl — return the running fakescan's capabilities and identity "
                    "(port, UUID, lab-name, supported commands).  "
                    "Caveat: fails fast if fakescan is not running; never spawns it."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="status",
                description=(
                    "GET /v1/status — detailed device state: profile, feeder, pages, "
                    "pattern, effects, faults, loopback, WSD counters.  "
                    "Caveat: a reading is evidence, not a pin."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="reset",
                description=(
                    "POST /v1/ctl reset — reset fakescan to post-start defaults "
                    "(clears pages, loopback, faults, effects, scenario, slow).  "
                    "Caveat: does not restart the process."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="pages",
                description=(
                    "POST /v1/ctl pages — load the feeder with synthetic pages.  "
                    "Caveat: clears any active loopback store."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["count"],
                    "properties": {
                        "count": {"type": "integer", "minimum": 0, "maximum": 30},
                        "side": {"type": "string", "enum": ["front", "back"]},
                        "order": {"type": "string", "enum": ["forward", "reverse"]},
                    },
                },
            ),
            Tool(
                name="pattern",
                description="POST /v1/ctl pattern — set the synthetic image pattern.",
                inputSchema={
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {
                            "type": "string",
                            "enum": [
                                "page_id", "wedges", "patches", "slant_f",
                                "corners", "duplex_pair", "qr", "wia_plain",
                            ],
                        },
                    },
                },
            ),
            Tool(
                name="scenario",
                description="POST /v1/ctl scenario — apply a tester progression step.",
                inputSchema={
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {
                            "type": "string",
                            "enum": ["order", "qr", "tone", "color", "orient", "crop", "duplex"],
                        },
                    },
                },
            ),
            Tool(
                name="fault",
                description=(
                    "POST /v1/ctl fault — set/clear faults (jam, cover_open, slow, …).  "
                    "Omit a key to leave its current value unchanged."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "jam": {"type": "boolean"},
                        "cover_open": {"type": "boolean"},
                        "multi_feed": {"type": "boolean"},
                        "lamp_error": {"type": "boolean"},
                        "busy": {"type": "boolean"},
                        "offline": {"type": "boolean"},
                        "ms": {"type": "integer"},
                        "jam_after_n": {"type": "integer"},
                        "empty_mid_job": {"type": "integer"},
                        "offline_after_n": {"type": "integer"},
                        "multi_feed_after_n": {"type": "integer"},
                        "abort_retrieve": {"type": "boolean"},
                        "abort_retrieve_after_n": {"type": "integer"},
                        "abort_retrieve_bytes": {"type": "integer"},
                        "lamp_warmup_ms": {"type": "integer"},
                    },
                },
            ),
            Tool(
                name="ctl",
                description=(
                    "POST /v1/ctl — send any named ctl command.  "
                    "Use ctl_info to see available commands.  "
                    "Caveat: prefer the typed tools above when possible."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["cmd"],
                    "properties": {
                        "cmd": {"type": "string"},
                        "args": {"type": "object"},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        _check_alive(base)

        try:
            if name == "ctl_info":
                result = _ctl_get(base)
            elif name == "status":
                result = _ctl_post(base, "status")
            elif name == "reset":
                result = _ctl_post(base, "reset")
            elif name == "pages":
                result = _ctl_post(base, "pages", arguments)
            elif name == "pattern":
                result = _ctl_post(base, "pattern", arguments)
            elif name == "scenario":
                result = _ctl_post(base, "scenario", arguments)
            elif name == "fault":
                result = _ctl_post(base, "fault", arguments)
            elif name == "ctl":
                cmd = arguments.pop("cmd")
                args_val = arguments.pop("args", None)
                result = _ctl_post(base, cmd, args_val)
            else:
                return [TextContent(type="text", text=f"unknown tool: {name}")]

        except httpx.HTTPStatusError as e:
            return [TextContent(
                type="text",
                text=f"fakescan error {e.response.status_code}: {e.response.text}",
            )]
        except RuntimeError as e:
            return [TextContent(type="text", text=str(e))]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fakescan-mcp",
        description=(
            "Attach-only MCP server for a running fakescan instance.  "
            "Never spawns or kills the device binary."
        ),
    )
    p.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        metavar="URL",
        help="fakescan HTTP base URL (default %(default)s)",
    )
    args = p.parse_args(argv)
    base = args.base_url.rstrip("/")

    # Fail fast if the device is not reachable at startup.
    try:
        _check_alive(base)
    except RuntimeError as e:
        print(f"fakescan-mcp: {e}", file=sys.stderr)
        return 1

    import asyncio

    async def run() -> None:
        srv = build_server(base)
        async with stdio_server() as (r, w):
            await srv.run(r, w, srv.create_initialization_options())

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
