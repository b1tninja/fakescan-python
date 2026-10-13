"""Where the sibling ``fakescan`` repo lives (profiles, WSD schema, built binary).

This package no longer sits inside the fakescan repo, so anything that used to be found relative
to the package is resolved here. Order: ``FAKESCAN_REPO``, then a ``fakescan`` directory next to
this repo (the monorepo layout under ``D:\\code``).
"""

from __future__ import annotations

import os
from pathlib import Path

# python/fakescan/_paths.py -> repo root is parents[2]; its sibling is the fakescan repo.
_THIS_REPO = Path(__file__).resolve().parents[2]


def fakescan_repo() -> Path:
    env = os.environ.get("FAKESCAN_REPO")
    return Path(env) if env else _THIS_REPO.parent / "fakescan"


def profiles_dir() -> Path:
    env = os.environ.get("FAKESCAN_PROFILES")
    return Path(env) if env else fakescan_repo() / "profiles"


def schema_dir() -> Path:
    return fakescan_repo() / "docs" / "reference" / "schema"
