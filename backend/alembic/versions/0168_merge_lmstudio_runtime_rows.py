"""runtimes — merge the two LM Studio rows into one engine row (2026-07-28)

SEPARATE from 0167 on purpose. 0167 only corrects labels; this one is a
CONTENT decision about what a runtime row means, and Mark should be able to
judge (and revert) it on its own.

The fiction
-----------
Two rows described one engine::

    nemotron-super   "Nemotron 3 Super"             http://<dgx>:1234/v1  lmstudio
    qwen-coder-lms   "Qwen3.6 35B A3B (LM Studio)"  http://<dgx>:1234/v1  lmstudio

Same endpoint, same LM Studio instance. Verified with Mark on 2026-07-28: LM
Studio serves exactly ONE chat model at a time (plus a permanently loaded
embedding model), and the model is switched IN LM STUDIO, not through an MC
runtime switch. So a per-model row cannot be true: the probe asks the same
`/v1/models` twice and has no way to tell which row is supposed to mean what.
This is the local counterpart to the drifted cloud labels 0167 repairs — a name
promising a specific model that the row cannot guarantee.

The registry already contains the correct shape for this: `qwen-general` is ONE
row ("Spark vLLM (Laguna/Qwen — switchable)") whose model follows the engine.

What this does
--------------
* `qwen-coder-lms` survives as the engine row: named after the ENGINE, with
  `model_identifier` set to NULL so the probe
  (`ensure_runtime_model_identifier` / runtime watcher) fills in whatever LM
  Studio currently serves, and with the union of both rows' role_tags so the
  "fallback" role does not silently disappear.
* `nemotron-super` is DISABLED, not deleted. A registry other things can point
  at is the wrong place for destructive cleanup, and `enabled = false` is
  trivially reversible.
* `qwen-coder-lms` is the survivor because it is the slug the rest of the
  codebase already references (docker_agent_sync, compose renderer tests, the
  historical Sparky link in 0078).

Safety
------
The merge is SKIPPED (with a warning, not an error) unless, on this
installation:
  * both rows exist, are `lmstudio`, and share the same endpoint, and
  * NO agent references either row.
Verified read-only against the live DB before writing this: neither row has an
agent bound (all 14 agents sit on anthropic-*/qwen-general/grok-cloud/
kimi-cloud). `runtime_schedules` keys on the generic string 'lmstudio', not on
either slug, so schedules are unaffected.

MERGE ORDER: numbered 0168 on top of 0167. This repo has had migration-number
collisions on parallel branches twice — if another 0168 lands first, RENUMBER
this file (revision + down_revision) so the chain stays linear.

Revision ID: 0168_merge_lmstudio_runtime_rows
Revises: 0167_runtime_display_name_derived
Create Date: 2026-07-28
"""
import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "0168_merge_lmstudio_runtime_rows"
down_revision = "0167_runtime_display_name_derived"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.lmstudio_merge")

SURVIVOR_SLUG = "qwen-coder-lms"
RETIRED_SLUG = "nemotron-super"
# Names the ENGINE, not a model — same pattern as `qwen-general`.
SURVIVOR_DISPLAY_NAME = "LM Studio (DGX — model follows the engine)"


def _row(bind, slug):
    return bind.execute(
        sa.text(
            "SELECT id, slug, display_name, endpoint, runtime_type, role_tags, ui_order "
            "FROM runtimes WHERE slug = :slug"
        ),
        {"slug": slug},
    ).mappings().first()


def _agent_count(bind, runtime_id) -> int:
    return bind.execute(
        sa.text("SELECT COUNT(*) FROM agents WHERE runtime_id = :rid"),
        {"rid": runtime_id},
    ).scalar_one()


def _tags(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [t for t in (value or []) if isinstance(t, str)]


def upgrade() -> None:
    bind = op.get_bind()
    survivor = _row(bind, SURVIVOR_SLUG)
    retired = _row(bind, RETIRED_SLUG)

    if survivor is None or retired is None:
        logger.info(
            "LM Studio merge skipped: %s/%s not both present on this installation",
            SURVIVOR_SLUG,
            RETIRED_SLUG,
        )
        return
    if survivor["runtime_type"] != "lmstudio" or retired["runtime_type"] != "lmstudio":
        logger.warning("LM Studio merge skipped: rows are not both runtime_type=lmstudio")
        return
    if (survivor["endpoint"] or "").rstrip("/") != (retired["endpoint"] or "").rstrip("/"):
        logger.warning(
            "LM Studio merge skipped: endpoints differ (%s vs %s) — these are two real engines",
            survivor["endpoint"],
            retired["endpoint"],
        )
        return
    bound = _agent_count(bind, survivor["id"]) + _agent_count(bind, retired["id"])
    if bound:
        logger.warning(
            "LM Studio merge skipped: %d agent(s) reference these runtimes — "
            "an operator must move them first",
            bound,
        )
        return

    merged_tags = _tags(survivor["role_tags"])
    for tag in _tags(retired["role_tags"]):
        if tag not in merged_tags:
            merged_tags.append(tag)
    tags_expr = "CAST(:tags AS jsonb)" if bind.dialect.name == "postgresql" else ":tags"

    # Keep the earlier of the two positions so the cockpit list does not jump.
    orders = [o for o in (survivor["ui_order"], retired["ui_order"]) if o is not None]
    ui_order = min(orders) if orders else None

    bind.execute(
        sa.text(
            "UPDATE runtimes SET display_name = :name, model_identifier = NULL, "
            f"role_tags = {tags_expr}, ui_order = COALESCE(:ui_order, ui_order) "
            "WHERE slug = :slug"
        ),
        {
            "name": SURVIVOR_DISPLAY_NAME,
            "tags": json.dumps(merged_tags),
            "ui_order": ui_order,
            "slug": SURVIVOR_SLUG,
        },
    )
    bind.execute(
        sa.text("UPDATE runtimes SET enabled = false WHERE slug = :slug"),
        {"slug": RETIRED_SLUG},
    )
    logger.info(
        "LM Studio merge: %s -> %r (tags=%s), %s disabled",
        SURVIVOR_SLUG,
        SURVIVOR_DISPLAY_NAME,
        merged_tags,
        RETIRED_SLUG,
    )


def downgrade() -> None:
    """Re-enable the retired row — the only part that is deterministic.

    The survivor's previous display name and role_tags are NOT restored: they
    were installation-specific strings, and hard-coding this installation's
    values here would write wrong data onto every other one. Re-enabling
    `nemotron-super` puts the registry back into the two-row state, which is
    what a downgrade is actually for; the labels can be edited in the UI.
    """
    op.get_bind().execute(
        sa.text("UPDATE runtimes SET enabled = true WHERE slug = :slug"),
        {"slug": RETIRED_SLUG},
    )
