"""Tests for scripts/context_detect.py — the Python twin of
docker/mc-agent-base/lib/context-detect.sh (CTX-01 Nachzug Teil 2, 2026-08-10).

Runs the SAME fixtures as the equivalence test (context_scrape_fixtures.py)
directly against the Python implementation, so a regression here is caught
independently of the bash comparison.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from .context_scrape_fixtures import FIXTURES

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "context_detect.py"


def _load_context_detect():
    spec = importlib.util.spec_from_file_location("context_detect_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def context_detect():
    return _load_context_detect()


@pytest.mark.parametrize("desc,harness,text,expected", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_scrape_context_pct(context_detect, desc, harness, text, expected):
    got = context_detect.scrape_context_pct(text, harness=harness)
    assert got == expected, f"{desc}: expected {expected!r}, got {got!r}"


def test_empty_text_returns_none(context_detect):
    assert context_detect.scrape_context_pct("", harness="claude") is None
    assert context_detect.scrape_context_pct(None, harness="claude") is None


def test_return_type_is_int_or_none(context_detect):
    got = context_detect.scrape_context_pct("ctx: 42", harness="claude")
    assert isinstance(got, int)
    assert context_detect.scrape_context_pct("no context here", harness="claude") is None
