"""connected model on score events

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26 08:00:00
"""

import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('score_events') as batch:
        batch.add_column(sa.Column('connected_model', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('score_events') as batch:
        batch.drop_column('connected_model')
