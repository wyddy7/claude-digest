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
from telegram import Bot

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
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
MOSCOW = pytz.timezone("Europe/Moscow")


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
    state, deliver per-user. The owner is a normal users row (seeded by
    db.ensure_owner_user), so the fan-out always covers them.

    Per-user errors are isolated (one user's failure never blocks others);
    expiry-warning + delivery gating are wired."""
    users = await db.list_active_users()
    if not users:
        logger.info("Fan-out: no active users — nothing to deliver")
        return

    logger.info(f"Fan-out: {len(users)} active user(s)")
    async with Bot(BOT_TOKEN) as bot:
        for user in users:
            tg_user_id = user["tg_user_id"]
            user_id = user["id"]
            try:
                if not await subscriptions.is_subscription_active(tg_user_id):
                    # Inactive: optionally warn near expiry, then skip delivery.
                    await subscriptions.maybe_warn_expiry(tg_user_id, bot)
                    continue

                # N6: enforce digests_per_day on the CRON path only.
                # Manual 📰 requests are always lenient (on-demand, user-initiated).
                daily_cap = await db.get_effective_limit(user_id, "digests_per_day", None)
                if daily_cap is not None:
                    try:
                        daily_cap = int(daily_cap)
                    except (TypeError, ValueError):
                        daily_cap = None
                if daily_cap is not None and daily_cap >= 0:
                    already_sent = await db.count_user_digests_today(user_id)
                    if already_sent >= daily_cap:
                        logger.info(
                            "Fan-out: skipping %s — digests_per_day cap %d reached (%d sent today)",
                            tg_user_id, daily_cap, already_sent,
                        )
                        continue

                posts_count = await _deliver_user_digest(bot, user)
                logger.info(f"Fan-out: delivered to {tg_user_id} ({posts_count} posts)")
            except Exception as e:
                logger.warning(f"Fan-out: user {tg_user_id} failed (isolated): {e}")


async def run_checkin():
    """Multi-tenant check-in fan-out.

    Delegates to handlers.checkin.run_checkin_fanout which iterates active users,
    gates each on subscriptions.is_subscription_active, reads per-user focus from
    user_settings, and delivers the check-in to their tg_user_id. The owner is a
    normal users row, so the fan-out covers them too.
    """
    from handlers.checkin import run_checkin_fanout

    await run_checkin_fanout(BOT_TOKEN)
    logger.info("Scheduled checkin fan-out complete")


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
    # Multi-tenant fan-out tick — iterates every active users row (owner included).
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
