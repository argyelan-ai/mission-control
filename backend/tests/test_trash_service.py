"""Tests for the trash_service soft-delete engine (escape/collision/no-unlink)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services import fs_service, trash_service
from app.services.fs_roots import RootBlocked, RootNotFound, get_deletable_root


@pytest.fixture
def deliverables_root(tmp_path, monkeypatch):
    """Point ~/.mc at a tmp dir and create the deliverables root on disk."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = tmp_path / ".mc" / "deliverables"
    base.mkdir(parents=True)
    return get_deletable_root("deliverables")


def test_trash_one_moves_into_trash_reversible(deliverables_root, tmp_path):
    base = deliverables_root.container_path
    (base / "a").mkdir()
    src = base / "a" / "b.txt"
    src.write_bytes(b"hello bytes")

    ts = trash_service.timestamp()
    s, rel = trash_service.validate_source(deliverables_root, "a/b.txt")
    dest = trash_service.trash_one(deliverables_root, s, rel, ts=ts)

    trash = tmp_path / ".mc" / ".trash"
    assert trash in dest.parents
    assert dest == trash / ts / "deliverables" / "a" / "b.txt"
    assert not src.exists()
    assert dest.exists()
    assert dest.read_bytes() == b"hello bytes"


def test_validate_source_rejects_empty_subpath(deliverables_root):
    """CRITICAL: '', '/', '.' would resolve to the root itself → whole-root trash."""
    for sp in ("", "/", "."):
        with pytest.raises(fs_service.FsAccessError):
            trash_service.validate_source(deliverables_root, sp)


def test_validate_source_rejects_dotdot_escape(deliverables_root):
    with pytest.raises(fs_service.FsAccessError):
        trash_service.validate_source(deliverables_root, "../../etc/passwd")


def test_validate_source_rejects_symlink(deliverables_root):
    base = deliverables_root.container_path
    target = base / "real.txt"
    target.write_text("real")
    link = base / "link.txt"
    link.symlink_to(target)
    with pytest.raises(fs_service.FsAccessError):
        trash_service.validate_source(deliverables_root, "link.txt")


def test_validate_source_not_found(deliverables_root):
    with pytest.raises(fs_service.FsNotFound):
        trash_service.validate_source(deliverables_root, "nope.txt")


def test_canonical_rel_normalizes(deliverables_root):
    base = deliverables_root.container_path
    (base / "a").mkdir()
    (base / "a" / "b.txt").write_text("x")
    (base / "a.txt").write_text("y")

    _, rel1 = trash_service.validate_source(deliverables_root, "a/./b.txt")
    assert rel1 == "a/b.txt"
    _, rel2 = trash_service.validate_source(deliverables_root, "a//b.txt")
    assert rel2 == "a/b.txt"
    _, rel3 = trash_service.validate_source(deliverables_root, "a/../a.txt")
    assert rel3 == "a.txt"


def test_trash_dest_collision_uniquified(deliverables_root):
    """CRITICAL: trashing the same canonical rel twice in one ts must preserve both."""
    base = deliverables_root.container_path
    ts = trash_service.timestamp()

    src1 = base / "dup.txt"
    src1.write_bytes(b"first")
    s1, rel1 = trash_service.validate_source(deliverables_root, "dup.txt")
    dest1 = trash_service.trash_one(deliverables_root, s1, rel1, ts=ts)

    # recreate same path, trash again under the same ts
    src2 = base / "dup.txt"
    src2.write_bytes(b"second")
    s2, rel2 = trash_service.validate_source(deliverables_root, "dup.txt")
    dest2 = trash_service.trash_one(deliverables_root, s2, rel2, ts=ts)

    assert dest1 != dest2
    assert dest1.exists() and dest2.exists()
    contents = {dest1.read_bytes(), dest2.read_bytes()}
    assert contents == {b"first", b"second"}


def test_dest_built_from_canonical_is_inside_trash(deliverables_root, tmp_path):
    dest = trash_service._dest("deliverables", "a/b.txt", ts="20260618-120000")
    trash = (tmp_path / ".mc" / ".trash").resolve()
    assert trash in dest.resolve().parents


# ── inverse helpers: parse / resolve / list / restore / purge ──────────────


@pytest.fixture
def trash_base(tmp_path, monkeypatch):
    """Point ~/.mc at a tmp dir and create ~/.mc/.trash on disk."""
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    base = tmp_path / ".mc" / ".trash"
    base.mkdir(parents=True)
    return base


def _seed_trashed(trash_base, ts, root_key, rel, content=b"x"):
    p = trash_base / ts / root_key / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_parse_trash_id_valid():
    ts, root_key, rel = trash_service.parse_trash_id("20260618-120000/deliverables/a/b.txt")
    assert (ts, root_key, rel) == ("20260618-120000", "deliverables", "a/b.txt")


def test_parse_trash_id_deleted_at_iso():
    ts, _, _ = trash_service.parse_trash_id("20260618-120000/deliverables/a/b.txt")
    assert trash_service.deleted_at_iso(ts) == "2026-06-18T12:00:00"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/abs/x",
        "20260618-120000/deliverables",  # no rel
        "notats/deliverables/x.txt",  # bad ts format
        "20261301-120000/deliverables/x.txt",  # bad month
        "20260618-120000/Deliverables/x.txt",  # uppercase root_key
        "20260618-120000/deliverables/../x.txt",  # traversal rel
        "20260618-120000/deliverables/./x.txt",  # dot rel
    ],
)
def test_parse_trash_id_rejects(bad):
    with pytest.raises(fs_service.FsAccessError):
        trash_service.parse_trash_id(bad)


def test_parse_trash_id_rejects_nul():
    with pytest.raises(fs_service.FsAccessError):
        trash_service.parse_trash_id("20260618-120000/deliverables/a\0b.txt")


def test_resolve_in_trash_happy(trash_base):
    p = _seed_trashed(trash_base, "20260618-120000", "deliverables", "a/b.txt")
    got = trash_service._resolve_in_trash("20260618-120000/deliverables/a/b.txt")
    assert got == p.resolve()


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "../escape"])
def test_resolve_in_trash_escape_rejected(trash_base, bad):
    with pytest.raises(fs_service.FsAccessError):
        trash_service._resolve_in_trash(bad)


def test_resolve_in_trash_nul_rejected(trash_base):
    with pytest.raises(fs_service.FsAccessError):
        trash_service._resolve_in_trash("a\0b")


def test_resolve_in_trash_refuses_symlinked_component(trash_base):
    """Plant <ts>/<root>/link -> <ts2>/<root2>; resolving through it must raise
    (NOT silently collapse to the link target via an early .resolve())."""
    # real target dir with a file
    real = trash_base / "20260101-000000" / "media" / "sub"
    real.mkdir(parents=True)
    (real / "x.txt").write_bytes(b"real")
    # a symlinked component inside .trash pointing at the real dir
    linkparent = trash_base / "20260618-120000" / "deliverables"
    linkparent.mkdir(parents=True)
    (linkparent / "link").symlink_to(real)
    with pytest.raises(fs_service.FsAccessError):
        trash_service._resolve_in_trash("20260618-120000/deliverables/link/x.txt")


def test_list_trash_empty_when_no_trash(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    assert trash_service.list_trash() == []


def test_list_trash_parses_entry(trash_base):
    _seed_trashed(trash_base, "20260618-120000", "deliverables", "a/b.txt", b"hello")
    entries = trash_service.list_trash()
    assert len(entries) == 1
    e = entries[0]
    assert e["trash_id"] == "20260618-120000/deliverables/a/b.txt"
    assert e["original_root"] == "deliverables"
    assert e["original_subpath"] == "a/b.txt"
    assert e["name"] == "b.txt"
    assert e["size"] == 5
    assert e["deleted_at"] == "2026-06-18T12:00:00"


def test_list_trash_skips_non_deletable_root(trash_base):
    """A planted .trash/<ts>/secrets/... blob is never advertised as restorable."""
    _seed_trashed(trash_base, "20260618-120000", "secrets", "leak.txt", b"s")
    _seed_trashed(trash_base, "20260618-120000", "deliverables", "ok.txt", b"o")
    names = {e["name"] for e in trash_service.list_trash()}
    assert names == {"ok.txt"}


def test_list_trash_skips_malformed(trash_base):
    (trash_base / "stray.txt").write_bytes(b"stray")  # top-level, < 3 segments
    assert trash_service.list_trash() == []


def test_list_trash_cap(trash_base):
    for i in range(5):
        _seed_trashed(trash_base, "20260618-120000", "deliverables", f"f{i}.txt")
    assert len(trash_service.list_trash(cap=3)) == 3


def test_restore_one_happy(trash_base, tmp_path, monkeypatch):
    # destination root must exist on disk
    (tmp_path / ".mc" / "deliverables").mkdir(parents=True)
    _seed_trashed(trash_base, "20260618-120000", "deliverables", "a/b.txt", b"bytes")
    root_key, dest_sub = trash_service.restore_one("20260618-120000/deliverables/a/b.txt")
    assert root_key == "deliverables"
    assert dest_sub == "a/b.txt"
    restored = tmp_path / ".mc" / "deliverables" / "a" / "b.txt"
    assert restored.read_bytes() == b"bytes"
    # source gone from .trash
    assert not (trash_base / "20260618-120000" / "deliverables" / "a" / "b.txt").exists()


def test_restore_one_uniquifies_no_overwrite(trash_base, tmp_path):
    base = tmp_path / ".mc" / "deliverables"
    base.mkdir(parents=True)
    (base / "c.txt").write_bytes(b"PREEXISTING")  # destination already exists
    _seed_trashed(trash_base, "20260618-120000", "deliverables", "c.txt", b"FROMTRASH")
    root_key, dest_sub = trash_service.restore_one("20260618-120000/deliverables/c.txt")
    assert dest_sub != "c.txt" and dest_sub.startswith("c.txt-")
    assert (base / "c.txt").read_bytes() == b"PREEXISTING"  # NOT overwritten
    assert (base / dest_sub).read_bytes() == b"FROMTRASH"


def test_restore_one_blocked_root_raises(trash_base, tmp_path):
    _seed_trashed(trash_base, "20260618-120000", "vault", "x.txt", b"v")
    with pytest.raises(RootBlocked):
        trash_service.restore_one("20260618-120000/vault/x.txt")


def test_restore_one_unknown_root_raises(trash_base, tmp_path):
    _seed_trashed(trash_base, "20260618-120000", "bogus", "x.txt", b"b")
    with pytest.raises(RootNotFound):
        trash_service.restore_one("20260618-120000/bogus/x.txt")


def test_restore_one_source_not_found(trash_base, tmp_path):
    (tmp_path / ".mc" / "deliverables").mkdir(parents=True)
    with pytest.raises(fs_service.FsNotFound):
        trash_service.restore_one("20260618-120000/deliverables/missing.txt")


def test_purge_one_removes_file_and_empty_parents(trash_base):
    p = _seed_trashed(trash_base, "20260618-120000", "deliverables", "only.txt")
    trash_service.purge_one("20260618-120000/deliverables/only.txt")
    assert not p.exists()
    # now-empty <root> and <ts> dirs cleaned up
    assert not (trash_base / "20260618-120000" / "deliverables").exists()
    assert not (trash_base / "20260618-120000").exists()
    # .trash itself survives
    assert trash_base.exists()


def test_purge_one_keeps_sibling_ts(trash_base):
    _seed_trashed(trash_base, "20260618-120000", "deliverables", "gone.txt")
    _seed_trashed(trash_base, "20260101-000000", "deliverables", "keep.txt")
    trash_service.purge_one("20260618-120000/deliverables/gone.txt")
    assert not (trash_base / "20260618-120000").exists()
    # sibling ts with files survives
    assert (trash_base / "20260101-000000" / "deliverables" / "keep.txt").exists()


def test_purge_one_escape_refused(trash_base, tmp_path):
    """Behavioural proof the hard-delete is confined to .trash: an escaping id
    raises and the targeted OUTSIDE file is NOT unlinked."""
    outside = tmp_path / ".mc" / "deliverables" / "precious.txt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"DO NOT DELETE")
    with pytest.raises(fs_service.FsAccessError):
        trash_service.purge_one("../deliverables/precious.txt")
    assert outside.exists() and outside.read_bytes() == b"DO NOT DELETE"


def test_module_has_no_hard_delete():
    """Invariant: NO hard-delete primitive in the SOFT-DELETE locus.

    The relocated invariant: ``os.remove`` / ``rmtree`` / ``.unlink`` /
    ``os.rmdir`` / ``os.unlink`` may appear ONLY inside the audited hard-delete
    locus (everything below the ``=== AUDITED HARD-DELETE LOCUS ===`` fence —
    i.e. ``purge_one`` + ``_cleanup_empty_parents``). The soft-delete locus
    above the fence must contain ZERO of them — this is what stops an accidental
    ``rm`` from creeping into the move-to-trash path.
    """
    src = Path(trash_service.__file__).read_text()
    # strip the module docstring (it documents the forbidden primitives by name)
    code = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
    fence = "=== AUDITED HARD-DELETE LOCUS ==="
    assert fence in code, "audited hard-delete fence missing"
    soft_locus = code.split(fence, 1)[0]
    for forbidden in ("os.remove", "rmtree", ".unlink", "os.rmdir", "os.unlink"):
        assert forbidden not in soft_locus, (
            f"hard-delete primitive {forbidden!r} leaked into the soft-delete locus"
        )


def test_hard_delete_only_guarded_in_purge_locus():
    """Every rmtree/unlink/rmdir line in the audited locus is preceded (within 3
    lines) by an is_relative_to / _resolve_in_trash containment reference."""
    lines = Path(trash_service.__file__).read_text().splitlines()
    # locate the fence
    fence_idx = next(i for i, l in enumerate(lines) if "AUDITED HARD-DELETE LOCUS" in l)
    for i in range(fence_idx, len(lines)):
        line = lines[i]
        # ignore comments/docstrings — only real calls
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        if any(p in line for p in ("rmtree(", "os.unlink(", "os.rmdir(", ".rmdir(")):
            window = "\n".join(lines[max(0, i - 3): i + 1])
            assert ("is_relative_to" in window) or ("_resolve_in_trash" in window), (
                f"unguarded hard-delete at line {i + 1}: {line!r}"
            )
