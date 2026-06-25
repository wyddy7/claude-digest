"""
Database layer using supabase-py HTTP client (PostgREST REST API).

Async httpx client — compatible with PTB's run_polling() event loop context.

Proxy boundary (important):
  Supabase is a SEPARATE service and is reachable DIRECTLY. It must NOT ride
  the Telegram HTTPS_PROXY, which is flaky — one 502 blip on the proxy would
  otherwise kill any DB call (e.g. the scheduler's list_active_users → whole
  digest fan-out crashes; happened 2026-06-25). supabase-py uses httpx with
  trust_env=True, so it would inherit HTTPS_PROXY implicitly; we exempt it via
  `NO_PROXY=.supabase.co` (set in docker-compose.yml). PTB passes the proxy to
  httpx EXPLICITLY for Telegram, so NO_PROXY does not affect Telegram polling.
  supabase-py's ClientOptions exposes no httpx-client injection in this version,
  so NO_PROXY is the surgical lever rather than a per-client trust_env=False.

Why not psycopg?
  psycopg.connect() inside PTB's run_polling() handler hangs on SSL handshake
  when httpx (PTB) already holds an active SSL tunnel through HTTPS_PROXY.
  TCP connect succeeds (0.10s) but psycopg's full SSL/auth sequence deadlocks.
  supabase-py uses httpx (same library as PTB) — no separate event loop.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from supabase import AsyncClient, create_async_client

try:  # AsyncClientOptions carries the postgrest read-timeout; guard import-shape
    from supabase.lib.client_options import AsyncClientOptions
except Exception:  # pragma: no cover
    AsyncClientOptions = None  # type: ignore[assignment, misc]

try:
    from postgrest.exceptions import APIError
except Exception:  # pragma: no cover - import-shape guard across supabase-py versions
    APIError = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# PostgREST surfaces a Postgres unique-violation as SQLSTATE 23505.
_UNIQUE_VIOLATION = "23505"


class DuplicateCharge(Exception):
    """Raised by insert_subscription_event when a telegram_payment_charge_id
    already exists (UNIQUE violation). The payment idempotency anchor — the
    caller treats this as 'charge already processed', not a DB outage."""

DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]
DEFAULT_MODEL = "anthropic/claude-3.5-haiku"

_client: Optional[AsyncClient] = None

# Cap the postgrest read timeout well below supabase-py's 120s default. The
# homelab → Supabase path intermittently PMTU-blackholes LARGE responses on one
# of Supabase's two backend IPs: the read then stalls for the full timeout while
# the OTHER IP returns in <1s. 120s = a 2-minute zombie hang per bad hit; 15s
# fails fast so _retry can re-issue on a fresh connection (usually the good IP).
_POSTGREST_TIMEOUT_S = 15

# Transient transport faults worth retrying (read timeouts, conn resets, etc.).
_DB_RETRY_ERRORS = (httpx.TimeoutException, httpx.TransportError)


async def _retry(thunk, *, attempts: int = 4, what: str = "db"):
    """Re-issue a Supabase query on transient transport/timeout faults.

    Each attempt MUST rebuild+resend the request (pass a thunk, not a prebuilt
    coroutine) so a fresh connection is drawn from the pool — the whole point,
    since the stall is per-connection (one dead backend IP). Use ONLY for reads
    (idempotent); a write that times out may have committed, so retrying it
    risks a duplicate."""
    last: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return await thunk()
        except _DB_RETRY_ERRORS as e:
            last = e
            logger.warning("[db] %s transient %s (attempt %d/%d) — retrying",
                           what, type(e).__name__, i + 1, attempts)
            await asyncio.sleep(0.5 * i)  # brief backoff; attempt 0 is immediate
    assert last is not None
    raise last


# ─── connection lifecycle ─────────────────────────────────────────────────────

async def init_pool(dsn: str) -> None:
    """Ignored — supabase-py uses URL+key, not DSN. Call init_supabase() instead."""
    logger.debug("init_pool: no-op (use init_supabase)")


async def init_supabase(url: str, key: str) -> None:
    """Create async supabase client and verify connectivity."""
    global _client
    if AsyncClientOptions is not None:
        _client = await create_async_client(
            url, key, AsyncClientOptions(postgrest_client_timeout=_POSTGREST_TIMEOUT_S)
        )
    else:  # pragma: no cover - older supabase-py without AsyncClientOptions
        _client = await create_async_client(url, key)
    # Verify connectivity (retried — startup must survive a single bad-IP hit).
    resp = await _retry(
        lambda: _client.table("user_state").select("id").eq("id", 1).execute(),
        what="init_supabase",
    )
    logger.info(f"DB connection verified (supabase-py HTTP, rows={len(resp.data)})")


async def close_pool() -> None:
    """Close supabase HTTP client. The supabase AsyncClient has no public close
    method across versions — try the known ones, swallow if absent (non-fatal)."""
    global _client
    if _client:
        for closer in ("aclose", "close"):
            fn = getattr(_client, closer, None)
            if callable(fn):
                try:
                    res = fn()
                    if hasattr(res, "__await__"):
                        await res
                except Exception as e:
                    logger.debug(f"close_pool: {closer} failed (non-fatal): {e}")
                break
        _client = None
    logger.info("DB connections closed")


def _get_client() -> AsyncClient:
    if _client is None:
        raise RuntimeError("DB not initialised — call init_supabase() first")
    return _client


# ─── user_state (legacy id=1 global) ─────────────────────────────────────────
# The single-tenant write path (save/add_history) was removed in the cutover —
# the owner is now a normal users+user_settings row. load() / _row_to_state()
# survive only as READ helpers: agent.py's legacy global chat tools and
# ensure_owner_user's one-time seeding read of the legacy row.

async def load() -> dict:
    """Read the legacy user_state row (id=1). Returns dict with defaults if
    missing. READ-ONLY surface — there is no longer a paired save()."""
    resp = await _get_client().table("user_state").select("*").eq("id", 1).execute()
    if not resp.data:
        return _defaults()
    return _row_to_state(resp.data[0])


def _defaults() -> dict:
    return {
        "channels": DEFAULT_CHANNELS[:],
        "current_focus": "",
        "focus_auto_reset": False,
        "model": DEFAULT_MODEL,
        "last_digest": "",
        "last_digest_time": "",
        "interaction_history": [],
    }


def _row_to_state(row: dict) -> dict:
    channels = row["channels"] if isinstance(row["channels"], list) else json.loads(row["channels"])
    history = row["interaction_history"] if isinstance(row["interaction_history"], list) else json.loads(row["interaction_history"])
    return {
        "channels": channels or DEFAULT_CHANNELS[:],
        "current_focus": row.get("current_focus") or "",
        "focus_auto_reset": bool(row.get("focus_auto_reset")),
        "model": row.get("model") or DEFAULT_MODEL,
        "last_digest": row.get("last_digest") or "",
        "last_digest_time": row.get("last_digest_time") or "",
        "interaction_history": history or [],
    }


# ─── digests (replaces digests_history.json) ──────────────────────────────────

async def load_history(limit: int = 0) -> list[dict]:
    """Load digest history. limit=0 means all, newest first."""
    def _q():
        q = _get_client().table("digests").select("*").order("id", desc=True)
        return (q.limit(limit) if limit else q).execute()
    resp = await _retry(_q, what="load_history")  # large digest_html payload
    rows = resp.data or []
    if limit:
        return list(reversed(rows))
    return rows


async def append_to_history(digest_html: str, posts_count: int) -> None:
    """Insert a new digest entry."""
    import pytz
    moscow = pytz.timezone("Europe/Moscow")
    now_msk = datetime.now(moscow)
    is_error = digest_html.startswith("Ошибка") or digest_html.startswith("Не нашёл")
    await _get_client().table("digests").insert({
        "date": now_msk.strftime("%Y-%m-%d"),
        "digest_html": digest_html,
        "posts_count": posts_count,
        "is_error": is_error,
    }).execute()
    logger.debug("db.append_to_history done")


async def append_user_digest(user_id: str, digest_html: str, posts_count: int) -> None:
    """Insert a digest row scoped to a tenant (user_id FK). The user_id column
    was added in migration 003; legacy rows without user_id are unaffected.
    is_error is derived from the digest body prefix to mirror the legacy path."""
    is_error = digest_html.startswith("Ошибка") or digest_html.startswith("Не нашёл")
    await _get_client().table("digests").insert({
        "user_id": user_id,
        "date": _digest_day_msk(),  # single source of the day bucket (see count_user_digests_today)
        "digest_html": digest_html,
        "posts_count": posts_count,
        "is_error": is_error,
    }).execute()
    logger.debug("db.append_user_digest done (user_id=%s)", user_id)


async def load_user_history(user_id: str, limit: int = 0) -> list[dict]:
    """Load digest history for a single tenant (user_id), newest first.
    limit=0 means all rows. Filtered by user_id so tenants never see each
    other's digests. Uses the idx_digests_user_id_id index from migration 003."""
    def _q():
        q = (
            _get_client().table("digests")
            .select("*")
            .eq("user_id", user_id)
            .order("id", desc=True)
        )
        return (q.limit(limit) if limit else q).execute()
    resp = await _retry(_q, what="load_user_history")  # large digest_html payload
    return resp.data or []


# ─── link_cache (reader dedup) ────────────────────────────────────────────────

def _url_hash(url: str) -> str:
    """Stable sha256 hex of a URL — the link_cache primary key."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def get_seen_urls(url_hashes: list[str], window_days: int) -> set[str]:
    """Return the subset of url_hashes fetched within the last window_days.

    Single batched PostgREST query (no psycopg). Empty input → empty set.
    """
    if not url_hashes:
        return set()
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    resp = await (
        _get_client()
        .table("link_cache")
        .select("url_hash")
        .in_("url_hash", url_hashes)
        .gte("last_fetched_date", cutoff)
        .execute()
    )
    return {row["url_hash"] for row in (resp.data or [])}


async def mark_urls_fetched(urls: list[str]) -> None:
    """Upsert (url_hash → today) for each successfully fetched URL."""
    if not urls:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = [
        {"url_hash": _url_hash(u), "url": u, "last_fetched_date": today}
        for u in dict.fromkeys(urls)  # dedupe preserving order
    ]
    await _get_client().table("link_cache").upsert(rows).execute()
    logger.debug(f"db.mark_urls_fetched: {len(rows)} urls")


# ─── multi-tenant: identity ───────────────────────────────────────────────────
#
# All single-tenant helpers above (load/save/load_history/...) stay unchanged for
# the legacy path. The functions below are the multi-tenant surface; they key on
# the numeric tg_user_id Telegram gives us and resolve to the internal UUID id.
# supabase-py (PostgREST) only — no psycopg/asyncpg.

def _is_unique_violation(err: Exception) -> bool:
    """True if a PostgREST error is a Postgres unique-violation (SQLSTATE 23505).
    Inspect the structured code first; fall back to a message probe."""
    code = getattr(err, "code", None)
    if code == _UNIQUE_VIOLATION:
        return True
    msg = str(getattr(err, "message", "") or err)
    return _UNIQUE_VIOLATION in msg or "duplicate key" in msg.lower()


async def get_user_by_tg_id(tg_user_id: int) -> Optional[dict]:
    """SELECT the user row by numeric tg_user_id. None if absent."""
    resp = await (
        _get_client().table("users").select("*").eq("tg_user_id", tg_user_id).execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


async def get_or_create_user(tg_user_id: int) -> dict:
    """Return the user row for tg_user_id, creating it (tier='trial',
    onboarding_state='new') plus the paired user_settings row seeded from
    tier_defaults['trial'] if absent. Idempotent on the tg_user_id UNIQUE
    constraint."""
    existing = await get_user_by_tg_id(tg_user_id)
    if existing:
        return existing

    client = _get_client()
    try:
        resp = await client.table("users").insert({
            "tg_user_id": tg_user_id,
            "tier": "trial",
            "onboarding_state": "new",
        }).execute()
        user = resp.data[0]
    except Exception as e:
        # Lost an insert race on tg_user_id UNIQUE — re-read the winner's row.
        if APIError is not None and isinstance(e, APIError) and _is_unique_violation(e):
            row = await get_user_by_tg_id(tg_user_id)
            if row:
                return row
        raise

    # Seed the paired user_settings row from the trial tier defaults (a COPY,
    # then individually overridable per user).
    trial_limits = await get_tier_limits("trial")
    try:
        await client.table("user_settings").insert({
            "user_id": user["id"],
            "limits": trial_limits,
        }).execute()
    except Exception as e:
        if not (APIError is not None and isinstance(e, APIError) and _is_unique_violation(e)):
            raise  # settings row already exists is fine; anything else propagates
    return user


async def load_settings(user_id: str) -> dict:
    """SELECT user_settings by UUID user_id. Raises if missing
    (get_or_create_user guarantees the paired row)."""
    resp = await (
        _get_client().table("user_settings").select("*").eq("user_id", user_id).execute()
    )
    rows = resp.data or []
    if not rows:
        raise RuntimeError(f"user_settings missing for user_id={user_id}")
    return rows[0]


async def save_settings(user_id: str, fields: dict) -> dict:
    """UPDATE user_settings for this user_id with the given fields. Returns the
    updated row."""
    payload = dict(fields)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    resp = await (
        _get_client().table("user_settings").update(payload).eq("user_id", user_id).execute()
    )
    rows = resp.data or []
    return rows[0] if rows else {}


# ─── multi-tenant: monthly chat-turn usage counter ────────────────────────────
#
# chat_turns_per_month is enforced at the call site via get_effective_limit. The
# running count is persisted per-user inside user_settings.personalization under a
# reserved "_usage" namespace, keyed by calendar month (UTC). This avoids a new
# table/migration; build_pipeline_config ignores unknown personalization keys, so
# the counter never leaks into the digest pipeline.

_USAGE_KEY = "_usage"
_CHAT_TURNS_KEY = "chat_turns"


def _current_usage_month() -> str:
    """Calendar month bucket (UTC), e.g. '2026-06'. The reset boundary."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def count_chat_turns_this_month(user_id: str) -> int:
    """Return how many chat turns this user has spent in the current UTC month.
    Reads the reserved personalization._usage.chat_turns[<month>] counter; absent
    months count as 0 (the month rolled over → fresh quota)."""
    settings = await load_settings(user_id)
    usage = (settings.get("personalization") or {}).get(_USAGE_KEY) or {}
    by_month = usage.get(_CHAT_TURNS_KEY) or {}
    return int(by_month.get(_current_usage_month(), 0) or 0)


async def record_chat_turn(user_id: str) -> int:
    """Increment this user's chat-turn counter for the current month and persist
    it. Returns the new count. Old months are pruned so the blob can't grow
    unbounded."""
    settings = await load_settings(user_id)
    personalization = dict(settings.get("personalization") or {})
    usage = dict(personalization.get(_USAGE_KEY) or {})
    by_month = dict(usage.get(_CHAT_TURNS_KEY) or {})

    month = _current_usage_month()
    new_count = int(by_month.get(month, 0) or 0) + 1
    # Keep only the current month — past months are dead weight.
    usage[_CHAT_TURNS_KEY] = {month: new_count}
    personalization[_USAGE_KEY] = usage
    await save_settings(user_id, {"personalization": personalization})
    return new_count


# ─── product telemetry + LLM cost: append-only usage_events table ─────────────
#
# Replaces the old JSONB per-month counters. Append-only rows WITH timestamps, so
# funnels / retention / DAU come for free; analytics no longer does read-merge-
# write on the same blob as the load-bearing chat_turns quota counter (which still
# lives in user_settings, untouched); and LLM cost is persisted (cost_usd) so unit
# economics are computable. See migrations/006_usage_events.sql.
#
# Every write is BEST-EFFORT: a telemetry failure — including the table not
# existing yet (safe deploy-before-migrate) — is swallowed. Instrumentation must
# never break a user action.
#
# Event vocabulary (event column):
#   digest_generated  — one pipeline run; carries cost_usd + per-model breakdown
#   digest_manual     — 📰 button press (counter; cost lives on digest_generated)
#   chat              — one chat turn (DAU/engagement; quota count stays in JSONB)
#   onboarding_done   — funnel activation step
#   payment           — Stars grant applied (revenue authority = subscription_events)
#   quota_hit         — a user hit chat_turns or digests_per_day cap (upsell signal)


async def log_event(
    user_id: Optional[str], event: str, payload: Optional[dict] = None,
    cost_usd: Optional[float] = None,
) -> None:
    """Append one telemetry row. Best-effort: swallows every failure (incl. a
    missing table) so a user action never breaks on analytics."""
    try:
        row: dict = {"event": event, "payload": payload or {}}
        if user_id:
            row["user_id"] = user_id
        if cost_usd is not None:
            row["cost_usd"] = round(float(cost_usd), 6)
        await _get_client().table("usage_events").insert(row).execute()
    except Exception as e:
        logger.debug("log_event(%s) failed (non-fatal): %s", event, e)


async def bump_usage(user_id: str, event: str) -> None:
    """Back-compat shim — a counter-style event with no payload/cost. Existing
    call sites (digest_manual, onboarding_done, payment) keep working unchanged;
    new rich events use log_event / record_digest_cost directly."""
    await log_event(user_id, event)


async def record_digest_cost(
    user_id: Optional[str], cost_summary: Optional[dict], *,
    posts_count: int, is_error: bool, source: str,
) -> None:
    """Persist one digest generation's LLM cost as a 'digest_generated' event.
    Prices the pipeline cost_summary via pricing.price_cost_summary; cost_usd is
    the denormalized total (cheap column SUM for the dashboard), payload keeps the
    full per-stage / per-model breakdown + read_mode + is_error + source."""
    try:
        import digest_bot.pricing as pricing
        priced = pricing.price_cost_summary(cost_summary or {})
        total = priced.get("total_cost_usd", 0.0)
        payload = {
            "source": source,
            "posts_count": int(posts_count or 0),
            "is_error": bool(is_error),
            "read_mode": priced.get("read_mode", ""),
            "total_cost_usd": total,
            "by_model": priced.get("by_model", {}),
            "by_stage": priced.get("by_stage", {}),
        }
        await log_event(user_id, "digest_generated", payload, cost_usd=total)
    except Exception as e:
        logger.debug("record_digest_cost failed (non-fatal): %s", e)


# ─── /stats dashboard aggregation (Python-side; scales to ~thousands of rows) ──
#
# Pulls a time window of usage_events + subscription_events + the users snapshot
# and computes the 4-tier dashboard in Python. At current scale this is a few
# hundred rows — a full fetch + Python sum is simpler and more robust than
# PostgREST aggregate RPCs. If usage_events ever grows past ~10k/window, move the
# SUMs server-side (a Postgres view or RPC) — the call sites won't change.

def _parse_ts(value) -> Optional[datetime]:
    """Parse a PostgREST ISO timestamp to an aware datetime (UTC). None on junk."""
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _fetch_window(table: str, cols: str, cutoff_iso: str) -> list[dict]:
    try:
        resp = (
            await _get_client().table(table).select(cols)
            .gte("created_at", cutoff_iso).execute()
        )
        return resp.data or []
    except Exception as e:
        logger.debug("_fetch_window(%s) failed (non-fatal): %s", table, e)
        return []


async def _fetch_all_users() -> list[dict]:
    try:
        resp = await _get_client().table("users").select(
            "id,tg_user_id,tier,created_at,pro_until,trial_ends_at,trial_used,onboarding_state"
        ).execute()
        return resp.data or []
    except Exception as e:
        logger.debug("_fetch_all_users failed (non-fatal): %s", e)
        return []


def build_dashboard(events: list[dict], subs: list[dict], users: list[dict],
                    *, now: datetime, days: int) -> dict:
    """Pure aggregation (no IO) over already-fetched rows → 4-tier dashboard.
    Split out from aggregate_stats so it is unit-testable offline."""
    import digest_bot.pricing as pricing
    econ_cutoff = now - timedelta(days=days)

    def in_econ(row) -> bool:
        ts = _parse_ts(row.get("created_at"))
        return ts is not None and ts >= econ_cutoff

    # ── Tier 1: unit economics ────────────────────────────────────────────────
    digest_evs = [e for e in events if e.get("event") == "digest_generated" and in_econ(e)]
    n_digests = len(digest_evs)
    total_cost = sum(float(e.get("cost_usd") or 0.0) for e in digest_evs)
    cost_by_user: dict = {}
    cost_by_model: dict = {}
    for e in digest_evs:
        uid = e.get("user_id")
        cost_by_user[uid] = cost_by_user.get(uid, 0.0) + float(e.get("cost_usd") or 0.0)
        for m, c in ((e.get("payload") or {}).get("by_model") or {}).items():
            cost_by_model[m] = cost_by_model.get(m, 0.0) + float(c or 0.0)
    paying_user_ids = {s.get("user_id") for s in subs
                       if in_econ(s) and s.get("stars_amount") and s.get("user_id")}
    revenue_stars = sum(int(s.get("stars_amount") or 0) for s in subs
                        if in_econ(s) and s.get("stars_amount"))
    revenue_usd = revenue_stars * pricing.STAR_USD
    n_users_with_cost = len([u for u in cost_by_user if u])

    economics = {
        "digests": n_digests,
        "total_cost_usd": round(total_cost, 4),
        "cost_per_digest_usd": round(total_cost / n_digests, 5) if n_digests else 0.0,
        "cost_per_user_usd": round(total_cost / n_users_with_cost, 4) if n_users_with_cost else 0.0,
        "revenue_stars": revenue_stars,
        "revenue_usd": round(revenue_usd, 2),
        "gross_margin_usd": round(revenue_usd - total_cost, 2),
        "paying_users": len(paying_user_ids),
        "cost_by_model": {m: round(c, 4) for m, c in
                          sorted(cost_by_model.items(), key=lambda kv: -kv[1])},
    }

    # ── Tier 2: funnel / activation ───────────────────────────────────────────
    def active_since(d: int) -> int:
        cut = now - timedelta(days=d)
        return len({e.get("user_id") for e in events if e.get("user_id")
                    and (_parse_ts(e.get("created_at")) or now) >= cut})

    signups = len([u for u in users if (_parse_ts(u.get("created_at")) or now) >= econ_cutoff])
    onboarded = len({e.get("user_id") for e in events
                     if e.get("event") == "onboarding_done" and in_econ(e) and e.get("user_id")})
    first_digest_users = len({e.get("user_id") for e in digest_evs if e.get("user_id")})
    tier_counts: dict = {}
    active_pro = 0
    for u in users:
        tier_counts[u.get("tier") or "?"] = tier_counts.get(u.get("tier") or "?", 0) + 1
        pu = _parse_ts(u.get("pro_until"))
        if pu and pu > now:
            active_pro += 1

    activation = {
        "users_total": len(users),
        "signups_in_window": signups,
        "onboarded_in_window": onboarded,
        "first_digest_users": first_digest_users,
        "dau": active_since(1),
        "wau": active_since(7),
        "mau": active_since(30),
        "tier_counts": tier_counts,
        "active_pro": active_pro,
        "signup_to_paid_pct": round(100.0 * len(paying_user_ids) / signups, 1) if signups else 0.0,
    }

    # ── Tier 3: engagement / quality ──────────────────────────────────────────
    errors = sum(1 for e in digest_evs if (e.get("payload") or {}).get("is_error"))
    quota_hits = [e for e in events if e.get("event") == "quota_hit" and in_econ(e)]
    chat_evs = [e for e in events if e.get("event") == "chat" and in_econ(e)]
    engagement = {
        "digest_error_rate_pct": round(100.0 * errors / n_digests, 1) if n_digests else 0.0,
        "digest_errors": errors,
        "quota_hits": len(quota_hits),
        "quota_hit_users": len({e.get("user_id") for e in quota_hits if e.get("user_id")}),
        "chat_turns": len(chat_evs),
        "chat_users": len({e.get("user_id") for e in chat_evs if e.get("user_id")}),
    }

    # ── Tier 4: product (read_mode cost delta) ────────────────────────────────
    rm_cost: dict = {}
    rm_count: dict = {}
    for e in digest_evs:
        rm = (e.get("payload") or {}).get("read_mode") or "off"
        rm_cost[rm] = rm_cost.get(rm, 0.0) + float(e.get("cost_usd") or 0.0)
        rm_count[rm] = rm_count.get(rm, 0) + 1
    read_mode_cost = {
        rm: {"digests": rm_count[rm], "avg_cost_usd": round(rm_cost[rm] / rm_count[rm], 5)}
        for rm in rm_cost
    }
    product = {"read_mode_cost": read_mode_cost}

    return {
        "window_days": days,
        "economics": economics,
        "activation": activation,
        "engagement": engagement,
        "product": product,
    }


async def aggregate_stats(days: int = 30) -> dict:
    """Fetch a window of usage_events + subscription_events + users and build the
    4-tier admin dashboard. Activity (DAU/WAU/MAU) always covers the fixed 1/7/30
    day windows; everything else uses `days`. Fetches max(days, 30) so the activity
    windows are always covered. Best-effort: missing tables → empty rows → zeros."""
    now = datetime.now(timezone.utc)
    fetch_days = max(int(days or 30), 30)
    cutoff = (now - timedelta(days=fetch_days)).isoformat()
    events = await _fetch_window("usage_events", "user_id,event,payload,cost_usd,created_at", cutoff)
    subs = await _fetch_window("subscription_events", "user_id,event_type,stars_amount,created_at", cutoff)
    users = await _fetch_all_users()
    return build_dashboard(events, subs, users, now=now, days=int(days or 30))


async def update_user_fields(tg_user_id: int, fields: dict) -> bool:
    """UPDATE the users row (by tg_user_id) with the given fields. Returns True if
    a row was updated. Used by subscription mutators (pro_until/trial_*)."""
    payload = dict(fields)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    resp = await (
        _get_client().table("users").update(payload).eq("tg_user_id", tg_user_id).execute()
    )
    return bool(resp.data)


# ─── multi-tenant: tier defaults + quotas ─────────────────────────────────────

async def get_tier_limits(tier: str) -> dict:
    """Return the limits JSONB blob for a tier (empty dict if the tier row is
    absent)."""
    resp = await (
        _get_client().table("tier_defaults").select("limits").eq("tier", tier).execute()
    )
    rows = resp.data or []
    return (rows[0].get("limits") or {}) if rows else {}


async def get_tier_default(tier: str, key: str, default: Any = None) -> Any:
    """Read a single key from a tier's limits blob (e.g. trial 'days',
    pro 'days_month'/'price_month_stars'). DB value, never a Python constant."""
    limits = await get_tier_limits(tier)
    return limits.get(key, default)


async def get_effective_limit(user_id: str, key: str, fallback: Any = None) -> Any:
    """Resolve a limit in order: user_settings.limits[key] (per-user override)
    ELSE tier_defaults[user.tier].limits[key] ELSE the explicit `fallback`.
    The single source every quota gate reads — no per-limit columns, no
    Python constants."""
    settings = await load_settings(user_id)
    overrides = settings.get("limits") or {}
    if key in overrides:
        return overrides[key]

    # Resolve the user's tier, then that tier's default for the key.
    resp = await _get_client().table("users").select("tier").eq("id", user_id).execute()
    rows = resp.data or []
    if rows:
        tier_limits = await get_tier_limits(rows[0]["tier"])
        if key in tier_limits:
            return tier_limits[key]
    return fallback


# ─── multi-tenant: subscription row reads/writes (logic lives in subscriptions.py) ──

async def update_subscription_row(
    tg_user_id: int, pro_until_iso: Optional[str], tier: Optional[str] = None
) -> bool:
    """Set users.pro_until to the given ISO string (or None), optionally flipping
    users.tier to the paid bundle name. Returns True if a row was updated. The
    stacking decision is made in subscriptions.update_subscription."""
    fields: dict = {"pro_until": pro_until_iso}
    if tier:
        fields["tier"] = tier
    return await update_user_fields(tg_user_id, fields)


async def grant_trial_row(tg_user_id: int, trial_ends_at_iso: str) -> bool:
    """Set trial_ends_at + trial_used=True in one update. The one-shot guard
    (skip if trial_used already True) is enforced by subscriptions.grant_trial."""
    return await update_user_fields(tg_user_id, {
        "trial_ends_at": trial_ends_at_iso,
        "trial_used": True,
    })


# ─── multi-tenant: payment ledger (idempotent via charge id) ──────────────────

async def insert_subscription_event(
    user_id: str,
    event_type: str,
    payload: Optional[dict] = None,
    stars_amount: Optional[int] = None,
    telegram_payment_charge_id: Optional[str] = None,
) -> dict:
    """INSERT into subscription_events. Raises DuplicateCharge if the
    telegram_payment_charge_id already exists (UNIQUE 23505); re-raises every
    other error so a real DB outage is never misread as 'already paid'."""
    row = {
        "user_id": user_id,
        "event_type": event_type,
        "payload": payload or {},
        "stars_amount": stars_amount,
        "telegram_payment_charge_id": telegram_payment_charge_id,
    }
    try:
        resp = await _get_client().table("subscription_events").insert(row).execute()
    except Exception as e:
        if APIError is not None and isinstance(e, APIError) and _is_unique_violation(e):
            raise DuplicateCharge(telegram_payment_charge_id or "") from e
        raise
    return (resp.data or [{}])[0]


async def delete_subscription_event(telegram_payment_charge_id: str) -> None:
    """Delete the ledger row for a charge id — used to roll back the idempotency
    gate when the subsequent grant fails, so the charge is retryable."""
    await (
        _get_client().table("subscription_events")
        .delete()
        .eq("telegram_payment_charge_id", telegram_payment_charge_id)
        .execute()
    )


async def record_payment_event(
    user_id: str,
    event_type: str,
    payload: Optional[dict] = None,
    stars_amount: Optional[int] = None,
    telegram_payment_charge_id: Optional[str] = None,
) -> bool:
    """Best-effort audit-log insert that swallows the duplicate-charge case.
    Returns True if newly inserted, False if the charge id already existed
    (UNIQUE conflict) so the caller skips re-applying the grant."""
    try:
        await insert_subscription_event(
            user_id=user_id,
            event_type=event_type,
            payload=payload,
            stars_amount=stars_amount,
            telegram_payment_charge_id=telegram_payment_charge_id,
        )
        return True
    except DuplicateCharge:
        return False


# ─── multi-tenant: scrape cache (cross-user cost lever) ────────────────────────

def _post_hash(stable_identity: str) -> str:
    """sha256 hex of a post's stable identity (tg post id when available, else a
    content hash). Mirrors _url_hash for link_cache."""
    return hashlib.sha256(stable_identity.encode("utf-8")).hexdigest()


async def scrape_cache_get(channel: str, ttl_seconds: int) -> list[dict]:
    """Return cached, non-stale rows for a channel: fetched_at >= now()-ttl.
    Empty list → caller scrapes live. ttl_seconds is an app constant (~6h)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
    resp = await (
        _get_client().table("scrape_cache")
        .select("*")
        .eq("channel", channel)
        .gte("fetched_at", cutoff)
        .order("fetched_at", desc=True)
        .execute()
    )
    return resp.data or []


async def scrape_cache_put(channel: str, posts: list[dict]) -> None:
    """UPSERT each post by (channel, post_hash). Each post dict may carry an
    explicit 'post_hash' (or 'id'/'stable_id' to hash); content + ad_verdict are
    stored; fetched_at is set server-side to now()."""
    if not posts:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for p in posts:
        ph = p.get("post_hash")
        if not ph:
            ident = str(p.get("id") or p.get("stable_id") or json.dumps(p, sort_keys=True))
            ph = _post_hash(ident)
        rows.append({
            "channel": channel,
            "post_hash": ph,
            "content": p.get("content", p),
            "ad_verdict": p.get("ad_verdict"),
            "fetched_at": now_iso,
        })
    await _get_client().table("scrape_cache").upsert(rows).execute()
    logger.debug(f"db.scrape_cache_put: {len(rows)} posts for {channel}")


# ─── multi-tenant: scheduler fan-out + personalization ────────────────────────

async def list_active_users() -> list[dict]:
    """SELECT users WHERE is_active = true. The scheduler iterates these and, per
    user, checks is_subscription_active / tier gates before delivery. is_active is
    the operational on/off switch, NOT the subscription state."""
    # Fan-out entry point — retried so one bad-IP hit can't crash the whole
    # daily digest (the original 2026-06-25 outage was this call dying outright).
    resp = await _retry(
        lambda: _get_client().table("users").select("*").eq("is_active", True).execute(),
        what="list_active_users",
    )
    return resp.data or []


# SPEC-payments §2.4 names this list_users_for_delivery; alias for that contract.
list_users_for_delivery = list_active_users


async def ensure_owner_user() -> None:
    """Idempotent backfill: if the env CHAT_ID owner has no users row, create one
    with tier='pro', a far-future pro_until (~100 years), onboarding_state='done',
    trial_used=True, and a paired user_settings row seeded from the legacy
    user_state (id=1) if present, otherwise from the 'pro' tier defaults.

    Safe to call multiple times — returns immediately if the row already exists
    and never downgrades or re-grants existing rows.

    The owner's numeric Telegram id is read from env (CHAT_ID); it is never
    hardcoded in this module.
    """
    import os

    raw = os.getenv("CHAT_ID", "0")
    try:
        owner_tg_id = int(raw)
    except ValueError:
        logger.warning("ensure_owner_user: CHAT_ID not an integer (%r) — skipping", raw)
        return
    if owner_tg_id == 0:
        logger.warning("ensure_owner_user: CHAT_ID=0 — skipping")
        return

    existing = await get_user_by_tg_id(owner_tg_id)
    if existing:
        logger.debug("ensure_owner_user: owner row already present (id=%s), no-op", existing.get("id"))
        return

    # Far-future pro_until: ~100 years from now.
    far_future = (datetime.now(timezone.utc) + timedelta(days=365 * 100)).isoformat()

    client = _get_client()
    try:
        resp = await client.table("users").insert({
            "tg_user_id": owner_tg_id,
            "tier": "pro",
            "onboarding_state": "done",
            "trial_used": True,
            "is_active": True,
            "pro_until": far_future,
        }).execute()
        user = resp.data[0]
    except Exception as e:
        if APIError is not None and isinstance(e, APIError) and _is_unique_violation(e):
            logger.info("ensure_owner_user: insert race, owner row already created — no-op")
            return
        raise

    user_id = user["id"]

    # Seed user_settings from legacy user_state if it exists; else from pro defaults.
    legacy: Optional[dict] = None
    try:
        resp2 = await client.table("user_state").select("*").eq("id", 1).execute()
        if resp2.data:
            legacy = _row_to_state(resp2.data[0])
    except Exception as e:
        logger.warning("ensure_owner_user: failed to load legacy user_state: %s", e)

    if legacy:
        settings_payload: dict = {
            "user_id": user_id,
            "channels": legacy.get("channels") or DEFAULT_CHANNELS[:],
            "current_focus": legacy.get("current_focus") or "",
            "model": legacy.get("model") or DEFAULT_MODEL,
            "last_digest": legacy.get("last_digest") or "",
            "last_digest_time": legacy.get("last_digest_time") or "",
            "interaction_history": legacy.get("interaction_history") or [],
        }
    else:
        pro_limits = await get_tier_limits("pro")
        settings_payload = {
            "user_id": user_id,
            "limits": pro_limits,
        }

    try:
        await client.table("user_settings").insert(settings_payload).execute()
    except Exception as e:
        if APIError is not None and isinstance(e, APIError) and _is_unique_violation(e):
            logger.info("ensure_owner_user: user_settings already exists — no-op")
        else:
            raise

    # Preserve the owner's digest back-catalog: legacy single-tenant digests were
    # written with user_id=NULL; link them to the owner so 📚 История shows the
    # full history. Nothing is lost in the multi-tenant cutover. Only runs when a
    # legacy user_state was present (i.e. this DB has single-tenant history).
    if legacy:
        try:
            linked = await link_orphan_digests(user_id)
            logger.info("ensure_owner_user: linked %s legacy digest(s) to owner", linked)
        except Exception as e:
            logger.warning("ensure_owner_user: digest history link failed (non-fatal): %s", e)

    logger.info(
        "ensure_owner_user: owner backfilled as pro row (user_id=%s, pro_until=%s, "
        "seeded_from=%s)",
        user_id, far_future[:10], "legacy_user_state" if legacy else "pro_defaults",
    )


async def link_orphan_digests(user_id: str) -> int:
    """Attach all unlinked (legacy single-tenant) digest rows to a user_id and
    return how many were updated. Idempotent — after the first run no digests
    have a NULL user_id. Lets the owner keep their full digest history through
    the multi-tenant cutover."""
    resp = await (
        _get_client().table("digests")
        .update({"user_id": user_id})
        .is_("user_id", "null")
        .execute()
    )
    return len(resp.data or [])


def _digest_day_msk() -> str:
    """The current digest 'day' bucket, in Europe/Moscow. The digests table 'date'
    column is written in MSK (append_user_digest), so the daily-cap count MUST
    bucket the same way — a UTC bucket here under-counted during 21:00–24:00 UTC
    and let an extra digest through right after MSK midnight."""
    import pytz
    return datetime.now(pytz.timezone("Europe/Moscow")).strftime("%Y-%m-%d")


async def count_user_digests_today(user_id: str) -> int:
    """Return how many digest rows exist for this user_id with date = today (MSK,
    matching how append_user_digest stamps the row). Used by the cron fan-out AND
    the manual 📰 cap to enforce digests_per_day without a separate counter
    column — the digests table is the authoritative source."""
    today = _digest_day_msk()
    resp = await (
        _get_client().table("digests")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("date", today)
        .execute()
    )
    # PostgREST returns the count in resp.count when count="exact" is set.
    # Fall back to len(resp.data) if the attribute is absent (version delta).
    count = getattr(resp, "count", None)
    if count is not None:
        return int(count)
    return len(resp.data or [])


async def reset_user_onboarding(tg_user_id: int) -> bool:
    """Reset users.onboarding_state to 'invited' and clear channels + current_focus
    in user_settings so the next /start re-runs the onboarding wizard.

    Does NOT touch trial_used, pro_until, or the invite flow — the user keeps
    their subscription state. Returns True if a users row was found and updated.
    """
    user = await get_user_by_tg_id(tg_user_id)
    if not user:
        return False
    user_id = user["id"]
    # Reset onboarding state so the next /start enters the wizard entry point.
    await update_user_fields(tg_user_id, {"onboarding_state": "invited"})
    # Clear channel list and focus so onboarding re-collects them from scratch.
    await save_settings(user_id, {
        "channels": [],
        "current_focus": "",
    })
    logger.info("reset_user_onboarding: tg_user_id=%s → invited, channels/focus cleared", tg_user_id)
    return True


async def delete_user_rows(tg_user_id: int) -> bool:
    """DELETE both the users row and the paired user_settings row for tg_user_id.
    The user_settings FK cascades on most DB schemas; we delete both explicitly for
    safety. Returns True if a users row existed (and was deleted)."""
    user = await get_user_by_tg_id(tg_user_id)
    if not user:
        return False
    user_id = user["id"]
    # Delete user_settings first to avoid FK violation if cascade is not set.
    try:
        await _get_client().table("user_settings").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logger.warning("delete_user_rows: user_settings delete failed (non-fatal): %s", e)
    await _get_client().table("users").delete().eq("tg_user_id", tg_user_id).execute()
    logger.info("delete_user_rows: deleted users + user_settings for tg_user_id=%s", tg_user_id)
    return True


async def load_personalization_db(tenant_id: str) -> dict:
    """Return the raw personalization JSONB for a tenant (the user's UUID id);
    empty dict if unset. NOTE: this blob is NOT prompt-ready — it may contain
    reserved namespaces (the "_usage" chat-turn counter) and is only a per-user
    OVERRIDE layer. Callers must pass it through
    personalization.resolve_personalization(blob, tg_user_id), which strips
    reserved keys and merges over the neutral default (or the owner's yaml for
    the owner) — never feed it to the prompt directly."""
    resp = await (
        _get_client().table("user_settings")
        .select("personalization")
        .eq("user_id", tenant_id)
        .execute()
    )
    rows = resp.data or []
    return (rows[0].get("personalization") or {}) if rows else {}
