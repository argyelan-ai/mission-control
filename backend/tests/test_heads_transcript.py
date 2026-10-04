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


@pytest.mark.parametrize("name", ["claude-run", "omp-run"])
def test_masking_survives_the_real_quoted_head_env_format(heads_root, name):
    """Regression for the review finding on PR #751: ``head_env_values``
    used to return the value WITH its surrounding quotes (a bare
    ``.strip()``), which only ever matches a hand-written, unquoted
    ``head.env`` — never the real one. ``services/heads/launcher._format_env``
    is the ONLY writer of a real ``head.env`` and always wraps a value in
    single quotes; this test writes the fixture's own value through that
    exact function (not a hand-typed ``KEY=value`` line) and proves the
    planted secret is still gone from both read surfaces. Live check against
    a real run (fcbf9b5d, 2026-10-04) found 4/4 quoted values and 0
    redactions before this fix — this is the regression guard for that."""
    from app.services.heads.launcher import _format_env

    run_id = install_fixture(heads_root, name)
    run = load_run(run_id)
    env_value = PLANTED[name][0]
    key = "ANTHROPIC_API_KEY" if name == "claude-run" else "OPENAI_API_KEY"
    formatted = _format_env({key: env_value})
    assert formatted == f"{key}='{env_value}'\n", "fixture and _format_env's own format must match"
    (run.folder / "head.env").write_text(formatted)

    located = tr.locate(run)
    result = tr.read(run, located, limit=1000, before_uuid=None)
    assert env_value not in json.dumps(result), "a real, quoted head.env value must still be masked"


@pytest.mark.parametrize("name", ["claude-run", "omp-run"])
async def test_masking_survives_the_real_quoted_head_env_format_via_log_endpoint(auth_client, heads_root, name):
    from app.services.heads.launcher import _format_env

    run_id = install_fixture(heads_root, name)
    env_value = PLANTED[name][0]
    key = "ANTHROPIC_API_KEY" if name == "claude-run" else "OPENAI_API_KEY"
    (heads_root / run_id / "head.env").write_text(_format_env({key: env_value}))
    (heads_root / run_id / "head.log").write_text(f"using key {env_value} to call the provider\nnext line\n")
    log = (await auth_client.get(f"/api/v1/heads/{run_id}/log")).text
    assert env_value not in log
    assert "next line" in log


@pytest.mark.parametrize("name", ["claude-run", "omp-run"])
def test_masking_leaves_the_model_value_readable(heads_root, name):
    """Review finding on PR #751: folding EVERY ≥8-char head.env value into
    ``extra`` also redacted ``ANTHROPIC_MODEL``/``ANTHROPIC_SMALL_FAST_MODEL``
    — a claude-harness run showed the model as "<redacted>" everywhere (279
    occurrences on the real run fcbf9b5d), which also made the harness
    asymmetric against the omp fixture (whose head.env has no *_MODEL key).
    Only *_API_KEY/*_TOKEN/*_SECRET values are secrets; the model must stay
    visible for both harnesses."""
    run_id = install_fixture(heads_root, name)
    run = load_run(run_id)
    located = tr.locate(run)
    result = tr.read(run, located, limit=1000, before_uuid=None)
    blob = json.dumps(result)
    assert "GLM-5.3-Flash-EXL3" in blob, "the model value must survive masking"


# ── shape layer: env-style keys, header, hf_ token, JWT ─────────────────


@pytest.mark.parametrize(
    "line,secret",
    [
        ("OPENAI_API_KEY=sk-live-abcdefghijklmnop", "sk-live-abcdefghijklmnop"),
        ("export ANTHROPIC_API_KEY=abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
        ("GH_TOKEN: abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
        ("x-api-key: abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
        ("X-API-KEY: ABCDEFGHIJKLMNOPQRSTUVWX", "ABCDEFGHIJKLMNOPQRSTUVWX"),
        ("token is hf_abcdefghijklmnopqrstuvwxyz012345", "hf_abcdefghijklmnopqrstuvwxyz012345"),
        (
            "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ),
    ],
)
def test_shape_layer_catches_env_style_header_hf_and_jwt_secrets(line, secret):
    out = redact.mask_text(line, ())
    assert secret not in out, f"{line!r} must be masked"
    assert redact.REDACTED in out


@pytest.mark.parametrize(
    "line,secret",
    [
        ('"OPENAI_API_KEY": "sk-live-abcdefghijklmnop"', "sk-live-abcdefghijklmnop"),
        ('{"apiKey":"abcdefghijklmnopqrstuvwx"}', "abcdefghijklmnopqrstuvwx"),
        ('{"access_token": "abcdefghijklmnopqrstuvwx"}', "abcdefghijklmnopqrstuvwx"),
        ("password=hunter2plutonium", "hunter2plutonium"),
        ("postgresql://dbuser:s3cr3t-passw0rd@db.internal:5432/mc", "s3cr3t-passw0rd"),
    ],
)
def test_shape_layer_catches_json_keys_password_eq_and_url_userinfo(line, secret):
    out = redact.mask_text(line, ())
    assert secret not in out, f"{line!r} must be masked"
    assert redact.REDACTED in out


def test_shape_layer_json_key_secret_keeps_the_key_name_readable():
    out = redact.mask_text('{"OPENAI_API_KEY": "sk-live-abcdefghijklmnop"}', ())
    assert '"OPENAI_API_KEY"' in out, "the key itself must survive masking, only the value is secret"


def test_shape_layer_url_userinfo_keeps_the_username_readable():
    out = redact.mask_text("postgresql://dbuser:s3cr3t-passw0rd@db.internal:5432/mc", ())
    assert "dbuser" in out, "the username half of userinfo is not the secret"


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


def test_transcript_swapped_to_a_symlink_after_locate_is_not_followed(heads_root, tmp_path):
    """TOCTOU: ``locate()`` only checks the path ONCE; the ETag computation
    and an ``asyncio.to_thread`` hop both sit between that check and the
    moment the file is actually read. If the head swaps the file for a
    symlink in that window, ``read()`` must notice at the moment of use
    (``_open_verified``'s ``O_NOFOLLOW`` open) — not trust the earlier
    check and follow it via a plain ``open()``."""
    run_id = install_fixture(heads_root, "claude-run")
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Located)
    secret_outside = tmp_path / "outside.jsonl"
    secret_outside.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "OUTSIDE-SECRET"}}))
    os.unlink(located.path)
    os.symlink(secret_outside, located.path)

    result = tr.read(run, located, limit=1000, before_uuid=None)
    assert "OUTSIDE-SECRET" not in json.dumps(result), "a post-locate() symlink swap must never be followed"
    assert result["source"] == "none" and result["reason"] == tr.REASON_NO_TRANSCRIPT


def test_transcript_swapped_via_intermediate_directory_after_locate_is_not_followed(heads_root, tmp_path):
    """TOCTOU round 2 (review finding on PR #751): the previous fix pinned
    identity with a fresh ``lstat()`` taken immediately before the
    ``O_NOFOLLOW`` open — but that lstat runs AFTER the possible swap too,
    so it just reports whatever file now sits there. It does not help when
    an INTERMEDIATE directory (not the final component) is swapped for a
    symlink between ``locate()`` and the actual read: ``O_NOFOLLOW`` only
    blocks a symlink on the FINAL component, so the open still succeeds,
    through the swapped parent, against an attacker-chosen file of the
    same name. ``locate()`` must pin the real file's identity
    (``st_dev``/``st_ino``) at validation time, in ``Located``, and
    ``read()`` must check the opened file against THAT pinned identity —
    not a fresh lstat taken after the swap could already have happened."""
    run_id = install_fixture(heads_root, "claude-run")
    run = load_run(run_id)
    located = tr.locate(run)
    assert isinstance(located, tr.Located)

    real_dir = located.path.parent
    foreign_dir = run.folder.parent / "foreign-intermediate"
    foreign_dir.mkdir()
    secret_outside = foreign_dir / located.path.name
    secret_outside.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "OUTSIDE-SECRET"}}))
    shutil.rmtree(real_dir)
    os.symlink(foreign_dir, real_dir)

    result = tr.read(run, located, limit=1000, before_uuid=None)
    assert "OUTSIDE-SECRET" not in json.dumps(result), "a post-locate() intermediate-directory swap must never be followed"
    assert result["source"] == "none" and result["reason"] == tr.REASON_NO_TRANSCRIPT


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


def test_etag_changes_when_only_the_state_changes(heads_root):
    """Review finding on PR #751: the ETag used to key only on file stats +
    page params, not the derived head state. The NORMAL end of a head (the
    wrapper writes ``exited`` to status.json strictly AFTER the
    transcript's last line) leaves the file completely unchanged — without
    ``state`` in the key, that transition produced no new ETag at all, so a
    client polling with ``If-None-Match`` would keep getting 304 and never
    learn the run ended."""
    run_id = install_fixture(heads_root, "omp-run")
    run = load_run(run_id)
    located = tr.locate(run)
    tag_running = tr.etag(located, 400, None, "running")
    tag_failed = tr.etag(located, 400, None, "failed")
    assert tag_running is not None and tag_failed is not None
    assert tag_running != tag_failed, "a state change alone must change the ETag"


async def test_chat_history_etag_changes_when_a_head_ends_with_no_new_transcript_bytes(auth_client, heads_root):
    """Same finding, through the real endpoint: flip status.json from
    running to exited WITHOUT touching the transcript file at all, and
    confirm a client's cached ETag is rejected (200, not 304) with
    aliveness now "ended" — the exact client-visible symptom the finding
    describes (a PR2 poller stuck showing "running" forever)."""
    run_id = install_fixture(heads_root, "omp-run")
    status_path = heads_root / run_id / ".wrapper" / "status.json"
    status_path.write_text(json.dumps({
        "run_id": run_id, "phase": "running", "supervisor_pid": os.getpid(),
        "started_at": "2026-10-04T09:00:00Z",
    }))
    (heads_root / run_id / ".wrapper" / "heartbeat").touch()

    first = await auth_client.get(f"/api/v1/heads/{run_id}/chat/history?limit=1000")
    assert first.status_code == 200
    assert first.json()["session"]["aliveness"] == "active"
    assert first.json()["session"]["live"] is True
    etag1 = first.headers["ETag"]

    # The transcript file itself is untouched — only the wrapper's status
    # flips, exactly like a real head ending.
    status_path.write_text(json.dumps({
        "run_id": run_id, "phase": "exited", "exit_code": 1, "reason": "no_pr",
        "started_at": "2026-10-04T09:00:00Z", "exited_at": "2026-10-04T09:05:00Z",
        "supervisor_pid": None, "pr_url": None,
    }))

    again = await auth_client.get(
        f"/api/v1/heads/{run_id}/chat/history?limit=1000", headers={"If-None-Match": etag1}
    )
    assert again.status_code == 200, "a state change must never be served as a stale 304"
    body = again.json()
    assert body["session"]["aliveness"] == "ended"
    # Review finding on PR #751: ``live`` used to come from read_history's
    # own mtime-recency heuristic (the transcript file is untouched here,
    # so that heuristic alone would still say True) rather than from the
    # derived head state — contradicting the "never from pane/process,
    # only head state" contract and able to mislead a PR 2 UI into showing
    # an ended head as still live.
    assert body["session"]["live"] is False, "an ended head must never report live:true"
    assert again.headers["ETag"] != etag1


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


async def test_chat_history_unknown_role_is_403(client, heads_root):
    """The PR body's test list claims "auth (401/403/200)" — 401
    (test_chat_history_requires_login_viewer_may_read, below) and 200 were
    covered; 403 was not (review finding on PR #751). A user whose role is
    not in ``ROLE_HIERARCHY`` (corrupted data, a role removed from the enum
    on a rolling deploy) must still be refused, not default-allowed —
    ``require_role(Role.VIEWER)`` has no room between "no role" (401,
    unauthenticated) and "viewer" (the lowest role that exists), so this is
    the only way 403 is reachable on a viewer-gated endpoint at all."""
    from app.auth import create_access_token
    from app.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    run_id = install_fixture(heads_root, "omp-run")
    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"u-{uid.hex[:6]}@mc.local", name="U", role="deprecated-role", is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), 'deprecated-role')}"
    resp = await client.get(f"/api/v1/heads/{run_id}/chat/history")
    assert resp.status_code == 403


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
