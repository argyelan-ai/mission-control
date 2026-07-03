"""normalize agent.workspace_path to ~/.mc/ layout

Revision ID: 0087
Revises: 0086
Create Date: 2026-04-21

Workstream H — `~/.mc/` home (see ADR-022).

Before this migration the `agents.workspace_path` column was a mix of
host paths (`~/.openclaw/workspace-rex`) and container paths
(`/workspace/Projects/`). Backend and Agent read the same field — backend
needs host paths for git operations, Agent sees container paths via
docker volume mounts. The split was fragile.

New convention:
  * `workspace_path` always stores a HOST path (what the backend sees).
  * Dispatch templates inject `/workspace` as the container-facing path
    (see `template_renderer.build_agent_context`). Agents never see
    host paths in their prompts.

Migration sets:
  * cli-bridge agents → `~/.mc/workspaces/<slug>` (host path)
  * Boss (host runtime) → `~/Workspace` (he reads directly on host)
  * Henry (openclaw gateway) → `~/Workspace/Projects` (read-only reference
    for look-up requests from the operator)
  * Any other → left as-is (operator migration later if needed)

Idempotent: only rewrites when the current value matches the legacy
patterns; hand-edits preserved.
"""
from alembic import op
import sqlalchemy as sa
import os

# revision identifiers, used by Alembic.
revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def _home() -> str:
    """Best-effort resolve of the HOST home.

    When this migration runs inside the backend container `HOME` points
    at the container-user (`/home/mcuser`), which is useless. The stack
    exports `HOME_HOST` pointing at the real host home — prefer that.
    Fall back to `expanduser('~')` only for local pytest setups.
    """
    return (
        os.environ.get("HOME_HOST")
        or os.path.expanduser("~")
        or os.environ.get("HOME")
        or ""
    )


def upgrade() -> None:
    import logging
    logger = logging.getLogger("alembic.runtime.migration")

    bind = op.get_bind()
    home = _home()
    if not home:
        # ADR-023 ultrareview: fail loud instead of silent no-op. An empty
        # $HOME would be a silent partial upgrade (alembic stamps 0087 as
        # applied, but no row was touched). Operator must set HOME_HOST.
        raise RuntimeError(
            "Migration 0087: HOME_HOST/HOME nicht gesetzt. "
            "Backend-Container braucht `HOME_HOST=${HOME}` in docker-compose.yml "
            "(siehe ADR-022). Ohne kann workspace_path nicht migriert werden."
        )

    mc_root = f"{home}/.mc/workspaces"

    # cli-bridge agents: point at their new workspace under ~/.mc/workspaces/.
    # Only rewrite legacy ~/.openclaw/workspace-*  or container-path values;
    # leave hand-edits untouched.
    #
    # Slug generator (ADR-023 ultrareview): MUST be identical to
    # `slugify_project()` in services/git_service.py — otherwise the
    # dispatch path and migration path point at different dirs. Postgres:
    # regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g') does the same
    # thing as Python `re.sub(r'[^a-z0-9]+', '-', name.lower())`.
    bind.execute(
        sa.text(
            """
            UPDATE agents
               SET workspace_path = :new_path || '/' ||
                   TRIM(BOTH '-' FROM regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g'))
             WHERE agent_runtime = 'cli-bridge'
               AND (
                    workspace_path LIKE :legacy_host
                 OR workspace_path LIKE :legacy_container
                 OR workspace_path IS NULL
               )
            """
        ),
        {
            "new_path": mc_root,
            "legacy_host": f"{home}/.openclaw/workspace-%",
            "legacy_container": "/workspace/%",
        },
    )

    # Boss runs on the host — he reads ~/Workspace/ directly.
    bind.execute(
        sa.text(
            "UPDATE agents SET workspace_path = :p "
            "WHERE name = 'Boss' AND agent_runtime = 'host'"
        ),
        {"p": f"{home}/Workspace"},
    )

    # Henry (gateway messenger) — read-oriented window into the operator's projects.
    bind.execute(
        sa.text(
            "UPDATE agents SET workspace_path = :p "
            "WHERE name = 'Henry' AND agent_runtime = 'openclaw'"
        ),
        {"p": f"{home}/Workspace/Projects"},
    )


def downgrade() -> None:
    # Restore the legacy ~/.openclaw/workspace-<slug> pattern for cli-bridge
    # agents. Boss + Henry downgrade is no-op (their old values were bogus
    # container paths, restoring them makes things worse).
    bind = op.get_bind()
    home = _home()
    if not home:
        return
    bind.execute(
        sa.text(
            """
            UPDATE agents
               SET workspace_path = :legacy_base || '-' || LOWER(REPLACE(name, ' ', '-'))
             WHERE agent_runtime = 'cli-bridge'
               AND workspace_path LIKE :new_prefix
            """
        ),
        {
            "legacy_base": f"{home}/.openclaw/workspace",
            "new_prefix": f"{home}/.mc/workspaces/%",
        },
    )
