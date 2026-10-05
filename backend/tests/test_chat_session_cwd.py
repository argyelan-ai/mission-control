"""``TranscriptAdapter.session_cwd`` — where a chat session works.

The chat's diff panel shows the repository the SESSION works in (operator
finding 04.10.2026: it showed a months-old commit of a finished task while
the chat had just committed elsewhere). Each adapter reads the folder from
its own transcript format; these tests pin that per harness.
"""
import json as _json

from app.services import omp_chat, transcript_chat
from app.services.transcript_adapters import adapter_for


class _Agent:
    def __init__(self, harness):
        self.slug = "agent"
        self.agent_runtime = "cli-bridge"
        self.harness = harness




def test_session_cwd_is_wired_per_harness():
    assert adapter_for(_Agent(harness="omp")).session_cwd is omp_chat.session_cwd
    assert adapter_for(_Agent(harness="hermes")).session_cwd is omp_chat.session_cwd
    for harness in ("claude", "openclaude", "kimi", None):
        assert adapter_for(_Agent(harness=harness)).session_cwd is transcript_chat.session_cwd, harness


def test_claude_session_cwd_is_the_last_recorded_cwd(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        _json.dumps({"type": "user", "cwd": "/home/agent", "uuid": "1"}) + "\n"
        + _json.dumps({"type": "assistant", "cwd": "/workspace/proj", "uuid": "2"}) + "\n"
        + _json.dumps({"type": "summary"}) + "\n"
    )
    assert transcript_chat.session_cwd(f) == "/workspace/proj"


def test_claude_session_cwd_falls_back_to_the_head_when_the_tail_has_none(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript_chat, "_SESSION_CWD_TAIL_BYTES", 64)
    f = tmp_path / "s.jsonl"
    f.write_text(
        _json.dumps({"type": "user", "cwd": "/workspace/first", "uuid": "1"}) + "\n"
        + _json.dumps({"type": "assistant", "message": "x" * 400}) + "\n"
    )
    assert transcript_chat.session_cwd(f) == "/workspace/first"


def test_claude_session_cwd_none_without_cwd_or_file(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_json.dumps({"type": "summary"}) + "\n")
    assert transcript_chat.session_cwd(f) is None
    assert transcript_chat.session_cwd(tmp_path / "missing.jsonl") is None


def _omp_line(**kw) -> str:
    return _json.dumps(kw) + "\n"


def test_omp_session_cwd_reads_the_session_header(tmp_path):
    f = tmp_path / "2026-10-04T12-16-36-679Z_abc.jsonl"
    f.write_text(_omp_line(type="title", title="") + _omp_line(type="session", id="abc", cwd="/workspace"))
    assert omp_chat.session_cwd(f) == "/workspace"


def test_omp_session_cwd_asks_the_sibling_when_the_acp_sink_header_is_empty(tmp_path):
    """Live layout 04.10.2026: the ACP bridge sink file (newest, the one the
    chat tails) records ``cwd: ""``; omp's own file of the SAME session has
    the real folder."""
    sink = tmp_path / "2026-10-04T12-16-36_abc.jsonl"
    sink.write_text(_omp_line(type="session", id="abc", cwd="", bridge="acp"))
    native = tmp_path / "2026-10-04T12-16-36-679Z_abc.jsonl"
    native.write_text(_omp_line(type="title") + _omp_line(type="session", id="abc", cwd="/workspace/proj"))
    other = tmp_path / "2026-10-01T09-00-00-000Z_zzz.jsonl"
    other.write_text(_omp_line(type="session", id="zzz", cwd="/workspace/foreign"))

    assert omp_chat.session_cwd(sink) == "/workspace/proj"


def test_omp_session_cwd_never_borrows_from_another_session(tmp_path):
    sink = tmp_path / "2026-10-04T12-16-36_abc.jsonl"
    sink.write_text(_omp_line(type="session", id="abc", cwd=""))
    (tmp_path / "2026-10-01T09-00-00-000Z_zzz.jsonl").write_text(
        _omp_line(type="session", id="zzz", cwd="/workspace/foreign")
    )
    assert omp_chat.session_cwd(sink) is None
