"""
Agent layer for digest_bot.

Two agents:
- digest_agent: stateless, orchestrates scraping → filtering → generation
- chat_agent:   stateful (Supabase checkpointer), conversational with history tools

Entry points:
- run_digest_agent(bot, chat_id, status_msg) — called by scheduler and trigger_digest tool
- run_chat_turn(user_id, message, user_data) — called by handle_text in bot.py
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytz
from langchain_core.tools import tool
from deepagents import create_deep_agent

from ai import build_system_prompt, filter_ads, generate_digest, filter_images
from personalization import load_personalization
from scraper import scrape_channel

logger = logging.getLogger(__name__)

MOSCOW = pytz.timezone("Europe/Moscow")
_DATA_DIR = Path(__file__).parent / "data"
_DATA_FILE = _DATA_DIR / "data.json"
_HISTORY_FILE = _DATA_DIR / "digests_history.json"

OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")


# ─── Shared state helpers ─────────────────────────────────────────────────────

def _load_data() -> dict:
    if _DATA_FILE.exists():
        try:
            return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _load_history() -> list:
    if not _HISTORY_FILE.exists():
        return []
    try:
        return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(history: list) -> None:
    content = json.dumps(history, ensure_ascii=False, indent=2)
    tmp = _HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(_HISTORY_FILE)


# ─── digest_agent tools ───────────────────────────────────────────────────────

@tool
def get_configured_channels() -> list[str]:
    """Return the list of Telegram channels configured by the user."""
    data = _load_data()
    return data.get("channels", [])


@tool
async def scrape_all_channels(channels: list[str]) -> list[dict]:
    """Scrape all given Telegram channels in parallel. Returns flat list of posts."""
    results = await asyncio.gather(
        *[scrape_channel(ch) for ch in channels],
        return_exceptions=True,
    )
    posts = []
    for ch, result in zip(channels, results):
        if isinstance(result, Exception):
            logger.warning(f"scrape_all_channels: {ch} failed: {result}")
        else:
            posts.extend(result)
    logger.info(f"scrape_all_channels: {len(posts)} posts from {len(channels)} channels")
    return posts


@tool
async def filter_ads_tool(posts: list[dict]) -> list[dict]:
    """Filter out pure ad posts. Returns only posts with real signal."""
    return await filter_ads(posts, OPENROUTER_KEY)


@tool
async def generate_digest_tool(posts: list[dict]) -> dict:
    """Generate the digest HTML from filtered posts. Returns digest_html, personal_html, stats_html."""
    data = _load_data()
    user_data = data.copy()
    user_data["openrouter_key"] = OPENROUTER_KEY
    recent = _load_history()[-3:]
    digest_html, personal_html, stats_html = await generate_digest(
        posts, user_data, recent_digests=recent
    )
    return {
        "digest_html": digest_html,
        "personal_html": personal_html or "",
        "stats_html": stats_html or "",
        "posts_count": len(posts),
    }


@tool
def load_recent_digests_tool(n: int = 3) -> list[dict]:
    """Load the N most recent digests for context deduplication."""
    history = _load_history()
    return history[-n:] if history else []


@tool
def save_digest_tool(digest_html: str, posts_count: int) -> str:
    """Persist a completed digest to history. Returns 'ok'."""
    history = _load_history()
    is_error = digest_html.startswith("Ошибка") or digest_html.startswith("Не нашёл")
    history.append({
        "id": len(history) + 1,
        "date": datetime.now(MOSCOW).strftime("%Y-%m-%d"),
        "datetime": datetime.now(MOSCOW).isoformat(),
        "digest": digest_html,
        "posts_count": posts_count,
        "is_error": is_error,
    })
    _save_history(history)
    return "ok"


# ─── chat_agent tools ─────────────────────────────────────────────────────────

@tool
def search_digest_history(query: str) -> list[dict]:
    """Search past digests by keyword. Returns matching entries (date, snippet)."""
    history = _load_history()
    q = query.lower()
    results = []
    for item in history:
        if item.get("is_error"):
            continue
        if q in item.get("digest", "").lower():
            results.append({
                "id": item.get("id"),
                "date": item["date"],
                "snippet": item["digest"][:300],
            })
    return results[-10:]


@tool
def get_recent_digests(n: int = 3) -> list[dict]:
    """Return the N most recent digest entries with date and content."""
    history = _load_history()
    recent = [h for h in history if not h.get("is_error")][-n:]
    return [{"id": h.get("id"), "date": h["date"], "digest": h["digest"][:600]} for h in recent]


@tool
def get_current_focus() -> str:
    """Return the user's current digest focus (if any)."""
    data = _load_data()
    return data.get("current_focus", "") or "не задан"


# ─── Agent factories ──────────────────────────────────────────────────────────

def _build_digest_system_prompt() -> str:
    cfg = load_personalization()
    profile = cfg.get("profile", {}).get("description", "")
    return (
        "You are a personal digest curator agent. Your job:\n"
        "1. Call get_configured_channels to get the channel list\n"
        "2. Call scrape_all_channels with that list\n"
        "3. Call filter_ads_tool on the scraped posts\n"
        "4. Call generate_digest_tool on the filtered posts\n"
        "5. Call save_digest_tool with the result\n"
        "6. Return a summary: how many channels, posts scraped, posts after filter, digest generated.\n\n"
        "Always follow this exact sequence. Do not skip steps.\n\n"
        f"User profile: {profile}"
    )


def create_digest_agent():
    """Stateless agent for scheduled digest generation."""
    return create_deep_agent(
        model="anthropic:claude-sonnet-4-6",
        system_prompt=_build_digest_system_prompt(),
        tools=[
            get_configured_channels,
            scrape_all_channels,
            filter_ads_tool,
            generate_digest_tool,
            load_recent_digests_tool,
            save_digest_tool,
        ],
    )


def create_chat_agent(checkpointer):
    """Stateful conversational agent with Supabase-backed memory."""
    data = _load_data()
    user_data = data.copy()
    user_data["openrouter_key"] = OPENROUTER_KEY
    system = build_system_prompt(user_data)
    system += (
        "\n\nYou have access to tools to search past digests and get context. "
        "Use search_digest_history when the user asks about past topics. "
        "Use get_recent_digests to reference what was covered recently. "
        "Use get_current_focus to understand what the user is currently focused on."
    )
    return create_deep_agent(
        model="anthropic:claude-sonnet-4-6",
        system_prompt=system,
        tools=[
            search_digest_history,
            get_recent_digests,
            get_current_focus,
        ],
        checkpointer=checkpointer,
    )


# ─── Entry points ─────────────────────────────────────────────────────────────

async def run_digest_agent() -> dict:
    """
    Run the digest agent and return result dict with digest_html, personal_html,
    stats_html, posts_count. Called by scheduler and trigger_digest tool.
    """
    agent = create_digest_agent()
    config = {"configurable": {"thread_id": "digest-run"}}
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Generate today's digest."}]},
        config,
    )
    # Extract the last tool result that contains digest data from save_digest_tool
    # The agent saves to disk; we read back the last entry for sending
    history = _load_history()
    if history:
        last = history[-1]
        return {
            "digest_html": last["digest"],
            "personal_html": "",
            "stats_html": "",
            "posts_count": last["posts_count"],
        }
    return {"digest_html": "Ошибка: дайджест не сохранён", "personal_html": "", "stats_html": "", "posts_count": 0}


async def run_chat_turn(user_id: int, message: str, checkpointer) -> str:
    """
    Run one chat turn. Returns agent's text response.
    checkpointer must be initialized and passed in (lifecycle managed in bot.py).
    """
    agent = create_chat_agent(checkpointer)
    config = {"configurable": {"thread_id": str(user_id)}}
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": message}]},
        config,
    )
    messages = result.get("messages", [])
    if messages:
        return messages[-1].content
    return "Не смог ответить."
