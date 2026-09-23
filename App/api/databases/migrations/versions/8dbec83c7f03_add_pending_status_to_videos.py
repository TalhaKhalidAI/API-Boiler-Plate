"""add pending status to videos

Revision ID: 8dbec83c7f03
Revises: f0f0913c346a
Create Date: 2026-09-22 18:31:27.712166

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8dbec83c7f03'
down_revision: Union[str, Sequence[str], None] = 'f0f0913c346a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_videos_status', 'videos', type_='check')
    op.create_check_constraint(
        'ck_videos_status',
        'videos',
        "status IN ('pending','processing','ready','failed','deleted')",
    )

def downgrade() -> None:
    op.drop_constraint('ck_videos_status', 'videos', type_='check')
    op.create_check_constraint(
        'ck_videos_status',
        'videos',
        "status IN ('processing','ready','failed','deleted')",
    )