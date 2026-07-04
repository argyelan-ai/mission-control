"""Repos registry — first-class repo model with per-repo working rules.

Creates the `repos` table (one row per GitHub repo, carrying rules_md that
gets injected into dispatch directives) and `projects.repo_id` FK.

Backfill: every distinct github_repo_name already present on projects
becomes a repo row (source='mc'), and the owning projects get linked.
Legacy github_repo_url/name fields stay in place — they remain the read
path for clone/PR flows and are synced whenever a project is (re-)linked.

Revision ID: 0137
Revises: 0136
"""
import os
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("default_branch", sa.String(), nullable=False, server_default="main"),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("rules_md", sa.String(), nullable=True),
        sa.Column("visibility", sa.String(), nullable=False, server_default="private"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.String(), nullable=False, server_default="mc"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_repos_full_name", "repos", ["full_name"], unique=True)

    op.add_column("projects", sa.Column("repo_id", sa.Uuid(), nullable=True))
    op.create_index("ix_projects_repo_id", "projects", ["repo_id"])
    op.create_foreign_key(
        "fk_projects_repo_id", "projects", "repos", ["repo_id"], ["id"]
    )

    # ── Backfill: distinct project repos → repo rows + links ──────────
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, github_repo_name, github_repo_url FROM projects "
            "WHERE github_repo_name IS NOT NULL AND github_repo_name != ''"
        )
    ).fetchall()

    owner = os.environ.get("GITHUB_OWNER", "")
    seen: dict[str, str] = {}  # full_name → repo id (as str)
    for project_id, raw_repo_name, repo_url in rows:
        # Canonical form is owner/name — legacy init-repo rows stored just
        # "mc-slug". Normalize when the owner is known (gh --repo needs it).
        repo_name = raw_repo_name
        if "/" not in repo_name and owner:
            repo_name = f"{owner}/{repo_name}"
        if repo_name not in seen:
            repo_id = str(uuid.uuid4())
            seen[repo_name] = repo_id
            url = (repo_url or f"https://github.com/{repo_name}.git").removesuffix(".git")
            bind.execute(
                sa.text(
                    "INSERT INTO repos (id, full_name, url, default_branch, "
                    "visibility, is_active, source) "
                    "VALUES (:id, :full_name, :url, 'main', 'private', :active, 'mc')"
                ),
                {"id": repo_id, "full_name": repo_name, "url": url, "active": True},
            )
        # Link + normalize the legacy name in one go: every downstream
        # consumer (`gh pr merge --repo`, `gh api repos/{name}/branches`)
        # needs owner/name — the bare "mc-slug" form silently broke them.
        bind.execute(
            sa.text(
                "UPDATE projects SET repo_id = :rid, github_repo_name = :rname "
                "WHERE id = :pid"
            ),
            {"rid": seen[repo_name], "rname": repo_name, "pid": project_id},
        )


def downgrade() -> None:
    op.drop_constraint("fk_projects_repo_id", "projects", type_="foreignkey")
    op.drop_index("ix_projects_repo_id", table_name="projects")
    op.drop_column("projects", "repo_id")
    op.drop_index("ix_repos_full_name", table_name="repos")
    op.drop_table("repos")
