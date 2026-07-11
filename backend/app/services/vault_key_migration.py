"""Vault-key → slug migration planner (Alembic 0152).

The MC_AGENT_TOKEN vault key used to be derived from the agent *name*
(`mc_token_{name.lower()}`, spaces preserved). That is fragile: a rename
orphans the key, and a space in the name breaks docker/.env.agents parsing
(2026-07-11: a leftover `mc_token_host testpilot` broke the parser). The new
scheme keys on the stable, insert-time `agent.slug` (`mc_token_{slug}`, spaces
→ dashes, never changed on rename).

Single-word agents are byte-identical under both schemes (`"rex".lower()` ==
slug `"rex"`), so only multi-word agents need a rename. This module holds the
pure planning logic so the rename / collision rules are unit-tested without a
live Alembic run; the migration itself just reads the DB, calls
``plan_key_migration``, and executes the returned ops (deletes before renames,
so a collision survivor can take the freed slug key without hitting the unique
constraint on ``secrets.key``).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Iterable, Mapping

logger = logging.getLogger("mc.vault_key_migration")


def _effective_slug(name: str, slug: str | None) -> str:
    """Mirror of Agent._agent_fill_slug / fs_service.agent_slug: prefer the
    persisted slug, fall back to the historical name→slug derivation."""
    return slug or name.lower().replace(" ", "-")


def find_slug_collisions(
    agents: Iterable[tuple[str | None, str | None]],
) -> set[str]:
    """Slug-derived token keys claimed by MORE THAN ONE distinct agent.

    ``agents.slug`` is NOT unique in the DB (no constraint — verified against
    the schema), so two agents whose names differ only by a space vs a dash
    ("Host Testpilot" / "Host-Testpilot") both derive slug "host-testpilot" and
    would target the same ``mc_token_host-testpilot`` key. Merging them by
    ``updated_at`` would destroy one agent's live token. These are genuine
    collisions that need manual resolution; the migration leaves them untouched
    and logs them rather than clobbering data.
    """
    counts = Counter(
        f"mc_token_{_effective_slug(name, slug)}" for name, slug in agents if name
    )
    return {key for key, count in counts.items() if count > 1}


def plan_key_migration(
    agents: Iterable[tuple[str | None, str | None]],
    secret_keys: Mapping[str, datetime],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Plan the rename/delete ops to move token keys onto the slug scheme.

    Args:
        agents: ``(name, slug)`` pairs for every agent in the DB. ``slug`` may
            be ``None`` for legacy rows (falls back to the name-derived slug).
        secret_keys: ``{key: updated_at}`` for every ``mc_token_*`` secret.

    Returns:
        ``(renames, deletes)`` where ``renames`` is a list of
        ``(old_key, new_key)`` and ``deletes`` a list of keys to drop. The
        caller MUST apply ``deletes`` before ``renames`` (a collision survivor
        renames into a slug key only after the stale occupant is removed).

    Only agent-owned keys are touched: orphaned secrets with no matching agent
    are left untouched (agent deletion cleans those up going forward).

    Genuine cross-agent slug collisions (two distinct agents deriving the same
    slug key) are left untouched — merging them would destroy a live token.
    They are returned by :func:`find_slug_collisions` for the caller to log.
    """
    agents = list(agents)
    collisions = find_slug_collisions(agents)

    renames: list[tuple[str, str]] = []
    deletes: list[str] = []

    for name, slug in agents:
        if not name:
            continue
        eff_slug = _effective_slug(name, slug)
        name_key = f"mc_token_{name.lower()}"
        slug_key = f"mc_token_{eff_slug}"
        if name_key == slug_key:
            continue  # single-word agent — key already canonical
        if slug_key in collisions:
            continue  # another agent claims this slug key — never clobber

        has_name = name_key in secret_keys
        has_slug = slug_key in secret_keys

        if has_name and has_slug:
            # Collision (rename+reset produced both). Keep the newer value.
            # Tie → prefer the already-canonical slug key.
            if secret_keys[slug_key] >= secret_keys[name_key]:
                deletes.append(name_key)
            else:
                deletes.append(slug_key)
                renames.append((name_key, slug_key))
        elif has_name:
            renames.append((name_key, slug_key))
        # has_slug only, or neither → nothing to do

    return renames, deletes


def migrate_connection(conn) -> tuple[list[tuple[str, str]], list[str]]:
    """Read the DB, plan, and execute the name→slug key migration on ``conn``.

    ``conn`` is a synchronous SQLAlchemy Connection (Alembic's ``op.get_bind()``
    in production; a test connection under test). Deletes run before renames so
    a collision survivor can take the freed slug key without violating the
    unique constraint on ``secrets.key``. Returns the ``(renames, deletes)``
    that were applied, for logging/assertion.
    """
    from sqlalchemy import text

    agents = conn.execute(text("SELECT name, slug FROM agents")).fetchall()
    secrets = conn.execute(
        text("SELECT key, updated_at FROM secrets WHERE key LIKE 'mc_token_%'")
    ).fetchall()

    agent_pairs = [(row[0], row[1]) for row in agents]
    collisions = find_slug_collisions(agent_pairs)
    if collisions:
        logger.warning(
            "vault key migration: %d cross-agent slug collision(s) LEFT UNTOUCHED "
            "— resolve the affected agents' tokens manually: %s",
            len(collisions),
            sorted(collisions),
        )

    renames, deletes = plan_key_migration(
        agents=agent_pairs,
        secret_keys={row[0]: row[1] for row in secrets},
    )

    for key in deletes:
        conn.execute(text("DELETE FROM secrets WHERE key = :key"), {"key": key})
    for old_key, new_key in renames:
        conn.execute(
            text("UPDATE secrets SET key = :new WHERE key = :old"),
            {"new": new_key, "old": old_key},
        )
    return renames, deletes


def revert_connection(conn) -> None:
    """Best-effort reverse of :func:`migrate_connection`: rename slug-form token
    keys back to the name form. Loss-tolerant — collisions merged on upgrade
    cannot be un-merged, orphans are left as-is. Only multi-word agents change.
    """
    from sqlalchemy import text

    agents = conn.execute(text("SELECT name, slug FROM agents")).fetchall()
    existing = {
        row[0]
        for row in conn.execute(
            text("SELECT key FROM secrets WHERE key LIKE 'mc_token_%'")
        ).fetchall()
    }

    for name, slug in agents:
        if not name:
            continue
        eff_slug = _effective_slug(name, slug)
        slug_key = f"mc_token_{eff_slug}"
        name_key = f"mc_token_{name.lower()}"
        if slug_key == name_key or slug_key not in existing or name_key in existing:
            continue
        conn.execute(
            text("UPDATE secrets SET key = :new WHERE key = :old"),
            {"new": name_key, "old": slug_key},
        )
