"""
Standalone scheduler for daily digest and check-in.
Runs as a separate process/container — isolated from bot polling event loop.

DB pool is initialized once at startup and closed on shutdown.
"""
import asyncio
import logging
import os
import signal

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot

import digest_bot.db as db
import digest_bot.subscriptions as subscriptions
from digest_bot.bot import DIGEST_HOUR, DIGEST_MINUTE, CHECKIN_HOUR, CHECKIN_MINUTE
from digest_bot.logging_setup import setup_logging

load_dotenv()
setup_logging("scheduler")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
MOSCOW = pytz.timezone("Europe/Moscow")


async def _deliver_user_digest(bot, user: dict) -> int:
    """Generate + deliver one user's digest. Delegates to the single shared
    generator (handlers.digest.deliver_digest) so the cron fan-out, the 📰
    button, and the onboarding preview all run the exact same per-user path —
    no duplicated config-build/send/save logic."""
    from digest_bot.handlers.digest import deliver_digest

    return await deliver_digest(bot, user, source="cron")


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

    logger.info("Fan-out: %s active user(s)", len(users))
    async with Bot(BOT_TOKEN) as bot:
        for user in users:
            tg_user_id = user["tg_user_id"]
            user_id = user["id"]
            try:
                if not await subscriptions.is_subscription_active(tg_user_id):
                    # Inactive: optionally warn near expiry, then skip delivery.
                    await subscriptions.maybe_warn_expiry(tg_user_id, bot)
                    continue

                # N6: enforce digests_per_day on the cron path. The manual 📰
                # button shares this same daily budget (handlers/digest.py), so a
                # user can't get extra digests by alternating cron + manual.
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
                logger.info("Fan-out: delivered to %s (%s posts)", tg_user_id, posts_count)
            except Exception as e:
                # %r + exc_info: empty-message exceptions (httpx.ReadTimeout has
                # str()=='') logged a bare ': ' that hid the cause (2026-06-26).
                logger.warning(
                    "Fan-out: user %s failed (isolated): %r", tg_user_id, e, exc_info=True
                )


async def run_checkin():
    """Multi-tenant check-in fan-out.

    Delegates to handlers.checkin.run_checkin_fanout which iterates active users,
    gates each on subscriptions.is_subscription_active, reads per-user focus from
    user_settings, and delivers the check-in to their tg_user_id. The owner is a
    normal users row, so the fan-out covers them too.
    """
    from digest_bot.handlers.checkin import run_checkin_fanout

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
        logger.info("Scheduler started — digest %02d:%02d MSK, checkin %02d:%02d MSK", DIGEST_HOUR, DIGEST_MINUTE, CHECKIN_HOUR, CHECKIN_MINUTE)
        await stop_event.wait()
    finally:
        scheduler.shutdown()
        await db.close_pool()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(_run())
