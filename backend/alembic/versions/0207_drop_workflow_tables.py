"""0207 — drop the workflow / automation / playbook tables (E5).

The old execution path next to the task pipeline (playbooks, automations,
workflow templates and runs) has been functionally dead since the gateway
sunset (ADR-051); its routers, services and models were removed in the same
change. All seven tables were empty on the reference install.

Drops (data included — take a backup first if an install still holds any):
  automations, playbook_versions, playbooks, workflow_step_runs,
  workflow_runs, workflow_template_versions, workflow_templates.

Surviving tables keep their columns, only the FK constraints that point into
the dropped tables go:
  skill_candidates.playbook_id / automation_id (skill lab, frozen), and on
  installs that still carry the model-less skill_runs table,
  skill_runs.source_workflow_run_id / playbook_id / automation_id.

Downgrade recreates the schema exactly as it was — empty. The DDL below is a
pg_dump of a fresh alembic upgrade 0206 database.

Revision ID: 0207_drop_workflow_tables
Revises: 0206_drop_news_tables
"""
from alembic import op

revision = "0207_drop_workflow_tables"
down_revision = "0206_drop_news_tables"
branch_labels = None
depends_on = None

# Children before parents, no CASCADE: an unknown dependent must fail loudly.
WORKFLOW_TABLES = (
    "automations",
    "playbook_versions",
    "playbooks",
    "workflow_step_runs",
    "workflow_runs",
    "workflow_template_versions",
    "workflow_templates",
)

# (table, constraint, column, referenced table) on surviving tables.
_OUTSIDE_FKS = (
    ("skill_candidates", "skill_candidates_playbook_id_fkey", "playbook_id", "playbooks"),
    ("skill_candidates", "skill_candidates_automation_id_fkey", "automation_id", "automations"),
    ("skill_runs", "skill_runs_source_workflow_run_id_fkey", "source_workflow_run_id", "workflow_runs"),
    ("skill_runs", "skill_runs_playbook_id_fkey", "playbook_id", "playbooks"),
    ("skill_runs", "skill_runs_automation_id_fkey", "automation_id", "automations"),
)

_SCHEMA = """
CREATE TABLE automations (
    id uuid NOT NULL,
    playbook_id uuid NOT NULL,
    workflow_id uuid,
    board_id uuid,
    project_id uuid,
    name character varying NOT NULL,
    summary text,
    status character varying NOT NULL,
    trigger_type character varying NOT NULL,
    trigger_config json,
    delivery_config json,
    runtime_overrides json,
    last_run_at timestamp with time zone,
    next_run_at timestamp with time zone,
    created_by character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE playbook_versions (
    id uuid NOT NULL,
    playbook_id uuid NOT NULL,
    version integer NOT NULL,
    snapshot json NOT NULL,
    change_reason text,
    created_by character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE playbooks (
    id uuid NOT NULL,
    workflow_id uuid,
    board_id uuid,
    project_id uuid,
    skill_pack_id uuid,
    default_agent_id uuid,
    kind character varying NOT NULL,
    name character varying NOT NULL,
    summary text,
    goal text,
    scope character varying NOT NULL,
    status character varying NOT NULL,
    current_version integer NOT NULL,
    input_contract json,
    output_contract json,
    current_config json NOT NULL,
    preview_markdown text,
    extra_metadata json,
    review_notes text,
    created_by character varying NOT NULL,
    approved_by character varying,
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE workflow_runs (
    id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    workflow_version integer NOT NULL,
    definition_snapshot json NOT NULL,
    triggered_by character varying NOT NULL,
    trigger_payload json,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    status character varying NOT NULL,
    current_step_key character varying,
    context json NOT NULL,
    total_cost_tokens integer NOT NULL,
    delivery_status character varying,
    delivery_error text,
    delivered_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE workflow_step_runs (
    id uuid NOT NULL,
    run_id uuid NOT NULL,
    step_key character varying NOT NULL,
    step_index integer NOT NULL,
    step_name character varying NOT NULL,
    step_type character varying NOT NULL,
    execution_mode character varying NOT NULL,
    executor_type character varying,
    attempt integer NOT NULL,
    status character varying NOT NULL,
    rendered_input text,
    session_key character varying,
    output_text text,
    output_json json,
    stdout text,
    stderr text,
    exit_code integer,
    http_status integer,
    artifacts json,
    evaluation_result json,
    error_code character varying,
    error_message text,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    tokens_used integer NOT NULL
);
CREATE TABLE workflow_template_versions (
    id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    version integer NOT NULL,
    definition_snapshot json NOT NULL,
    change_reason text,
    created_by character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE workflow_templates (
    id uuid NOT NULL,
    board_id uuid,
    project_id uuid,
    name character varying NOT NULL,
    description text,
    trigger_type character varying NOT NULL,
    trigger_config json,
    status character varying NOT NULL,
    current_version integer NOT NULL,
    current_definition json NOT NULL,
    max_runtime_minutes integer NOT NULL,
    policy_profile character varying NOT NULL,
    execution_policy json,
    delivery_config json,
    reflect_on character varying NOT NULL,
    next_run_at timestamp with time zone,
    last_validated_at timestamp with time zone,
    created_by character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY automations
    ADD CONSTRAINT automations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY playbook_versions
    ADD CONSTRAINT playbook_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY playbooks
    ADD CONSTRAINT playbooks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY playbook_versions
    ADD CONSTRAINT uq_playbook_version UNIQUE (playbook_id, version);
ALTER TABLE ONLY workflow_template_versions
    ADD CONSTRAINT uq_workflow_template_version UNIQUE (workflow_id, version);
ALTER TABLE ONLY workflow_runs
    ADD CONSTRAINT workflow_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY workflow_step_runs
    ADD CONSTRAINT workflow_step_runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY workflow_template_versions
    ADD CONSTRAINT workflow_template_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY workflow_templates
    ADD CONSTRAINT workflow_templates_pkey PRIMARY KEY (id);
CREATE INDEX ix_automations_playbook_id ON automations USING btree (playbook_id);
CREATE INDEX ix_playbook_versions_playbook_id ON playbook_versions USING btree (playbook_id);
CREATE INDEX ix_playbooks_kind ON playbooks USING btree (kind);
CREATE INDEX ix_workflow_runs_workflow_id ON workflow_runs USING btree (workflow_id);
CREATE INDEX ix_workflow_step_runs_run_id ON workflow_step_runs USING btree (run_id);
CREATE INDEX ix_workflow_template_versions_workflow_id ON workflow_template_versions USING btree (workflow_id);
CREATE INDEX ix_workflow_templates_board_id ON workflow_templates USING btree (board_id);
CREATE INDEX ix_workflow_templates_project_id ON workflow_templates USING btree (project_id);
ALTER TABLE ONLY automations
    ADD CONSTRAINT automations_board_id_fkey FOREIGN KEY (board_id) REFERENCES boards(id);
ALTER TABLE ONLY automations
    ADD CONSTRAINT automations_playbook_id_fkey FOREIGN KEY (playbook_id) REFERENCES playbooks(id);
ALTER TABLE ONLY automations
    ADD CONSTRAINT automations_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects(id);
ALTER TABLE ONLY automations
    ADD CONSTRAINT automations_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES workflow_templates(id);
ALTER TABLE ONLY playbook_versions
    ADD CONSTRAINT playbook_versions_playbook_id_fkey FOREIGN KEY (playbook_id) REFERENCES playbooks(id);
ALTER TABLE ONLY playbooks
    ADD CONSTRAINT playbooks_board_id_fkey FOREIGN KEY (board_id) REFERENCES boards(id);
ALTER TABLE ONLY playbooks
    ADD CONSTRAINT playbooks_default_agent_id_fkey FOREIGN KEY (default_agent_id) REFERENCES agents(id);
ALTER TABLE ONLY playbooks
    ADD CONSTRAINT playbooks_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects(id);
ALTER TABLE ONLY playbooks
    ADD CONSTRAINT playbooks_skill_pack_id_fkey FOREIGN KEY (skill_pack_id) REFERENCES skill_packs(id);
ALTER TABLE ONLY playbooks
    ADD CONSTRAINT playbooks_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES workflow_templates(id);
ALTER TABLE ONLY workflow_runs
    ADD CONSTRAINT workflow_runs_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES workflow_templates(id);
ALTER TABLE ONLY workflow_step_runs
    ADD CONSTRAINT workflow_step_runs_run_id_fkey FOREIGN KEY (run_id) REFERENCES workflow_runs(id);
ALTER TABLE ONLY workflow_template_versions
    ADD CONSTRAINT workflow_template_versions_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES workflow_templates(id);
ALTER TABLE ONLY workflow_templates
    ADD CONSTRAINT workflow_templates_board_id_fkey FOREIGN KEY (board_id) REFERENCES boards(id);
ALTER TABLE ONLY workflow_templates
    ADD CONSTRAINT workflow_templates_project_id_fkey FOREIGN KEY (project_id) REFERENCES projects(id);
"""


def upgrade() -> None:
    for table, constraint, _column, _ref in _OUTSIDE_FKS:
        op.execute(f"ALTER TABLE IF EXISTS {table} DROP CONSTRAINT IF EXISTS {constraint}")
    for table in WORKFLOW_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    for statement in _SCHEMA.split(";\n"):
        if statement.strip():
            op.execute(statement.strip().rstrip(";"))
    for table, constraint, column, ref in _OUTSIDE_FKS:
        # skill_runs has no model and is missing on fresh installs.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('{table}') IS NOT NULL THEN
                    ALTER TABLE {table} ADD CONSTRAINT {constraint}
                        FOREIGN KEY ({column}) REFERENCES {ref}(id);
                END IF;
            END
            $$;
            """
        )
