"""The operator's time zone reaches the containers that count days.

``settings.usage_timezone`` decides the calendar day of the bench video
outro (``orchestrator._branding_date_label``), the Insights default and the
engine-death report. config.py reads it from ``USAGE_TIMEZONE`` (or ``TZ``),
but docker-compose.yml lists every variable by hand — there is no env_file —
so a value set in ``.env`` never arrived: a run at 00:19 Swiss time on
23.10. showed "2026-10-22" in the video (round-1 user test, t2 KANN-2).
The default stays UTC, so nothing changes until a deployer sets it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["backend", "mc-worker"])
def test_usage_timezone_is_passed_through(service):
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"][service]["environment"]
    assert env.get("USAGE_TIMEZONE") == "${USAGE_TIMEZONE:-UTC}"


def test_env_example_documents_it():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "USAGE_TIMEZONE=" in text
