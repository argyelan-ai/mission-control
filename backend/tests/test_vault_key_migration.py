"""Pure planner for the vault-key → slug migration (Alembic 0152).

The old token vault key was name-derived (`mc_token_{name.lower()}`, spaces
preserved); the new scheme is slug-derived (`mc_token_{slug}`, spaces → dashes).
Single-word agents are byte-identical under both schemes, so only multi-word
agents need a rename. `plan_key_migration` is a pure function so the rename /
collision logic is unit-tested here without a live Alembic run.
"""
from datetime import datetime, timezone

from app.services.vault_key_migration import plan_key_migration


def _dt(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=timezone.utc)


def test_single_word_agent_is_noop():
    """name.lower() == slug → key already canonical, nothing to do."""
    renames, deletes = plan_key_migration(
        agents=[("Rex", "rex")],
        secret_keys={"mc_token_rex": _dt(1)},
    )
    assert renames == []
    assert deletes == []


def test_multiword_agent_renames_space_key_to_dash_slug():
    renames, deletes = plan_key_migration(
        agents=[("Host Testpilot", "host-testpilot")],
        secret_keys={"mc_token_host testpilot": _dt(1)},
    )
    assert renames == [("mc_token_host testpilot", "mc_token_host-testpilot")]
    assert deletes == []


def test_already_migrated_slug_key_is_noop():
    """Only the dash-form key exists → already migrated, no op."""
    renames, deletes = plan_key_migration(
        agents=[("Host Testpilot", "host-testpilot")],
        secret_keys={"mc_token_host-testpilot": _dt(1)},
    )
    assert renames == []
    assert deletes == []


def test_collision_keeps_newer_slug_key_and_deletes_older_name_key():
    """Both forms exist (rename+reset history). slug-key is newer → keep it,
    drop the stale space-form key. No rename needed (survivor already canonical)."""
    renames, deletes = plan_key_migration(
        agents=[("Host Testpilot", "host-testpilot")],
        secret_keys={
            "mc_token_host testpilot": _dt(1),   # older
            "mc_token_host-testpilot": _dt(5),   # newer
        },
    )
    assert renames == []
    assert deletes == ["mc_token_host testpilot"]


def test_collision_keeps_newer_name_key_then_renames_it():
    """Both forms exist but the space-form is newer → keep its value, delete the
    stale dash-key first, then rename the survivor to the canonical slug key."""
    renames, deletes = plan_key_migration(
        agents=[("Host Testpilot", "host-testpilot")],
        secret_keys={
            "mc_token_host testpilot": _dt(5),   # newer
            "mc_token_host-testpilot": _dt(1),   # older
        },
    )
    assert deletes == ["mc_token_host-testpilot"]
    assert renames == [("mc_token_host testpilot", "mc_token_host-testpilot")]


def test_slug_none_falls_back_to_name_derived_slug():
    """Legacy rows without a persisted slug still migrate via the name→slug rule."""
    renames, deletes = plan_key_migration(
        agents=[("Host Testpilot", None)],
        secret_keys={"mc_token_host testpilot": _dt(1)},
    )
    assert renames == [("mc_token_host testpilot", "mc_token_host-testpilot")]
    assert deletes == []


def test_orphan_secret_without_agent_is_left_untouched():
    """A secret with no owning agent must not be renamed or deleted by the
    migration (delete_agent handles agent-owned cleanup going forward)."""
    renames, deletes = plan_key_migration(
        agents=[("Rex", "rex")],
        secret_keys={
            "mc_token_rex": _dt(1),
            "mc_token_ghost agent": _dt(1),  # no matching agent
        },
    )
    assert renames == []
    assert deletes == []


def test_agent_without_name_is_skipped():
    renames, deletes = plan_key_migration(
        agents=[("", "whatever"), (None, "x")],
        secret_keys={},
    )
    assert renames == []
    assert deletes == []


# --- Integration: migrate_connection / revert_connection against real SQLite ---

import sqlalchemy as sa
import pytest


def _make_db():
    """Minimal in-memory SQLite with just the columns the migration reads."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE agents (name TEXT, slug TEXT)"))
        conn.execute(
            sa.text(
                "CREATE TABLE secrets (key TEXT UNIQUE, updated_at TEXT)"
            )
        )
    return engine


def _keys(conn):
    rows = conn.execute(
        sa.text("SELECT key FROM secrets ORDER BY key")
    ).fetchall()
    return [r[0] for r in rows]


def test_migrate_connection_renames_multiword_leaves_singleword():
    from app.services.vault_key_migration import migrate_connection

    engine = _make_db()
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO agents (name, slug) VALUES ('Rex', 'rex')")
        )
        conn.execute(
            sa.text(
                "INSERT INTO agents (name, slug) VALUES "
                "('Host Testpilot', 'host-testpilot')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO secrets (key, updated_at) VALUES "
                "('mc_token_rex', '2026-07-01'), "
                "('mc_token_host testpilot', '2026-07-01')"
            )
        )
        migrate_connection(conn)
        assert _keys(conn) == ["mc_token_host-testpilot", "mc_token_rex"]


def test_migrate_connection_collision_deletes_stale_and_frees_slug_key():
    """Both forms present, name-form newer → the unique constraint on
    secrets.key would blow up if the delete didn't run before the rename."""
    from app.services.vault_key_migration import migrate_connection

    engine = _make_db()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (name, slug) VALUES "
                "('Host Testpilot', 'host-testpilot')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO secrets (key, updated_at) VALUES "
                "('mc_token_host testpilot', '2026-07-05'), "   # newer → survives
                "('mc_token_host-testpilot', '2026-07-01')"     # older → deleted
            )
        )
        migrate_connection(conn)  # must not raise UNIQUE violation
        assert _keys(conn) == ["mc_token_host-testpilot"]
        # survivor carries the NEWER value's row (space-form was newer)
        val = conn.execute(
            sa.text(
                "SELECT updated_at FROM secrets WHERE key='mc_token_host-testpilot'"
            )
        ).scalar()
        assert val == "2026-07-05"


def test_revert_connection_restores_name_form():
    from app.services.vault_key_migration import migrate_connection, revert_connection

    engine = _make_db()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (name, slug) VALUES "
                "('Host Testpilot', 'host-testpilot')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO secrets (key, updated_at) VALUES "
                "('mc_token_host testpilot', '2026-07-01')"
            )
        )
        migrate_connection(conn)
        assert _keys(conn) == ["mc_token_host-testpilot"]
        revert_connection(conn)
        assert _keys(conn) == ["mc_token_host testpilot"]
