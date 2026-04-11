"""initial tables: digests and user_state

Revision ID: 001
Revises:
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("digest_html", sa.Text(), nullable=False),
        sa.Column("posts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_table(
        "user_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channels", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("current_focus", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("focus_auto_reset", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "model",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'anthropic/claude-3.5-haiku'"),
        ),
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

    # Seed single user_state row (id=1, always upserted)
    op.execute(
        "INSERT INTO user_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("user_state")
    op.drop_table("digests")
