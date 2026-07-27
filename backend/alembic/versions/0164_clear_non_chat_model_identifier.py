"""runtimes.model_identifier — clear rows pinned to a non-chat model (Issue #161)

`probe_runtime_model` used to take `data[0].id` from `/v1/models` blindly.
LM Studio routinely serves an embedding model alongside the chat one with no
guaranteed response order, so two runtimes landed on an embedding model as
their "chat" model_identifier: `nemotron-super` and `qwen-coder-lms` were
both observed pinned to `text-embedding-nomic-embed-text-v1.5` (2026-07-25).

The code fix (agent_runtime_switch.select_probed_model / _is_chat_capable)
stops this from happening again, but does not repair rows already written
with a bad value. This migration sets `model_identifier` back to NULL for
any row whose value contains one of the same denylist markers used by
`_is_chat_capable` — NULL rather than a guessed replacement, because
`ensure_runtime_model_identifier` / the runtime watcher will re-probe and
persist the correct chat model the next time the runtime is touched.
Guessing a name here would risk writing a second wrong value.

Predicate is deliberately narrow (same markers as the code denylist, nothing
broader) to avoid touching rows that are already correct.

MERGE ORDER: this revises 0163_boss_harness_claude, which lives on
`fix/model-hardcode-sanitation` (PR #158) and is not on main yet. **Merge #158
first.** If it lands after this one instead, renumber both so the chain stays
linear and single-headed — alembic has no way to express "either order".

Revision ID: 0164
Revises: 0163
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0164"
down_revision = "0163"
branch_labels = None
depends_on = None

# Mirrors app.services.agent_runtime_switch._NON_CHAT_MODEL_MARKERS. Kept as a
# literal copy (not imported) because Alembic migrations must stay runnable
# even after the app-code denylist changes shape or moves.
_NON_CHAT_MODEL_MARKERS = (
    "embed",
    "rerank",
    "whisper",
    "tts-",
    "-tts",
    "stable-diffusion",
    "clip-",
)


def upgrade() -> None:
    conditions = " OR ".join(
        "lower(model_identifier) LIKE :marker_{i}".format(i=i)
        for i in range(len(_NON_CHAT_MODEL_MARKERS))
    )
    params = {
        f"marker_{i}": f"%{marker}%"
        for i, marker in enumerate(_NON_CHAT_MODEL_MARKERS)
    }
    op.execute(
        sa.text(
            f"""
            UPDATE runtimes
            SET model_identifier = NULL
            WHERE model_identifier IS NOT NULL
              AND ({conditions})
            """
        ).bindparams(**params)
    )


def downgrade() -> None:
    # No-op: the pre-migration value was wrong (a non-chat model masquerading
    # as the runtime's chat model_identifier). There is nothing correct to
    # restore it to — re-running the probe (ensure_runtime_model_identifier /
    # the runtime watcher) is the only sound way to repopulate this column.
    pass
