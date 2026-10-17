"""Migration 0112 must not touch the disk on a fresh database.

Measured on a fresh Linux install whose login user is uid 1001: the backend
container (uid 1000) ran ``alembic upgrade head``, 0112 did
``mkdir ~/.mc/vault`` unconditionally, ``PermissionError`` aborted the whole
migration chain, the backend never became healthy. A fresh database has no
``board_memory`` rows, so there is nothing to copy and no reason to create a
directory — the vault tree comes from the boot step
(app.services.mc_home_preflight). With rows but no write permission the copy
is skipped with a warning instead of crashing; the rows stay unstamped.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0112_vault_cutover.py"


def _load(monkeypatch):
    # backend/alembic/ (the migrations folder) shadows the alembic package when
    # tests run from backend/, and the real ``op`` proxy only works inside a
    # migration context anyway — hand the module a stand-in ``alembic.op``.
    fake_op = types.SimpleNamespace(get_bind=None, add_column=None, execute=None)
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = fake_op
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)
    spec = importlib.util.spec_from_file_location("mig_0112_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return _Result(self._rows)


def _row():
    return SimpleNamespace(
        id=uuid.uuid4(),
        content="remember this",
        memory_type="lesson",
        agent_id=None,
        board_id=None,
        tags=["x"],
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        agent_name="Builder",
        board_slug=None,
    )


@pytest.fixture
def mig(monkeypatch, tmp_path):
    mod = _load(monkeypatch)
    calls: dict = {}

    def _rig(rows, vault_root: Path):
        mod.op.get_bind = lambda: _Conn(rows)
        monkeypatch.setattr(mod, "_vault_root", lambda: vault_root)
        monkeypatch.setattr(
            mod, "_add_frozen_at_column", lambda stamp=True: calls.setdefault("stamp", stamp)
        )
        return calls

    mod.rig = _rig
    return mod


def test_fresh_db_creates_no_directory(mig, tmp_path):
    vault = tmp_path / ".mc" / "vault"
    calls = mig.rig([], vault)

    mig.upgrade()

    assert not vault.exists()
    assert not (tmp_path / ".mc").exists()
    assert calls["stamp"] is True  # schema part still runs


def test_rows_are_copied_when_writable(mig, tmp_path):
    vault = tmp_path / ".mc" / "vault"
    calls = mig.rig([_row()], vault)

    mig.upgrade()

    files = list((vault / "memory").rglob("*.md"))
    assert len(files) == 1
    assert "remember this" in files[0].read_text()
    assert calls["stamp"] is True


def test_unwritable_vault_skips_copy_instead_of_crashing(mig, tmp_path, monkeypatch, capsys):
    vault = tmp_path / ".mc" / "vault"
    calls = mig.rig([_row()], vault)
    real_mkdir = Path.mkdir

    def _deny(self, *a, **k):
        if str(self).startswith(str(tmp_path / ".mc")):
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _deny)

    mig.upgrade()  # must not raise — the chain continues

    assert calls["stamp"] is False  # uncopied rows stay recognisable (frozen_at NULL)
    out = capsys.readouterr().out
    assert "Vault cutover SKIPPED" in out
    assert "PUID/PGID" in out
