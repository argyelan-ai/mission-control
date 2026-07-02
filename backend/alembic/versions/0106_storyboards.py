"""0106_storyboards

Revision ID: 0106
Revises: 0105
Create Date: 2026-05-09

Storyboard table — Operator-as-Director architecture.

A storyboard is the planned visual breakdown of a viral short BEFORE rendering.
It contains an ordered array of "beats" with a 13-field schema per beat
(spoken_text, focal_element, camera_movement, transition, color_temperature,
component_key, visual_metaphor, etc). The operator edits storyboards in the Director
Console, approves them, then Davinci is dispatched to render strictly what
the storyboard specifies.

1:1 relationship with content_pipeline. Status flow:
    draft → approved → rendering → rendered → published
The operator can re-edit a draft any time. Once approved, only re-render allowed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = '0106'
down_revision: Union[str, None] = '0105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'storyboards',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'content_pipeline_id', UUID(as_uuid=True),
            sa.ForeignKey('content_pipelines.id', ondelete='CASCADE'),
            nullable=False, unique=True,
        ),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('topic_summary', sa.Text(), nullable=True),
        sa.Column('topic_cluster', sa.String(), nullable=True),  # hard_news|opinion|release|tutorial|funding
        sa.Column('duration_s', sa.Float(), nullable=False, server_default='22.0'),
        sa.Column(
            'status', sa.String(), nullable=False, server_default='draft',
        ),  # draft|approved|rendering|rendered|review|published|rejected
        sa.Column('beats', JSONB(), nullable=False, server_default='[]'),
        # Voiceover — either ElevenLabs-generated OR reuse-existing
        sa.Column('voiceover_path', sa.String(), nullable=True),
        sa.Column('alignment_path', sa.String(), nullable=True),
        sa.Column('use_existing_voiceover', sa.Boolean(), nullable=False, server_default=sa.false()),
        # Render output
        sa.Column('mp4_path', sa.String(), nullable=True),
        sa.Column('props_json_path', sa.String(), nullable=True),
        sa.Column('render_log', sa.Text(), nullable=True),
        # Operator approval trail
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by', sa.String(), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        # Davinci task linkage
        sa.Column(
            'render_task_id', UUID(as_uuid=True),
            sa.ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True,
        ),
        # Diversity metadata for memory-table queries
        sa.Column('diversity_fingerprint', JSONB(), nullable=True),
        # Timestamps
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
    )
    op.create_index('ix_storyboards_status', 'storyboards', ['status'])
    op.create_index('ix_storyboards_topic_cluster', 'storyboards', ['topic_cluster'])
    op.create_index('ix_storyboards_created_at', 'storyboards', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_storyboards_created_at', table_name='storyboards')
    op.drop_index('ix_storyboards_topic_cluster', table_name='storyboards')
    op.drop_index('ix_storyboards_status', table_name='storyboards')
    op.drop_table('storyboards')
