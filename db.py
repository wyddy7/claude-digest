"""
Async database layer using psycopg3.
Replaces file-based data.json and digests_history.json.

Pool is initialized once in bot.py post_init and injected via module-level variable.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]
DEFAULT_MODEL = "anthropic/claude-3.5-haiku"

# Module-level pool — set by init_pool(), used by all functions
_pool: Optional[AsyncConnectionPool] = None


async def init_pool(dsn: str) -> AsyncConnectionPool:
    global _pool
    _pool = AsyncConnectionPool(dsn, min_size=1, max_size=5, open=False)
    await _pool.open()
    logger.info("DB pool opened")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        logger.info("DB pool closed")


def _pool_or_error() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() first")
    return _pool


# ─── user_state (replaces data.json) ─────────────────────────────────────────

async def load() -> dict:
    """Load user state row (id=1). Returns dict with defaults if missing."""
    pool = _pool_or_error()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM user_state WHERE id = 1")
            row = await cur.fetchone()
    if not row:
        return _defaults()
    return _row_to_state(row)


async def save(data: dict) -> None:
    """Upsert user state (id=1)."""
    clean = _sanitize(data)
    pool = _pool_or_error()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO user_state
                (id, channels, current_focus, focus_auto_reset, model,
                 last_digest, last_digest_time, interaction_history, updated_at)
            VALUES (1, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                channels            = EXCLUDED.channels,
                current_focus       = EXCLUDED.current_focus,
                focus_auto_reset    = EXCLUDED.focus_auto_reset,
                model               = EXCLUDED.model,
                last_digest         = EXCLUDED.last_digest,
                last_digest_time    = EXCLUDED.last_digest_time,
                interaction_history = EXCLUDED.interaction_history,
                updated_at          = now()
            """,
            (
                json.dumps(clean.get("channels", DEFAULT_CHANNELS)),
                clean.get("current_focus", ""),
                clean.get("focus_auto_reset", False),
                clean.get("model", DEFAULT_MODEL),
                clean.get("last_digest", ""),
                clean.get("last_digest_time", ""),
                json.dumps(clean.get("interaction_history", [])),
            ),
        )


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
        "current_focus": row["current_focus"] or "",
        "focus_auto_reset": bool(row["focus_auto_reset"]),
        "model": row["model"] or DEFAULT_MODEL,
        "last_digest": row["last_digest"] or "",
        "last_digest_time": row["last_digest_time"] or "",
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
    """Load digest history. limit=0 means all."""
    pool = _pool_or_error()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if limit:
                await cur.execute(
                    "SELECT * FROM digests ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = await cur.fetchall()
                return list(reversed(rows))
            else:
                await cur.execute("SELECT * FROM digests ORDER BY id DESC")
                return await cur.fetchall()


async def append_to_history(digest_html: str, posts_count: int) -> None:
    """Insert a new digest entry."""
    from datetime import timezone
    import pytz
    moscow = pytz.timezone("Europe/Moscow")
    now_msk = datetime.now(moscow)
    is_error = digest_html.startswith("Ошибка") or digest_html.startswith("Не нашёл")
    pool = _pool_or_error()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO digests (date, created_at, digest_html, posts_count, is_error)
            VALUES (%s, now(), %s, %s, %s)
            """,
            (now_msk.strftime("%Y-%m-%d"), digest_html, posts_count, is_error),
        )
