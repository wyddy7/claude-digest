"""
Agent layer for digest_bot.

Two components:
- run_digest_pipeline: stateless deterministic pipeline (scrape → filter → generate → save)
- chat_agent:          stateful (Supabase checkpointer), conversational with history tools

Entry points:
- run_digest_pipeline(on_status) — called by handlers.digest.deliver_digest + scheduler
- run_chat_turn(user_id, message, checkpointer, *, scope_user_id) — called by
  handlers.chat._chat_with_digest (the unified multi-tenant text router)
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

import digest_bot.db as db
import digest_bot.reader as reader
from digest_bot.ai import build_system_prompt, filter_ads, generate_digest, summarize_chat_history
from digest_bot.personalization import load_personalization, resolve_personalization
from digest_bot.pipeline_config import READ_MODE_AGENTIC, READ_MODE_EXTRACT
from digest_bot.scraper import scrape_channel

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CHAT_MAX_TOKENS = 1200

# Chat context compaction thresholds. MemorySaver is in-memory only and resets
# on container restart, so this only matters for long single sessions.
COMPACT_TRIGGER = 30   # total messages above which compaction is attempted
COMPACT_TARGET = 10    # messages we want to keep in the tail
COMPACT_SLACK = 5      # ± positions to search for a HumanMessage boundary


def _make_model(role: str = "chat", model_id: str | None = None) -> ChatOpenAI:
    """Build a ChatOpenAI pointed at OpenRouter.

    model_id: the per-user model from user_settings (db.load_settings always
    populates it with db.DEFAULT_MODEL). When given it wins, so
    each tenant's chat runs on THEIR selected model. Only when it's absent do we
    fall back to the owner yaml models.<role> (legacy) and then a hard default —
    the owner's private yaml no longer silently sets the chat model for everyone.
    """
    if not model_id:
        model_id = load_personalization().get("models", {}).get(role) or db.DEFAULT_MODEL
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


GET_RECENT_DIGESTS_PER_ITEM_CAP = 8000  # sanity bound, not a normal-case limit


def _search_digest_history_results(history: list[dict], query: str) -> list[dict]:
    """Pure: filter a list of digest rows by query. Shared by the global and the
    per-user (scoped) tool so the matching logic lives in one place."""
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


def _recent_digests_payload(history: list[dict]) -> list[dict]:
    """Pure: shape recent digest rows for the tool return. Truncating tool returns
    to 600 chars is what caused the 2026-05-05 "no mention in history" miss; the
    cap is a sanity bound for pathological digests (~10x typical body)."""
    return [
        {
            "id": h.get("id"),
            "date": h["date"],
            "digest": h.get("digest_html", "")[:GET_RECENT_DIGESTS_PER_ITEM_CAP],
        }
        for h in history if not h.get("is_error")
    ]


# ── per-user (tenant-scoped) tool factory ─────────────────────────────────────

def _make_user_scoped_tools(user_id: str) -> list:
    """Build the chat tools bound to ONE tenant's data. The agent never sees a
    user_id argument — it is closed over here — so a tenant can only ever read
    their own digests/focus (db.load_user_history / db.load_settings, scoped by
    user_id), never the legacy global db.load()/db.load_history()."""

    @tool
    async def search_digest_history(query: str) -> list[dict]:
        """Search past digests by keyword. Returns matching entries (date, snippet)."""
        history = await db.load_user_history(user_id)
        return _search_digest_history_results(history, query)

    @tool
    async def get_recent_digests(n: int = 3) -> list[dict]:
        """Return the N most recent digest entries with date and content."""
        history = await db.load_user_history(user_id, limit=n)
        return _recent_digests_payload(history)

    @tool
    async def get_current_focus() -> str:
        """Return the user's current digest focus (if any)."""
        settings = await db.load_settings(user_id)
        return settings.get("current_focus", "") or "не задан"

    return [search_digest_history, get_recent_digests, get_current_focus]


# ─── Chat agent factory ───────────────────────────────────────────────────────

def create_chat_agent(system_prompt: str, checkpointer, user_id: str, model_id: str | None = None):
    """Stateful conversational agent with checkpointer-backed memory.

    user_id: the tenant's UUID. The agent's tools are scoped to that tenant's
    data (db.load_user_history / db.load_settings) — the agent never sees a
    user_id argument, so a tenant can only ever read their own digests/focus.
    There is no shared mutable tool state — scoped tools are fresh closures per
    call."""
    system = system_prompt + (
        "\n\nYou have access to tools to search past digests and get context. "
        "Use search_digest_history when the user asks about past topics. "
        "Use get_recent_digests to reference what was covered recently. "
        "Use get_current_focus to understand what the user is currently focused on."
    )
    tools = _make_user_scoped_tools(user_id)
    return create_deep_agent(
        model=_make_model("chat", model_id=model_id),
        system_prompt=system,
        tools=tools,
        checkpointer=checkpointer,
    )


async def clear_chat_thread(checkpointer, tg_user_id: int) -> None:
    """Wipe one user's conversational memory (the /clear command). The thread id
    is the numeric tg id — the same key run_chat_turn uses for MemorySaver."""
    await checkpointer.adelete_thread(str(tg_user_id))


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

def _build_cost_summary(config, usage_log: list[dict], reader_stats: dict | None) -> dict:
    """Aggregate per-stage token usage + reader extraction stats so off-mode vs
    extract-mode is empirically comparable. SaaS seam: tenant_id would key
    per-tenant cost logging here (not implemented)."""
    per_stage: dict[str, dict] = {}
    for u in usage_log:
        s = per_stage.setdefault(
            u["stage"],
            {"model": u.get("model", ""), "prompt_tokens": 0, "completion_tokens": 0,
             "calls": 0, "api_cost_usd": 0.0},
        )
        s["prompt_tokens"] += u.get("prompt_tokens", 0)
        s["completion_tokens"] += u.get("completion_tokens", 0)
        s["calls"] += 1
        if not s.get("model"):
            s["model"] = u.get("model", "")
        # OpenRouter's authoritative per-call USD cost, summed across the stage's
        # calls. None entries (provider didn't report) leave it at 0.0 → priced
        # from the fallback table in pricing.price_cost_summary.
        c = u.get("cost_usd")
        if c is not None:
            s["api_cost_usd"] += float(c)
    rs = reader_stats or {}
    return {
        "read_mode": config.read_mode,
        "per_stage_tokens": per_stage,
        "extraction_attempted": rs.get("attempted", 0),
        "extraction_ok": rs.get("ok", 0),
        "urls_skipped_dedup": rs.get("urls_skipped_dedup", 0),
        "urls_skipped_cap": rs.get("urls_skipped_cap", 0),
    }


async def run_digest_pipeline(config, *, db_module=db, llm_client=None, fetcher=None, on_status=None) -> dict:
    """
    Run the digest pipeline directly — no agent overhead.
    Steps: load channels → scrape (parallel) → filter ads → generate → save.

    config:     PipelineConfig (read_mode, per-stage model registry, guardrails).
    db_module:  injected db layer (defaults to the real db module).
    llm_client: injected AsyncOpenAI-compatible client (built by the caller).
    fetcher:    injected httpx client for the reader layer (used from P4).
    on_status:  async callable(text: str) — called before each step.
    Returns dict: digest_html, personal_html, stats_html, posts_count.
    """
    if llm_client is None:
        raise ValueError("run_digest_pipeline requires an llm_client (see pipeline_config.make_openrouter_client)")

    # Grade B (iterative model-driven fetch loop) is intentionally NOT
    # implemented inside this deterministic pipeline — it would cross the
    # "no agent framework in run_digest_pipeline" invariant. It must live in a
    # separate, explicitly flagged module. Fail fast rather than silently
    # falling through to off-mode behavior.
    if config.read_mode == READ_MODE_AGENTIC:
        raise NotImplementedError(
            "Grade B reader (read_mode=agentic) is a stub — it must live in a "
            "separate flagged module, never bolted onto run_digest_pipeline. "
            "See digest_bot/CLAUDE.md."
        )

    usage_log: list[dict] = []  # per-LLM-call token usage, aggregated into cost_summary below

    async def _status(label: str):
        if on_status:
            try:
                await on_status(label)
            except Exception:
                pass

    # Step 1 — load channels. Multi-tenant: the caller's own channels + profile
    # ride on the config (set by build_pipeline_config from the user's settings).
    # Legacy/test callers leave those empty and fall back to the global db row.
    await _status(_DIGEST_STATUS["channels"])
    multitenant = bool(config.user_data) or bool(config.channels)
    if multitenant:
        data = dict(config.user_data)
        channels = list(config.channels)
    else:
        data = await db_module.load()
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

    # Step 2b — reader layer (Grade A): deep-read content behind post links.
    reader_stats = None
    if config.read_mode == READ_MODE_EXTRACT:
        await _status("📖 Читаю статьи по ссылкам...")
        reader_stats = await reader.read_posts(
            posts, config=config, client=llm_client, fetcher=fetcher,
            db_module=db_module, usage_log=usage_log,
        )
        logger.info(f"[digest] reader: {reader_stats}")

    # Step 3 — filter ads
    await _status(_DIGEST_STATUS["filter"])
    filtered = await filter_ads(
        posts, client=llm_client, model=config.models["ad_filter"].model_id, usage_log=usage_log
    )
    logger.info(f"[digest] after ad-filter: {len(filtered)} posts")

    # Step 4 — generate digest
    await _status(_DIGEST_STATUS["generate"])
    user_data = data.copy()
    recent = config.recent_digests if multitenant else await db_module.load_history(limit=3)
    digest_model = config.models["digest"].model_id
    logger.info(f"[digest] generating | posts={len(filtered)} | model={digest_model}")
    digest_html, personal_html, stats_html = await generate_digest(
        filtered, user_data, client=llm_client, model=digest_model,
        recent_digests=recent, usage_log=usage_log,
        # Per-user resolved profile/prompt rules (privacy boundary). Empty
        # config → None → build_system_prompt's NEUTRAL default, never the
        # owner's yaml.
        personalization=config.personalization or None,
    )
    logger.info(f"[digest] generated  | digest_len={len(digest_html)} | personal={'yes' if personal_html else 'no'}")

    # Step 5 — save to history. Legacy single-tenant writes the global history
    # row here; multi-tenant callers record per-user history themselves
    # (db.append_user_digest in handlers/digest.deliver_digest), so skip it.
    await _status(_DIGEST_STATUS["save"])
    if not multitenant:
        await db_module.append_to_history(digest_html, len(filtered))

    cost_summary = _build_cost_summary(config, usage_log, reader_stats)
    logger.info(f"[digest] cost_summary: {cost_summary}")
    logger.info(f"[digest] done | posts_count={len(filtered)}")

    return {
        "digest_html": digest_html,
        "personal_html": personal_html or "",
        "stats_html": stats_html or "",
        "posts_count": len(filtered),
        "cost_summary": cost_summary,
    }


async def run_chat_turn(
    user_id: int, message: str, checkpointer, *, scope_user_id: str
) -> str:
    """
    Run one chat turn. checkpointer lifecycle managed in bot.py post_init/post_shutdown.

    user_id:       thread key (the numeric Telegram user id) — MemorySaver keys
                   per-user conversation state on this, so tenants never share a
                   chat thread.
    scope_user_id: the tenant's UUID. The system prompt + agent tools read ONLY
                   that tenant's settings/history (db.load_settings /
                   db.load_user_history) — a tenant never sees another's data.
    """
    settings = await db.load_settings(scope_user_id)
    user_data = {
        "current_focus": settings.get("current_focus") or "",
        "interaction_history": settings.get("interaction_history") or [],
        "openrouter_key": os.getenv("OPENROUTER_KEY"),
    }
    # load_user_history is newest-first; build_system_prompt/get_recent_digests
    # expect oldest-first (the legacy load_history(limit=) contract), so reverse.
    recent = list(reversed(await db.load_user_history(scope_user_id, limit=3)))
    # Per-user personalization (privacy boundary): owner → private yaml,
    # tenant → neutral default + their own DB overrides. user_id here is the
    # numeric tg id, which is what is_owner() keys on.
    personalization = resolve_personalization(
        settings.get("personalization"), user_id
    )
    system_prompt = build_system_prompt(
        user_data, recent_digests=recent, personalization=personalization
    )
    agent = create_chat_agent(
        system_prompt, checkpointer, user_id=scope_user_id,
        model_id=settings.get("model"),
    )
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
