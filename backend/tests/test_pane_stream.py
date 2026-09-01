"""Der Terminal-Strom: an- und abschalten, und wo er landet (Live-Schicht P1b).

``tmux pipe-pane`` schreibt die Ausgabe eines Panes fortlaufend in eine Datei —
Push statt Abfrage. Die Datei liegt in ``/home/agent/.claude``, und dieses
Verzeichnis ist bei JEDEM Agenten-Container auf den Host gemountet (live
geprueft fuer claude, omp und kimi). Damit liest das Backend den Strom mit
derselben Mechanik wie ein Transkript — kein neuer Transportweg.
"""
import subprocess
from pathlib import Path

import pytest

from app.services import pane_stream


class _StubAgent:
    def __init__(self, agent_runtime="cli-bridge", slug="rex", name="Rex"):
        self.agent_runtime = agent_runtime
        self.slug = slug
        self.name = name


def test_stream_path_lives_in_the_shared_config_mount(monkeypatch, tmp_path):
    monkeypatch.setattr(pane_stream, "AGENTS_ROOT", tmp_path)
    path = pane_stream.stream_path_for(_StubAgent(slug="rex"))
    assert path == tmp_path / "rex" / "claude-config" / pane_stream.STREAM_FILENAME


def test_host_agents_have_no_stream():
    assert pane_stream.stream_path_for(_StubAgent(agent_runtime="host", slug="boss")) is None


@pytest.mark.asyncio
async def test_start_truncates_and_pipes_into_the_shared_file(monkeypatch, tmp_path):
    """Beim Einschalten wird die Datei geleert.

    Sonst liest der erste Poll den Rest der letzten Sitzung als frischen Text —
    genau der Fehler, der beim Transkript-Tailer schon einmal Historie als
    'live' ausgab."""
    monkeypatch.setattr(pane_stream, "AGENTS_ROOT", tmp_path)
    stale = tmp_path / "rex" / "claude-config" / pane_stream.STREAM_FILENAME
    stale.parent.mkdir(parents=True)
    stale.write_text("Reste der letzten Sitzung")

    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    path = await pane_stream.start(_StubAgent(slug="rex"))

    assert path == stale
    assert stale.read_text() == "", "die Reste der letzten Sitzung stehen noch da"
    assert calls[-1][:7] == ["docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent", "mc-agent-rex"]
    assert calls[-1][7:11] == ["tmux", "pipe-pane", "-t", "rex:0"]
    assert "-O" in calls[-1]
    assert any(pane_stream.STREAM_FILENAME in part for part in calls[-1])


@pytest.mark.asyncio
async def test_stop_switches_the_pipe_off_without_a_target_command(monkeypatch, tmp_path):
    """``pipe-pane`` ohne Kommando schaltet ab — mit Kommando liefe ein zweiter
    Schreiber weiter und die Datei wuechse ohne Zuschauer."""
    monkeypatch.setattr(pane_stream, "AGENTS_ROOT", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: (calls.append(list(argv)),
                            subprocess.CompletedProcess(argv, 0, "", ""))[1],
    )

    await pane_stream.stop(_StubAgent(slug="rex"))

    assert calls[-1][7:] == ["tmux", "pipe-pane", "-t", "rex:0"]


@pytest.mark.asyncio
async def test_start_never_raises_when_docker_is_unreachable(monkeypatch, tmp_path):
    """Ein fehlender Strom darf den Chat nie beschaedigen — er ist die Kuer,
    das Transkript ist die Pflicht."""
    monkeypatch.setattr(pane_stream, "AGENTS_ROOT", tmp_path)

    def _boom(argv, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert await pane_stream.start(_StubAgent(slug="rex")) is None
