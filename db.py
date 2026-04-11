"""
Sync database layer using psycopg3, run in a thread via asyncio.to_thread().

All public functions are async so callers (bot.py, scheduler.py) need no changes.
Internally each function opens a fresh synchronous connection in a thread pool,
executes the query, and closes the connection.

Why not AsyncConnection?
  psycopg.AsyncConnection.connect() blocks the asyncio event loop in the PTB
  handler context (inside run_polling()). asyncio.wait_for() cannot fire because
  the event loop thread itself is blocked by psycopg's internal SSL/TCP setup.
  Moving to a thread frees the event loop completely.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]
DEFAULT_MODEL = "anthropic/claude-3.5-haiku"

# DSN set by init_pool(); used by every _connect() call
_dsn: str = ""


# ─── connection lifecycle ─────────────────────────────────────────────────────

async def init_pool(dsn: str) -> None:
    """Store DSN and verify connectivity. No pool created."""
    global _dsn
    _dsn = dsn
    def _check():
        with psycopg.connect(_dsn, connect_timeout=10) as conn:
            conn.execute("SELECT 1").fetchone()
    await asyncio.to_thread(_check)
    logger.info("DB connection verified (sync-in-thread, no pool)")


async def close_pool() -> None:
    """No-op — no persistent pool to close."""
    logger.info("DB connections closed")


def _connect_sync() -> psycopg.Connection:
    """Open a synchronous psycopg connection. Runs inside a thread."""
    if not _dsn:
        raise RuntimeError("DB not initialised — call init_pool() first")
    return psycopg.connect(
        _dsn,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=5,
        keepalives_count=5,
    )


# ─── user_state (replaces data.json) ─────────────────────────────────────────

async def load() -> dict:
    """Load user state row (id=1). Returns dict with defaults if missing."""
    import threading
    logger.info(f"db.load: before to_thread, loop={asyncio.get_event_loop()}")
    def _do():
        logger.info(f"db.load: _do started in thread={threading.current_thread().name}")
        try:
            with _connect_sync() as conn:
                logger.info("db.load: connected")
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM user_state WHERE id = 1")
                    row = cur.fetchone()
                    logger.info(f"db.load: query done, row={'found' if row else 'empty'}")
                    return row
        except Exception as e:
            logger.error(f"db.load: exception in thread: {e}", exc_info=True)
            raise
    row = await asyncio.to_thread(_do)
    logger.info("db.load: to_thread returned")
    if not row:
        return _defaults()
    return _row_to_state(row)


async def save(data: dict) -> None:
    """Upsert user state (id=1)."""
    clean = _sanitize(data)
    def _do():
        with _connect_sync() as conn:
            conn.execute(
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
    await asyncio.to_thread(_do)


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
    """Load digest history. limit=0 means all, newest first."""
    def _do():
        with _connect_sync() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if limit:
                    cur.execute(
                        "SELECT * FROM digests ORDER BY id DESC LIMIT %s", (limit,)
                    )
                    rows = cur.fetchall()
                    return list(reversed(rows))
                else:
                    cur.execute("SELECT * FROM digests ORDER BY id DESC")
                    return cur.fetchall()
    return await asyncio.to_thread(_do)


async def append_to_history(digest_html: str, posts_count: int) -> None:
    """Insert a new digest entry."""
    import pytz
    moscow = pytz.timezone("Europe/Moscow")
    now_msk = datetime.now(moscow)
    is_error = digest_html.startswith("Ошибка") or digest_html.startswith("Не нашёл")
    def _do():
        with _connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO digests (date, created_at, digest_html, posts_count, is_error)
                VALUES (%s, now(), %s, %s, %s)
                """,
                (now_msk.strftime("%Y-%m-%d"), digest_html, posts_count, is_error),
            )
    await asyncio.to_thread(_do)
