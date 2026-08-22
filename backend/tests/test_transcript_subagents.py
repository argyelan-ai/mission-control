"""Subagenten-Laeufe einer Sitzung auflisten.

Claude Code legt seit ~2.1.2xx je Subagent eine eigene Datei an:

    <projekt>/<sitzung>/subagents/agent-<name>-<hash>.jsonl
                                 agent-<name>-<hash>.meta.json

Im HAUPT-Transkript steht davon nichts mehr — live gemessen am 22.08.2026:
3444 Zeilen, davon ``isSidechain: true``: 0. Der Spawn erscheint dort nur als
Werkzeugaufruf ``Agent``. Wer den Verlauf eines Subagenten zeigen will, muss
also diese Dateien lesen; aus dem Hauptstrom ist er nicht rekonstruierbar.
"""

import json

import pytest

from app.services import transcript_chat as tc

_HEAD = tc._SUBAGENT_HEAD_LINES


def _make_session(tmp_path, name="sess1"):
    """Legt <tmp>/sess1.jsonl an und gibt (pfad, subagents-ordner) zurueck."""
    session = tmp_path / f"{name}.jsonl"
    session.write_text('{"type":"user"}\n', encoding="utf-8")
    subdir = tmp_path / name / "subagents"
    subdir.mkdir(parents=True)
    return session, subdir


def _write_run(subdir, run_id, meta=None, started="2026-08-22T10:00:00.000Z"):
    (subdir / f"agent-{run_id}.jsonl").write_text(
        json.dumps({"agentId": run_id, "timestamp": started, "type": "user"}) + "\n",
        encoding="utf-8",
    )
    if meta is not None:
        (subdir / f"agent-{run_id}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )


def test_no_subagents_dir_is_simply_empty(tmp_path):
    """13,7 % der Sitzungen mit Agent-Aufruf haben keinen Ordner (gemessen).
    Das ist kein Fehler und darf nichts protokollieren."""
    session = tmp_path / "s.jsonl"
    session.write_text("{}\n", encoding="utf-8")

    assert tc.subagent_runs(session) == []


def test_lists_a_run_with_its_profile(tmp_path):
    session, subdir = _make_session(tmp_path)
    _write_run(
        subdir,
        "afinal-review-dc72e94a",
        {
            "agentType": "final-review",
            "name": "final-review",
            "description": "Final-Review der ganzen Branch",
            "model": "claude-opus-5",
            "color": "green",
            "teamName": "session-964aa71d",
        },
    )

    runs = tc.subagent_runs(session)

    assert len(runs) == 1
    assert runs[0]["runId"] == "afinal-review-dc72e94a"
    assert runs[0]["name"] == "final-review"
    assert runs[0]["agentType"] == "final-review"
    assert runs[0]["description"] == "Final-Review der ganzen Branch"
    assert runs[0]["model"] == "claude-opus-5"
    assert runs[0]["color"] == "green"
    assert runs[0]["teamName"] == "session-964aa71d"


def test_a_run_without_a_profile_is_still_listed(tmp_path):
    """Nur ``agentType`` ist in 100 % der Steckbriefe vorhanden, ``name`` in
    50 %, ``model`` in 57 % (gemessen ueber 754 Steckbriefe). Ein Lauf ohne
    Steckbrief verschwindet trotzdem nicht — er hat einen Verlauf, den man
    zeigen kann. Fehlende Felder sind ``None``, nicht erfunden."""
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "a46a1b292e22b4f7c", meta=None)

    runs = tc.subagent_runs(session)

    assert len(runs) == 1
    assert runs[0]["runId"] == "a46a1b292e22b4f7c"
    assert runs[0]["name"] is None
    assert runs[0]["agentType"] is None


def test_a_broken_profile_does_not_lose_the_run(tmp_path):
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "akaputt", {"agentType": "x"})
    (subdir / "agent-akaputt.meta.json").write_text("{kein json", encoding="utf-8")

    runs = tc.subagent_runs(session)

    assert [r["runId"] for r in runs] == ["akaputt"]
    assert runs[0]["name"] is None


def test_internal_helpers_and_the_journal_are_not_subagents(tmp_path):
    """``journal.jsonl`` ist das Protokoll eines Workflows, ``aside_question``
    und ``acompact`` sind CLI-interne Hilfsagenten. Keiner davon ist ein
    delegierter Auftrag, den ein Operator sehen will."""
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "aechter-lauf", {"agentType": "worker"})
    _write_run(subdir, "aside_question-abc", {"agentType": "aside_question"})
    _write_run(subdir, "acompact-def", {"agentType": "compact"})
    (subdir / "journal.jsonl").write_text("{}\n", encoding="utf-8")

    assert [r["runId"] for r in tc.subagent_runs(session)] == ["aechter-lauf"]


def test_runs_come_back_in_start_order(tmp_path):
    """Die Reihenfolge ist die Startreihenfolge, nicht die alphabetische —
    sonst zeigte die Oberflaeche eine Abfolge, die es nie gab."""
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "azzz-frueh", {"agentType": "a"}, started="2026-08-22T10:00:00.000Z")
    _write_run(subdir, "aaaa-spaet", {"agentType": "b"}, started="2026-08-22T11:00:00.000Z")

    assert [r["runId"] for r in tc.subagent_runs(session)] == ["azzz-frueh", "aaaa-spaet"]


def test_the_workflows_subtree_stays_out(tmp_path):
    """396 von 1245 Subagenten-Dateien liegen unter ``subagents/workflows/``.
    Deren Steckbriefe tragen ausser ``agentType`` praktisch nichts und sie
    gehoeren zu einem eigenen Thema. Flach lesen, nicht absteigen."""
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "aoben", {"agentType": "a"})
    nested = subdir / "workflows"
    nested.mkdir()
    _write_run(nested, "aunten", {"agentType": "b"})

    assert [r["runId"] for r in tc.subagent_runs(session)] == ["aoben"]


def test_an_unreadable_directory_is_empty_not_an_error(tmp_path):
    """Gleiche Hausregel wie bei den Nachbarn: nie werfen. Ein Fehler beim
    Auflisten darf den ganzen Verlauf nicht mitreissen."""
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "aegal", {"agentType": "a"})
    subdir.chmod(0o000)
    try:
        assert tc.subagent_runs(session) == []
    finally:
        subdir.chmod(0o755)


def test_only_the_head_of_a_run_is_read(tmp_path):
    """Eine gemessene Subagenten-Datei war 13,8 MB gross. Der Startzeitpunkt
    steht in der ersten Zeile — der Rest wird nicht angefasst.

    Geprueft wird das an einer Datei, deren Zeitstempel ABSICHTLICH ausserhalb
    des Kopfes liegt: wer die ganze Datei liest, findet ihn und faellt hier
    durch. Die erste Fassung dieses Tests legte den Zeitstempel in Zeile 1 —
    sie war damit auch ohne Lesegrenze gruen und prueft nichts (Sabotage-Probe
    22.08.2026).
    """
    session, subdir = _make_session(tmp_path)
    path = subdir / "agent-atief.jsonl"
    kopflos = [json.dumps({"type": "filler"}) for _ in range(_HEAD + 20)]
    kopflos.append(json.dumps({"timestamp": "2026-08-22T09:00:00.000Z"}))
    path.write_text("\n".join(kopflos) + "\n", encoding="utf-8")
    (subdir / "agent-atief.meta.json").write_text('{"agentType":"a"}', encoding="utf-8")

    runs = tc.subagent_runs(session)

    assert len(runs) == 1
    assert runs[0]["startedAt"] is None, "die ganze Datei wurde gelesen"


def test_the_start_time_comes_from_the_head(tmp_path):
    """Gegenprobe zum Test darueber: liegt der Zeitstempel IM Kopf, wird er
    auch gefunden. Ohne diese Haelfte koennte die Lesegrenze auf 0 stehen und
    der Test oben bliebe gruen."""
    session, subdir = _make_session(tmp_path)
    _write_run(subdir, "aoben", {"agentType": "a"}, started="2026-08-22T09:00:00.000Z")

    assert tc.subagent_runs(session)[0]["startedAt"] == "2026-08-22T09:00:00.000Z"
