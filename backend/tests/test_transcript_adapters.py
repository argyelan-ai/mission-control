"""Tests fuer das Adapter-Register der Sessions-Chat-Ansicht.

Das Register beantwortet die Frage „welche CLI lese ich hier eigentlich".
Vorher war sie fest mit „Claude Code" beantwortet — mit drei Folgen, die alle
drei still falsch waren (siehe Modul-Docstring von ``transcript_adapters``).
Diese Tests halten die Weichenstellung fest.
"""
import pytest

import dataclasses
from pathlib import Path
from app.services import omp_chat, pane_state, transcript_chat
from app.services.transcript_adapters import TranscriptAdapter, adapter_for


class _Agent:
    def __init__(self, slug="sparky", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness


def test_omp_agent_gets_the_omp_adapter():
    a = adapter_for(_Agent(harness="omp"))
    assert a.name == "omp"
    assert a.process_name == "omp"
    assert a.resolve_transcript_dir is omp_chat.resolve_transcript_dir
    assert a.find_active_session is omp_chat.find_active_session
    assert a.transcript_allowed is omp_chat.transcript_allowed
    assert a.parse_pane_state is omp_chat.parse_pane_state
    assert a.peek_entry_id is omp_chat.peek_entry_id
    assert a.transcript_suggests_turn_ended is omp_chat.transcript_suggests_turn_ended


def test_claude_agent_gets_the_claude_adapter():
    a = adapter_for(_Agent(slug="davinci", harness="claude"))
    assert a.name == "claude"
    assert a.process_name == "claude"
    assert a.resolve_transcript_dir is transcript_chat.resolve_transcript_dir
    assert a.find_active_session is transcript_chat.find_active_session
    assert a.transcript_allowed is transcript_chat.transcript_allowed
    assert a.parse_pane_state is pane_state.parse_pane_state


@pytest.mark.parametrize(
    "agent",
    [None, _Agent(harness=None), _Agent(harness="kimi"), _Agent(harness="hermes"), object()],
)
def test_unknown_harness_falls_back_to_claude(agent):
    """Kein Privacy-Loch: der Claude-Adapter entscheidet danach selbst, ob
    dieser Agent ueberhaupt ein Transkript hat. Ein fremder Harness ohne
    eigenen Adapter landet im selben Zustand wie vor dem Register."""
    assert adapter_for(agent).name == "claude"


def test_claude_parser_factory_is_stateless():
    a = adapter_for(_Agent(harness="claude"))
    assert a.new_parser() is transcript_chat.parse_transcript_line


def test_omp_parser_factory_returns_a_fresh_instance_each_time():
    """Der omp-Parser fuehrt Zustand (die Effort-Stufe) — zwei Lesevorgaenge
    duerfen ihn sich nicht teilen."""
    a = adapter_for(_Agent(harness="omp"))
    p1, p2 = a.new_parser(), a.new_parser()
    assert p1 is not p2
    assert isinstance(p1, omp_chat.OmpLineParser)


def test_every_adapter_offers_the_full_contract():
    """Ein neuer Harness darf kein Feld auslassen — sonst faellt der Kern erst
    im Betrieb auf die Nase.

    Die Feldliste wird aus der Dataclass GELESEN, nicht hier gepflegt. Die
    Vorfassung zaehlte acht Namen von Hand auf und uebersah dadurch bereits
    ``name`` und ``session_scan_root``; ein neu ergaenztes Feld waere still
    ungeprueft geblieben — also genau in dem Moment blind, fuer den der
    Waechter gedacht ist.
    """
    for harness in ("claude", "openclaude", "omp", "kimi", None):
        a = adapter_for(_Agent(harness=harness))
        for field in dataclasses.fields(TranscriptAdapter):
            value = getattr(a, field.name)
            if field.name in ("name", "process_name"):
                assert isinstance(value, str) and value, (harness, field.name)
            else:
                assert callable(value), (harness, field.name)


def test_only_claude_shaped_adapters_look_for_subagent_runs():
    """Subagenten-Dateien schreibt nur Claude Code (und sein Fork openclaude).
    omp hat nachweislich keine Sidechains — es bekommt den leeren Standard,
    ohne dass irgendwo ein ``if harness ==`` noetig waere."""
    for harness in ("claude", "openclaude"):
        a = adapter_for(_Agent(harness=harness))
        assert a.subagent_runs is transcript_chat.subagent_runs, harness

    for harness in ("omp",):
        a = adapter_for(_Agent(harness=harness))
        assert a.subagent_runs(Path("/gibt/es/nicht.jsonl")) == [], harness


def test_claude_stamp_usage_still_reaches_the_statusline_reader(tmp_path, monkeypatch):
    """Der Adapter darf die Claude-Verfeinerung nicht verlieren — sie leitet
    Wurzel + Session-ID aus dem Pfad ab, wie es ``read_history`` vorher
    selbst tat."""
    seen = {}

    def fake(ev, root, session_id):
        seen["root"] = root
        seen["session_id"] = session_id

    monkeypatch.setattr(transcript_chat, "_stamp_usage_source", fake)
    path = tmp_path / "claude-config" / "projects" / "-home-agent" / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    adapter_for(_Agent(harness="claude")).stamp_usage({"kind": "usage"}, path)
    assert seen == {"root": tmp_path / "claude-config", "session_id": "sess-1"}


def test_omp_and_claude_disagree_about_the_same_pane():
    """Die Kernbegruendung fuer das Register: dieselbe Pane-Aufnahme heisst
    fuer die beiden CLIs etwas anderes. Der Claude-Parser sieht in der
    omp-TUI nichts, was er kennt."""
    from tests.test_omp_chat import PANE_WORKING_GENERIC

    assert omp_chat.parse_pane_state(PANE_WORKING_GENERIC, False)["status"] == "working"
    assert pane_state.parse_pane_state(PANE_WORKING_GENERIC, False)["status"] == "unknown"
