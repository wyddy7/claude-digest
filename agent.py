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
import logging
import os

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from deepagents import create_deep_agent
from deepagents.middleware.permissions import FilesystemPermission
from langgraph.errors import GraphRecursionError

import db
from ai import build_system_prompt, filter_ads, generate_digest
from personalization import load_personalization
from scraper import scrape_channel

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _make_model() -> ChatOpenAI:
    """ChatOpenAI pointed at OpenRouter. Read key lazily so load_dotenv() order doesn't matter."""
    key = os.getenv("OPENROUTER_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_KEY env var is not set")
    return ChatOpenAI(
        model="anthropic/claude-sonnet-4-6",
        api_key=key,
        base_url=OPENROUTER_BASE,
    )


# ─── digest_agent tools ───────────────────────────────────────────────────────

@tool
async def get_configured_channels() -> list[str]:
    """Return the list of Telegram channels configured by the user."""
    data = await db.load()
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
    return await filter_ads(posts, os.getenv("OPENROUTER_KEY"))


@tool
async def generate_digest_tool(posts: list[dict]) -> dict:
    """Generate the digest HTML from filtered posts. Returns digest_html, personal_html, stats_html."""
    data = await db.load()
    user_data = data.copy()
    user_data["openrouter_key"] = os.getenv("OPENROUTER_KEY")
    recent = await db.load_history(limit=3)
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
async def load_recent_digests_tool(n: int = 3) -> list[dict]:
    """Load the N most recent digests for context deduplication."""
    return await db.load_history(limit=n)


@tool
async def save_digest_tool(digest_html: str, posts_count: int) -> str:
    """Persist a completed digest to history. Returns 'ok'."""
    await db.append_to_history(digest_html, posts_count)
    return "ok"


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
        if q in item.get("digest_html", item.get("digest", "")).lower():
            results.append({
                "id": item.get("id"),
                "date": item["date"],
                "snippet": item.get("digest_html", item.get("digest", ""))[:300],
            })
    return results[-10:]


@tool
async def get_recent_digests(n: int = 3) -> list[dict]:
    """Return the N most recent digest entries with date and content."""
    history = await db.load_history(limit=n)
    return [
        {"id": h.get("id"), "date": h["date"], "digest": h.get("digest_html", h.get("digest", ""))[:600]}
        for h in history if not h.get("is_error")
    ]


@tool
async def get_current_focus() -> str:
    """Return the user's current digest focus (if any)."""
    data = await db.load()
    return data.get("current_focus", "") or "не задан"


# ─── Agent factories ──────────────────────────────────────────────────────────

def _build_digest_system_prompt() -> str:
    cfg = load_personalization()
    profile = cfg.get("profile", {}).get("description", "")
    return (
        "You are a personal digest curator agent. Your ONLY job is to run these 5 steps in order:\n"
        "1. Call get_configured_channels\n"
        "2. Call scrape_all_channels with the returned list\n"
        "3. Call filter_ads_tool on the posts\n"
        "4. Call generate_digest_tool on the filtered posts\n"
        "5. Call save_digest_tool with digest_html and posts_count from step 4\n"
        "Then respond with: channels count, posts scraped, posts after filter, done.\n\n"
        "IMPORTANT: Do NOT use ls, read_file, write_file, glob, grep, execute, or any filesystem tools. "
        "You have no filesystem access. Only use the 5 tools listed above.\n\n"
        f"User profile: {profile}"
    )


def create_digest_agent():
    """Stateless agent for scheduled digest generation."""
    return create_deep_agent(
        model=_make_model(),
        system_prompt=_build_digest_system_prompt(),
        tools=[
            get_configured_channels,
            scrape_all_channels,
            filter_ads_tool,
            generate_digest_tool,
            load_recent_digests_tool,
            save_digest_tool,
        ],
        # Deny all filesystem access — deepagents adds ls/read_file/etc by default,
        # we don't want the agent wasting steps on filesystem exploration.
        permissions=[
            FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="deny"),
        ],
    )


def create_chat_agent(system_prompt: str, checkpointer):
    """Stateful conversational agent with Supabase-backed memory."""
    system = system_prompt
    system += (
        "\n\nYou have access to tools to search past digests and get context. "
        "Use search_digest_history when the user asks about past topics. "
        "Use get_recent_digests to reference what was covered recently. "
        "Use get_current_focus to understand what the user is currently focused on."
    )
    return create_deep_agent(
        model=_make_model(),
        system_prompt=system,
        tools=[
            search_digest_history,
            get_recent_digests,
            get_current_focus,
        ],
        checkpointer=checkpointer,
    )


# ─── Tool call → Telegram label map ──────────────────────────────────────────

_DIGEST_TOOL_LABELS = {
    "get_configured_channels": "📡 Загружаю список каналов...",
    "scrape_all_channels":     "⏳ Скрейплю каналы параллельно...",
    "filter_ads_tool":         "🔍 Фильтрую рекламу...",
    "load_recent_digests_tool":"📚 Загружаю историю дайджестов...",
    "generate_digest_tool":    "🤖 Генерирую дайджест...",
    "save_digest_tool":        "💾 Сохраняю в базу...",
}

_CHAT_TOOL_LABELS = {
    "search_digest_history": "🔎 Ищу в истории дайджестов...",
    "get_recent_digests":    "📚 Читаю последние дайджесты...",
    "get_current_focus":     "🎯 Проверяю текущий фокус...",
}


# ─── Entry points ─────────────────────────────────────────────────────────────

async def run_digest_agent(on_status=None) -> dict:
    """
    Run the digest agent with live Telegram status updates.
    on_status: async callable(text: str) — called on each tool start.
    Saves result to DB, returns dict for sending.
    """
    agent = create_digest_agent()
    # recursion_limit: each tool call = 2+ LangGraph steps; 5 tools + LLM rounds ~= 15-20 steps.
    # Set 50 to have headroom without risk of infinite loops.
    config = {"recursion_limit": 50, "configurable": {"thread_id": "digest-run"}}

    try:
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": "Generate today's digest."}]},
            config,
            version="v2",
        ):
            kind = event.get("event")
            if kind == "on_tool_start":
                tool_name = event.get("name", "")
                label = _DIGEST_TOOL_LABELS.get(tool_name)
                logger.info(f"[digest_agent] tool_start: {tool_name}")
                if label and on_status:
                    try:
                        await on_status(label)
                    except Exception:
                        pass
            elif kind == "on_tool_end":
                tool_name = event.get("name", "")
                logger.info(f"[digest_agent] tool_end:   {tool_name}")
    except GraphRecursionError:
        logger.warning("[digest_agent] hit recursion limit — reading last saved digest")

    history = await db.load_history(limit=1)
    if history:
        last = history[-1]
        return {
            "digest_html": last.get("digest_html", last.get("digest", "")),
            "personal_html": "",
            "stats_html": "",
            "posts_count": last["posts_count"],
        }
    return {"digest_html": "Ошибка: дайджест не сохранён", "personal_html": "", "stats_html": "", "posts_count": 0}


async def run_chat_turn(user_id: int, message: str, checkpointer) -> str:
    """
    Run one chat turn with tool call logging.
    checkpointer lifecycle managed in bot.py post_init/post_shutdown.
    """
    data = await db.load()
    user_data = data.copy()
    user_data["openrouter_key"] = os.getenv("OPENROUTER_KEY")
    system_prompt = build_system_prompt(user_data)
    agent = create_chat_agent(system_prompt, checkpointer)
    config = {"configurable": {"thread_id": str(user_id)}}

    final_text = "Не смог ответить."
    async for event in agent.astream_events(
        {"messages": [{"role": "user", "content": message}]},
        config,
        version="v2",
    ):
        kind = event.get("event")
        if kind == "on_tool_start":
            tool_name = event.get("name", "")
            label = _CHAT_TOOL_LABELS.get(tool_name, tool_name)
            logger.info(f"[chat_agent] tool_start: {tool_name}")
        elif kind == "on_tool_end":
            logger.info(f"[chat_agent] tool_end:   {event.get('name', '')}")
        elif kind == "on_chat_model_end":
            # Capture final AI message
            output = event.get("data", {}).get("output")
            if output and hasattr(output, "content") and output.content:
                final_text = output.content

    return final_text
