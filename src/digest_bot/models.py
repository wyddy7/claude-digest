"""
SQLAlchemy table definitions. Used by Alembic for autogenerate and by db.py for queries.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    TIMESTAMP,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    model = Column(Text, nullable=False, default="anthropic/claude-sonnet-5")
    last_digest = Column(Text, nullable=False, default="")
    last_digest_time = Column(Text, nullable=False, default="")
    interaction_history = Column(JSONB, nullable=False, default=list)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LinkCache(Base):
    """Dedup cache for the reader layer: a URL fetched within the dedup window
    is skipped on subsequent daily runs. The structural advantage over an
    in-memory cache is that the digest is daily, so a persistent date is enough."""

    __tablename__ = "link_cache"

    url_hash = Column(Text, primary_key=True)          # sha256 hex of the URL
    url = Column(Text, nullable=False)                 # original URL (provenance/debug)
    last_fetched_date = Column(Text, nullable=False)   # "YYYY-MM-DD" (MSK)
    tenant_id = Column(Text, nullable=False, default="", server_default="")  # reserved — SaaS seam


# ── multi-tenant identity + config core (dev-time only, mirrors SPEC-schema.sql) ──


class TierDefault(Base):
    """Named default bundle. Seeds user_settings.limits at provisioning. A tier is
    just a row of default values; effective per-user limit resolves override →
    tier default → code-side fallback (see db.get_effective_limit)."""

    __tablename__ = "tier_defaults"

    tier = Column(Text, primary_key=True)
    limits = Column(JSONB, nullable=False, server_default=text("'{}'"))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class User(Base):
    """Identity + subscription + onboarding state. Subscription activeness is
    ALWAYS computed from pro_until/trial_ends_at at runtime — never stored as a
    boolean. is_active is a separate operational scheduler-fan-out gate."""

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    tg_user_id = Column(BigInteger, nullable=False, unique=True)   # numeric tg id; NEVER @username
    tier = Column(Text, ForeignKey("tier_defaults.tier"), nullable=False, server_default=text("'trial'"))
    pro_until = Column(TIMESTAMP(timezone=True), nullable=True)
    trial_ends_at = Column(TIMESTAMP(timezone=True), nullable=True)
    trial_used = Column(Boolean, nullable=False, server_default=text("false"))
    onboarding_state = Column(Text, nullable=False, server_default=text("'new'"))
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    trial_warn_sent = Column(JSONB, nullable=False, server_default=text("'{}'"))  # expiry-warn debounce state
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class UserSettings(Base):
    """One row per user (1:1 with users). Hot config object load_settings() hands
    the pipeline. Replaces the legacy single-row user_state at cutover."""

    __tablename__ = "user_settings"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    channels = Column(JSONB, nullable=False, server_default=text("'[]'"))
    current_focus = Column(Text, nullable=False, server_default=text("''"))
    focus_auto_reset = Column(Boolean, nullable=False, server_default=text("false"))
    model = Column(Text, nullable=False, server_default=text("'anthropic/claude-sonnet-5'"))
    limits = Column(JSONB, nullable=False, server_default=text("'{}'"))            # per-user overrides
    personalization = Column(JSONB, nullable=False, server_default=text("'{}'"))   # load_personalization(tenant_id) home
    timezone = Column(Text, nullable=False, server_default=text("'Europe/Moscow'"))
    delivery_hour = Column(SmallInteger, nullable=False, server_default=text("13"))
    delivery_minute = Column(SmallInteger, nullable=False, server_default=text("0"))
    last_digest = Column(Text, nullable=False, server_default=text("''"))
    last_digest_time = Column(Text, nullable=False, server_default=text("''"))
    interaction_history = Column(JSONB, nullable=False, server_default=text("'[]'"))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class ScrapeCache(Base):
    """Shared, cross-user channel scrape + ad-filter verdicts. When N users track
    the same channel it is scraped/ad-filtered once per fetch window, not N times.
    TTL enforced app-side at read time (see db.scrape_cache_get)."""

    __tablename__ = "scrape_cache"

    channel = Column(Text, primary_key=True)
    post_hash = Column(Text, primary_key=True)
    content = Column(JSONB, nullable=False, server_default=text("'{}'"))
    ad_verdict = Column(Boolean, nullable=True)
    fetched_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class SubscriptionEvent(Base):
    """Append-only payment / subscription audit log. telegram_payment_charge_id is
    UNIQUE — the idempotency anchor that makes apply_successful_payment run exactly
    once per real charge (duplicate Telegram delivery hits the unique constraint)."""

    __tablename__ = "subscription_events"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'"))
    stars_amount = Column(Integer, nullable=True)
    telegram_payment_charge_id = Column(Text, nullable=True, unique=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
