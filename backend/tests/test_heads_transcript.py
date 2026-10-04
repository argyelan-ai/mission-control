"""Head transcript reader (docs/decisions/085 Nachtrag 2026-10-04 §4):
``services/heads/transcript.py`` (locate/etag/read), ``services/heads/
redact.py`` (mask_text/mask_tree/head_env_values), the strict
``transcript_adapters.adapter_for_harness`` and the
``GET /heads/{run_id}/chat/history`` + ``GET /heads/cleanup`` endpoints.

Fixtures (``tests/fixtures/heads/{claude-run,omp-run}``) are short, scrubbed
prefixes of two REAL head transcripts (see the bauplan's build instructions)
— real event shapes, no private content. ``head.env`` in both is synthetic
and its value is deliberately planted into one transcript line, so the
masking tests prove something real: the value is there to be leaked if
masking regresses, not decorative.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from app.services.heads import redact
from app.services.heads import transcript as tr
from app.services.heads.files import load_run
from app.services.transcript_adapters import adapter_for, adapter_for_harness
from app.services import token_harvester
from tests.heads_backend_helpers import heads_root  # noqa: F401 (fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "heads"


def install_fixture(heads_root: Path, name: str, *, run_id: str | None = None) -> str:
    """Copies a scrubbed fixture run folder into the test ``heads_root``,
    optionally under a different run id (spec.json + folder name both
    renamed, so ``load_run`` still accepts it)."""
    spec = json.loads((FIXTURES / name / "spec.json").read_text())
    real_run_id = spec["run_id"]
    use_id = run_id or real_run_id
    dest = heads_root / use_id
    shutil.copytree(FIXTURES / name, dest)
    if run_id and run_id != real_run_id:
        spec["run_id"] = run_id
        (dest / "spec.json").write_text(json.dumps(spec))
    return use_id


PLANTED = {
    "claude-run": ("test-fake-9f3a7c21b8e4", "ghp_abcdefghijklmnopqrstuvwx", "Bearer zzzzzzzzzzzzzzzzzzzzzzzz"),
    "omp-run": ("test-fake-2b6e81cfa903", "ghp_abcdefghijklmnopqrstuvwx", "Bearer zzzzzzzzzzzzzzzzzzzzzzzz"),
}


# ── 1/2 — real fixtures, right reader, right kinds, subagent excluded ────


def test_claude_fixture_reads_with_right_reader_and_kinds(heads_root):
    run_id = install_fixture(heads_root, "claude-run")
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Located) and located.reader == "claude"
    result = tr.read(run, located, limit=1000, before_uuid=None)
    assert result["source"] == "transcript" and result["reader"] == "claude" and result["reason"] is None
    assert len(result["events"]) > 0
    kinds = {e["kind"] for e in result["events"]}
    assert {"message", "tool", "thinking"} <= kinds


def test_omp_fixture_reads_and_never_picks_the_sidecar_file(heads_root):
    run_id = install_fixture(heads_root, "omp-run")
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Located) and located.reader == "omp"
    # The sidecar one level below the flat session file was never a
    # candidate (locate() must have picked the ONE flat .jsonl):
    assert located.path.parent.name == "omp-sessions"
    result = tr.read(run, located, limit=1000, before_uuid=None)
    assert len(result["events"]) > 0


def test_sabotage_adapter_for_is_the_documented_trap() -> None:
    """The trap ``anhang.md`` section B names: ``adapter_for`` is duck-typed
    on an object's ``.harness`` ATTRIBUTE. Passed a plain harness STRING (a
    string has no ``.harness`` attribute) it silently resolves to the Claude
    adapter, no matter which harness name was passed — reproduced here with
    the literal call the bauplan's sabotage step describes
    (``adapter_for(spec["harness"])`` instead of
    ``adapter_for_harness(spec["harness"])``). ``transcript.locate`` must
    never make this call; this test is the guard that a regression back to
    it would trip (an omp transcript read through the Claude parser finds
    zero of its own events)."""
    assert adapter_for("omp").name == "claude"  # the trap, preserved on purpose
    assert adapter_for_harness("omp").name == "omp"  # the fix this module uses


# ── 3 — masking, with the sabotage spelled out as a real assertion ──────


@pytest.mark.parametrize("name", ["claude-run", "omp-run"])
def test_masking_removes_the_planted_secrets(heads_root, name):
    run_id = install_fixture(heads_root, name)
    run = load_run(run_id)
    located = tr.locate(run)
    result = tr.read(run, located, limit=1000, before_uuid=None)
    blob = json.dumps(result)
    env_value, ghp, bearer = PLANTED[name]
    for secret in (env_value, ghp, bearer):
        assert secret not in blob, f"{secret!r} leaked into the response"

    # Sabotage, spelled out, against the RAW (unmasked) line: without
    # head.env's own values folded in as ``extra``, the env-provider key (no
    # recognisable shape — it carries no ``ghp_``/``sk-``/``Bearer`` prefix)
    # is NOT caught by shape-only masking. This is exactly why ``read()``
    # passes ``redact.head_env_values(run)`` into ``mask_tree`` — removing
    # that argument (``head_env_values`` "leer liefern") is the bauplan's
    # named sabotage, and this assertion is what makes it visible: it fails
    # if that wiring is ever lost.
    raw = located.path.read_text()
    assert env_value in raw, "fixture must still plant the value raw, or this probe is meaningless"
    shape_only = redact.mask_text(raw, ())
    assert env_value in shape_only, "shape-only masking should NOT catch the env value by itself"
    assert ghp not in shape_only and bearer not in shape_only, "shape-based masking should still catch these two"


# ── 4 — symlinks, never followed ─────────────────────────────────────────


def test_symlinked_transcript_file_is_rejected(heads_root, tmp_path):
    run_id = install_fixture(heads_root, "claude-run")
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Located)
    secret_outside = tmp_path / "outside.jsonl"
    secret_outside.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "OUTSIDE-SECRET"}}))
    os.unlink(located.path)
    os.symlink(secret_outside, located.path)
    located2 = tr.locate(run)
    assert isinstance(located2, tr.Unavailable) and located2.reason == "no_transcript"


def test_symlinked_omp_sessions_dir_is_rejected(heads_root):
    run_id = install_fixture(heads_root, "omp-run")
    run = load_run(run_id)
    real_dir = run.folder / "omp-sessions"
    foreign = run.folder.parent / "foreign-sessions"
    shutil.move(str(real_dir), str(foreign))
    os.symlink(foreign, real_dir)
    located = tr.locate(run)
    assert isinstance(located, tr.Unavailable) and located.reason == "no_transcript"


def test_symlinked_claude_project_dir_is_rejected(heads_root):
    run_id = install_fixture(heads_root, "claude-run")
    run = load_run(run_id)
    projects = run.folder / "claude-config" / "projects"
    (real_dir,) = [p for p in projects.iterdir() if p.is_dir()]
    foreign = run.folder.parent / "foreign-project"
    shutil.move(str(real_dir), str(foreign))
    os.symlink(foreign, real_dir)
    located = tr.locate(run)
    assert isinstance(located, tr.Unavailable) and located.reason == "no_transcript"


# ── 5 — no reader / not_yet / no_transcript ──────────────────────────────


def test_unknown_harness_has_no_reader(heads_root):
    from tests.heads_backend_helpers import make_run

    run_id = make_run(heads_root, harness="kimi", status={"phase": "running", "started_at": "2026-10-01T00:00:00Z"})
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Unavailable) and located.reason == "no_reader"


def test_running_without_a_transcript_yet_is_not_yet(heads_root):
    from tests.heads_backend_helpers import make_run

    run_id = make_run(
        heads_root, harness="omp", status={"phase": "running", "started_at": "2026-10-01T00:00:00Z"},
        heartbeat_age=3,
    )
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Unavailable) and located.reason == "not_yet"


def test_exited_without_a_transcript_is_no_transcript(heads_root):
    from tests.heads_backend_helpers import make_run

    run_id = make_run(heads_root, harness="omp", status={"phase": "exited", "exit_code": 0})
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Unavailable) and located.reason == "no_transcript"
    result = tr.read(run, located, limit=100, before_uuid=None)
    assert result == {
        "events": [],
        "session": {"sessionId": run_id, "live": False, "startedAt": None, "aliveness": "ended"},
        "hasMore": False,
        "subagentRuns": [],
        "source": "none",
        "reader": None,
        "reason": "no_transcript",
    }


def test_oversized_transcript_is_too_large(heads_root, monkeypatch):
    run_id = install_fixture(heads_root, "omp-run")
    run = load_run(run_id)
    monkeypatch.setattr(tr, "MAX_TRANSCRIPT_BYTES", 10)
    located = tr.locate(run)
    assert isinstance(located, tr.Unavailable) and located.reason == "too_large"


# ── 6 — ETag / 304 ────────────────────────────────────────────────────────


def test_etag_changes_when_the_file_grows(heads_root):
    run_id = install_fixture(heads_root, "omp-run")
    run = load_run(run_id)
    located = tr.locate(run)
    tag1 = tr.etag(located, 400, None)
    assert tag1 is not None
    located_path = located.path
    with located_path.open("a") as f:
        f.write(json.dumps({"type": "message", "id": str(uuid.uuid4()), "timestamp": "2026-10-04T09:30:00.000Z",
                            "message": {"role": "user", "content": "one more line"}}) + "\n")
    run2 = load_run(run_id)
    located2 = tr.locate(run2)
    tag2 = tr.etag(located2, 400, None)
    assert tag2 is not None and tag2 != tag1


def test_etag_is_none_for_an_unavailable_transcript(heads_root):
    from tests.heads_backend_helpers import make_run

    run_id = make_run(heads_root, harness="omp", status={"phase": "exited", "exit_code": 0})
    run = load_run(run_id)
    located = tr.locate(run)
    assert tr.etag(located, 400, None) is None


# ── 7/8 — API endpoint: auth, 404s, heads_disabled, 304 round trip ──────


async def test_chat_history_endpoint_round_trip(auth_client, heads_root):
    run_id = install_fixture(heads_root, "claude-run")
    resp = await auth_client.get(f"/api/v1/heads/{run_id}/chat/history?limit=1000")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "transcript" and len(body["events"]) > 0
    etag = resp.headers["ETag"]
    again = await auth_client.get(
        f"/api/v1/heads/{run_id}/chat/history?limit=1000", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304


async def test_chat_history_unknown_run_is_404(auth_client, heads_root):
    assert (await auth_client.get(f"/api/v1/heads/{uuid.uuid4()}/chat/history")).status_code == 404
    assert (await auth_client.get("/api/v1/heads/not-a-uuid/chat/history")).status_code == 404


async def test_chat_history_requires_login_viewer_may_read(client, heads_root):
    from app.auth import create_access_token
    from app.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    run_id = install_fixture(heads_root, "omp-run")
    assert (await client.get(f"/api/v1/heads/{run_id}/chat/history")).status_code == 401

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"v-{uid.hex[:6]}@mc.local", name="V", role="viewer", is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), 'viewer')}"
    assert (await client.get(f"/api/v1/heads/{run_id}/chat/history")).status_code == 200


async def test_chat_history_disabled_is_404(auth_client, heads_root, monkeypatch):
    from app.config import settings

    run_id = install_fixture(heads_root, "claude-run")
    monkeypatch.setattr(settings, "heads_enabled", False)
    resp = await auth_client.get(f"/api/v1/heads/{run_id}/chat/history")
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "heads_disabled"


def test_every_harvester_head_harness_has_a_reader_glob_the_harvester_covers(heads_root):
    """Gleichlauf-Waechter: every harness the token harvester already reads
    head transcripts for must also resolve through ``adapter_for_harness``,
    and a file matching the ADAPTER's (narrower) glob must also match the
    HARVESTER's (broader, subagent-inclusive) glob — one piece of knowledge,
    not two that can drift apart."""
    for harness, harvester_glob in token_harvester._HEAD_TRANSCRIPT_GLOBS.items():
        adapter = adapter_for_harness(harness)
        assert adapter is not None, f"{harness} has no adapter_for_harness reader"
        assert adapter.head_transcript_glob, f"{harness} adapter has no head_transcript_glob"

        folder = heads_root / f"glob-check-{harness}"
        # Build one file at the adapter's pattern (literal stand-ins for the
        # single '*' segments) and confirm the harvester's own glob also
        # matches that exact path.
        parts = adapter.head_transcript_glob.split("/")
        rel = Path(*["sess" if p == "*" else p.replace("*", "file") for p in parts])
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")
        assert target in set(folder.glob(adapter.head_transcript_glob))
        assert target in set(folder.glob(harvester_glob)), (
            f"{harness}: harvester glob {harvester_glob!r} does not cover adapter glob "
            f"{adapter.head_transcript_glob!r} match {target}"
        )


# ── 9 — /log still masks, now also head.env values ───────────────────────


async def test_log_endpoint_masks_head_env_values_too(auth_client, heads_root):
    run_id = install_fixture(heads_root, "claude-run")
    (heads_root / run_id / "head.log").write_text(
        "using key test-fake-9f3a7c21b8e4 to call the provider\nnext line\n"
    )
    log = (await auth_client.get(f"/api/v1/heads/{run_id}/log")).text
    assert "test-fake-9f3a7c21b8e4" not in log
    assert "next line" in log


# ── GET /heads/cleanup ────────────────────────────────────────────────────


async def test_cleanup_endpoint_reports_null_before_any_gc_run(auth_client, heads_root):
    assert (await auth_client.get("/api/v1/heads/cleanup")).json() == {"report": None}


async def test_cleanup_endpoint_reflects_the_hosts_report_file(auth_client, heads_root):
    report = {"mode": "dry_run", "at": "2026-10-04T12:00:00Z", "runs": [], "heads_bytes": 0}
    (heads_root / "gc-report.json").write_text(json.dumps(report))
    resp = await auth_client.get("/api/v1/heads/cleanup")
    assert resp.status_code == 200 and resp.json() == {"report": report}


async def test_cleanup_endpoint_ignores_a_corrupt_report(auth_client, heads_root):
    (heads_root / "gc-report.json").write_text("not json")
    assert (await auth_client.get("/api/v1/heads/cleanup")).json() == {"report": None}
