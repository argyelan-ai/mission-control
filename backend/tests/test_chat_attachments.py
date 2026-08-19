"""Tests für den Chat-Anhang-Speicher (services/chat_attachments.py).

Der Speicher ist bewusst NICHT `reference_ingest`: Referenzen für Tasks haben
eine MIME-Allowlist, ein 20-Dateien-Limit und eine DB-Zeile — für einen Chat
passt keines davon (Operator-Entscheid 19.08.2026: "alle Dateitypen, alle
Agenten"). Was übernommen wird, ist die Härtung: Traversal-Guard auf dem ROHEN
Namen, Prüfsummen-Präfix gegen Kollisionen, realpath-Gegenprobe, Grössenlimit.
"""
import os
import time

import pytest

from app.services import chat_attachments as ca


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Verlegt den Anhang-Root in tmp_path — nie den echten ~/.mc lesen."""
    target = tmp_path / "references"
    monkeypatch.setattr(ca, "_references_root", lambda: str(target))
    return target


# ── Ablage ────────────────────────────────────────────────────────────────


def test_store_writes_file_and_returns_absolute_path(root):
    res = ca.store_attachment(slug="freecode", filename="foto.png", contents=b"abc")

    assert os.path.isfile(res.path)
    assert open(res.path, "rb").read() == b"abc"
    # Der zurückgegebene Pfad ist absolut — genau so, wie ihn der Agent
    # später liest (Host- und Container-Pfad sind identisch, 1:1-Mount).
    assert os.path.isabs(res.path)
    assert res.name == "foto.png"
    assert res.bytes == 3


def test_store_partitions_by_agent_and_month(root):
    res = ca.store_attachment(slug="rex", filename="a.txt", contents=b"x")

    rel = os.path.relpath(res.path, str(root))
    parts = rel.split(os.sep)
    assert parts[0] == "chat"
    assert parts[1] == "rex"
    assert len(parts[2]) == 7 and parts[2][4] == "-"  # JJJJ-MM
    # Prüfsummen-Präfix, damit zwei gleichnamige Dateien sich nie überschreiben
    assert parts[3].endswith("-a.txt")


def test_same_name_different_content_does_not_overwrite(root):
    a = ca.store_attachment(slug="rex", filename="bild.png", contents=b"eins")
    b = ca.store_attachment(slug="rex", filename="bild.png", contents=b"zwei")

    assert a.path != b.path
    assert open(a.path, "rb").read() == b"eins"
    assert open(b.path, "rb").read() == b"zwei"


def test_identical_upload_is_deduplicated(root):
    a = ca.store_attachment(slug="rex", filename="bild.png", contents=b"gleich")
    b = ca.store_attachment(slug="rex", filename="bild.png", contents=b"gleich")

    # Gleicher Inhalt + gleicher Name -> derselbe Pfad, keine zweite Kopie.
    assert a.path == b.path


# ── Alle Dateitypen (Operator-Entscheid) ──────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    ["a.png", "a.pdf", "a.zip", "a.mov", "a.heic", "a.svg", "a.html", "a.xyz", "noext"],
)
def test_every_file_type_is_accepted(root, filename):
    """Keine MIME-Allowlist: ob der Agent die Datei lesen kann, ist seine
    Sache — das UI verspricht nichts, es legt nur ab. Aktive Inhalte sind
    dadurch NICHT gefährlich: fs_service liefert sie als Download aus, nie
    inline (siehe test_fs_service_active_content_download)."""
    res = ca.store_attachment(slug="rex", filename=filename, contents=b"x")
    assert os.path.isfile(res.path)


# ── Härtung ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "evil",
    ["../../etc/passwd", "..\\..\\win.ini", "a/b.png", "..", "/abs/pfad.png"],
)
def test_path_traversal_is_refused_on_the_raw_name(root, evil):
    with pytest.raises(ca.ChatAttachmentError):
        ca.store_attachment(slug="rex", filename=evil, contents=b"x")


def test_slug_is_validated_too(root):
    """Der Slug kommt aus der DB, aber er baut einen Pfad — er wird trotzdem
    geprüft. Verteidigung in der Tiefe, nicht Vertrauen auf den Aufrufer."""
    with pytest.raises(ca.ChatAttachmentError):
        ca.store_attachment(slug="../boss", filename="a.png", contents=b"x")


def test_oversize_is_refused_with_a_readable_reason(root):
    too_big = b"x" * (ca.MAX_BYTES + 1)
    with pytest.raises(ca.ChatAttachmentError) as exc:
        ca.store_attachment(slug="rex", filename="gross.bin", contents=too_big)
    assert "25" in str(exc.value)  # die Grenze steht in der Meldung


def test_empty_file_is_refused(root):
    with pytest.raises(ca.ChatAttachmentError):
        ca.store_attachment(slug="rex", filename="leer.png", contents=b"")


def test_missing_filename_gets_a_fallback(root):
    res = ca.store_attachment(slug="rex", filename="", contents=b"x")
    assert os.path.isfile(res.path)


# ── Bild-Erkennung fürs UI ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [("a.png", True), ("a.JPG", True), ("a.webp", True), ("a.heic", True),
     ("a.pdf", False), ("a.txt", False), ("noext", False)],
)
def test_is_image_flag(root, filename, expected):
    res = ca.store_attachment(slug="rex", filename=filename, contents=b"x")
    assert res.is_image is expected


# ── Aufräumen ─────────────────────────────────────────────────────────────


def test_cleanup_removes_files_older_than_the_window(root):
    old = ca.store_attachment(slug="rex", filename="alt.png", contents=b"a")
    new = ca.store_attachment(slug="rex", filename="neu.png", contents=b"b")
    ancient = time.time() - (ca.RETENTION_DAYS + 1) * 86400
    os.utime(old.path, (ancient, ancient))

    removed = ca.cleanup_old_attachments()

    assert not os.path.exists(old.path)
    assert os.path.exists(new.path)
    assert removed == 1


def test_cleanup_survives_a_missing_root(root):
    """Nie beim Aufräumen sterben — der Ordner kann fehlen (frische Instanz)."""
    assert ca.cleanup_old_attachments() == 0


def test_cleanup_leaves_other_reference_trees_alone(root):
    """Nur `chat/` wird aufgeräumt. Task-Referenzen gehören der References-API
    und dürfen von hier NIE angefasst werden (Lehre aus
    feedback_cleanup_scripts_scope_to_own_ids)."""
    foreign = root / "task" / "1234"
    foreign.mkdir(parents=True)
    victim = foreign / "wichtig.png"
    victim.write_bytes(b"x")
    ancient = time.time() - (ca.RETENTION_DAYS + 5) * 86400
    os.utime(victim, (ancient, ancient))

    ca.cleanup_old_attachments()

    assert victim.exists()
