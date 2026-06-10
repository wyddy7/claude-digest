"""users, settings, tier defaults + digests.user_id (multi-tenant identity core)

Revision ID: 003
Revises: 002
Create Date: 2026-06-10

NOTE: Alembic is not wired into CI/deploy for this project (migrations are
applied manually against a dev/test Supabase project — NOT prod in this run).
This file is the canonical Alembic twin of SPEC-schema.sql for the identity +
config core. Apply with `alembic upgrade head`, or run the equivalent DDL in
the Supabase SQL editor (SPEC-schema.sql is the parallel hand-apply path).

Idempotency: the seed of tier_defaults uses ON CONFLICT (tier) DO NOTHING so a
re-run never clobbers an owner-tuned tier.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


# Tier-default seed rows. Limit numbers are bare DB-seeded defaults (no
# rationale): every gate in the bot reads one of these via get_effective_limit.
# 'trial' carries days=3 (the 3-day Pro trial length, read from DB not code).
# 'power' is documented as a FUTURE tier — seeded but not surfaced/sold at MVP.
_TIER_SEED = {
    "trial": {
        "days": 3,
        "channels_max": 15,
        "digests_per_day": 1,
        "chat_turns_per_month": 50,
        "posts_per_channel_cap": 30,
        "history_days": 0,
        "custom_focus": True,
        "private_channels": False,
        "semantic_search": False,
        "models": [],
    },
    "pro": {
        "price_month_stars": 900,
        "price_quarter_stars": 2400,
        "price_anchor_month_stars": 1200,
        "price_anchor_quarter_stars": 3600,
        "days_month": 30,
        "days_quarter": 90,
        "channels_max": 15,
        "digests_per_day": 1,
        "chat_turns_per_month": 50,
        "posts_per_channel_cap": 30,
        "history_days": 0,
        "custom_focus": True,
        "private_channels": False,
        "semantic_search": False,
        "models": [],
    },
    "power": {
        "future": True,
        "price_month_stars": None,
        "price_quarter_stars": None,
        "channels_max": 9999,
        "digests_per_day": 3,
        "chat_turns_per_month": 200,
        "posts_per_channel_cap": 40,
        "history_days": 0,
        "custom_focus": True,
        "private_channels": True,
        "semantic_search": True,
        "models": [],
    },
}


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── tier_defaults ────────────────────────────────────────────────────────
    op.create_table(
        "tier_defaults",
        sa.Column("tier", sa.Text(), primary_key=True),
        sa.Column("limits", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Idempotent seed: ON CONFLICT DO NOTHING preserves owner-tuned tiers.
    import json

    for tier, limits in _TIER_SEED.items():
        op.execute(
            sa.text(
                "INSERT INTO tier_defaults (tier, limits) VALUES (:tier, CAST(:limits AS jsonb)) "
                "ON CONFLICT (tier) DO NOTHING"
            ).bindparams(tier=tier, limits=json.dumps(limits))
        )
    op.execute("ALTER TABLE tier_defaults ENABLE ROW LEVEL SECURITY")

    # ── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column(
            "tier",
            sa.Text(),
            sa.ForeignKey("tier_defaults.tier"),
            nullable=False,
            server_default=sa.text("'trial'"),
        ),
        sa.Column("pro_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("trial_ends_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("onboarding_state", sa.Text(), nullable=False, server_default=sa.text("'new'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("trial_warn_sent", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_users_pro_until", "users", ["pro_until"])
    op.create_index("idx_users_trial_ends_at", "users", ["trial_ends_at"])
    op.create_index("idx_users_is_active", "users", ["is_active"])
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")

    # ── user_settings ────────────────────────────────────────────────────────
    op.create_table(
        "user_settings",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channels", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("current_focus", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("focus_auto_reset", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "model",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'anthropic/claude-3.5-haiku'"),
        ),
        sa.Column("limits", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("personalization", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'Europe/Moscow'")),
        sa.Column("delivery_hour", sa.SmallInteger(), nullable=False, server_default=sa.text("13")),
        sa.Column("delivery_minute", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_digest", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("last_digest_time", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("interaction_history", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute("ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY")

    # ── digests.user_id (nullable: legacy rows predate users) ────────────────
    op.add_column(
        "digests",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("idx_digests_user_id", "digests", ["user_id"])
    op.create_index("idx_digests_user_id_id", "digests", ["user_id", sa.text("id DESC")])


def downgrade() -> None:
    op.drop_index("idx_digests_user_id_id", table_name="digests")
    op.drop_index("idx_digests_user_id", table_name="digests")
    op.drop_column("digests", "user_id")
    op.drop_table("user_settings")
    op.drop_index("idx_users_is_active", table_name="users")
    op.drop_index("idx_users_trial_ends_at", table_name="users")
    op.drop_index("idx_users_pro_until", table_name="users")
    op.drop_table("users")
    op.drop_table("tier_defaults")
