"""Marketing: set the crossed-out anchor prices to +30% of the live price.

Revision ID: 005
Revises: 004
Create Date: 2026-06-12

Pure marketing config — the actual charged prices (price_month_stars=900,
price_quarter_stars=2400) are unchanged. Only the strike-through "anchor"
shown next to them moves to price * 1.3 so the paywall reads as an active
discount (900 vs ~~1170~~, 2400 vs ~~3120~~). Values live in the pro tier's
limits JSONB in tier_defaults, so this is a JSONB update, not a column change.

Idempotent: re-running just re-sets the same two keys.
"""
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE tier_defaults
        SET limits = limits
            || jsonb_build_object('price_anchor_month_stars', 1170)
            || jsonb_build_object('price_anchor_quarter_stars', 3120)
        WHERE tier = 'pro'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE tier_defaults
        SET limits = limits
            || jsonb_build_object('price_anchor_month_stars', 1200)
            || jsonb_build_object('price_anchor_quarter_stars', 3600)
        WHERE tier = 'pro'
        """
    )
