"""runtimes.display_name — repair label drift by deriving the name (2026-07-28)

The registry looked like it held duplicates::

    slug                     display_name                            model_identifier
    anthropic-claude-opus    "Claude Opus 4.7 (Anthropic Pro/Max)"   claude-opus-4-8
    anthropic-claude-sonnet  "Claude Sonnet 4.6 (Anthropic Pro/Max)" claude-sonnet-5
    anthropic-claude-opus-5  "claude-opus-5"                         claude-opus-5
    ollama-cloud             "Ollama Cloud (glm-5.1)"                glm-5.1
    ollama-cloud-glm-5-2     "glm-5.2"                               glm-5.2

They are not duplicates but LABEL DRIFT: three names were typed by hand, two
were written raw by the catalog bind. Two of the hand-typed ones carry a
version the row does not run — and `claude-sonnet-4-6` is a real Anthropic
model, so "Claude Sonnet 4.6" on a row driving `claude-sonnet-5` reads as a
different model rather than as a typo.

This migration re-labels the affected rows with the derived name.
`app.services.runtime_naming.derive_runtime_display_name` is IMPORTED rather
than copied (same pattern as 0152 importing `vault_key_migration`): the whole
point of this change is that there is exactly one naming rule, so a private
copy here would be able to disagree with it.

Scope, deliberately narrow:
* Only rows whose endpoint host is a KNOWN provider (api.anthropic.com,
  ollama.com, cli-chat-proxy.grok.com, api.kimi.com) AND that carry a
  `model_identifier`. Everything else the rule returns None for and this
  migration does not touch: local/curated runtimes (vLLM, LM Studio, unsloth,
  omp, hermes) keep names that carry information no model id has ("Spark vLLM
  (Laguna/Qwen — switchable)"), and rows whose model the probe has not filled
  in yet cannot be named at all.
* `slug` is NOT changed. Agents reference `runtime_id`, but slugs appear in
  configs, skills and docs — renaming them would be a real outage risk for a
  cosmetic gain. The dedupe guard added to the catalog bind (endpoint +
  model_identifier) is what stops the historic slug spellings from producing a
  second row from here on.
* Re-running is a no-op: only rows where the derived name DIFFERS are updated.

MERGE ORDER: numbered 0167 on top of 0166_thread_telegram_topic_id, which was
head at authoring time. Parallel branches have collided on migration numbers
twice already in this repo — if another 0167 lands first, RENUMBER this file
(revision + down_revision) so the chain stays linear and single-headed.
Alembic cannot express "either order".

Revision ID: 0168_runtime_display_names
Revises: 0167_project_telegram_topic_id
Create Date: 2026-07-28
"""
import logging

import sqlalchemy as sa
from alembic import op

from app.services.runtime_naming import derive_runtime_display_name

revision = "0168_runtime_display_names"
down_revision = "0167_project_telegram_topic_id"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime_naming")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT slug, display_name, endpoint, model_identifier, runtime_type "
            "FROM runtimes"
        )
    ).fetchall()

    for slug, display_name, endpoint, model_identifier, runtime_type in rows:
        derived = derive_runtime_display_name(endpoint, model_identifier, runtime_type)
        if not derived or derived == display_name:
            continue
        logger.info("runtime %s: %r -> %r", slug, display_name, derived)
        bind.execute(
            sa.text("UPDATE runtimes SET display_name = :name WHERE slug = :slug"),
            {"name": derived, "slug": slug},
        )


def downgrade() -> None:
    # No-op, on purpose. The pre-migration labels were hand-typed and partly
    # FALSE ("Claude Opus 4.7" on a row running claude-opus-4-8). They are not
    # recoverable from any column — the only way to restore them would be to
    # hard-code this one installation's strings into the migration, which would
    # write wrong names onto every other installation on downgrade.
    # Nothing is lost by leaving the derived names in place: no schema changed,
    # `model_identifier` (the actual truth) was never touched, and an operator
    # who wants a different label can just edit the runtime.
    pass
