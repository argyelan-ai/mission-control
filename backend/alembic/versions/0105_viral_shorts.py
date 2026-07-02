"""0105_viral_shorts

Revision ID: 0105
Revises: 0104
Create Date: 2026-05-07

Argyelan Viral-Shorts-Pipeline:
- TrendSignal (neu) — Multi-Source Trend-Aggregation für Topic-Picking
- NewsArticle erweitert — 8-Dim Virality-Score, Hook-Varianten
- ContentPipeline erweitert — viral_metadata, auto_publish, pipeline_kind,
  mp4_path, captions_per_platform
- NewsPostSchedule.platform bleibt freier String → tiktok/youtube_shorts ohne
  Schema-Aenderung nutzbar (kein Enum-Constraint vorhanden, siehe news.py)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = '0105'
down_revision: Union[str, None] = '0104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. TrendSignal (neue Tabelle)
    op.create_table(
        'trend_signals',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('topic_keyword', sa.String(), nullable=False),
        sa.Column('topic_cluster_id', UUID(as_uuid=True), nullable=True),
        sa.Column('engagement_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('sample_post_text', sa.Text(), nullable=True),
        sa.Column('sample_post_url', sa.String(), nullable=True),
        sa.Column('language', sa.String(length=10), nullable=False, server_default='en'),
        sa.Column('dach_relevance', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            'related_news_id', UUID(as_uuid=True),
            sa.ForeignKey('news_articles.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column(
            'captured_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('extra_metadata', JSONB(), nullable=True),
    )
    op.create_index('ix_trend_signals_topic_keyword', 'trend_signals', ['topic_keyword'])
    op.create_index('ix_trend_signals_expires_at', 'trend_signals', ['expires_at'])
    op.create_index('ix_trend_signals_source', 'trend_signals', ['source'])

    # 2. NewsArticle erweitern (Virality-Score + Hook-Varianten)
    op.add_column('news_articles', sa.Column('viral_score_dimensions', JSONB(), nullable=True))
    op.add_column('news_articles', sa.Column('viral_score_total', sa.Float(), nullable=True))
    op.add_column('news_articles', sa.Column('hook_variants', JSONB(), nullable=True))

    # 3. ContentPipeline erweitern (Viral-Shorts spezifisch)
    op.add_column('content_pipelines', sa.Column('viral_metadata', JSONB(), nullable=True))
    op.add_column(
        'content_pipelines',
        sa.Column('auto_publish', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'content_pipelines',
        sa.Column('pipeline_kind', sa.String(), nullable=False, server_default='article'),
    )
    op.add_column('content_pipelines', sa.Column('mp4_path', sa.String(), nullable=True))
    op.add_column('content_pipelines', sa.Column('captions_per_platform', JSONB(), nullable=True))
    op.create_index(
        'ix_content_pipelines_pipeline_kind', 'content_pipelines', ['pipeline_kind']
    )

    # 4. Globale viral_shorts settings — als Rows in bestehender settings-artiger Tabelle
    #    Wir nutzen board_memory NICHT für globale Settings (zu generic).
    #    Stattdessen: neue dedizierte Tabelle viral_shorts_settings (Singleton-Pattern).
    op.create_table(
        'viral_shorts_settings',
        sa.Column('id', sa.Integer(), primary_key=True, server_default='1'),
        sa.Column('auto_publish_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('auto_publish_min_score', sa.Integer(), nullable=False, server_default='75'),
        sa.Column('daily_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('cron_expression', sa.String(), nullable=False, server_default='0 8 * * *'),
        sa.Column('cron_timezone', sa.String(), nullable=False, server_default='Europe/Berlin'),
        sa.Column('voice_id', sa.String(), nullable=True),
        sa.Column('soul_id', sa.String(), nullable=True),
        sa.Column('extra', JSONB(), nullable=True),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
        sa.CheckConstraint('id = 1', name='viral_shorts_settings_singleton'),
    )
    # Singleton-Row inserten
    op.execute(
        "INSERT INTO viral_shorts_settings (id, voice_id, soul_id) VALUES "
        "(1, 'znNXE8slCa5Zuhfucx40', '7fb52119-ee69-4f1e-9716-9ee03903b60c')"
    )


def downgrade() -> None:
    op.drop_table('viral_shorts_settings')

    op.drop_index('ix_content_pipelines_pipeline_kind', table_name='content_pipelines')
    op.drop_column('content_pipelines', 'captions_per_platform')
    op.drop_column('content_pipelines', 'mp4_path')
    op.drop_column('content_pipelines', 'pipeline_kind')
    op.drop_column('content_pipelines', 'auto_publish')
    op.drop_column('content_pipelines', 'viral_metadata')

    op.drop_column('news_articles', 'hook_variants')
    op.drop_column('news_articles', 'viral_score_total')
    op.drop_column('news_articles', 'viral_score_dimensions')

    op.drop_index('ix_trend_signals_source', table_name='trend_signals')
    op.drop_index('ix_trend_signals_expires_at', table_name='trend_signals')
    op.drop_index('ix_trend_signals_topic_keyword', table_name='trend_signals')
    op.drop_table('trend_signals')
