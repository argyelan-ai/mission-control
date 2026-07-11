"""Tests for `mc finish` preflight + idempotency (2026-05-16 DNA-PDF Bug).

Background: a researcher posted 3 reflection-comments in 53 s for the same
task because `mc finish` POSTed the comment, the PATCH then failed with
422 (open checklist), the agent saw Exit 1, retried, and hit the same
trap again. Fix: pre-flight every Backend gate the PATCH would check, so
a doomed call never leaves a junk reflection behind. Plus dedup-window
for an honest retry to skip the second POST.

Each test mocks the http Client to verify which endpoints get hit AND in
what order — preflight failures must NOT trigger POST.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────

GOOD_REFLECTION = (
    "## Was wurde gemacht\nDNA PDF generiert mit allen Sektionen aus dem Vault.\n\n"
    "## Was hat funktioniert\nmc pdf mit stdin heredoc, Vault-Suche lieferte voller Content.\n\n"
    "## Was war unklar\nTask-Briefing recht offen, musste aus Vault rekonstruieren.\n\n"
    "## Lesson fuer Agent-Memory\nFuer Brand-DNA-Tasks immer zuerst Vault scannen.\n"
)

BOARD_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
AGENT_ID = "33333333-3333-3333-3333-333333333333"


class _Args:
    def __init__(self, message=GOOD_REFLECTION, review=False, task_id=None):
        self.message = message
        self.review = review
        self.task_id = task_id


def _mock_cfg(monkeypatch=None):
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, TASK_ID)
    return cfg


def _mock_client(responses):
    """responses: list of (method, path_substr, value-or-exception).

    Each .request() call pops the matching response. Order matters.
    """
    client = MagicMock()
    calls = []

    def request(method, path, body=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        for i, (m, p, value) in enumerate(responses):
            if m == method and p in path:
                responses.pop(i)
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(
            f"unmocked request: {method} {path}\nremaining: {responses}"
        )

    client.request.side_effect = request
    client.calls = calls
    return client


def _task(status="in_progress", agent_id=AGENT_ID):
    return {"id": TASK_ID, "status": status, "assigned_agent_id": agent_id}


def _checklist(*items):
    return [{"id": f"id-{i}", "title": t, "status": s} for i, (t, s) in enumerate(items)]


# ── E1: Open checklist items → fail fast, no POST ──────────────────────────


def test_open_checklist_blocks_before_post():
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", _checklist(("PDF erstellen", "pending"), ("Deliverable registrieren", "done"))),
    ])
    with pytest.raises(UsageError) as exc:
        commands._cmd_finish(_Args(), client, cfg)
    assert "1 Checklist-Item(s) noch offen" in str(exc.value)
    assert "PDF erstellen" in str(exc.value)
    # Critical: no POST happened.
    assert not any(c["method"] == "POST" for c in client.calls)


def test_multiple_open_checklist_items_show_first_three():
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", _checklist(
            ("a", "pending"), ("b", "pending"), ("c", "pending"),
            ("d", "pending"), ("e", "pending"),
        )),
    ])
    with pytest.raises(UsageError) as exc:
        commands._cmd_finish(_Args(), client, cfg)
    msg = str(exc.value)
    assert "5 Checklist-Item(s) noch offen" in msg
    assert "+ 2 weitere" in msg


def test_skipped_items_dont_block():
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", _checklist(("a", "done"), ("b", "skipped"))),
        ("GET", "/comments", []),
        ("POST", "/comments", {"id": "comment-1"}),
        ("PATCH", "/tasks/", {"status": "done"}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0


# ── E2: Task already at target status → idempotent skip ────────────────────


def test_task_already_done_skips_both_post_and_patch():
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task(status="done")),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0
    # No POST, no PATCH — pure idempotent no-op.
    assert not any(c["method"] in ("POST", "PATCH") for c in client.calls)


def test_task_already_review_with_review_flag_skips():
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task(status="review")),
    ])
    rc = commands._cmd_finish(_Args(review=True), client, cfg)
    assert rc == 0
    assert not any(c["method"] in ("POST", "PATCH") for c in client.calls)


# ── E3: Wrong source status → fail fast ────────────────────────────────────


@pytest.mark.parametrize("bad_status", ["inbox", "blocked", "failed"])
def test_invalid_source_status_blocks(bad_status):
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task(status=bad_status)),
    ])
    with pytest.raises(UsageError) as exc:
        commands._cmd_finish(_Args(), client, cfg)
    assert bad_status in str(exc.value)
    assert "in_progress" in str(exc.value) and "review" in str(exc.value)
    assert not any(c["method"] == "POST" for c in client.calls)


# ── E5: Literal `\n` shell-escape → fail with hint ─────────────────────────


def test_literal_backslash_n_rejected():
    bad = (
        "## Was wurde gemacht\\nfoo\\n## Was hat funktioniert\\nbar\\n"
        "## Was war unklar\\nbaz\\n## Lesson fuer Agent-Memory\\nqux qux qux qux"
    )
    cfg = _mock_cfg()
    client = MagicMock()
    with pytest.raises(UsageError) as exc:
        commands._cmd_finish(_Args(message=bad), client, cfg)
    assert "literal" in str(exc.value).lower() and "newline" in str(exc.value).lower()
    # Pre-validation runs BEFORE any HTTP — client untouched.
    client.request.assert_not_called()


def test_real_newlines_pass_validation():
    """Sanity guard: the legit form must NOT trigger the literal-\\n check."""
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/comments", {"id": "c"}),
        ("PATCH", "/tasks/", {}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0


# ── E6: Recent reflection from same agent → skip POST, only PATCH ──────────


def test_recent_self_reflection_skips_post_runs_patch():
    """Honest retry path: previous reflection exists in dedup window, second
    `mc finish` call should ONLY run the status PATCH — no junk dupe."""
    import datetime as _dt
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat()
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", [{
            "comment_type": "reflection",
            "author_type": "agent",
            "author_agent_id": AGENT_ID,
            "created_at": recent,
        }]),
        # No POST expected.
        ("PATCH", "/tasks/", {"status": "done"}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0
    assert not any(c["method"] == "POST" for c in client.calls)


def test_old_reflection_does_not_dedup():
    """Reflection > dedup window ago → still POST a fresh one (legitimate
    second finish on a separate work session, e.g. after re-open)."""
    import datetime as _dt
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).isoformat()
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", [{
            "comment_type": "reflection",
            "author_type": "agent",
            "author_agent_id": AGENT_ID,
            "created_at": old,
        }]),
        ("POST", "/comments", {"id": "c"}),
        ("PATCH", "/tasks/", {}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0


def test_other_agents_reflection_does_not_dedup():
    """Reflection from a DIFFERENT agent in the dedup window must not block
    THIS agent's reflection — each agent owns its own audit-trail row."""
    import datetime as _dt
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat()
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", [{
            "comment_type": "reflection",
            "author_type": "agent",
            "author_agent_id": "different-agent-uuid",
            "created_at": recent,
        }]),
        ("POST", "/comments", {"id": "c"}),
        ("PATCH", "/tasks/", {}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0


def test_progress_comment_does_not_count_as_reflection():
    """A recent `progress` comment must NOT trigger the dedup-skip — only
    actual `reflection`-type comments count."""
    import datetime as _dt
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat()
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", [{
            "comment_type": "progress",
            "author_type": "agent",
            "author_agent_id": AGENT_ID,
            "created_at": recent,
        }]),
        ("POST", "/comments", {"id": "c"}),
        ("PATCH", "/tasks/", {}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0


# ── E7: PATCH fails after successful POST → clear retry guidance ───────────


def test_patch_failure_after_post_surfaces_recovery_hint(capsys):
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/comments", {"id": "c-1"}),
        ("PATCH", "/tasks/", RuntimeError("HTTP 500 backend down")),
    ])
    with pytest.raises(RuntimeError):
        commands._cmd_finish(_Args(), client, cfg)
    err = capsys.readouterr().err
    assert "Reflexion wurde gepostet" in err
    assert "mc done" in err  # default target → mc done


def test_patch_failure_with_review_flag_suggests_mc_review(capsys):
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/comments", {"id": "c-1"}),
        ("PATCH", "/tasks/", RuntimeError("HTTP 500")),
    ])
    with pytest.raises(RuntimeError):
        commands._cmd_finish(_Args(review=True), client, cfg)
    err = capsys.readouterr().err
    assert "mc review" in err


def test_patch_failure_after_skipped_post_no_recovery_hint(capsys):
    """If we skipped the POST (recent reflection dedup), a PATCH-fail must
    NOT pretend a comment was just posted — that would be misleading."""
    import datetime as _dt
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat()
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", [{
            "comment_type": "reflection",
            "author_type": "agent",
            "author_agent_id": AGENT_ID,
            "created_at": recent,
        }]),
        ("PATCH", "/tasks/", RuntimeError("HTTP 500")),
    ])
    with pytest.raises(RuntimeError):
        commands._cmd_finish(_Args(), client, cfg)
    err = capsys.readouterr().err
    # Wrong message would be: "Reflexion wurde gepostet" — assert it isn't.
    assert "Reflexion wurde gepostet" not in err


# ── Reflection content validation (existing path, expanded coverage) ───────


def test_missing_required_field_rejected():
    bad = (
        "## Was wurde gemacht\nfoo\n\n"
        "## Was hat funktioniert\nbar\n\n"
        "## Was war unklar\nbaz\n"
        # missing "Lesson fuer Agent-Memory"
    )
    with pytest.raises(UsageError) as exc:
        commands._validate_reflection(bad)
    assert "Lesson" in str(exc.value)


def test_too_short_rejected(monkeypatch):
    """Length-floor exists but the four header strings already total ~90
    chars, so monkeypatch MIN_CHARS to make the floor verifiable."""
    monkeypatch.setattr(commands, "REFLECTION_MIN_CHARS", 500)
    short = (
        "## Was wurde gemacht\nx\n## Was hat funktioniert\ny\n"
        "## Was war unklar\nz\n## Lesson fuer Agent-Memory\nq\n"
    )
    with pytest.raises(UsageError) as exc:
        commands._validate_reflection(short)
    assert "zu kurz" in str(exc.value).lower()


# ── Forgiving validation (B1): trivial local-model variance must pass ──────


def test_validate_accepts_hash_level_variants():
    """### / # header levels are accepted (not only ##)."""
    text = (
        "### Was wurde gemacht\n" + "a" * 25 + "\n"
        "# Was hat funktioniert\n" + "b" * 25 + "\n"
        "### Was war unklar\n" + "c" * 25 + "\n"
        "### Lesson fuer Agent-Memory\n" + "d" * 25
    )
    commands._validate_reflection(text)  # must not raise


def test_validate_accepts_english_headers():
    text = (
        "## What was done\n" + "a" * 25 + "\n"
        "## What worked\n" + "b" * 25 + "\n"
        "## What was unclear\n" + "c" * 25 + "\n"
        "## Lesson for agent memory\n" + "d" * 25
    )
    commands._validate_reflection(text)  # must not raise


def test_validate_accepts_fuer_umlaut_and_case():
    text = (
        "## was wurde gemacht:\n" + "a" * 25 + "\n"
        "## Was hat funktioniert\n" + "b" * 25 + "\n"
        "## Was war unklar\n" + "c" * 25 + "\n"
        "## Lesson für Agent-Memory\n" + "d" * 25
    )
    commands._validate_reflection(text)  # must not raise


def test_validate_canonical_still_passes():
    commands._validate_reflection(GOOD_REFLECTION)  # byte-identical strict path


def test_finish_normalizes_english_headers_before_post():
    """`mc finish` with English headers -> POSTed reflection carries canonical
    German headers so the memory pipeline sees them."""
    english = (
        "## What was done\n" + "a" * 25 + "\n"
        "## What worked\n" + "b" * 25 + "\n"
        "## What was unclear\n" + "c" * 25 + "\n"
        "## Lesson for agent memory\n" + "d" * 25
    )
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/comments", {"id": "c"}),
        ("PATCH", "/tasks/", {}),
    ])
    rc = commands._cmd_finish(_Args(message=english), client, cfg)
    assert rc == 0
    post = next(c for c in client.calls if c["method"] == "POST")
    content = post["body"]["content"]
    assert "## Was wurde gemacht" in content
    assert "## Lesson fuer Agent-Memory" in content
    assert "What was done" not in content


# ── `mc checklist skip <id>` — out-of-role items (2026-07-08 handoff fix) ───
#
# An agent can hit a checklist item it physically cannot do (a live Vercel
# deploy needing npm/node = a Deployer's job, not an omp agent's). Before this
# fix, the only options were `mc checklist done` (a lie) or leaving `mc
# finish` blocked forever. `skip` reuses the existing `skipped` status, which
# `_preflight_finish` already treats as non-blocking (see
# test_skipped_items_dont_block above).


class _ChecklistSkipArgs:
    def __init__(self, item_id="id-0", reason=None):
        self.action = "skip"
        self.item_id = item_id
        self.reason = reason


def _mock_cfg_checklist():
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, TASK_ID)
    return cfg


def test_checklist_skip_patches_status_skipped():
    cfg = _mock_cfg_checklist()
    client = _mock_client([
        ("GET", "/checklist", _checklist(("Vercel Deploy", "pending"))),
        ("PATCH", "/checklist/id-0", {"id": "id-0", "status": "skipped"}),
    ])
    rc = commands._cmd_checklist(_ChecklistSkipArgs(item_id="id-0"), client, cfg)
    assert rc == 0
    patch_call = next(c for c in client.calls if c["method"] == "PATCH")
    assert patch_call["body"] == {"status": "skipped"}
    assert not any(c["method"] == "POST" for c in client.calls)


def test_checklist_skip_with_reason_posts_comment():
    cfg = _mock_cfg_checklist()
    client = _mock_client([
        ("GET", "/checklist", _checklist(("Vercel Deploy", "pending"))),
        ("PATCH", "/checklist/id-0", {"id": "id-0", "status": "skipped"}),
        ("POST", "/comments", {"id": "comment-1"}),
    ])
    rc = commands._cmd_checklist(
        _ChecklistSkipArgs(item_id="id-0", reason="needs npm/node, out of role for omp agent"),
        client, cfg,
    )
    assert rc == 0
    post_call = next(c for c in client.calls if c["method"] == "POST")
    assert "id-0" in post_call["body"]["content"]
    assert "skipped: needs npm/node" in post_call["body"]["content"]


def test_checklist_skip_without_reason_no_comment():
    cfg = _mock_cfg_checklist()
    client = _mock_client([
        ("GET", "/checklist", _checklist(("Vercel Deploy", "pending"))),
        ("PATCH", "/checklist/id-0", {"id": "id-0", "status": "skipped"}),
    ])
    rc = commands._cmd_checklist(_ChecklistSkipArgs(item_id="id-0"), client, cfg)
    assert rc == 0
    assert not any(c["method"] == "POST" for c in client.calls)


def test_checklist_skip_unknown_id_raises_usage_error():
    cfg = _mock_cfg_checklist()
    client = _mock_client([
        ("GET", "/checklist", _checklist(("Vercel Deploy", "pending"))),
    ])
    with pytest.raises(UsageError):
        commands._cmd_checklist(_ChecklistSkipArgs(item_id="nonexistent"), client, cfg)
