"""Tests für den Referenz-Speicher (services/reference_ingest.py).

Der Speicher ist EINE Implementierung für drei Wege: References-Upload,
Slack-Datei-Ingest und Chat-Anhänge. Die beiden strengen Wege bekommen die
Voreinstellung (MIME-Allowlist, 20 Dateien pro Ziel); der Chat schaltet genau
diese zwei Hürden ab (Operator-Entscheid 19.08.2026: „alle Dateitypen, alle
Agenten"). Alles andere — Traversal-Guard auf dem ROHEN Namen,
Prüfsummen-Präfix, realpath-Gegenprobe, Grössenlimit, atomares Schreiben —
gilt für alle Aufrufer gleich.

Die erste Hälfte der Datei ist darum die wichtigste: sie hält fest, dass ein
Aufrufer, der NICHTS übergibt, sich exakt wie vorher verhält.
"""
import os
import uuid

import pytest
from sqlmodel import select

from app.models.reference_file import ReferenceFile
from app.services import reference_ingest as ri


@pytest.fixture
def refs_root(tmp_path, monkeypatch):
    """Alle mc_home()-Aufrufe auf ein Temp-Verzeichnis umbiegen."""
    from app.config import settings
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path / ".mc" / "references"


async def _store(session, **kwargs):
    kwargs.setdefault("contents", b"inhalt")
    kwargs.setdefault("filename", "datei.png")
    kwargs.setdefault("mime", "image/png")
    return await ri.store_reference(session, **kwargs)


@pytest.fixture
async def agent_id(make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")
    return agent.id


# ── Voreinstellung: bestehende Aufrufer bleiben unverändert ───────────────


async def test_default_call_keeps_the_mime_allowlist(session, refs_root, agent_id):
    """References-Upload und Slack-Ingest übergeben nichts — für sie muss die
    Allowlist weiter greifen, sonst hätte der Umbau still eine Absicherung
    entfernt (Review-Fund M1: aktive Inhalte im App-Origin)."""
    with pytest.raises(ri.ReferenceIngestError):
        await _store(session, mime="video/quicktime", filename="a.mov", agent_id=agent_id)


async def test_default_call_keeps_the_files_per_target_limit(session, refs_root, agent_id):
    for i in range(ri.MAX_FILES_PER_ENTITY):
        await _store(session, contents=f"nr-{i}".encode(), agent_id=agent_id)

    with pytest.raises(ri.ReferenceIngestError) as exc:
        await _store(session, contents=b"eins-zu-viel", agent_id=agent_id)
    assert "20" in str(exc.value)


async def test_default_call_keeps_the_size_cap(session, refs_root, agent_id):
    with pytest.raises(ri.ReferenceIngestError) as exc:
        await _store(session, contents=b"x" * (ri.MAX_BYTES + 1), agent_id=agent_id)
    assert "25" in str(exc.value)  # die Grenze steht in der Meldung


async def test_too_large_has_its_own_class(session, refs_root, agent_id):
    """Der Aufrufer trennt 413 von 422 an der Klasse, nicht an der Meldung.
    Vorher entschied der Chat-Router das per `"maximal" in str(exc)` — ein
    Umformulieren der deutschen Meldung hätte den Status still gekippt."""
    with pytest.raises(ri.ReferenceTooLargeError):
        await _store(session, contents=b"x" * (ri.MAX_BYTES + 1), agent_id=agent_id)

    # Und umgekehrt: eine andere Ablehnung darf NICHT als "zu gross" gelten.
    with pytest.raises(ri.ReferenceIngestError) as exc:
        await _store(session, mime="video/quicktime", agent_id=agent_id)
    assert not isinstance(exc.value, ri.ReferenceTooLargeError)


# ── Abschaltbar für den Chat ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,mime",
    [("a.heic", "image/heic"), ("b.mov", "video/quicktime"),
     ("c.html", "text/html"), ("d.xyz", None), ("ohneendung", None)],
)
async def test_allowed_mimes_none_takes_every_type(
    session, refs_root, agent_id, filename, mime
):
    """`allowed_mimes=None` = keine Typen-Prüfung. Ob ein Agent die Datei
    versteht, ist nicht unsere Zusage — das UI legt ab und nennt den Pfad.
    Gefährlich ist das nicht: fs_service liefert aktive Inhalte immer als
    Download aus, nie inline (test_fs_service_active_content_download)."""
    ref = await _store(
        session, filename=filename, mime=mime, agent_id=agent_id, allowed_mimes=None
    )
    assert os.path.isfile(os.path.join(ri.references_root(), ref.rel_path))


async def test_max_files_none_removes_the_cap(session, refs_root, agent_id):
    for i in range(ri.MAX_FILES_PER_ENTITY + 3):
        ref = await _store(session, contents=f"nr-{i}".encode(),
                           agent_id=agent_id, max_files=None)
    assert os.path.isfile(os.path.join(ri.references_root(), ref.rel_path))


async def test_max_files_can_be_set_lower(session, refs_root, agent_id):
    await _store(session, contents=b"eins", agent_id=agent_id, max_files=1)
    with pytest.raises(ri.ReferenceIngestError):
        await _store(session, contents=b"zwei", agent_id=agent_id, max_files=1)


# ── Härtung: gilt für alle Aufrufer gleich ────────────────────────────────


@pytest.mark.parametrize(
    "evil", ["../../etc/passwd", "..\\..\\win.ini", "a/b.png", "..", "/abs/pfad.png"]
)
async def test_path_traversal_is_refused_on_the_raw_name(
    session, refs_root, agent_id, evil
):
    with pytest.raises(ri.ReferenceIngestError):
        await _store(session, filename=evil, agent_id=agent_id, allowed_mimes=None)


async def test_nul_byte_in_name_does_not_reach_the_filesystem(
    session, refs_root, agent_id
):
    """Ein NUL-Byte im Namen killt jeden open()/subprocess-Aufruf mit einem
    ValueError — das wäre ein 500 statt einer Meldung."""
    ref = await _store(session, filename="fo\x00to.png", agent_id=agent_id)
    assert "\x00" not in ref.original_name
    assert os.path.isfile(os.path.join(ri.references_root(), ref.rel_path))


async def test_same_name_different_content_does_not_overwrite(
    session, refs_root, agent_id
):
    a = await _store(session, filename="bild.png", contents=b"eins", agent_id=agent_id)
    b = await _store(session, filename="bild.png", contents=b"zwei", agent_id=agent_id)

    root = ri.references_root()
    assert a.rel_path != b.rel_path
    assert open(os.path.join(root, a.rel_path), "rb").read() == b"eins"
    assert open(os.path.join(root, b.rel_path), "rb").read() == b"zwei"


async def test_identical_upload_lands_on_the_same_path(session, refs_root, agent_id):
    """Gleicher Inhalt + gleicher Name → derselbe Pfad, keine zweite Kopie auf
    der Platte. Ein versehentlich doppelt eingefügter Screenshot verdoppelt
    nichts (die DB führt beide Zeilen — die kosten nichts)."""
    a = await _store(session, filename="bild.png", contents=b"gleich", agent_id=agent_id)
    b = await _store(session, filename="bild.png", contents=b"gleich", agent_id=agent_id)
    assert a.rel_path == b.rel_path


# ── Atomares Schreiben ────────────────────────────────────────────────────


async def test_write_is_atomic_no_half_file_on_abort(
    session, refs_root, agent_id, monkeypatch
):
    """Bricht das Schreiben mittendrin ab, darf unter dem ZIELnamen nichts
    liegen — ein halber Anhang, den der Agent dann liest, ist schlimmer als
    gar keiner. Deshalb erst `.part`, dann `os.replace`."""
    real_open = open

    class HalfWriter:
        """Schreibt die Hälfte und stirbt — wie eine volle Platte."""

        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            self._fh.write(data[: len(data) // 2])
            raise OSError("Platte voll")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def half_open(path, mode="r", *args, **kwargs):
        fh = real_open(path, mode, *args, **kwargs)
        return HalfWriter(fh) if "w" in mode else fh

    # WICHTIG: erst den Pfad festhalten. `monkeypatch.undo()` würde auch das
    # umgebogene home_host zurückdrehen — die Prüfung liefe dann gegen den
    # echten ~/.mc und wäre still immer grün.
    kind_dir = os.path.join(ri.references_root(), "agent", str(agent_id))

    with monkeypatch.context() as m:
        m.setattr("builtins.open", half_open)
        with pytest.raises(OSError):
            await _store(session, filename="halb.png", contents=b"x" * 100,
                         agent_id=agent_id)

    leftovers = os.listdir(kind_dir) if os.path.isdir(kind_dir) else []
    # Weder eine halbe Zieldatei noch ein liegengebliebenes .part-Fragment.
    assert leftovers == [], leftovers


async def test_no_row_without_a_file(session, refs_root, agent_id, monkeypatch):
    """Scheitert das Schreiben, darf keine DB-Zeile entstehen, die auf eine
    Datei zeigt, die es nicht gibt."""
    real_open = open

    def refuse(path, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError("Platte voll")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr("builtins.open", refuse)
        with pytest.raises(OSError):
            await _store(session, filename="x.png", contents=b"x", agent_id=agent_id)

    rows = (await session.exec(
        select(ReferenceFile).where(ReferenceFile.agent_id == agent_id)
    )).all()
    assert rows == []


# ── Bild-Erkennung fürs UI ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [("a.png", True), ("a.JPG", True), ("a.webp", True), ("a.heic", True),
     ("a.avif", True), ("a.pdf", False), ("a.txt", False), ("ohneendung", False),
     ("", False)],
)
def test_is_image_reference(filename, expected):
    assert ri.is_image_reference(filename) is expected


# ── Ownership ─────────────────────────────────────────────────────────────


async def test_agent_owned_reference_lands_under_its_agent_id(
    session, refs_root, agent_id
):
    """Der Besitzer baut den Pfad — und ein Agent-Besitzer ist eine UUID aus
    der DB, kein vom Operator wählbarer String. Damit kann hier gar kein Name
    mehr ein Verzeichnis erfinden."""
    ref = await _store(session, agent_id=agent_id, allowed_mimes=None)

    assert ref.agent_id == agent_id
    assert ref.rel_path.split(os.sep)[:2] == ["agent", str(agent_id)]


async def test_exactly_one_owner_is_required(session, refs_root, agent_id):
    with pytest.raises(ri.ReferenceIngestError):
        await _store(session)
    with pytest.raises(ri.ReferenceIngestError):
        await _store(session, agent_id=agent_id, task_id=uuid.uuid4())
