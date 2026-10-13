"""Tests for fakescan.wsd_docs (markdown reference doc generator)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fakescan import _paths
from fakescan.wsd_docs import generate_reference_docs, main

SCHEMA_DIR = _paths.schema_dir()

CANONICAL_PAGES = [
    "README.md",
    "00-Overview.md",
    "01-WKVs-WellKnownValues.md",
    "02-Common-Types.md",
    "03-Document.md",
    "04-Job.md",
    "05-Scanner.md",
    "06-WDPScan.md",
]


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "docs_out"



def test_generate_returns_eight_paths(out_dir: Path) -> None:
    written = generate_reference_docs(schema_dir=SCHEMA_DIR, out_dir=out_dir)
    assert len(written) == 8
    assert all(isinstance(p, Path) for p in written)


def test_canonical_pages_exist(out_dir: Path) -> None:
    generate_reference_docs(schema_dir=SCHEMA_DIR, out_dir=out_dir)
    for name in CANONICAL_PAGES:
        assert (out_dir / name).is_file(), f"missing generated page: {name}"


def test_readme_relative_links_resolve(out_dir: Path) -> None:
    generate_reference_docs(schema_dir=SCHEMA_DIR, out_dir=out_dir)
    readme = (out_dir / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)#\s]+\.md)\)", readme)
    assert links, "expected relative .md links in README.md"
    missing = [
        link for link in links if link.startswith(("http://", "https://", "/"))
        or not (out_dir / link).is_file()
    ]
    assert not missing, f"dangling README links: {missing}"


def test_main_out_dir_returns_zero(out_dir: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["--out-dir", str(out_dir)]) == 0
    for name in CANONICAL_PAGES:
        assert (out_dir / name).is_file()


def test_page_has_type_heading_and_table_row(out_dir: Path) -> None:
    generate_reference_docs(schema_dir=SCHEMA_DIR, out_dir=out_dir)
    for name in ("02-Common-Types.md", "03-Document.md"):
        text = (out_dir / name).read_text(encoding="utf-8")
        assert re.search(r"^## \S", text, flags=re.M), f"{name}: no '## ' heading"
        assert re.search(r"^\|[^|]*\|", text, flags=re.M), f"{name}: no table row"
