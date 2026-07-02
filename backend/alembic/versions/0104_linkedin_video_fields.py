"""0104_linkedin_video_fields

Revision ID: 0104
Revises: 0103
Create Date: 2025-05-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0104'
down_revision: Union[str, None] = '0103'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add LinkedIn Video specific fields to content_pipelines."""
    op.add_column('content_pipelines', sa.Column('script_md', sa.Text(), nullable=True))
    op.add_column('content_pipelines', sa.Column('design_md_path', sa.String(), nullable=True))
    op.add_column('content_pipelines', sa.Column('video_draft_path', sa.String(), nullable=True))
    op.add_column('content_pipelines', sa.Column('video_final_path', sa.String(), nullable=True))
    op.add_column('content_pipelines', sa.Column('linkedin_post_text', sa.Text(), nullable=True))
    op.add_column('content_pipelines', sa.Column('linkedin_posted_url', sa.String(), nullable=True))
    op.add_column('content_pipelines', sa.Column('linkedin_credential_id', sa.Uuid(), nullable=True))
    
    op.create_foreign_key(
        'fk_content_pipelines_linkedin_credential',
        'content_pipelines', 'credentials',
        ['linkedin_credential_id'], ['id']
    )


def downgrade() -> None:
    """Remove LinkedIn Video fields."""
    op.drop_constraint('fk_content_pipelines_linkedin_credential', 'content_pipelines', type_='foreignkey')
    op.drop_column('content_pipelines', 'linkedin_credential_id')
    op.drop_column('content_pipelines', 'linkedin_posted_url')
    op.drop_column('content_pipelines', 'linkedin_post_text')
    op.drop_column('content_pipelines', 'video_final_path')
    op.drop_column('content_pipelines', 'video_draft_path')
    op.drop_column('content_pipelines', 'design_md_path')
    op.drop_column('content_pipelines', 'script_md')
