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


# --- Cross-agent slug collision guard (adversarial review 2026-07-11) ---
# slug is NOT unique in the DB. Two DISTINCT agents whose names differ only by
# a space vs dash ("Host Testpilot" vs "Host-Testpilot") both derive slug
# "host-testpilot". Blindly merging their keys by updated_at would DESTROY one
# agent's live token. The migration must leave such genuine collisions untouched
# and flag them for manual resolution — never silently clobber.


def test_cross_agent_slug_collision_emits_no_ops():
    from app.services.vault_key_migration import find_slug_collisions

    agents = [
        ("Host Testpilot", "host-testpilot"),   # space-name, token at space key
        ("Host-Testpilot", "host-testpilot"),   # dash-name, token at dash key
    ]
    secret_keys = {
        "mc_token_host testpilot": _dt(1),   # agent A's live token
        "mc_token_host-testpilot": _dt(5),   # agent B's live token — must survive
    }
    renames, deletes = plan_key_migration(agents, secret_keys)
    # Neither agent's key is touched — no rename, no delete.
    assert renames == []
    assert deletes == []
    # And the collision is discoverable for logging.
    assert find_slug_collisions(agents) == {"mc_token_host-testpilot"}


def test_cross_agent_collision_where_dash_agent_is_single_word_key():
    """B's name is already dash-form so B.name_key == B.slug_key == A.slug_key.
    A must not delete/rename onto B's live token."""
    agents = [
        ("Multi Word", "multi-word"),
        ("Multi-Word", "multi-word"),
    ]
    secret_keys = {
        "mc_token_multi word": _dt(1),
        "mc_token_multi-word": _dt(1),  # B's live token
    }
    renames, deletes = plan_key_migration(agents, secret_keys)
    assert renames == []
    assert deletes == []


def test_no_false_collision_for_same_agent_rename_reset_history():
    """A SINGLE agent with both key forms (created spaced, reset after rename)
    is NOT a cross-agent collision and must still be merged."""
    from app.services.vault_key_migration import find_slug_collisions

    agents = [("Host Testpilot", "host-testpilot")]
    assert find_slug_collisions(agents) == set()
    renames, deletes = plan_key_migration(
        agents,
        secret_keys={
            "mc_token_host testpilot": _dt(1),
            "mc_token_host-testpilot": _dt(5),
        },
    )
    assert deletes == ["mc_token_host testpilot"]
    assert renames == []


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
