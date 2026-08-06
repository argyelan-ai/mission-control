"""ssh_process runtime type + one-click install fields on local_recipes.

Two halves of the same feature (PR 6):

runtimes
  * stop_command    — the engine's own stop script, when it ships one. NULL
    falls back to ``pkill -x <process_name>``.
  * process_name    — what ``pgrep -x`` must find for the runtime to count as
    running. This is the ssh_process equivalent of ``container_name``: without
    it MC can start a host process but never observe or stop it again.
  * exclusive_memory — "this runtime owns the whole box". Deliberately NOT
    reusing ``single_instance``: that flag means "only one AGENT may bind to
    this runtime" (Phase 24 / HERM-04, enforced in agent_runtime_switch and
    shown as a lock in the UI). A 110 GiB model that no agent may switch to is
    the opposite of what this feature is for, so memory exclusivity gets its
    own column.

local_recipes
  * install_template — the command that puts the engine on the box. Rendered
    and run as a background job (services/recipe_install), never at start time.
  * stop_template / process_name — what a runtime created from this recipe gets
    as stop_command / process_name. The catalogue has to carry them: the UI
    creates the runtime row, and "how is this engine stopped" is knowledge of
    the recipe, not something a frontend may invent.
  * author / author_url — attribution. Community engines are somebody's work
    and the card says whose.

The author backfill is data, not schema: rows for the builtin seed already
exist in every deployed DB, and the seeder is insert-only (it never revisits a
known slug), so without this UPDATE the credits would only ever show up on a
fresh install. Scoped to source_registry='builtin' AND author IS NULL so an
operator edit or an imported registry is never overwritten.

Revision ID: 0177_ssh_process_runtime
Revises: 0176_local_recipes
"""
import sqlalchemy as sa

from alembic import op

revision = "0177_ssh_process_runtime"
down_revision = "0176_local_recipes"
branch_labels = None
depends_on = None


# slug → (author, author_url) for the recipes shipped by 0176's seed file.
_BUILTIN_AUTHORS = {
    "qwen36-general-spark": ("Alibaba Qwen Team", "https://qwenlm.github.io"),
    "laguna-s21-nvfp4": ("poolside", "https://poolside.ai"),
    "qwen3-coder-next-80b-a3b-gb10": (
        "saricles (quant) · Alibaba Qwen Team (model)",
        "https://huggingface.co/saricles/Qwen3-Coder-Next-NVFP4-GB10",
    ),
    "deepseek-v4-flash-spark": ("DeepSeek", "https://www.deepseek.com"),
    "gemma4-27b-nvfp4": (
        "Google DeepMind",
        "https://deepmind.google/technologies/gemma/",
    ),
    "qwen36-27b-nvfp4": (
        "Unsloth (quant) · Alibaba Qwen Team (model)",
        "https://huggingface.co/unsloth/Qwen3.6-27B-NVFP4",
    ),
    "qwen3-embedding-0.6b": (
        "Alibaba Qwen Team",
        "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF",
    ),
    "qwen3-8b-gguf-q4": (
        "Alibaba Qwen Team",
        "https://huggingface.co/Qwen/Qwen3-8B-GGUF",
    ),
}


def upgrade() -> None:
    op.add_column("runtimes", sa.Column("stop_command", sa.Text(), nullable=True))
    op.add_column("runtimes", sa.Column("process_name", sa.String(length=64), nullable=True))
    op.add_column(
        "runtimes",
        sa.Column(
            "exclusive_memory",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    op.add_column("local_recipes", sa.Column("install_template", sa.Text(), nullable=True))
    op.add_column("local_recipes", sa.Column("stop_template", sa.Text(), nullable=True))
    op.add_column("local_recipes", sa.Column("process_name", sa.String(length=64), nullable=True))
    op.add_column("local_recipes", sa.Column("author", sa.String(length=128), nullable=True))
    op.add_column("local_recipes", sa.Column("author_url", sa.String(length=512), nullable=True))

    recipes = sa.table(
        "local_recipes",
        sa.column("slug", sa.String),
        sa.column("author", sa.String),
        sa.column("author_url", sa.String),
        sa.column("source_registry", sa.String),
    )
    for slug, (author, author_url) in _BUILTIN_AUTHORS.items():
        op.execute(
            recipes.update()
            .where(recipes.c.slug == slug)
            .where(recipes.c.source_registry == "builtin")
            .where(recipes.c.author.is_(None))
            .values(author=author, author_url=author_url)
        )


def downgrade() -> None:
    op.drop_column("local_recipes", "author_url")
    op.drop_column("local_recipes", "author")
    op.drop_column("local_recipes", "process_name")
    op.drop_column("local_recipes", "stop_template")
    op.drop_column("local_recipes", "install_template")
    op.drop_column("runtimes", "exclusive_memory")
    op.drop_column("runtimes", "process_name")
    op.drop_column("runtimes", "stop_command")
