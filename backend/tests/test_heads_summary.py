"""`services/heads/summary.py` (`build_summary`) + `GET /heads/{run_id}/summary`
(bauplan `heads-sichtbar` PR 2 §3.1).

Two levels, same as `test_heads_transcript.py`:
  - unit tests call `build_summary` directly against a tiny stand-in for
    `HeadRun` (real head.env fixtures, hand-built record text) — the
    parsing rules themselves, independent of the HTTP/window plumbing;
  - one HTTP test through `auth_client` proves the router wires `_load` →
    `build_summary` → 404 correctly, using the same `make_run`/vault-job
    pattern `test_heads_api.py` already uses for a real run record.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.services.heads import redact, summary
from tests.heads_backend_helpers import heads_root, iso, make_run  # noqa: F401 (fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "heads"


def _fake_run(*, folder: Path, text: str, spec: dict | None = None, pr_url: str | None = None):
    return SimpleNamespace(
        run_id="fake-run",
        folder=folder,
        run_record_text=text,
        spec=spec or {"branch": "mc-head/fake"},
        pr_url=pr_url,
    )


# ── 1 — the real, minimal fixtures (both harnesses): almost everything is None ──


def test_minimal_real_fixture_record_is_mostly_none_never_guessed():
    for name in ("claude-run", "omp-run"):
        text = (FIXTURES / name / "run-record.md").read_text()
        run = _fake_run(folder=FIXTURES / name, text=text)
        out = summary.build_summary(run)
        assert out["status"] == "passed"
        assert out["result_line"] == "Fixture run record for the transcript-reader tests (scrubbed, no real content)."
        # Nothing in this record's ``## Result``-only body names any of these
        # — a real-looking but absent fact must stay ``None``, never a guess.
        for key in ("sabotage", "kz_ok", "bypass", "operator_minutes", "helpers"):
            assert out[key] is None, key
        assert out["tests"] == {"failed_before": None, "passed_after": None}
        assert out["review"] is None


# ── 2 — every field present, masked against THIS run's own head.env ──────────

RICH_TEMPLATE = """---
id: job-x
type: run-record
agent: head
date: 2026-10-04
head_run: fake-run
---

# Run record: rich fixture

Heartbeat: 2026-10-04 09:25 · step 7 done · waiting for: nothing
Status: passed

## Context brief (max. 60 lines, written in step 0 before any code)
- Already exists: nothing found — searched the router and the lib for a summary endpoint
- Decisions that apply: ADR-085 §4 Nachtrag (read-only chat view)

## Result
- The chat view now shows a run-record summary card instead of raw markdown; key secret {secret}

## Evidence
- Red test before: `pytest backend/tests/test_heads_summary.py` → ModuleNotFoundError: summary secret {secret}
- Green after: `pytest backend/tests/test_heads_summary.py` → 9 passed, no leak of {secret}
- Sabotage probe: dropped the mask call → the field leaked {secret} · restored → green
- Reviewer (fresh subagent): PASSED — exists already? no. decision? ADR-085 §4. flow? chat card before/after.
- Bypass: 0 — none needed

## Numbers
| Quota 7 days | start 40% -> end 42% |
|---|---|
| Helpers | 2 subagents · 1 local steps |
| Operator minutes | head estimate: 35 min (1 questions) · operator corrects: ___ |
"""


def test_rich_record_every_field_and_masked_against_this_runs_head_env():
    secret = redact.head_env_values(SimpleNamespace(folder=FIXTURES / "claude-run"))[0]
    assert secret == "test-fake-9f3a7c21b8e4"  # the probe only means something if it is really there
    text = RICH_TEMPLATE.format(secret=secret)
    assert secret in text  # sabotage: the un-redacted source really does carry it
    run = _fake_run(folder=FIXTURES / "claude-run", text=text, spec={"branch": "mc-head/rich"}, pr_url="https://github.com/o/r/pull/9")

    out = summary.build_summary(run)
    assert out["status"] == "passed"
    assert secret not in out["result_line"]
    assert secret not in out["tests"]["failed_before"]
    assert secret not in out["tests"]["passed_after"]
    assert out["sabotage"] is True
    assert out["review"] == "helper"
    assert out["bypass"] == 0
    assert out["operator_minutes"] == 35
    assert out["helpers"] == 2
    assert out["branch"] == "mc-head/rich"
    assert out["pr_url"] == "https://github.com/o/r/pull/9"
    # kz_ok: a real context brief with no "unavailable" line present → True.
    assert out["kz_ok"] is True


def test_kz_brief_unavailable_line_makes_kz_ok_false():
    text = RICH_TEMPLATE.format(secret="x" * 20).replace(
        "- Already exists: nothing found — searched the router and the lib for a summary endpoint",
        "kz brief unavailable: tool not installed on this box",
    )
    run = _fake_run(folder=FIXTURES / "claude-run", text=text)
    assert summary.build_summary(run)["kz_ok"] is False


def test_review_self_and_negative_sabotage_bullet():
    text = RICH_TEMPLATE.format(secret="x" * 20)
    text = text.replace("Reviewer (fresh subagent): PASSED", "Reviewer (self): PASSED")
    text = text.replace("Sabotage probe: dropped the mask call → the field leaked xxxxxxxxxxxxxxxxxxxx · restored → green", "Sabotage probe: none")
    run = _fake_run(folder=FIXTURES / "claude-run", text=text)
    out = summary.build_summary(run)
    assert out["review"] == "self"
    assert out["sabotage"] is False


# ── 3 — a broken record (frontmatter + heading only, no body at all) ─────────


def test_record_with_no_sections_at_all_is_all_none_not_a_500():
    run = _fake_run(folder=FIXTURES / "claude-run", text="---\nhead_run: fake-run\n---\n\n# Run record\n")
    out = summary.build_summary(run)
    assert out["status"] is None and out["result_line"] is None
    assert out["tests"] == {"failed_before": None, "passed_after": None}
    for key in ("sabotage", "kz_ok", "review", "bypass", "operator_minutes", "helpers"):
        assert out[key] is None


# ── 4 — the HTTP endpoint: 404, then a real run record through the router ───


async def test_summary_endpoint_404_without_a_run_record(auth_client, heads_root):
    run_id = make_run(heads_root, status={"phase": "running", "started_at": iso(time.time())})
    resp = await auth_client.get(f"/api/v1/heads/{run_id}/summary")
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "run_record_missing"
    assert (await auth_client.get("/api/v1/heads/not-a-uuid/summary")).status_code == 404


async def test_summary_endpoint_200_with_a_real_run_record(auth_client, heads_root):
    from app.config import settings

    now = time.time()
    run_id = make_run(heads_root, status={
        "phase": "exited", "exit_code": 0,
        "started_at": iso(now - 300), "exited_at": iso(now - 10),
        "pr_url": "https://github.com/owner/demo/pull/42",
    })
    job = Path(settings.vault_path) / "jobs" / f"2026-09-23-fix-{run_id[:4]}"
    job.mkdir(parents=True, exist_ok=True)
    path = job / "run-record.md"
    path.write_text(
        "---\nid: job-x\ntype: run-record\nagent: head\ndate: 2026-09-23\n"
        f"head_run: {run_id}\n---\n\n# Run record\n\nStatus: passed\n\n"
        "## Result\n- Added the summary endpoint.\n"
    )
    os.utime(path, (now - 10, now - 10))
    sp = heads_root / run_id / ".wrapper" / "status.json"
    st = json.loads(sp.read_text())
    st["run_record_path"] = str(path)
    sp.write_text(json.dumps(st))

    resp = await auth_client.get(f"/api/v1/heads/{run_id}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "passed"
    assert body["result_line"] == "Added the summary endpoint."
    assert body["pr_url"] == "https://github.com/owner/demo/pull/42"
    assert body["sabotage"] is None and body["bypass"] is None
