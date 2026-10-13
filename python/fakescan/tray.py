"""Make sure the fakescan daemon, which carries the tray icon, is running.

The tray lives INSIDE the daemon (``fakescan --manage``): there is no separate tray process.
It shows only on Windows, in an interactive user session, in a daemon built with the ``tray``
cargo feature; ``fakescan --manage --no-tray`` disables it. An embedded :class:`fakescan.Server`
never shows a tray. So "start the tray" means "ensure the daemon is up":

>>> from fakescan import tray
>>> tray.ensure_daemon()        # doctest: +SKIP
'attached'                      # already answering /health; nothing was spawned

Attach-first and idempotent. This module never kills or restarts anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

import httpx

from . import _paths
from .client import DEFAULT_BASE_URL
from .manager import is_loopback_url

EXE_ENV = "FAKESCAN_EXE"
KNOWN_DAEMON_DIR = Path(r"D:\temp\fakescan-daemon")


def _exe_name() -> str:
    return "fakescan.exe" if os.name == "nt" else "fakescan"


def health(base_url: str, timeout: float = 1.0) -> bool:
    """True when ``GET {base_url}/health`` answers 200 ``ok``."""
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
        return r.status_code == 200 and r.text.strip() == "ok"
    except httpx.HTTPError:
        return False


def find_daemon_exe(exe: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate fakescan: explicit ``exe``, ``$FAKESCAN_EXE``, PATH, ``D:\temp\fakescan-daemon``,
    then ``$CARGO_TARGET_DIR`` and ``$FAKESCAN_REPO/target`` (``debug`` then ``release``).
    An explicit ``exe`` that does not exist is an error (``FileNotFoundError``), not a fallthrough."""
    name = _exe_name()
    if exe:
        p = Path(exe)
        if not p.is_file():
            raise FileNotFoundError(f"fakescan executable not found: {p}")
        return p
    env = os.environ.get(EXE_ENV)
    if env and Path(env).is_file():
        return Path(env)
    on_path = shutil.which("fakescan")
    if on_path:
        return Path(on_path)
    candidates = [KNOWN_DAEMON_DIR / name]
    for root in (os.environ.get("CARGO_TARGET_DIR"), str(_paths.fakescan_repo() / "target")):
        if root:
            candidates += [Path(root) / "debug" / name, Path(root) / "release" / name]
    return next((c for c in candidates if c.is_file()), None)


def _spawn_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
        kw["close_fds"] = True
    else:
        kw["start_new_session"] = True
    return kw


def ensure_daemon(
    base_url: str = DEFAULT_BASE_URL,
    exe: str | os.PathLike[str] | None = None,
    args: Sequence[str] = (),
    opener: Callable[..., Any] = subprocess.Popen,
    *,
    timeout: float = 20.0,
    health_check: Callable[[str], bool] = health,
    sleep: Callable[[float], Any] = time.sleep,
) -> str:
    """Return ``'attached'`` if a daemon already answers ``/health`` at ``base_url``; otherwise spawn
    ``fakescan --manage [args]`` detached (no console window), wait up to ``timeout`` seconds for
    ``/health`` and return ``'started'``.

    ``opener(cmd_list, **popen_kwargs)`` does the spawning (default ``subprocess.Popen``; inject
    a fake in tests). Raises ``FileNotFoundError`` (no executable found), ``ValueError`` (a
    non-loopback ``base_url`` cannot be started from here) or ``TimeoutError`` (spawned but never
    healthy; the process is left alone). A non-default port is passed as ``--listen 127.0.0.1:PORT``
    unless ``args`` already has ``--listen``.
    """
    if health_check(base_url):
        return "attached"
    if not is_loopback_url(base_url):
        raise ValueError(f"cannot start a daemon for non-local {base_url}")
    path = find_daemon_exe(exe)
    if path is None:
        raise FileNotFoundError(
            f"fakescan executable not found (set {EXE_ENV}, put it on PATH, or build it under "
            f"{_paths.fakescan_repo() / 'target'})"
        )
    cmd = [str(path), "--manage", *args]
    port = urlsplit(base_url).port
    if port and port != 3920 and not any(a == "--listen" or a.startswith("--listen=") for a in args):
        cmd += ["--listen", f"127.0.0.1:{port}"]
    opener(cmd, **_spawn_kwargs())
    deadline = time.monotonic() + timeout
    while True:
        if health_check(base_url):
            return "started"
        if time.monotonic() >= deadline:
            raise TimeoutError(f"daemon spawned ({path}) but {base_url}/health did not answer in {timeout}s")
        sleep(0.25)


start = ensure_daemon  # contract name: ``fakescan.tray.start()``
