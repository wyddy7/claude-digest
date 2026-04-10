"""
SQLAlchemy table definitions. Used by Alembic for autogenerate and by db.py for queries.
"""

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Text,
    TIMESTAMP,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

metadata = MetaData()


class Base(DeclarativeBase):
    metadata = metadata


class Digest(Base):
    __tablename__ = "digests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Text, nullable=False)           # "YYYY-MM-DD"
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    digest_html = Column(Text, nullable=False)
    posts_count = Column(Integer, nullable=False, default=0)
    is_error = Column(Boolean, nullable=False, default=False)


class UserState(Base):
    __tablename__ = "user_state"

    id = Column(Integer, primary_key=True, default=1)
    channels = Column(JSONB, nullable=False, default=list)
    current_focus = Column(Text, nullable=False, default="")
    focus_auto_reset = Column(Boolean, nullable=False, default=False)
    model = Column(Text, nullable=False, default="anthropic/claude-3.5-haiku")
    last_digest = Column(Text, nullable=False, default="")
    last_digest_time = Column(Text, nullable=False, default="")
    interaction_history = Column(JSONB, nullable=False, default=list)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
