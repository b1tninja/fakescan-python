"""The pytest plugin (fakescan.safety_pytest) in throw-away sessions: no device, no network."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

FAKE = "00000000-0000-4000-8000-00000000abcd"
REAL = "e3248000-80ce-11db-8000-3c2af435905f"


def run(pytester, *, env=None, conftest="", test="", args=()):
    pytester.makeconftest("pytest_plugins = ['fakescan.safety_pytest']\n" + conftest)
    pytester.makepyfile(test_x=test or "def test_ok(device_policy):\n    assert device_policy.mode == 'fakes-only'\n")
    for k in ("PDFCRAFT_TEST_DEVICE_POLICY", "PDFCRAFT_ALLOW_REAL_DEVICE_TESTS", "CRAFT_SCAN_DENY", "PDFCRAFT_SCAN_DENY",
              "CRAFT_PRINT_DENY", "PDFCRAFT_PRINT_DENY"):
        pytester._monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        pytester._monkeypatch.setenv(k, v)
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs", *args)


def test_default_session_is_fakes_only_with_autouse_fixture(pytester):
    r = run(pytester, test="def test_ok():\n    from fakescan import safety\n    assert safety.current_policy().mode == 'fakes-only'\n")
    r.assert_outcomes(passed=1)


def test_loose_policy_refuses_the_session(pytester):
    r = run(pytester, env={"PDFCRAFT_TEST_DEVICE_POLICY": "deny-only"})
    assert r.ret != 0 and "fakes-only" in r.stderr.str() + r.stdout.str()
    r = run(pytester, env={"PDFCRAFT_TEST_DEVICE_POLICY": "nonsense"})
    assert r.ret != 0


def test_self_check_stops_the_whole_session_before_any_test(pytester):
    conf = (
        "from fakescan.safety_pytest import register_target_provider\n"
        f"register_target_provider(lambda: ['{FAKE}', {{'uuid': '{REAL}', 'name': 'Secret Office'}}])\n"
    )
    r = run(pytester, conftest=conf, test="def test_never():\n    raise SystemExit('ran')\n")
    out = r.stdout.str() + r.stderr.str()
    assert r.ret == 3 and "self-check failed" in out
    assert REAL not in out and "Secret Office" not in out
    assert "1 passed" not in out and "test_never" not in out.replace("test_x.py", "")


def test_self_check_passes_for_fakes_and_loopback(pytester):
    conf = (
        "from fakescan.safety_pytest import register_target_provider\n"
        f"register_target_provider(lambda: ['{FAKE}', 'http://127.0.0.1:5000/p'])\n"
    )
    run(pytester, conftest=conf).assert_outcomes(passed=1)


def test_deny_marker_in_targets_stops_the_session(pytester):
    conf = (
        "from fakescan.safety_pytest import register_target_provider\n"
        f"register_target_provider(lambda: [{{'uuid': '{FAKE}', 'name': 'my brother'}}])\n"
    )
    r = run(pytester, conftest=conf, env={"PDFCRAFT_SCAN_DENY": "brother"})
    assert r.ret == 3


def test_real_device_ok_marker_skips_unless_exact_token(pytester):
    test = (
        "import pytest\n"
        "@pytest.mark.real_device_ok('My Named Device')\n"
        "def test_real(device_policy):\n"
        "    assert device_policy.allowed({'name': 'My Named Device'})\n"
        "    assert not device_policy.allowed({'name': 'Other'})\n"
        "@pytest.mark.real_device_ok()\n"
        "def test_no_token():\n    pass\n"
        "def test_plain(device_policy):\n"
        "    assert not device_policy.allowed({'name': 'My Named Device'})\n"
    )
    r = run(pytester, test=test)
    r.assert_outcomes(passed=1, skipped=2)
    r = run(pytester, test=test, env={"PDFCRAFT_ALLOW_REAL_DEVICE_TESTS": "my named device"})  # not exact
    r.assert_outcomes(passed=1, skipped=2)
    r = run(pytester, test=test, env={"PDFCRAFT_ALLOW_REAL_DEVICE_TESTS": "My Named Device"})
    r.assert_outcomes(passed=2, skipped=1)  # the empty-token marker never runs
    r = run(pytester, test=test, env={"PDFCRAFT_ALLOW_REAL_DEVICE_TESTS": "My Named Device", "PDFCRAFT_SCAN_DENY": "named"})
    r.assert_outcomes(failed=1, passed=1, skipped=1)  # a deny pattern still wins
