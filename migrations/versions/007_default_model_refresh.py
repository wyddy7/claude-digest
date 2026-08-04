"""Repoint the user_settings.model column default at a live model.

Migration 003 froze `anthropic/claude-3.5-haiku` as the column server_default.
That model was retired 2026-02-19, so every user_settings row inserted without
an explicit `model` (which is what get_or_create_user did until 2026-08-04)
silently got a 404-ing model id and blew up on their first digest.

Two halves:
  1. Repoint the column default at the current db.DEFAULT_MODEL.
  2. Backfill rows still holding a retired id.

db.get_or_create_user now writes `model` explicitly, so the column default is
belt-and-braces — but a stale default here is exactly how the bug happened, so
it does not get to stay wrong.

No new tables are created, so the "every migration ends with ENABLE ROW LEVEL
SECURITY" rule does not apply here.

Revision ID: 007
Revises: 006
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

NEW_DEFAULT = "anthropic/claude-sonnet-5"
OLD_DEFAULT = "anthropic/claude-3.5-haiku"

# Retired on OpenRouter as of 2026-08-04 — any row still pointing here 404s.
RETIRED = ("anthropic/claude-3.5-haiku", "anthropic/claude-3.7-sonnet")


def upgrade() -> None:
    op.alter_column(
        "user_settings",
        "model",
        existing_type=sa.Text(),
        server_default=sa.text(f"'{NEW_DEFAULT}'"),
    )
    op.execute(
        sa.text("UPDATE user_settings SET model = :new WHERE model = ANY(:retired)")
        .bindparams(new=NEW_DEFAULT, retired=list(RETIRED))
    )


def downgrade() -> None:
    op.alter_column(
        "user_settings",
        "model",
        existing_type=sa.Text(),
        server_default=sa.text(f"'{OLD_DEFAULT}'"),
    )
