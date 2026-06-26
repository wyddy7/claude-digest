"""Surface: daily check-in fan-out + per-user callbacks.

`run_checkin_fanout` is called by the scheduler and fans out over all active
users in the DB: per user it reads current_focus from user_settings, builds the
check-in keyboard, and sends to the user's tg_user_id — gated on an active
subscription.

The three inline-keyboard callbacks (ci_yes / ci_no / ci_talk) are scoped to
the calling user:
- ci_yes  — acknowledges the digest.
- ci_no   — resends THAT user's last_digest from their own user_settings.
- ci_talk — enables the chat path (sets user_data state) so the next free-text
            message is routed to the conversational agent (C6 pattern).

All user-visible text is in handlers/strings.py.
No PTB imports in the scheduler-side helper (only telegram.Bot is needed there).
"""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import digest_bot.db as db
import digest_bot.subscriptions as subscriptions
from digest_bot.handlers.strings import (
    CHECKIN_BTN_NO,
    CHECKIN_BTN_TALK,
    CHECKIN_BTN_YES,
    CHECKIN_FOCUS_SUFFIX,
    CHECKIN_NO_EMPTY,
    CHECKIN_NO_PREFIX,
    CHECKIN_QUESTION,
    CHECKIN_TALK_PROMPT,
    CHECKIN_YES_ANSWER,
    CHECKIN_YES_BODY,
)

logger = logging.getLogger(__name__)


def _make_checkin_kb() -> InlineKeyboardMarkup:
    """Inline keyboard shared by every check-in message (owner and tenant)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(CHECKIN_BTN_YES, callback_data="ci_yes"),
        InlineKeyboardButton(CHECKIN_BTN_NO, callback_data="ci_no"),
        InlineKeyboardButton(CHECKIN_BTN_TALK, callback_data="ci_talk"),
    ]])


def _checkin_text(focus: str) -> str:
    """Build the check-in message body.  focus may be empty."""
    if focus:
        # The focus is already escaped at the call site if Markdown is used.
        suffix = CHECKIN_FOCUS_SUFFIX.format(focus=focus)
    else:
        suffix = ""
    return CHECKIN_QUESTION + suffix


async def send_checkin(bot, tg_user_id: int, focus: str = "") -> None:
    """Send a single check-in message to one user. Called by the fan-out loop."""
    from telegram.helpers import escape_markdown

    safe_focus = escape_markdown(focus, version=1) if focus else ""
    text = _checkin_text(safe_focus)
    await bot.send_message(
        tg_user_id,
        text,
        reply_markup=_make_checkin_kb(),
        parse_mode="Markdown",
    )


async def run_checkin_fanout(bot_token: str) -> None:
    """Fan-out check-in over all active users.

    Each user is gated on subscriptions.is_subscription_active — inactive users
    receive no check-in (and maybe_warn_expiry is called as a side-effect).
    Per-user failures are isolated so one bad tg_user_id cannot block others.

    The Bot instance is created here (one connection, one context manager) so
    this function can be called from the scheduler without sharing a Bot object
    with the polling loop.
    """
    users = await db.list_active_users()
    if not users:
        logger.info("checkin fan-out: no active users — skipped")
        return

    logger.info("checkin fan-out: %d active user(s)", len(users))
    async with Bot(bot_token) as bot:
        for user in users:
            tg_user_id = user["tg_user_id"]
            user_id = user["id"]
            try:
                if not await subscriptions.is_subscription_active(tg_user_id):
                    await subscriptions.maybe_warn_expiry(tg_user_id, bot)
                    continue
                settings = await db.load_settings(user_id)
                focus = settings.get("current_focus") or ""
                await send_checkin(bot, tg_user_id, focus)
                logger.info("checkin fan-out: sent to %d", tg_user_id)
            except Exception as exc:
                # %r + exc_info so empty-message exceptions (e.g. httpx.ReadTimeout)
                # still record their type and traceback instead of a bare ': '.
                logger.warning(
                    "checkin fan-out: user %d failed (isolated): %r", tg_user_id, exc, exc_info=True
                )


# ─── PTB callback handlers ────────────────────────────────────────────────────


async def _safe_answer(query, text: str = "", show_alert: bool = False) -> None:
    """Answer a callback query, swallowing BadRequest (expired query)."""
    from telegram.error import BadRequest

    try:
        await query.answer(text, show_alert=show_alert)
    except BadRequest as exc:
        logger.warning("checkin callback answer failed (query expired): %s", exc)


async def cb_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ci_yes / ci_no / ci_talk callbacks for any user.

    The callback is routed here via pattern=r"^ci_" in bot.py.  The user-scoped
    state (last_digest, chat mode) is read from the per-user DB row resolved by
    the middleware (context.user_data["user"]).
    """
    from digest_bot.bot import DIGEST_HOUR, DIGEST_MINUTE  # schedule constants live in bot.py

    q = update.callback_query
    action = q.data

    # The middleware resolves the calling user into context.user_data["user"].
    user_row = context.user_data.get("user")

    if action == "ci_yes":
        await _safe_answer(q, CHECKIN_YES_ANSWER)
        await q.edit_message_text(
            CHECKIN_YES_BODY.format(hour=DIGEST_HOUR, minute=DIGEST_MINUTE)
        )

    elif action == "ci_no":
        await _safe_answer(q)
        # Fetch this user's last_digest from their settings row.
        if user_row:
            settings = await db.load_settings(user_row["id"])
            last_digest = settings.get("last_digest") or ""
        else:
            last_digest = ""

        if last_digest:
            await q.edit_message_text(CHECKIN_NO_PREFIX)
            await context.bot.send_message(
                q.message.chat_id,
                f"📰 <b>Дайджест</b>\n\n{last_digest}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await q.edit_message_text(CHECKIN_NO_EMPTY)

    elif action == "ci_talk":
        # No state flag needed: in the unified router any subsequent free text
        # already falls through to the chat-with-digest agent.
        await _safe_answer(q)
        await q.edit_message_text(CHECKIN_TALK_PROMPT)
