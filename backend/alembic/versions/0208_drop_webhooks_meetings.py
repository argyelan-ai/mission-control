"""0208 — drop the GitHub-webhook, meeting and Discord-config tables (E5).

Three features without a caller were removed in the same change: the
GitHub/local-push webhook receiver, agent "meetings" (superseded by the
group chat, ADR-075) and the Discord channel/guild config API. Discord
notifications themselves stay; guild and category come from the
environment (DISCORD_GUILD_ID / DISCORD_CATEGORY_ID) only.

Drops (data included — take a backup first if an install still holds any):
  webhook_payloads, webhooks, agent_meeting_messages, agent_meetings,
  discord_config. ``agent_messages`` stays.

No surviving table has a foreign key into these tables.

Downgrade recreates the schema exactly as it was — empty (the one-row
``discord_config`` is not re-seeded). The DDL below is a pg_dump of a fresh
``alembic upgrade 0207`` database.

Revision ID: 0208_drop_webhooks_meetings
Revises: 0207_drop_workflow_tables
"""
from alembic import op

revision = "0208_drop_webhooks_meetings"
down_revision = "0207_drop_workflow_tables"
branch_labels = None
depends_on = None

# Children before parents, no CASCADE: an unknown dependent must fail loudly.
DROPPED_TABLES = (
    "webhook_payloads",
    "webhooks",
    "agent_meeting_messages",
    "agent_meetings",
    "discord_config",
)

_SCHEMA = """
CREATE TABLE agent_meeting_messages (
    id uuid NOT NULL,
    meeting_id uuid NOT NULL,
    agent_id uuid,
    agent_name character varying,
    role character varying NOT NULL,
    content text NOT NULL,
    round integer DEFAULT 1 NOT NULL,
    topic_index integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE agent_meetings (
    id uuid NOT NULL,
    board_id uuid NOT NULL,
    title character varying NOT NULL,
    meeting_type character varying DEFAULT 'ad_hoc'::character varying NOT NULL,
    status character varying DEFAULT 'scheduled'::character varying NOT NULL,
    agenda json,
    participant_ids json,
    summary text,
    decisions json,
    action_items json,
    memory_id uuid,
    scheduled_at timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE discord_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    guild_id text,
    category_id text,
    bot_configured boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE webhook_payloads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    webhook_id uuid NOT NULL,
    payload jsonb NOT NULL,
    headers jsonb,
    source_ip text,
    processed boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);
CREATE TABLE webhooks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    board_id uuid NOT NULL,
    name text NOT NULL,
    secret text,
    is_enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);
ALTER TABLE ONLY agent_meeting_messages
    ADD CONSTRAINT agent_meeting_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY agent_meetings
    ADD CONSTRAINT agent_meetings_pkey PRIMARY KEY (id);
ALTER TABLE ONLY discord_config
    ADD CONSTRAINT discord_config_pkey PRIMARY KEY (id);
ALTER TABLE ONLY webhook_payloads
    ADD CONSTRAINT webhook_payloads_pkey PRIMARY KEY (id);
ALTER TABLE ONLY webhooks
    ADD CONSTRAINT webhooks_pkey PRIMARY KEY (id);
CREATE INDEX ix_agent_meeting_messages_meeting_id ON agent_meeting_messages USING btree (meeting_id);
CREATE INDEX ix_agent_meetings_board_id ON agent_meetings USING btree (board_id);
ALTER TABLE ONLY agent_meeting_messages
    ADD CONSTRAINT agent_meeting_messages_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES agents(id);
ALTER TABLE ONLY agent_meeting_messages
    ADD CONSTRAINT agent_meeting_messages_meeting_id_fkey FOREIGN KEY (meeting_id) REFERENCES agent_meetings(id);
ALTER TABLE ONLY agent_meetings
    ADD CONSTRAINT agent_meetings_board_id_fkey FOREIGN KEY (board_id) REFERENCES boards(id);
ALTER TABLE ONLY agent_meetings
    ADD CONSTRAINT agent_meetings_memory_id_fkey FOREIGN KEY (memory_id) REFERENCES board_memory(id);
ALTER TABLE ONLY webhook_payloads
    ADD CONSTRAINT webhook_payloads_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE;
ALTER TABLE ONLY webhooks
    ADD CONSTRAINT webhooks_board_id_fkey FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE;
"""


def upgrade() -> None:
    for table in DROPPED_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    for statement in _SCHEMA.split(";\n"):
        if statement.strip():
            op.execute(statement.strip().rstrip(";"))
