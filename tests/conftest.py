"""Start one fakescan for the local matrix. Live tests still use an already-running server."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from fakescan import _paths

ROOT = _paths.fakescan_repo()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cargo_target_dir() -> Path:
    """Respect CARGO_TARGET_DIR so we do not run a stale ROOT/target binary."""
    env = os.environ.get("CARGO_TARGET_DIR", "").strip()
    return Path(env) if env else ROOT / "target"


def _ensure_binary() -> Path:
    subprocess.run(
        ["cargo", "build", "-p", "fakescan", "--bin", "fakescan"],
        cwd=ROOT,
        check=True,
    )
    name = "fakescan.exe" if os.name == "nt" else "fakescan"
    binary = _cargo_target_dir() / "debug" / name
    if not binary.is_file():
        raise RuntimeError(f"cargo build did not produce {binary}")
    return binary


@pytest.fixture(scope="session")
def fakescan_base_url(tmp_path_factory) -> str:
    """fakescan on 127.0.0.1 with Scan Service routes and probe listen off.

    A distinct lab name and device UUID keep this process off the installed
    Fake scanner's pipe and WS-Discovery identity.
    """
    port = _free_port()
    binary = _ensure_binary()
    log_path = ROOT / "target" / "local-matrix.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            str(binary),
            "--listen",
            f"127.0.0.1:{port}",
            "--profiles",
            "profiles",
            "--profile",
            "brother-wsd",
            "--lab-name",
            "local-matrix",
            "--device-uuid",
            "00000000-0000-4000-8000-00000000e11e",
            "--no-probe-listen",
        ],
        cwd=ROOT,
        # --lab-name attaches the registry: keep it off the user's real devices.json
        env=dict(os.environ, FAKESCAN_REGISTRY=str(tmp_path_factory.mktemp("matrix-registry") / "devices.json")),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    up = False
    while time.time() < deadline:
        if proc.poll() is not None:
            log.flush()
            raise RuntimeError(f"fakescan exited {proc.returncode}; see {log_path}")
        try:
            response = httpx.get(f"{base}/health", timeout=0.4)
            if response.status_code == 200 and response.text.strip() == "ok":
                up = True
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    if not up:
        proc.terminate()
        log.flush()
        raise RuntimeError(f"fakescan did not answer {base}/health; see {log_path}")
    os.environ["FAKESCAN_BASE_URL"] = base
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def fakescan_binary() -> Path:
    """The built fakescan binary (honors CARGO_TARGET_DIR)."""
    return _ensure_binary()


class Daemon:
    """A `fakescan --manage` process on a free port with a private registry file. No discovery."""

    def __init__(self, binary: Path, registry: Path, log_path: Path, port: int | None = None) -> None:
        self.binary, self.registry, self.log_path = binary, registry, log_path
        self.port = port or _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Daemon":
        env = dict(os.environ, FAKESCAN_REGISTRY=str(self.registry))
        env.pop("FAKESCAN_NAME", None)
        log = self.log_path.open("a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                str(self.binary), "--manage", "--listen", f"127.0.0.1:{self.port}",
                "--no-wsd", "--no-probe-listen", "--profiles", str(_paths.profiles_dir()),
                # distinct pipe name: never the user's `default` pipe
                "--lab-name", f"pytest-manage-{self.port}",
            ],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"fakescan exited {self.proc.returncode}; see {self.log_path}")
            try:
                if httpx.get(f"{self.base_url}/health", timeout=0.4).text.strip() == "ok":
                    return self
            except httpx.HTTPError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"fakescan did not answer; see {self.log_path}")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None
