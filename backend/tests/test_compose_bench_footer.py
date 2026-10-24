"""The operator's handle reaches the containers that write bench artefacts.

``w1_kit.footer()`` reads ``MC_BENCH_FOOTER`` for the X-Kit texts (API
process) and for the result card / video payload (the W1 runner, which
lives in the mc-worker background loop). docker-compose.yml lists every
variable by hand — there is no env_file — so a value set only in ``.env``
never arrives and the public footer stays empty. The default is empty:
the handle lives in the operator's ``.env``, never in the repo.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["backend", "mc-worker"])
def test_bench_footer_is_passed_through(service):
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = compose["services"][service]["environment"]
    assert env.get("MC_BENCH_FOOTER") == "${MC_BENCH_FOOTER:-}"


def test_env_example_documents_it_without_a_handle():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    line = next((l for l in text.splitlines() if "MC_BENCH_FOOTER=" in l), None)
    assert line is not None
    # A generic example only — never a real handle in the public repo.
    assert line.split("=", 1)[1].strip() in ("", "@yourhandle")
