"""The fake UUID namespace: identity is the UUID prefix, never the name."""

from __future__ import annotations

import uuid as _uuid

#: Every fakescan/fakeprint device UUID starts with this prefix; keep in sync with the Rust crates
#: (``fakescan::FAKE_UUID_PREFIX``).
FAKE_UUID_PREFIX = "00000000-0000-4000-8000-"


def is_fake_uuid(value: object) -> bool:
    """True if ``value`` is a UUID in the fake namespace (:data:`FAKE_UUID_PREFIX`).

    Case-insensitive; accepts surrounding braces and ``urn:uuid:`` / ``uuid:`` forms. Anything that is
    not a string (or not a full 36-character UUID) is ``False``."""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    for pre in ("urn:uuid:", "uuid:"):
        if v.startswith(pre):
            v = v[len(pre) :]
            break
    v = v.strip("{}").strip()
    return len(v) == 36 and v.startswith(FAKE_UUID_PREFIX)


def random_fake_uuid() -> str:
    """A random UUID inside the fake namespace (prefix + 12 random hex digits)."""
    return FAKE_UUID_PREFIX + _uuid.uuid4().hex[-12:]
