"""pytest plugin for :mod:`fakescan.safety`: ``pytest_plugins = ["fakescan.safety_pytest"]`` in a harness conftest.

What it does
------------
* at configure time: reads ``PDFCRAFT_TEST_DEVICE_POLICY`` (+ the legacy deny names), refuses a session whose policy
  is not ``fakes-only``, makes it the active policy and installs the guard over the pdfcraft / fakeprint entry
  points (:func:`fakescan.safety.install`), before any test module is imported;
* the autouse fixture ``device_policy`` yields the active :class:`~fakescan.safety.Policy` and fails a test when the
  guard was removed under it;
* the startup self-check: ``register_target_provider(fn)`` adds a function returning the device references a
  session is about to use (the fakes list of a live daemon, the embedded servers' endpoints ...); after collection
  every target must be allowed, else the WHOLE session stops (``pytest.exit``), before the first test runs;
* the marker ``real_device_ok("<exact device name token>")`` is the only way to run a test against a non-fake: it
  skips unless ``PDFCRAFT_ALLOW_REAL_DEVICE_TESTS`` equals the token exactly. Nothing in the harnesses uses it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any

import pytest

from . import safety

_providers: list[Callable[[], Iterable[Any]]] = []
_installation: safety.Installation | None = None
_session_policy: safety.Policy | None = None


def register_target_provider(fn: Callable[[], Iterable[Any]]) -> None:
    """``fn()`` returns the device references the session will use; they are checked after collection."""
    _providers.append(fn)


def session_policy() -> safety.Policy:
    assert _session_policy is not None, "fakescan.safety_pytest is not active"
    return _session_policy


def pytest_configure(config: pytest.Config) -> None:
    global _installation, _session_policy
    config.addinivalue_line(
        "markers",
        "real_device_ok(token): runs against a non-fake device; skipped unless PDFCRAFT_ALLOW_REAL_DEVICE_TESTS "
        "equals the exact device name token (no harness test uses it)",
    )
    try:
        policy = safety.Policy.from_env()
    except safety.PolicyError as e:
        raise pytest.UsageError(str(e)) from None
    if policy.mode != "fakes-only":
        raise pytest.UsageError("the test harness requires the 'fakes-only' device policy (PDFCRAFT_TEST_DEVICE_POLICY)")
    _session_policy = policy
    safety.set_policy(policy)
    _installation = safety.install(policy)


def pytest_unconfigure(config: pytest.Config) -> None:
    global _installation
    if _installation is not None:
        _installation.uninstall()
        _installation = None


def _marker_token(item: pytest.Item) -> str | None:
    m = item.get_closest_marker("real_device_ok")
    if m is None:
        return None
    token = m.args[0] if m.args else m.kwargs.get("token")
    return token if isinstance(token, str) and token.strip() else ""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    allow = os.environ.get(safety.ALLOW_REAL_ENV, "")
    for it in items:
        token = _marker_token(it)
        if token is None:
            continue
        if not token or allow != token:
            it.add_marker(pytest.mark.skip(reason=f"real_device_ok: set {safety.ALLOW_REAL_ENV} to the exact device name token"))


def pytest_collection_finish(session: pytest.Session) -> None:
    targets: list[Any] = []
    for fn in _providers:
        targets.extend(fn())
    try:
        session_policy().self_check(targets)
    except safety.DeviceNotAllowed as e:
        pytest.exit(f"device safety self-check failed ({e.reason}): a target of this session is not a provably fake device", returncode=3)


@pytest.fixture(autouse=True)
def device_policy(request: pytest.FixtureRequest):
    """The active device policy; the guard must still be installed. A ``real_device_ok`` test (when allowed) runs
    with its one named device let through (deny patterns still win)."""
    base = session_policy()
    assert safety.current_policy() is base, "the device policy was replaced under the test"
    assert _installation is not None, "the device guard is not installed"
    token = _marker_token(request.node)
    if token and os.environ.get(safety.ALLOW_REAL_ENV, "") == token:
        with safety.use_policy(base.with_allowed_names(token)) as p:
            yield p
        return
    yield base
