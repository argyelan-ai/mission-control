import pytest
from app.services.vault_cleanup import (
    is_h1_audit_trail,
    is_h2_reflection_or_stub,
    is_h3_test_or_failed,
    classify,
    NoteSample,
)


def make_note(agent="system", note_type="journal", tags=None, content="x" * 200):
    return NoteSample(
        path="memory/x.md",
        agent=agent,
        note_type=note_type,
        tags=tags or [],
        content=content,
    )


# H1
def test_h1_matches_system_journal_with_auto_tag():
    note = make_note(agent="system", note_type="journal", tags=["auto", "task_done"])
    assert is_h1_audit_trail(note) is True

def test_h1_rejects_user_journal():
    note = make_note(agent="hermes", note_type="journal", tags=["auto"])
    assert is_h1_audit_trail(note) is False

def test_h1_rejects_system_lesson():
    note = make_note(agent="system", note_type="lesson", tags=["auto"])
    assert is_h1_audit_trail(note) is False

def test_h1_rejects_system_journal_without_auto_tag():
    note = make_note(agent="system", note_type="journal", tags=["manual"])
    assert is_h1_audit_trail(note) is False


# H2
def test_h2_matches_reflection_fold_tag():
    note = make_note(tags=["reflection_fold"])
    assert is_h2_reflection_or_stub(note) is True

def test_h2_matches_short_content():
    note = make_note(content="x" * 149)
    assert is_h2_reflection_or_stub(note) is True

def test_h2_rejects_long_content_without_reflection_fold():
    note = make_note(content="x" * 200, tags=["manual"])
    assert is_h2_reflection_or_stub(note) is False

def test_h2_matches_pointer_stub():
    note = make_note(content="Datei: `/home/agent/work/output.md`")
    assert is_h2_reflection_or_stub(note) is True


# H3
def test_h3_matches_test_agent():
    note = make_note(agent="tester")
    assert is_h3_test_or_failed(note) is True

def test_h3_matches_failed_task_prefix():
    note = make_note(content="**Task fehlgeschlagen:** synthetic-Bug18Test")
    assert is_h3_test_or_failed(note) is True

def test_h3_matches_test_project_tag():
    note = make_note(tags=["test-project"])
    assert is_h3_test_or_failed(note) is True

def test_h3_rejects_normal_task_comment():
    note = make_note(content="**Task erledigt:** real-feature implementation")
    assert is_h3_test_or_failed(note) is False


# classify dispatcher
def test_classify_h1_returns_bucket_and_confidence():
    note = make_note(agent="system", note_type="journal", tags=["auto"])
    assert classify(note) == ("H1", 0.98)

def test_classify_returns_none_when_no_heuristic_matches():
    note = make_note(agent="hermes", note_type="knowledge", tags=["manual"], content="x" * 500)
    assert classify(note) is None

def test_classify_h2_only_when_no_h1():
    # short content + auto tag but user agent → H2 (not H1)
    note = make_note(agent="hermes", note_type="journal", tags=["auto"], content="short")
    assert classify(note) == ("H2", 0.95)
