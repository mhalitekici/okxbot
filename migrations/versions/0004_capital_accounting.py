"""Add capital cycles, durable limits and owned accounting. Preserve all existing rows."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
N = sa.Numeric(38, 18)
T = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "persistent_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("account_fingerprint", sa.String(64)),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "capital_cycles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("active_key", sa.String(8), unique=True),
        sa.Column("allocation_base_equity", N, nullable=False),
        sa.Column("slot_count", sa.Integer(), nullable=False),
        sa.Column("slot_target_margin", N, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("completed_at", T),
    )
    columns = [
        sa.Column("environment", sa.String(8), nullable=False, server_default="demo"),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("capital_cycles.id")),
        sa.Column("slot_number", sa.Integer()),
        sa.Column("target_margin", N),
        sa.Column("actual_margin", N),
        sa.Column("estimated_risk", N),
        sa.Column("position_id", sa.String(64)),
        sa.Column("closed_at", T),
        sa.Column("close_cause", sa.String(32)),
        sa.Column("accounting_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("funding_verified_at", T),
    ]
    for column in columns:
        op.add_column("trade_orders", column)
    op.create_table(
        "capital_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cycle_id", sa.Integer(), sa.ForeignKey("capital_cycles.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("trade_orders.id"), unique=True),
        sa.Column("reserved_margin", N, nullable=False),
        sa.UniqueConstraint("cycle_id", "number"),
    )
    op.create_table(
        "order_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("trade_orders.id"), nullable=False),
        sa.Column("exchange_id", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("cause", sa.String(32)),
    )
    op.create_index("ix_order_links_order_id", "order_links", ["order_id"])
    op.create_table(
        "account_bills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bill_id", sa.String(64), nullable=False, unique=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("trade_orders.id")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("inst_id", sa.String(64)),
        sa.Column("amount", N, nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("timestamp", T, nullable=False),
    )
    op.create_index("ix_account_bills_order_id", "account_bills", ["order_id"])
    op.add_column(
        "executions", sa.Column("order_id", sa.Integer(), sa.ForeignKey("trade_orders.id"))
    )
    op.add_column("executions", sa.Column("role", sa.String(8)))
    op.add_column("executions", sa.Column("side", sa.String(8)))
    op.create_index("ix_executions_order_id", "executions", ["order_id"])
    op.add_column("bot_events", sa.Column("event_key", sa.String(128)))
    op.add_column(
        "bot_events",
        sa.Column("delivery_state", sa.String(16), nullable=False, server_default="pending"),
    )
    op.add_column("bot_events", sa.Column("telegram_message_id", sa.BigInteger()))
    op.create_unique_constraint("uq_bot_events_event_key", "bot_events", ["event_key"])
    op.create_table(
        "daily_reports",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("bot_events.id"), nullable=False, unique=True
        ),
        sa.Column("created_at", T, nullable=False),
    )
    # Old executions are account-wide records. Attribute only exact known entry IDs.
    op.execute(
        sa.text(
            "UPDATE executions SET order_id=t.id, role='entry' FROM trade_orders t "
            "WHERE executions.exchange_order_id=t.exchange_id AND executions.inst_id=t.inst_id"
        )
    )
    # No existing emergency/paused state or user settings are reset.


def downgrade():
    op.drop_table("daily_reports")
    op.drop_constraint("uq_bot_events_event_key", "bot_events", type_="unique")
    op.drop_column("bot_events", "event_key")
    op.drop_column("bot_events", "telegram_message_id")
    op.drop_column("bot_events", "delivery_state")
    op.drop_index("ix_executions_order_id", table_name="executions")
    for name in ("side", "role", "order_id"):
        op.drop_column("executions", name)
    op.drop_table("account_bills")
    op.drop_table("order_links")
    op.drop_table("capital_slots")
    for name in (
        "funding_verified_at",
        "accounting_complete",
        "close_cause",
        "closed_at",
        "position_id",
        "estimated_risk",
        "actual_margin",
        "target_margin",
        "slot_number",
        "cycle_id",
        "environment",
    ):
        op.drop_column("trade_orders", name)
    op.drop_table("capital_cycles")
    op.drop_table("persistent_settings")
