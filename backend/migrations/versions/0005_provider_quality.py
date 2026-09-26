"""quality rating on saved custom endpoints

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26 09:30:00
"""

import sqlalchemy as sa
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('provider_keys') as batch:
        batch.add_column(sa.Column('quality', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('provider_keys') as batch:
        batch.drop_column('quality')
