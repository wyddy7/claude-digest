"""
Database layer using supabase-py HTTP client (PostgREST REST API).

Uses HTTPS → goes through HTTPS_PROXY → avoids libpq/psycopg SSL deadlock
with PTB's httpx event loop. All functions are async and compatible with
PTB's run_polling() event loop context.

Why not psycopg?
  psycopg.connect() inside PTB's run_polling() handler hangs on SSL handshake
  when httpx (PTB) already holds an active SSL tunnel through HTTPS_PROXY.
  TCP connect succeeds (0.10s) but psycopg's full SSL/auth sequence deadlocks.
  supabase-py uses httpx (same library as PTB) — works through proxy natively.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from supabase import AsyncClient, create_async_client

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]
DEFAULT_MODEL = "anthropic/claude-3.5-haiku"

_client: Optional[AsyncClient] = None


# ─── connection lifecycle ─────────────────────────────────────────────────────

async def init_pool(dsn: str) -> None:
    """Ignored — supabase-py uses URL+key, not DSN. Call init_supabase() instead."""
    logger.debug("init_pool: no-op (use init_supabase)")


async def init_supabase(url: str, key: str) -> None:
    """Create async supabase client and verify connectivity."""
    global _client
    _client = await create_async_client(url, key)
    # Verify connectivity
    resp = await _client.table("user_state").select("id").eq("id", 1).execute()
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


# ─── user_state (replaces data.json) ─────────────────────────────────────────

async def load() -> dict:
    """Load user state row (id=1). Returns dict with defaults if missing."""
    resp = await _get_client().table("user_state").select("*").eq("id", 1).execute()
    if not resp.data:
        return _defaults()
    return _row_to_state(resp.data[0])


async def save(data: dict) -> None:
    """Upsert user state (id=1)."""
    clean = _sanitize(data)
    payload = {
        "id": 1,
        "channels": clean.get("channels", DEFAULT_CHANNELS),
        "current_focus": clean.get("current_focus", ""),
        "focus_auto_reset": clean.get("focus_auto_reset", False),
        "model": clean.get("model", DEFAULT_MODEL),
        "last_digest": clean.get("last_digest", ""),
        "last_digest_time": clean.get("last_digest_time", ""),
        "interaction_history": clean.get("interaction_history", []),
    }
    await _get_client().table("user_state").upsert(payload).execute()
    logger.debug("db.save done")


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


def _sanitize(data: dict) -> dict:
    clean = dict(data)
    clean.pop("openrouter_key", None)
    clean.pop("description", None)
    clean.setdefault("channels", DEFAULT_CHANNELS[:])
    clean.setdefault("focus_auto_reset", False)
    return clean


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


# ─── interaction_history helper ───────────────────────────────────────────────

async def add_history(entry: str) -> None:
    """Append a short entry to interaction_history (kept last 20)."""
    data = await load()
    history = data.get("interaction_history", [])
    history.append(f"{datetime.now().strftime('%d.%m %H:%M')} — {entry[:120]}")
    data["interaction_history"] = history[-20:]
    await save(data)


# ─── digests (replaces digests_history.json) ──────────────────────────────────

async def load_history(limit: int = 0) -> list[dict]:
    """Load digest history. limit=0 means all, newest first."""
    q = _get_client().table("digests").select("*").order("id", desc=True)
    if limit:
        q = q.limit(limit)
    resp = await q.execute()
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
