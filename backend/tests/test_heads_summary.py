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
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.services.heads import redact, summary
from tests.heads_backend_helpers import heads_root, iso, make_run  # noqa: F401 (fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "heads"
TEMPLATE_PATH = Path(__file__).parents[1] / "templates" / "heads" / "head-AGENTS.md"


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
    # RICH_TEMPLATE's Evidence section has no "kz check:" bullet at all (it
    # only carries "Reviewer"/"Bypass") — extracted, never guessed: None,
    # not a guess lifted from the unrelated Context brief (review finding on
    # PR #756 round 2 — see test_kz_check_bullet_absent_is_none_never_guessed
    # below for the exact scenario this used to get wrong).
    assert out["kz_ok"] is None


def test_kz_brief_unavailable_context_line_never_sets_kz_ok():
    """"kz brief unavailable: …" lives in the Context-brief section and
    answers a different question (was step 0's research brief available)
    than kz_ok (did the push-time kz check actually pass). Review finding on
    PR #756 round 1 + round 2: the old parser read kz_ok off this line (or
    off the mere presence/absence of that line) whenever Evidence had no own
    "kz check:" bullet — so a run whose Context brief happened to mention
    this phrase, or happened not to, silently got a kz_ok guess instead of
    None. With no "kz check:" bullet in Evidence, kz_ok must stay None
    regardless of what the Context brief says."""
    text = RICH_TEMPLATE.format(secret="x" * 20).replace(
        "- Already exists: nothing found — searched the router and the lib for a summary endpoint",
        "kz brief unavailable: tool not installed on this box",
    )
    run = _fake_run(folder=FIXTURES / "claude-run", text=text)
    assert summary.build_summary(run)["kz_ok"] is None


def test_kz_check_bullet_absent_is_none_never_guessed():
    """Review finding on PR #756 round 2, reproduced live: real run
    6bcf49ea (Status running, Evidence section empty — the run never reached
    step 6) gave kz_ok=True under the old Context-brief fallback, and so did
    both PR-1 fixtures and a Status: failed record, since none of them carry
    a "kz brief unavailable" line either. A real, filled in-repo-template
    record (the same fixture the parser's own drift/shape tests use) with
    its "kz check:" bullet removed and Status changed to failed must give
    kz_ok None — a run that never reached (or never finished) step 6 has no
    kz check result to report, whatever its Context brief says."""
    text = (FIXTURES / "claude-run" / "run-record-filled.md").read_text()
    text = text.replace("Status: passed", "Status: failed")
    text = text.replace(
        '- kz check: `kz check --pr-body-file .pr-body.md` → first run 2 new (missing PR sections), after completing the body → "kz check: 0 new, 4 known, 0 fixed, 0 skipped -> OK"; push hook ran kz check again → OK.\n',
        "",
    )
    assert "kz check" not in text.split("## Evidence", 1)[1].split("## Questions", 1)[0]
    run = _fake_run(folder=FIXTURES / "claude-run", text=text)
    out = summary.build_summary(run)
    assert out["status"] == "failed"
    assert out["kz_ok"] is None


def test_raw_template_placeholders_give_sabotage_and_review_none():
    """The unfilled "Run record template" block in head-AGENTS.md itself —
    ``- Sabotage check: <what was broken> → red · restored → green`` and
    ``- Review: <fresh helper PASSED/FAILED | self-review> — …`` — must read
    as None, not as a real answer (review finding on PR #756 round 3: these
    exact placeholders used to parse as sabotage=True and review='helper')."""
    text = TEMPLATE_PATH.read_text()
    m = re.search(r"## Run record template\n.*?```markdown\n(.*?)\n```", text, re.DOTALL)
    assert m, "head-AGENTS.md: 'Run record template' markdown code block not found"
    block = _replace_once(m.group(1), "Status: <running | passed | failed>", "Status: passed")
    run = _fake_run(folder=FIXTURES / "claude-run", text=block)
    out = summary.build_summary(run)
    assert out["sabotage"] is None
    assert out["review"] is None
    assert out["kz_ok"] is None
    assert out["result_line"] is None
    assert out["tests"] == {"failed_before": None, "passed_after": None}


def test_review_self_and_negative_sabotage_bullet():
    text = RICH_TEMPLATE.format(secret="x" * 20)
    text = text.replace("Reviewer (fresh subagent): PASSED", "Reviewer (self): PASSED")
    text = text.replace("Sabotage probe: dropped the mask call → the field leaked xxxxxxxxxxxxxxxxxxxx · restored → green", "Sabotage probe: none")
    run = _fake_run(folder=FIXTURES / "claude-run", text=text)
    out = summary.build_summary(run)
    assert out["review"] == "self"
    assert out["sabotage"] is False


def test_sabotage_negative_with_explanation_after_dash_is_still_false():
    """"n/a — docs-only change" must read as sabotage=False: the bullet's own
    first clause (before the template's " — " separator) is the exact word
    "n/a". Comparing the WHOLE value against `_NEGATIVE` (the old behaviour)
    never matched this — only a bare "none"/"n/a" with nothing after it did —
    so any run that explained its "n/a" (which real runs do) silently read
    as sabotage=True (review finding on PR #756 round 3)."""
    text = RICH_TEMPLATE.format(secret="x" * 20).replace(
        "Sabotage probe: dropped the mask call → the field leaked xxxxxxxxxxxxxxxxxxxx · restored → green",
        "Sabotage probe: n/a — docs-only change",
    )
    run = _fake_run(folder=FIXTURES / "claude-run", text=text)
    assert summary.build_summary(run)["sabotage"] is False


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


# ── 5 — real, scrubbed head-AGENTS.md-shaped records (review finding on ────
# PR #756: the parser above was written against the OLD personal-skill
# template and never matched a real run on disk — live-checked over all 26
# real run records in ~/.mc/heads: failed_before/sabotage/review/
# operator_minutes were 0/26). These two fixtures are scrubbed copies of a
# real PASSED run per harness (`run-record-filled.md`, next to each
# harness's existing minimal fixture).


def test_real_claude_run_record_parses_the_in_repo_template_labels():
    text = (FIXTURES / "claude-run" / "run-record-filled.md").read_text()
    run = _fake_run(folder=FIXTURES / "claude-run", text=text, spec={"branch": "mc-head/2026-10-01-fixture-claude-run-1111"})
    out = summary.build_summary(run)

    assert out["status"] == "passed"
    # "Failing test before" (not the old "Red test before") must parse, split
    # at the FIRST arrow (not the last — the old behaviour would have cut
    # this down to just the ModuleNotFoundError text), and — review finding
    # on PR #756 round 3 — SHORTENED to its key result: the real bullet's
    # "(ModuleNotFoundError: …)" parenthetical aside is cut, not shown; a
    # ~500-character "Green after" explanation, backticks and all, is exactly
    # what made the real card unreadable on the phone.
    assert out["tests"]["failed_before"] == '"11 failed"'
    # "Green after" carries TWO arrows on this real line — splitting at the
    # first one keeps "11 passed" (the actual result); the semicolon right
    # after it then cuts the ~500-character "full suite … 24 failed, 9765
    # passed …" explanation, which is no longer part of the card's fact.
    assert out["tests"]["passed_after"] == '"11 passed"'
    assert out["sabotage"] is True  # "Sabotage check:" (not "Sabotage probe:")
    assert out["kz_ok"] is True  # Evidence "kz check: … -> OK", not the Context-brief heuristic
    assert out["review"] == "self"  # "Review: self, no helper available …" — "self" wins over the later word "helper"
    assert out["bypass"] == 0
    assert out["helpers"] == 0  # plain "- Helpers: 0" bullet, not the unrelated "- Quota: … no helpers" line above it
    assert out["operator_minutes"] == 0  # "- Operator minutes (estimate): 0 (unattended)"
    assert out["branch"] == "mc-head/2026-10-01-fixture-claude-run-1111"


def test_no_backtick_survives_into_any_rendered_fact():
    """Review finding on PR #756 round 3, reproduced live: this real fixture's
    ``## Result`` bullet names `backend/app/services/pr_merge_monitor.py`,
    `pr_url`, `` `changed_by="system"` `` (a DOUBLED backtick, escaping the
    inner quote) and more, all as markdown inline code — the card has no
    markdown renderer, so every one of those showed up as literal backtick
    characters. None of the three prose fields this module ever emits may
    carry a backtick."""
    text = (FIXTURES / "claude-run" / "run-record-filled.md").read_text()
    assert "`" in text  # the sabotage: the raw source really is backtick-laden
    run = _fake_run(folder=FIXTURES / "claude-run", text=text, spec={"branch": "mc-head/2026-10-01-fixture-claude-run-1111"})
    out = summary.build_summary(run)

    assert out["result_line"] and "pr_merge_monitor.py" in out["result_line"]  # content kept
    assert "`" not in out["result_line"]
    assert "`" not in out["tests"]["failed_before"]
    assert "`" not in out["tests"]["passed_after"]


def test_real_omp_run_record_parses_the_in_repo_template_labels():
    text = (FIXTURES / "omp-run" / "run-record-filled.md").read_text()
    run = _fake_run(folder=FIXTURES / "omp-run", text=text, spec={"branch": "mc-head/2026-10-04-fixture-omp-run-2222"})
    out = summary.build_summary(run)

    assert out["status"] == "passed"
    assert out["tests"]["failed_before"] is not None and "1 failed" in out["tests"]["failed_before"]
    assert out["tests"]["passed_after"] is not None and "29 passed" in out["tests"]["passed_after"]
    assert out["sabotage"] is True
    assert out["kz_ok"] is True  # "kz check: … -> OK (1 skipped)"
    assert out["review"] == "helper"  # "Review: fresh helper (reviewer agent) PASSED — …"
    assert out["bypass"] == 0
    assert out["helpers"] == 1  # "- Helpers: 1 (reviewer)"
    assert out["operator_minutes"] == 25  # "- Operator minutes (estimate): ~25" — leading "~" allowed
    assert out["branch"] == "mc-head/2026-10-04-fixture-omp-run-2222"


def test_a_red_kz_check_never_reads_as_yes():
    """The exact PR #756 bug: kz_ok used to come from the Context brief's
    "kz brief unavailable: …" line, so a run with a fine context brief but
    an actually-FAILED kz check still showed "kz check: Yes". Sabotage: feed
    a real record with its "-> OK" kz-check line turned into a red one."""
    text = (FIXTURES / "claude-run" / "run-record-filled.md").read_text()
    red_text = text.replace(
        '"kz check: 0 new, 4 known, 0 fixed, 0 skipped -> OK"; push hook ran kz check again → OK.',
        '"kz check: 2 new, 4 known, 0 fixed, 0 skipped"; push hook refused the push.',
    )
    assert red_text != text  # the sabotage actually changed something
    run = _fake_run(folder=FIXTURES / "claude-run", text=red_text)
    assert summary.build_summary(run)["kz_ok"] is False

    unavailable_text = text.replace(
        '`kz check --pr-body-file .pr-body.md` → first run 2 new (missing PR sections), after completing the body → "kz check: 0 new, 4 known, 0 fixed, 0 skipped -> OK"; push hook ran kz check again → OK.',
        "kz check unavailable: no kz binary on this box",
    )
    assert unavailable_text != text
    run2 = _fake_run(folder=FIXTURES / "claude-run", text=unavailable_text)
    assert summary.build_summary(run2)["kz_ok"] is None


# ── 6 — drift guard: the parser must stay in sync with the template it ─────
# actually parses (`backend/templates/heads/head-AGENTS.md`), not a copy of
# its wording frozen into this test file.


def _replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    assert count == 1, f"drift: expected exactly one occurrence of {old!r} in head-AGENTS.md's run-record template, found {count}"
    return text.replace(old, new, 1)


def test_template_run_record_block_parses_into_every_field_build_summary_promises():
    """Fills `head-AGENTS.md`'s own "Run record template" code block with one
    distinctive, checkable value per field this module extracts, then
    asserts `build_summary` reads every one of them. If a future edit to
    that template renames or reshapes a bullet this module depends on,
    `_replace_once` fails here — loudly, in CI — instead of every future
    head's run record silently going back to all-`None` the way PR #756's
    bug did."""
    text = TEMPLATE_PATH.read_text()
    m = re.search(r"## Run record template\n.*?```markdown\n(.*?)\n```", text, re.DOTALL)
    assert m, "head-AGENTS.md: 'Run record template' markdown code block not found"
    block = m.group(1)

    block = _replace_once(block, "Status: <running | passed | failed>", "Status: passed")
    block = _replace_once(
        block,
        "- Failing test before: `<command>` → <key line>",
        "- Failing test before: `pytest -q` → 3 failed",
    )
    block = _replace_once(
        block,
        '- Green after: `<command>` → <e.g. "42 passed">',
        "- Green after: `pytest -q` → 42 passed",
    )
    block = _replace_once(
        block,
        "- Sabotage check: <what was broken> → red · restored → green",
        "- Sabotage check: removed the guard → red · restored → green",
    )
    block = _replace_once(
        block,
        '- kz check: <pasted output, last line e.g. "OK (4 skipped)" | red lines + "Known limits" in the PR | no .kohaerenz.yaml | unavailable: …>',
        "- kz check: 0 new, 0 known, 0 fixed, 0 skipped -> OK",
    )
    block = _replace_once(
        block,
        "- Review: <fresh helper PASSED/FAILED | self-review> — <1 sentence, incl. the three reviewer questions>",
        "- Review: fresh helper PASSED — looks good",
    )
    block = _replace_once(
        block,
        "- Bypass: <0 | n — why> (admin merge, skipped check, push around the queue)",
        "- Bypass: 0 — none needed",
    )
    block = _replace_once(block, "- Helpers: <n>", "- Helpers: 3")
    block = _replace_once(block, "- Operator minutes (estimate): <n>", "- Operator minutes (estimate): 12")

    run = _fake_run(folder=FIXTURES / "claude-run", text=block)
    out = summary.build_summary(run)
    assert out["status"] == "passed"
    assert out["tests"]["failed_before"] == "3 failed"
    assert out["tests"]["passed_after"] == "42 passed"
    assert out["sabotage"] is True
    assert out["kz_ok"] is True
    assert out["review"] == "helper"
    assert out["bypass"] == 0
    assert out["helpers"] == 3
    assert out["operator_minutes"] == 12
