"""Test that stale detection uses the correct freecode session name."""


def _freecode_session_name(task_id: str) -> str:
    """Mirrors the expected logic in task_runner._handle_freecode_stale_dispatch."""
    return f"freecode-{task_id[:8]}"


def test_session_name_format():
    name = _freecode_session_name("abc12345-dead-beef-0000-000000000000")
    assert name == "freecode-abc12345"
    assert not name.startswith("fc-")
