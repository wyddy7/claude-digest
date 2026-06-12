import asyncio
import logging
import os
import sys

# psycopg3 requires SelectorEventLoop on Windows (incompatible with ProactorEventLoop)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import db
from logging_setup import setup_logging

load_dotenv()
setup_logging("bot")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HTTPS_PROXY = os.getenv("HTTPS_PROXY")

_polling_blips: list[float] = []
_BLIP_WINDOW_SEC = 60.0
_BLIP_WARN_THRESHOLD = 5


def _mask_proxy(url: str | None) -> str:
    if not url:
        return "none"
    try:
        scheme, rest = url.split("://", 1)
        host = rest.split("@", 1)[-1]
        return f"{scheme}://***@{host}"
    except ValueError:
        return "malformed"

# Schedule — single source of truth for both bot.py and scheduler.py. Imported
# by scheduler.py and handlers/checkin.py — keep here.
DIGEST_HOUR, DIGEST_MINUTE = 13, 0
CHECKIN_HOUR, CHECKIN_MINUTE = 18, 0


async def _post_init(app: Application) -> None:
    """Initialize supabase client and in-memory checkpointer at startup."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL or SUPABASE_KEY not set")
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required")

    await db.init_supabase(SUPABASE_URL, SUPABASE_KEY)
    await db.ensure_owner_user()

    from langgraph.checkpoint.memory import MemorySaver
    app.bot_data["checkpointer"] = MemorySaver()

    # Publish the visible command menu (the "/" button in Telegram). Admin
    # commands are intentionally omitted — they are not user-facing.
    try:
        await app.bot.set_my_commands([
            BotCommand("menu", "Главное меню"),
            BotCommand("help", "Справка по командам"),
            BotCommand("next", "Когда следующий дайджест"),
            BotCommand("clear", "Очистить диалог с ассистентом"),
            BotCommand("buy", "Оформить подписку"),
        ])
    except Exception as e:
        logger.warning("set_my_commands failed (non-fatal): %s", e)

    logger.info("DB ready (supabase-py), checkpointer ready (MemorySaver)")


async def _post_shutdown(app: Application) -> None:
    """Close DB pool on shutdown."""
    await db.close_pool()
    logger.info("DB connections closed")


def main():
    req = HTTPXRequest(
        proxy=HTTPS_PROXY,
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    get_updates_req = HTTPXRequest(
        proxy=HTTPS_PROXY,
        connect_timeout=10.0,
        read_timeout=40.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    logger.info("[transport] proxy=%s read_to=20s get_updates_read_to=40s",
                _mask_proxy(HTTPS_PROXY))

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(get_updates_req)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Single multi-tenant path. The group=-1 resolve_user middleware runs for
    # EVERY update: it invite-gates, attaches the resolved users row, and (for
    # messages) dispatches into the handlers/ package — text, menu buttons,
    # /start, onboarding, and utility commands (/menu /help /next /clear;
    # /in is admin-only)
    # are all owned there. The owner is a normal users row, so they flow through
    # the same path. Callbacks (onb|/buy|/s|/h|/ci_) fall through to their
    # dedicated CallbackQueryHandlers; payment + admin COMMANDS fall through to
    # their CommandHandlers (see _FALLTHROUGH_COMMANDS in handlers/middleware).
    from handlers.middleware import resolve_user
    from handlers import onboarding as onboarding_surface
    from handlers import subscription as subscription_surface
    from handlers import admin as admin_surface
    from handlers import settings as settings_surface
    from handlers import history as history_surface
    from handlers.checkin import cb_checkin as checkin_cb

    app.add_handler(TypeHandler(Update, resolve_user), group=-1)

    # Callback surfaces.
    app.add_handler(CallbackQueryHandler(onboarding_surface.cb, pattern=r"^onb\|"))
    app.add_handler(CallbackQueryHandler(subscription_surface.cb_buy, pattern=r"^buy\|"))
    app.add_handler(CallbackQueryHandler(settings_surface.cb, pattern=r"^s\|"))
    app.add_handler(CallbackQueryHandler(history_surface.cb, pattern=r"^h\|"))
    app.add_handler(CallbackQueryHandler(checkin_cb, pattern=r"^ci_"))

    # --- payments (Stars) ---
    app.add_handler(CommandHandler("buy", subscription_surface.cmd_buy))
    app.add_handler(PreCheckoutQueryHandler(subscription_surface.pre_checkout))
    app.add_handler(MessageHandler(
        filters.SUCCESSFUL_PAYMENT, subscription_surface.successful_payment
    ))
    # --- admin (ADMIN_ID-gated; non-admins silently ignored) ---
    app.add_handler(CommandHandler("give_pro", admin_surface.cmd_give_pro))
    app.add_handler(CommandHandler("revoke_pro", admin_surface.cmd_revoke_pro))
    app.add_handler(CommandHandler("grant_trial", admin_surface.cmd_grant_trial))
    app.add_handler(CommandHandler("reset_user", admin_surface.cmd_reset_user))
    app.add_handler(CommandHandler("stats", admin_surface.cmd_stats))

    app.add_error_handler(error_handler)

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)) and not isinstance(update, Update):
        now = asyncio.get_event_loop().time()
        _polling_blips.append(now)
        cutoff = now - _BLIP_WINDOW_SEC
        while _polling_blips and _polling_blips[0] < cutoff:
            _polling_blips.pop(0)
        count = len(_polling_blips)
        logger.info("[polling] transient %s (blip #%d in last %ds), PTB will retry",
                    type(err).__name__, count, int(_BLIP_WINDOW_SEC))
        if count >= _BLIP_WARN_THRESHOLD:
            logger.warning("[polling] proxy unstable: %d blips in %ds — see %s",
                           count, int(_BLIP_WINDOW_SEC), _mask_proxy(HTTPS_PROXY))
        return

    logger.error("Unhandled exception", exc_info=err)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Что-то пошло не так. Попробуй ещё раз.",
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
