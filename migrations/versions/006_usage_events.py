"""usage_events (product telemetry + LLM cost ledger)

Revision ID: 006
Revises: 005
Create Date: 2026-06-13

NOTE: Alembic is not wired into CI/deploy for this project (migrations are
applied manually). Apply with `alembic upgrade head`, or run the equivalent DDL
in the Supabase SQL editor — the hand-apply twin lives in
`migrations/006_usage_events.sql`.

WHY A SEPARATE TABLE (not subscription_events): subscription_events is the
MONEY ledger — idempotent on telegram_payment_charge_id, low-volume, audited.
usage_events is high-volume product telemetry (every digest/chat/quota-hit) and
best-effort (writes swallow failures). Mixing the two would pollute the payment
idempotency anchor and make revenue queries scan telemetry. Revenue is read from
subscription_events; cost + engagement from usage_events; the /stats dashboard
joins them in Python.

cost_usd is DENORMALIZED out of payload so unit-economics aggregation is a cheap
column SUM with its own index, instead of JSONB extraction. payload keeps the
full breakdown (per-stage tokens, by-model cost, read_mode, is_error, source).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID, NUMERIC

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Nullable: an event may outlive a deleted user (SET NULL), and rare
        # system events have no actor. Every normal event sets it.
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        # USD cost of this event (LLM spend). NULL for non-cost events.
        sa.Column("cost_usd", NUMERIC(12, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_usage_events_created", "usage_events", [sa.text("created_at DESC")])
    op.create_index(
        "idx_usage_events_user_created",
        "usage_events",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_usage_events_event_created",
        "usage_events",
        ["event", sa.text("created_at DESC")],
    )
    op.execute("ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("idx_usage_events_event_created", table_name="usage_events")
    op.drop_index("idx_usage_events_user_created", table_name="usage_events")
    op.drop_index("idx_usage_events_created", table_name="usage_events")
    op.drop_table("usage_events")
