"""The fake UUID namespace: identity is the UUID prefix, never the name."""

from __future__ import annotations

import fakescan

BASE = "00000000-0000-4000-8000-0000000000f2"


def test_prefix_constant_and_forms():
    assert fakescan.FAKE_UUID_PREFIX == "00000000-0000-4000-8000-"
    for v in (BASE, BASE.upper(), "{" + BASE + "}", "urn:uuid:" + BASE, "uuid:" + BASE, f" {BASE} "):
        assert fakescan.is_fake_uuid(v), v


def test_everything_else_is_foreign():
    for v in ("3f2b9c1e-7d44-4a55-9e0b-1c2d3e4f5a6b", "11111111-2222-4333-8444-555555555555", "",
              "00000000-0000-4000-8000-", "00000000-0000-4000-8001-0000000000f2", None, 5, b"x"):
        assert not fakescan.is_fake_uuid(v), v


def test_default_uuids_satisfy_the_rule():
    from fakescan._uuids import random_fake_uuid

    assert fakescan.is_fake_uuid(random_fake_uuid()) and random_fake_uuid() != random_fake_uuid()
