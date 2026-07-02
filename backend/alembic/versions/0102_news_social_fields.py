"""news_social_fields

Revision ID: 0102
Revises: 0101
Create Date: 2026-05-04 20:30:00

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0102'
down_revision: Union[str, None] = '0101'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('news_articles', sa.Column('suggested_tweet', sa.Text(), nullable=True))
    op.add_column('news_articles', sa.Column('suggested_linkedin', sa.Text(), nullable=True))
    op.add_column('news_articles', sa.Column('approved_tweet', sa.Text(), nullable=True))
    op.add_column('news_articles', sa.Column('approved_linkedin', sa.Text(), nullable=True))
    op.add_column('news_articles', sa.Column('social_status', sa.String(length=20), nullable=False, server_default='pending'))
    op.create_index(op.f('ix_news_articles_social_status'), 'news_articles', ['social_status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_news_articles_social_status'), table_name='news_articles')
    op.drop_column('news_articles', 'social_status')
    op.drop_column('news_articles', 'approved_linkedin')
    op.drop_column('news_articles', 'approved_tweet')
    op.drop_column('news_articles', 'suggested_linkedin')
    op.drop_column('news_articles', 'suggested_tweet')
