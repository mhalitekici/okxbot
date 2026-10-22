"""Preserve reconciliation diagnostics and version safety state; never reset locks."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "runtime_state",
        sa.Column("state_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    for name in ("emergency_at", "last_reconciliation_at", "reconciliation_recovered_at"):
        op.add_column("runtime_state", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column("runtime_state", sa.Column("last_reconciliation_ok", sa.Boolean(), nullable=True))
    for name in ("last_reconciliation_error", "last_reconciliation_summary"):
        op.add_column("runtime_state", sa.Column(name, sa.JSON(), nullable=True))


def downgrade():
    for name in (
        "last_reconciliation_summary",
        "last_reconciliation_error",
        "last_reconciliation_ok",
        "reconciliation_recovered_at",
        "last_reconciliation_at",
        "emergency_at",
        "state_revision",
    ):
        op.drop_column("runtime_state", name)
