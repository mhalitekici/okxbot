"""Retain quote, signal levels and fill-based protection audit data without altering locks."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("trade_orders", sa.Column("execution_context", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("trade_orders", "execution_context")
