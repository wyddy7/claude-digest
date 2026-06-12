"""subscription_events (payment ledger) + scrape_cache (cross-user cost lever)

Revision ID: 004
Revises: 003
Create Date: 2026-06-10

NOTE: Alembic is not wired into CI/deploy for this project (migrations are
applied manually against a dev/test Supabase project — NOT prod in this run).
This file is the canonical Alembic twin of SPEC-schema.sql for the payments
ledger and the shared scrape cache. Apply with `alembic upgrade head`, or run
the equivalent DDL in the Supabase SQL editor.

subscription_events.telegram_payment_charge_id is UNIQUE — it is the
idempotency anchor: a duplicate successful_payment delivery inserts the same
charge id, the unique constraint rejects it, and the handler treats the
conflict as "already processed".
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── subscription_events (append-only payment audit log) ──────────────────
    op.create_table(
        "subscription_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("stars_amount", sa.Integer(), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.Text(), nullable=True, unique=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_subscription_events_user_id",
        "subscription_events",
        ["user_id", sa.text("created_at DESC")],
    )
    op.execute("ALTER TABLE subscription_events ENABLE ROW LEVEL SECURITY")

    # ── scrape_cache (shared, cross-user scrape + ad-filter verdicts) ────────
    op.create_table(
        "scrape_cache",
        sa.Column("channel", sa.Text(), nullable=False, primary_key=True),
        sa.Column("post_hash", sa.Text(), nullable=False, primary_key=True),
        sa.Column("content", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ad_verdict", sa.Boolean(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_scrape_cache_channel_fetched",
        "scrape_cache",
        ["channel", sa.text("fetched_at DESC")],
    )
    op.execute("ALTER TABLE scrape_cache ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("idx_scrape_cache_channel_fetched", table_name="scrape_cache")
    op.drop_table("scrape_cache")
    op.drop_index("idx_subscription_events_user_id", table_name="subscription_events")
    op.drop_table("subscription_events")
