"""Phase 24 plan 01 — Migration 0095 (single_instance column + Hermes seed).

Verification strategy mirrors test_migration_0091.py:

1. **Source-text assertions** — load the alembic revision file as a plain
   Python module (filename starts with a digit; we inject an ``alembic.op``
   shim into ``sys.modules`` so the file loads without running DDL) and
   confirm:
   - revision/down_revision metadata
   - upgrade() body uses parameterized ``sa.text(...)`` calls (no f-string
     interpolation — STRIDE T-24-01 mitigation)
   - INSERT for hermes-vllm runtime contains correct slug, type, endpoint,
     model_identifier, single_instance flag, ON CONFLICT clause
   - INSERT for Hermes agent uses agent_runtime='host' (NOT 'cli-bridge'),
     workspace_path is absolute (no tilde), provision_status='local',
     ON CONFLICT (name) DO NOTHING for idempotency
   - downgrade() drops only the column, never the rows (defensive per
     24-RESEARCH.md skizze)

2. **Schema assertion** — introspect the live SQLite schema (created via
   SQLModel.metadata.create_all in conftest) to confirm
   ``runtimes.single_instance`` exists, is BOOLEAN, NOT NULL, defaults
   to false, and that ``Runtime()`` instantiation defaults the field to
   ``False`` (Pitfall: must NOT default to None).
"""
import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.runtime import Runtime
from tests.conftest import test_engine

REVISION_PATH = (
    pathlib.Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "0095_hermes_runtime_and_single_instance.py"
)


def _load_migration():
    """Load alembic revision 0095 as a plain Python module."""
    if not REVISION_PATH.is_file():
        pytest.fail(f"Migration 0095 not present at {REVISION_PATH}")

    op_shim = types.SimpleNamespace(
        add_column=lambda *a, **k: None,
        drop_column=lambda *a, **k: None,
        create_index=lambda *a, **k: None,
        drop_index=lambda *a, **k: None,
        get_bind=lambda: types.SimpleNamespace(execute=lambda *a, **k: None),
        execute=lambda *a, **k: None,
    )
    import alembic as _alembic

    _alembic.op = op_shim
    sys.modules["alembic.op"] = op_shim

    spec = importlib.util.spec_from_file_location("mig0095", str(REVISION_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_metadata():
    """revision/down_revision wired correctly."""
    module = _load_migration()
    assert module.revision == "0095"
    assert module.down_revision == "0094"


def test_upgrade_adds_single_instance_column():
    """upgrade() adds the column with BOOLEAN type, NOT NULL, default false."""
    src = REVISION_PATH.read_text()
    assert "single_instance" in src
    # Either sa.Boolean() or 'BOOLEAN' literal — the column-add call.
    assert "sa.Boolean" in src or "BOOLEAN" in src
    # op.add_column("runtimes", ...) — allow whitespace/newlines after first arg.
    assert 'add_column(' in src and '"runtimes"' in src
    # server_default false so existing rows inherit the safe default.
    assert "server_default" in src
    # NOT NULL — single_instance is not optional.
    assert "nullable=False" in src


def test_upgrade_seeds_hermes_runtime():
    """Hermes runtime row inserted with correct fields + ON CONFLICT clause."""
    src = REVISION_PATH.read_text()
    assert "hermes-vllm" in src
    assert "Qwen/Qwen3.6-35B-A3B-FP8" in src
    assert "http://192.0.2.10:8000/v1" in src
    # runtime_type=hermes (new type per plan 02/03 follow-up).
    assert "'hermes'" in src or '"hermes"' in src
    # Idempotency on slug.
    assert "ON CONFLICT" in src
    # Parameterized — no f-string interpolation (STRIDE T-24-01).
    assert "f\"INSERT" not in src
    assert "f'INSERT" not in src


def test_upgrade_seeds_hermes_agent():
    """Hermes agent row inserted with host runtime + absolute workspace path."""
    src = REVISION_PATH.read_text()
    assert "'Hermes'" in src or '"Hermes"' in src
    # Critical: agent_runtime='host', NOT 'cli-bridge' (Pitfall 1 from CONTEXT.md).
    # Strip module docstring before checking — comparison comments may name
    # the bad value, that's fine. We're checking the executable body.
    assert "'host'" in src or '"host"' in src
    # Crude split: everything after the first ``def upgrade`` is executable code.
    code_only = src.split("def upgrade")[1] if "def upgrade" in src else src
    assert "'cli-bridge'" not in code_only and '"cli-bridge"' not in code_only
    # Absolute workspace path — derived from the host home via _home(),
    # never a tilde and never a machine-specific literal (Pitfall 5).
    assert '_home()}/.openclaw/agents/hermes' in src
    assert "~/.openclaw/agents/hermes" not in src
    assert "/Users/" not in src
    # provision_status='local' per L-B (provisioning happens in plan 08).
    assert "'local'" in src or '"local"' in src
    # Idempotency on agent name.
    assert "ON CONFLICT (name) DO NOTHING" in src or "ON CONFLICT(name) DO NOTHING" in src


def test_downgrade_is_defensive():
    """downgrade() drops only the column. Runtime + agent rows survive."""
    src = REVISION_PATH.read_text()
    assert "def downgrade()" in src
    assert 'drop_column("runtimes", "single_instance")' in src
    # Defensive — no DELETE FROM runtimes / agents in downgrade body.
    # Crude but effective: split on def downgrade and assert no DELETE there.
    downgrade_body = src.split("def downgrade()")[1]
    assert "DELETE FROM runtimes" not in downgrade_body
    assert "DELETE FROM agents" not in downgrade_body


async def test_runtime_model_has_single_instance_field():
    """Runtime SQLModel exposes single_instance as BOOLEAN NOT NULL DEFAULT false."""
    async with test_engine.connect() as conn:
        cols_list = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_columns("runtimes")
        )
    cols = {c["name"]: c for c in cols_list}

    assert "single_instance" in cols
    assert cols["single_instance"]["nullable"] is False

    # SQLModel-level default must be False (NOT None).
    rt = Runtime(slug="test-rt", display_name="Test", runtime_type="cloud", endpoint="x")
    assert rt.single_instance is False


def test_to_registry_dict_includes_single_instance():
    """to_registry_dict() exposes single_instance for legacy dict consumers."""
    rt = Runtime(
        slug="test-rt",
        display_name="Test",
        runtime_type="hermes",
        endpoint="http://x",
        single_instance=True,
    )
    d = rt.to_registry_dict()
    assert d["single_instance"] is True
