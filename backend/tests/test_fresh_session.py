"""Frische Sitzung ohne Datei — der ``/new``-Fall bei omp.

Befund 01.09.2026 (Operator, live nachgemessen an ``dem omp-Agenten``): omp
legt bei ``/new`` KEINE neue Sitzungsdatei an; die entsteht erst mit der
ersten Nachricht. Bis dahin ist die ALTE Datei die neueste auf der Platte —
``find_active_session`` liefert sie, und der Chat zeigt den alten Verlauf
weiter, obwohl im Terminal laengst ``✔ New session started`` steht.

Dieses Modul merkt sich pro Agent den Moment, ab dem eine frische Sitzung
gilt. Eine Datei, die AELTER ist als dieser Moment, gehoert zum alten
Gespraech und wird nicht mehr gezeigt.
"""

from __future__ import annotations

import os

import pytest

from app.services import fresh_session


@pytest.fixture(autouse=True)
def _clean_marks():
    fresh_session.reset_for_tests()
    yield
    fresh_session.reset_for_tests()


def test_unmarked_agent_is_never_stale(tmp_path):
    f = tmp_path / "old.jsonl"
    f.write_text("{}\n")
    assert fresh_session.is_stale("a1", f) is False


def test_file_older_than_the_mark_is_stale(tmp_path):
    f = tmp_path / "old.jsonl"
    f.write_text("{}\n")
    os.utime(f, (1_000, 1_000))
    fresh_session.mark("a1", at=2_000)
    assert fresh_session.is_stale("a1", f) is True


def test_file_newer_than_the_mark_ends_the_fresh_state(tmp_path):
    old = tmp_path / "old.jsonl"
    old.write_text("{}\n")
    os.utime(old, (1_000, 1_000))
    fresh_session.mark("a1", at=2_000)
    assert fresh_session.is_stale("a1", old) is True

    new = tmp_path / "new.jsonl"
    new.write_text("{}\n")
    os.utime(new, (3_000, 3_000))
    assert fresh_session.is_stale("a1", new) is False
    # Die neue Datei hat die Marke verbraucht: auch die alte gilt nicht mehr
    # als „verdeckt" — es gibt jetzt eine echte neue Sitzung, die Rangfolge
    # auf der Platte stimmt wieder.
    assert fresh_session.is_stale("a1", old) is False


def test_marks_are_per_agent(tmp_path):
    f = tmp_path / "old.jsonl"
    f.write_text("{}\n")
    os.utime(f, (1_000, 1_000))
    fresh_session.mark("a1", at=2_000)
    assert fresh_session.is_stale("other", f) is False
