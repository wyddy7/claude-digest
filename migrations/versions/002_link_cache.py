"""link_cache: dedup cache for the reader layer

Revision ID: 002
Revises: 001
Create Date: 2026-05-28

NOTE: Alembic is not wired into CI/deploy for this project (migrations are
applied manually against Supabase). This file is the canonical schema for the
link_cache table; apply it with `alembic upgrade head` from a machine that has
alembic + sqlalchemy installed, or run the equivalent CREATE TABLE in the
Supabase SQL editor. See docs/digest-bot-reader-plan.md.
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "link_cache",
        sa.Column("url_hash", sa.Text(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("last_fetched_date", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    # Enable RLS with NO policies: the bot connects with the service_role key
    # (which bypasses RLS), while anon/authenticated keys get deny-by-default.
    # This is a server-only table — no client should ever read it directly.
    op.execute("ALTER TABLE link_cache ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("link_cache")
