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
import difflib
import logging
import os
import re

from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from deepagents import create_deep_agent

import db
from ai import build_system_prompt, filter_ads, generate_digest, summarize_chat_history
from personalization import load_personalization
from scraper import scrape_channel

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CHAT_MAX_TOKENS = 1200

# Chat context compaction thresholds. MemorySaver is in-memory only and resets
# on container restart, so this only matters for long single sessions.
COMPACT_TRIGGER = 30   # total messages above which compaction is attempted
COMPACT_TARGET = 10    # messages we want to keep in the tail
COMPACT_SLACK = 5      # ± positions to search for a HumanMessage boundary


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
        max_tokens=CHAT_MAX_TOKENS,
    )


# ─── chat_agent tools ─────────────────────────────────────────────────────────

# Search tunables. Strict substring is tried first (fast path); if it misses,
# token-level fuzzy is the fallback. Threshold values picked to catch typos
# like "whispr" → "wispr" (ratio 0.83) without matching unrelated short words.
SEARCH_TOKEN_MIN_LEN = 3
SEARCH_FUZZY_TOKEN_RATIO = 0.75
SEARCH_TOKEN_COVERAGE = 0.5


def _matches_query(query: str, content: str) -> bool:
    """
    Decide whether `content` matches `query`. Strict substring is checked
    first; on miss, fall back to token-level overlap with a per-token fuzzy
    matcher to absorb typos and inflections. Pure function — kept testable
    without DB.
    """
    if not query or not content:
        return False
    q = query.lower().strip()
    c = content.lower()
    if q in c:
        return True
    q_tokens = [t for t in re.split(r"\s+", q) if len(t) >= SEARCH_TOKEN_MIN_LEN]
    if not q_tokens:
        return False
    content_words = re.findall(rf"\w{{{SEARCH_TOKEN_MIN_LEN},}}", c)
    if not content_words:
        return False
    matched = 0
    for tok in q_tokens:
        if tok in c:
            matched += 1
            continue
        for word in content_words:
            if difflib.SequenceMatcher(None, tok, word).ratio() >= SEARCH_FUZZY_TOKEN_RATIO:
                matched += 1
                break
    return matched / len(q_tokens) >= SEARCH_TOKEN_COVERAGE


@tool
async def search_digest_history(query: str) -> list[dict]:
    """Search past digests by keyword. Returns matching entries (date, snippet)."""
    history = await db.load_history()
    results = []
    for item in history:
        if item.get("is_error"):
            continue
        content = item.get("digest_html", "")
        if _matches_query(query, content):
            results.append({
                "id": item.get("id"),
                "date": item["date"],
                "snippet": content[:300],
            })
    return results[-10:]


GET_RECENT_DIGESTS_PER_ITEM_CAP = 8000  # sanity bound, not a normal-case limit


@tool
async def get_recent_digests(n: int = 3) -> list[dict]:
    """Return the N most recent digest entries with date and content."""
    history = await db.load_history(limit=n)
    # Truncating tool returns to 600 chars is what caused the 2026-05-05
    # "no mention in history" miss. Cap is a sanity bound for pathological
    # digests (~10x typical body), not a normal-case limit.
    return [
        {
            "id": h.get("id"),
            "date": h["date"],
            "digest": h.get("digest_html", "")[:GET_RECENT_DIGESTS_PER_ITEM_CAP],
        }
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


# ─── Chat context compaction ──────────────────────────────────────────────────

def _find_safe_cut(messages: list, target_keep: int = COMPACT_TARGET, slack: int = COMPACT_SLACK) -> int:
    """
    Find an index `cut` such that messages[cut:] is a clean tail starting on a
    HumanMessage. Searches forward first (drop more, keep smaller tail), then
    backward (keep more) within `slack` of the desired position. Returns -1 if
    no HumanMessage boundary exists in that window.

    Why: we cannot break an AIMessage(tool_calls=...) → ToolMessage pair —
    OpenAI/OpenRouter rejects orphaned tool messages. The only universally safe
    boundary is right before a HumanMessage.
    """
    n = len(messages)
    if n <= target_keep:
        return -1
    target_idx = n - target_keep
    upper = min(n, target_idx + slack + 1)
    for i in range(target_idx, upper):
        if isinstance(messages[i], HumanMessage):
            return i
    lower = max(0, target_idx - slack)
    for i in range(target_idx - 1, lower - 1, -1):
        if isinstance(messages[i], HumanMessage):
            return i
    return -1


async def _compact_if_needed(agent, config) -> int | None:
    """
    Compact accumulated chat history when message count exceeds COMPACT_TRIGGER.
    Returns number of messages removed, or None if skipped.
    Non-fatal: any failure is logged and swallowed by the caller.
    """
    snap = await agent.aget_state(config)
    msgs = (snap.values or {}).get("messages", []) if snap else []
    if len(msgs) <= COMPACT_TRIGGER:
        return None

    cut_idx = _find_safe_cut(msgs)
    if cut_idx <= 0:
        logger.debug(f"[compact] no safe cut for {len(msgs)} msgs, skipping")
        return None

    head = msgs[:cut_idx]
    summary_input = []
    for m in head:
        role = getattr(m, "type", None) or m.__class__.__name__.lower().replace("message", "")
        content = m.content if isinstance(m.content, str) else str(m.content)
        if content.strip():
            summary_input.append({"role": role, "content": content})
    if not summary_input:
        return None

    summary_text = await summarize_chat_history(summary_input, os.getenv("OPENROUTER_KEY", ""))
    if not summary_text:
        logger.warning("[compact] empty summary, skipping aupdate_state")
        return None

    remove_ops = [RemoveMessage(id=m.id) for m in head if getattr(m, "id", None)]
    if not remove_ops:
        logger.debug("[compact] no message ids in head, skipping (test fixture or pre-add_messages state)")
        return None

    summary_msg = HumanMessage(content=f"[Сжатая история беседы]: {summary_text}")
    await agent.aupdate_state(config, {"messages": remove_ops + [summary_msg]})
    logger.info(f"[compact] {len(msgs)} → {len(msgs) - len(remove_ops) + 1} msgs (summarized {len(remove_ops)})")
    return len(remove_ops)


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
    recent = await db.load_history(limit=3)
    system_prompt = build_system_prompt(user_data, recent_digests=recent)
    agent = create_chat_agent(system_prompt, checkpointer)
    config = {"configurable": {"thread_id": str(user_id)}}

    try:
        removed = await _compact_if_needed(agent, config)
        if removed:
            logger.info(f"[chat_agent] compacted {removed} prior messages")
    except Exception as e:
        logger.warning(f"[chat_agent] compact failed (non-fatal): {e}")

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
