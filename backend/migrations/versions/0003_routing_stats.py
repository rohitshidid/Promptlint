"""routing stats on score events

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26 10:00:00
"""

import sqlalchemy as sa
from alembic import op


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

COLUMNS = [
    ('strategy', sa.String(length=16)),
    ('task_type', sa.String(length=32)),
    ('routed_model', sa.String(length=200)),
    ('routed_provider', sa.String(length=40)),
    ('baseline_model', sa.String(length=200)),
    ('est_cost_usd', sa.Float()),
    ('est_baseline_usd', sa.Float()),
    ('clarify_first', sa.Boolean()),
    ('exec_ok', sa.Boolean()),
    ('executed_model', sa.String(length=200)),
    ('executed_provider', sa.String(length=40)),
    ('exec_attempts', sa.Integer()),
    ('exec_input_tokens', sa.Integer()),
    ('exec_output_tokens', sa.Integer()),
    ('exec_cost_usd', sa.Float()),
    ('exec_baseline_usd', sa.Float()),
]


def upgrade() -> None:
    with op.batch_alter_table('score_events') as batch:
        for name, type_ in COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('score_events') as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
