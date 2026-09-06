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


# ── Pane-Groesse ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pane_size_reads_width_and_height_from_tmux(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stdout="168x45\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert await pane_stream.pane_size(_StubAgent(slug="rex")) == (168, 45)
    assert calls[-1][7:9] == ["tmux", "display"]
    assert "rex:0" in calls[-1]


@pytest.mark.asyncio
async def test_pane_size_falls_back_to_80x24_when_tmux_is_unreachable(monkeypatch):
    def _fake_run(argv, **kwargs):
        raise OSError("docker weg")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert await pane_stream.pane_size(_StubAgent(slug="rex")) == (80, 24)


# ── Abgerissener Strom (Container-Neustart) ───────────────────────────────────
#
# Ein Runtime-Wechsel startet den Container neu; der neue tmux-Server kennt
# das ``pipe-pane`` des alten nicht mehr. Der Chat-Tailer schaltete den Strom
# nur einmal beim Start ein — danach stand der Emulator auf dem letzten Bild
# vor dem Neustart, und jede neue Frage zeigte die alte Historie, bis das
# Transkript sie abloeste (06.09.2026, omp-Agent).


@pytest.mark.asyncio
async def test_is_piping_reads_the_pane_pipe_flag_from_tmux(monkeypatch):
    seen: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stdout="1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert await pane_stream.is_piping(_StubAgent(slug="rex")) is True
    assert seen[-1][7:] == ["tmux", "display", "-p", "-t", "rex:0", "#{pane_pipe}"]

    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, returncode=0, stdout="0\n", stderr=""),
    )
    assert await pane_stream.is_piping(_StubAgent(slug="rex")) is False


@pytest.mark.asyncio
async def test_is_piping_is_unknown_when_tmux_does_not_answer(monkeypatch):
    """Kein Urteil ohne Antwort: ein Timeout darf NICHT als 'abgerissen'
    gelten, sonst wuerde der Tailer bei jedem Docker-Schluckauf den Emulator
    leeren."""
    def _boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", _boom)
    assert await pane_stream.is_piping(_StubAgent(slug="rex")) is None
    assert await pane_stream.is_piping(_StubAgent(agent_runtime="host", slug="boss")) is None


@pytest.mark.asyncio
async def test_ensure_restarts_the_pipe_only_when_it_is_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(pane_stream, "AGENTS_ROOT", tmp_path)
    calls: list[list[str]] = []
    pipe_state = {"out": "0\n"}

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stdout=pipe_state["out"], stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Abgerissen -> neu eingeschaltet, Pfad kommt zurueck.
    path = await pane_stream.ensure(_StubAgent(slug="rex"))
    assert path == tmp_path / "rex" / "claude-config" / pane_stream.STREAM_FILENAME
    assert any("pipe-pane" in c for c in calls), "der Strom wurde nicht neu eingeschaltet"

    # Laeuft -> nichts anfassen (kein zweiter Schreiber, keine Leerung).
    calls.clear()
    pipe_state["out"] = "1\n"
    assert await pane_stream.ensure(_StubAgent(slug="rex")) is None
    assert not any("pipe-pane" in c for c in calls)

    # Unbekannt (tmux stumm) -> ebenfalls nichts anfassen.
    calls.clear()
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(OSError("docker weg")))
    assert await pane_stream.ensure(_StubAgent(slug="rex")) is None
