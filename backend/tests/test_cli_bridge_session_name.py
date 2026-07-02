"""Unit test for cli-bridge session name logic."""


def _session_name(agent_name: str, task_id: str) -> str:
    """Mirrors the logic in scripts/cli-bridge.py."""
    return f"{agent_name}-{task_id[:8]}"


def test_session_name_freecode():
    name = _session_name("freecode", "abc12345-0000-0000-0000-000000000000")
    assert name == "freecode-abc12345"


def test_session_name_cody():
    name = _session_name("cody", "deadbeef-0000-0000-0000-000000000000")
    assert name == "cody-deadbeef"


def test_session_name_strips_to_8_chars():
    name = _session_name("rex", "1234567890abcdef")
    assert name == "rex-12345678"
