-- usage_events — product telemetry + LLM cost ledger.
-- Hand-apply twin of migrations/versions/006_usage_events.py for the Supabase
-- SQL editor (Alembic is not wired into CI/deploy here). Idempotent: safe to
-- re-run (IF NOT EXISTS everywhere).
--
-- Apply against PROD Supabase once, then the bot's best-effort writes start
-- landing. Code deployed before this runs degrades gracefully (writes are
-- swallowed, the dashboard reports zeros) — so ordering is not load-bearing.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS usage_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    event       TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    cost_usd    NUMERIC(12, 6),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_events_created
    ON usage_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_created
    ON usage_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_event_created
    ON usage_events (event, created_at DESC);

ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
