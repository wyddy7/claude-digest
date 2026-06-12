"""
Standalone scheduler for daily digest and check-in.
Runs as a separate process/container — isolated from bot polling event loop.

DB pool is initialized once at startup and closed on shutdown.
"""
import asyncio
import logging
import os
import signal

import httpx
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

import db
import subscriptions
from agent import run_digest_pipeline
from bot import DIGEST_HOUR, DIGEST_MINUTE, CHECKIN_HOUR, CHECKIN_MINUTE
from delivery import send_digest_chunks
from personalization import load_personalization
from pipeline_config import build_pipeline_config, make_openrouter_client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
MOSCOW = pytz.timezone("Europe/Moscow")


async def run_digest():
    logger.info("Scheduled digest starting")
    async with Bot(BOT_TOKEN) as bot:
        status_msg = None

        async def _on_status(text: str):
            nonlocal status_msg
            try:
                if status_msg is None:
                    status_msg = await bot.send_message(CHAT_ID, text)
                else:
                    await status_msg.edit_text(text)
            except Exception as e:
                logger.warning(f"scheduler: status update failed: {e}")

        cfg_data = await db.load()
        cfg_yaml = load_personalization()
        config = build_pipeline_config(cfg_data, cfg_yaml)
        llm_client = make_openrouter_client(OPENROUTER_KEY)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as fetcher:
            result = await run_digest_pipeline(
                config, llm_client=llm_client, fetcher=fetcher, on_status=_on_status
            )

        digest_html = result["digest_html"]
        personal_html = result.get("personal_html", "")
        stats_html = result.get("stats_html", "")
        posts_count = result.get("posts_count", 0)

        if status_msg:
            try:
                await status_msg.edit_text("✅ Готово")
            except Exception:
                pass

        # Save to user_state
        data = await db.load()
        if data.get("focus_auto_reset") and data.get("current_focus"):
            data["current_focus"] = ""
        data["last_digest"] = digest_html
        import datetime
        data["last_digest_time"] = datetime.datetime.now().isoformat()
        await db.save(data)
        await db.add_history(f"Дайджест ({posts_count} постов)")

        from datetime import datetime as dt
        date_str = dt.now(MOSCOW).strftime("%d.%m.%Y")
        full_text = f"📰 <b>Дайджест {date_str}</b>\n\n{digest_html}"

        max_len = 4096
        chunks = []
        if len(full_text) <= max_len:
            chunks = [full_text]
        else:
            paragraphs = full_text.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) + 2 <= max_len:
                    current = current + ("\n\n" if current else "") + para
                else:
                    if current:
                        chunks.append(current)
                    while len(para) > max_len:
                        chunks.append(para[:max_len])
                        para = para[max_len:]
                    current = para
            if current:
                chunks.append(current)

        for chunk in chunks:
            await bot.send_message(CHAT_ID, chunk, parse_mode="HTML", disable_web_page_preview=True)

        personal_parts = [p for p in [personal_html, stats_html] if p]
        if personal_parts:
            await bot.send_message(
                CHAT_ID, "\n\n".join(personal_parts),
                parse_mode="HTML", disable_web_page_preview=True,
            )

    logger.info(f"Scheduled digest done: {posts_count} posts")


async def _deliver_user_digest(bot, user: dict) -> int:
    """Generate + deliver one user's digest using their own channels/settings/
    personalization. Returns posts_count. Per-user chat_id = the numeric
    tg_user_id (the value Telegram routes on)."""
    from datetime import datetime as dt

    user_id = user["id"]
    tg_user_id = user["tg_user_id"]
    settings = await db.load_settings(user_id)

    cfg_data = {
        "channels": settings.get("channels") or [],
        "current_focus": settings.get("current_focus") or "",
        "focus_auto_reset": bool(settings.get("focus_auto_reset")),
        "model": settings.get("model") or db.DEFAULT_MODEL,
        "last_digest": settings.get("last_digest") or "",
        "last_digest_time": settings.get("last_digest_time") or "",
        "interaction_history": settings.get("interaction_history") or [],
    }
    # Personalization: DB per-tenant home, falling back to the legacy yaml template.
    cfg_yaml = await db.load_personalization_db(user_id) or load_personalization()
    config = build_pipeline_config(cfg_data, cfg_yaml)
    llm_client = make_openrouter_client(OPENROUTER_KEY)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as fetcher:
        result = await run_digest_pipeline(config, llm_client=llm_client, fetcher=fetcher)

    digest_html = result["digest_html"]
    posts_count = result.get("posts_count", 0)
    date_str = dt.now(MOSCOW).strftime("%d.%m.%Y")
    full_text = f"📰 <b>Дайджест {date_str}</b>\n\n{digest_html}"
    await send_digest_chunks(
        bot, tg_user_id, full_text,
        result.get("personal_html", ""), result.get("stats_html", ""),
    )

    await db.save_settings(user_id, {
        "last_digest": digest_html,
        "last_digest_time": dt.now().isoformat(),
    })
    return posts_count


async def run_digest_fanout():
    """Multi-tenant fan-out tick: iterate active users, gate each on subscription
    state, deliver per-user. If no users rows exist (legacy mode), fall through to
    the existing single-tenant run_digest() so the live bot is unaffected.

    STUB: per-user errors are isolated (one user's failure never blocks others);
    expiry-warning + delivery gating are wired, the per-user pipeline reuse is the
    surface a later stage hardens (rate limits, parallelism caps, retries)."""
    users = await db.list_active_users()
    if not users:
        logger.info("Fan-out: no users rows — legacy single-tenant path")
        await run_digest()
        return

    logger.info(f"Fan-out: {len(users)} active user(s)")
    async with Bot(BOT_TOKEN) as bot:
        for user in users:
            tg_user_id = user["tg_user_id"]
            try:
                if not await subscriptions.is_subscription_active(tg_user_id):
                    # Inactive: optionally warn near expiry, then skip delivery.
                    await subscriptions.maybe_warn_expiry(tg_user_id, bot)
                    continue
                posts_count = await _deliver_user_digest(bot, user)
                logger.info(f"Fan-out: delivered to {tg_user_id} ({posts_count} posts)")
            except Exception as e:
                logger.warning(f"Fan-out: user {tg_user_id} failed (isolated): {e}")


async def run_checkin():
    data = await db.load()
    focus = data.get("current_focus", "")
    focus_line = f" Как дела с *{escape_markdown(focus, version=1)}*?" if focus else ""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Прочитал", callback_data="ci_yes"),
        InlineKeyboardButton("❌ Не успел", callback_data="ci_no"),
        InlineKeyboardButton("💬 Поговорить", callback_data="ci_talk"),
    ]])
    async with Bot(BOT_TOKEN) as bot:
        await bot.send_message(
            CHAT_ID,
            f"Эй, успел глянуть дайджест?{focus_line}",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    logger.info("Scheduled checkin sent")


async def _run():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        logger.error("SUPABASE_URL or SUPABASE_KEY not set")
        return

    await db.init_supabase(url, key)
    await db.ensure_owner_user()
    logger.info("DB ready (supabase-py)")

    scheduler = AsyncIOScheduler(timezone=MOSCOW)
    # Multi-tenant fan-out tick. Falls back to the legacy single-tenant run_digest()
    # when no users rows exist, so the live single-tenant bot is unaffected.
    scheduler.add_job(run_digest_fanout, "cron", hour=DIGEST_HOUR, minute=DIGEST_MINUTE, misfire_grace_time=300, name="daily_digest")
    scheduler.add_job(run_checkin, "cron", hour=CHECKIN_HOUR, minute=CHECKIN_MINUTE, misfire_grace_time=300, name="daily_checkin")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop_event.set))

    try:
        scheduler.start()
        logger.info(f"Scheduler started — digest {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} MSK, checkin {CHECKIN_HOUR:02d}:{CHECKIN_MINUTE:02d} MSK")
        await stop_event.wait()
    finally:
        scheduler.shutdown()
        await db.close_pool()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(_run())
