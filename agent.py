"""
Agent layer for digest_bot.

Two components:
- run_digest_pipeline: stateless deterministic pipeline (scrape → filter → generate → save)
- chat_agent:          stateful (Supabase checkpointer), conversational with history tools

Entry points:
- run_digest_pipeline(on_status) — called by scheduler and bot
- run_chat_turn(user_id, message, checkpointer) — called by handle_text in bot.py
"""

import asyncio
import logging
import os

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from deepagents import create_deep_agent

import db
from ai import build_system_prompt, filter_ads, generate_digest
from personalization import load_personalization
from scraper import scrape_channel

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _make_model(role: str = "chat") -> ChatOpenAI:
    """Build a ChatOpenAI pointed at OpenRouter. role: 'chat' | 'digest'."""
    cfg = load_personalization()
    model_id = cfg.get("models", {}).get(role, "anthropic/claude-sonnet-4-6")
    key = os.getenv("OPENROUTER_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_KEY env var is not set")
    return ChatOpenAI(
        model=model_id,
        api_key=key,
        base_url=OPENROUTER_BASE,
    )


# ─── chat_agent tools ─────────────────────────────────────────────────────────

@tool
async def search_digest_history(query: str) -> list[dict]:
    """Search past digests by keyword. Returns matching entries (date, snippet)."""
    history = await db.load_history()
    q = query.lower()
    results = []
    for item in history:
        if item.get("is_error"):
            continue
        content = item.get("digest_html", "")
        if q in content.lower():
            results.append({
                "id": item.get("id"),
                "date": item["date"],
                "snippet": content[:300],
            })
    return results[-10:]


@tool
async def get_recent_digests(n: int = 3) -> list[dict]:
    """Return the N most recent digest entries with date and content."""
    history = await db.load_history(limit=n)
    return [
        {"id": h.get("id"), "date": h["date"], "digest": h.get("digest_html", "")[:600]}
        for h in history if not h.get("is_error")
    ]


@tool
async def get_current_focus() -> str:
    """Return the user's current digest focus (if any)."""
    data = await db.load()
    return data.get("current_focus", "") or "не задан"


# ─── Chat agent factory ───────────────────────────────────────────────────────

def create_chat_agent(system_prompt: str, checkpointer):
    """Stateful conversational agent with Supabase-backed memory."""
    system = system_prompt + (
        "\n\nYou have access to tools to search past digests and get context. "
        "Use search_digest_history when the user asks about past topics. "
        "Use get_recent_digests to reference what was covered recently. "
        "Use get_current_focus to understand what the user is currently focused on."
    )
    return create_deep_agent(
        model=_make_model("chat"),
        system_prompt=system,
        tools=[
            search_digest_history,
            get_recent_digests,
            get_current_focus,
        ],
        checkpointer=checkpointer,
    )


# ─── Status labels ────────────────────────────────────────────────────────────

_CHAT_TOOL_LABELS = {
    "search_digest_history": "🔎 Ищу в истории дайджестов...",
    "get_recent_digests":    "📚 Читаю последние дайджесты...",
    "get_current_focus":     "🎯 Проверяю текущий фокус...",
}

_DIGEST_STATUS = {
    "channels": "📡 Загружаю список каналов...",
    "scrape":   "⏳ Скрейплю каналы параллельно...",
    "filter":   "🔍 Фильтрую рекламу...",
    "generate": "🤖 Генерирую дайджест...",
    "save":     "💾 Сохраняю в базу...",
}


# ─── Entry points ─────────────────────────────────────────────────────────────

async def run_digest_pipeline(on_status=None) -> dict:
    """
    Run the digest pipeline directly — no agent overhead.
    Steps: load channels → scrape (parallel) → filter ads → generate → save.
    on_status: async callable(text: str) — called before each step.
    Returns dict: digest_html, personal_html, stats_html, posts_count.
    """
    async def _status(label: str):
        if on_status:
            try:
                await on_status(label)
            except Exception:
                pass

    # Step 1 — load channels
    await _status(_DIGEST_STATUS["channels"])
    data = await db.load()
    channels = data.get("channels", [])
    logger.info(f"[digest] channels ({len(channels)}): {channels}")

    # Step 2 — scrape in parallel
    await _status(_DIGEST_STATUS["scrape"])
    results = await asyncio.gather(
        *[scrape_channel(ch) for ch in channels],
        return_exceptions=True,
    )
    posts = []
    for ch, result in zip(channels, results):
        if isinstance(result, Exception):
            logger.warning(f"[digest] scrape failed: {ch}: {result}")
        else:
            posts.extend(result)
    logger.info(f"[digest] scraped {len(posts)} posts from {len(channels)} channels")

    # Step 3 — filter ads
    await _status(_DIGEST_STATUS["filter"])
    key = os.getenv("OPENROUTER_KEY")
    filtered = await filter_ads(posts, key)
    logger.info(f"[digest] after ad-filter: {len(filtered)} posts")

    # Step 4 — generate digest
    await _status(_DIGEST_STATUS["generate"])
    user_data = data.copy()
    user_data["openrouter_key"] = key
    recent = await db.load_history(limit=3)
    logger.info(f"[digest] generating | posts={len(filtered)} | model={user_data.get('model', '?')}")
    digest_html, personal_html, stats_html = await generate_digest(
        filtered, user_data, recent_digests=recent
    )
    logger.info(f"[digest] generated  | digest_len={len(digest_html)} | personal={'yes' if personal_html else 'no'}")

    # Step 5 — save to history
    await _status(_DIGEST_STATUS["save"])
    await db.append_to_history(digest_html, len(filtered))
    logger.info(f"[digest] done | posts_count={len(filtered)}")

    return {
        "digest_html": digest_html,
        "personal_html": personal_html or "",
        "stats_html": stats_html or "",
        "posts_count": len(filtered),
    }


async def run_chat_turn(user_id: int, message: str, checkpointer) -> str:
    """
    Run one chat turn. checkpointer lifecycle managed in bot.py post_init/post_shutdown.
    """
    data = await db.load()
    user_data = data.copy()
    user_data["openrouter_key"] = os.getenv("OPENROUTER_KEY")
    system_prompt = build_system_prompt(user_data)
    agent = create_chat_agent(system_prompt, checkpointer)
    config = {"configurable": {"thread_id": str(user_id)}}

    logger.info(f"[chat_agent] turn start | user={user_id} | msg={message[:80]!r}")
    final_text = "Не смог ответить."
    model_calls = 0
    try:
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config,
            version="v2",
        ):
            kind = event.get("event")
            if kind == "on_tool_start":
                tool_name = event.get("name", "")
                tool_input = event.get("data", {}).get("input", "")
                logger.info(f"[chat_agent] tool_start: {tool_name} | input={str(tool_input)[:120]!r}")
            elif kind == "on_tool_end":
                tool_name = event.get("name", "")
                tool_output = event.get("data", {}).get("output", "")
                logger.info(f"[chat_agent] tool_end:   {tool_name} | output={str(tool_output)[:120]!r}")
            elif kind == "on_chat_model_start":
                model_calls += 1
                msgs = event.get("data", {}).get("input", {}).get("messages", [])
                logger.info(f"[chat_agent] model_call #{model_calls} | ctx_messages={len(msgs)}")
            elif kind == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output and hasattr(output, "content") and output.content:
                    final_text = output.content
                logger.info(f"[chat_agent] model_done  #{model_calls} | reply={final_text[:100]!r}")
    except Exception as e:
        logger.error(f"[chat_agent] error: {e}", exc_info=True)
        return f"Ошибка агента: {e}"

    logger.info(f"[chat_agent] turn end   | model_calls={model_calls} | reply_len={len(final_text)}")
    return final_text
