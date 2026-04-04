"""
Standalone scheduler for daily digest and check-in.
Runs as a separate process/container — isolated from bot polling event loop.
"""
import asyncio
import logging
import os
import signal

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
MOSCOW = pytz.timezone("Europe/Moscow")


async def run_digest():
    from bot import do_send_digest

    logger.info("Scheduled digest starting")
    async with Bot(BOT_TOKEN) as bot:
        await do_send_digest(bot, CHAT_ID)
    logger.info("Scheduled digest done")


async def run_checkin():
    from bot import load

    data = load()
    focus = data.get("current_focus", "")
    focus_line = f" Как дела с *{escape_markdown(focus, version=1)}*?" if focus else ""
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Прочитал", callback_data="ci_yes"),
            InlineKeyboardButton("❌ Не успел", callback_data="ci_no"),
            InlineKeyboardButton("💬 Поговорить", callback_data="ci_talk"),
        ]]
    )
    async with Bot(BOT_TOKEN) as bot:
        await bot.send_message(
            CHAT_ID,
            f"Эй, успел глянуть дайджест?{focus_line}",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    logger.info("Scheduled checkin sent")


async def _run():
    from bot import DIGEST_HOUR, DIGEST_MINUTE, CHECKIN_HOUR, CHECKIN_MINUTE

    scheduler = AsyncIOScheduler(timezone=MOSCOW)
    scheduler.add_job(run_digest, "cron", hour=DIGEST_HOUR, minute=DIGEST_MINUTE, misfire_grace_time=300, name="daily_digest")
    scheduler.add_job(run_checkin, "cron", hour=CHECKIN_HOUR, minute=CHECKIN_MINUTE, misfire_grace_time=300, name="daily_checkin")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
    except NotImplementedError:
        # Windows: loop.add_signal_handler is Unix-only
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop_event.set))

    scheduler_running = False
    try:
        scheduler.start()
        scheduler_running = True
        logger.info(f"Scheduler started — digest {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} MSK, checkin {CHECKIN_HOUR:02d}:{CHECKIN_MINUTE:02d} MSK")
        await stop_event.wait()
    finally:
        if scheduler_running:
            scheduler.shutdown()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(_run())
