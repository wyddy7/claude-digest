"""Non-owner text router.

Dispatches the 6-button reply keyboard to its surface and owns the onboarding
free-text escape hatches. ⚙️ Настройки and 🎯 Фокус are wired to
handlers.settings; 📚 История is wired to handlers.history (paginated,
tenant-scoped). The owner's rich single-tenant router in bot.py is untouched.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import db
from agent import run_chat_turn
from handlers import digest as digest_surface
from handlers import history as history_surface
from handlers import onboarding as onboarding_surface
from handlers import profile as profile_surface
from handlers import settings as settings_surface
from handlers import subscription as subscription_surface
from handlers.menu import main_kb_saas
from handlers.strings import (
    BTN_DIGEST,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_SETTINGS,
    BTN_SUBSCRIPTION,
    CHAT_ERROR,
    CHAT_LIMIT_HIT,
    CHAT_THINKING,
    FALLBACK,
)

logger = logging.getLogger(__name__)

# DB fallback if chat_turns_per_month is somehow absent from tier_defaults.
_CHAT_TURNS_FALLBACK = 50


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry for non-owner text. Onboarding free-text is consumed first; then
    settings sub-states; then the menu buttons; then a polite fallback. The
    owner never reaches here (the middleware leaves owner updates for the
    legacy bot.py handlers)."""
    text = (update.message.text or "").strip()

    # 1) Onboarding escape-hatch free-text (channels / focus typing).
    if await onboarding_surface.handle_text(update, context):
        return

    # 2) Settings free-text sub-states (editing_focus / adding_channel).
    if await settings_surface.handle_text(update, context):
        return

    # 3) /cancel clears any settings sub-state.
    if text == "/cancel":
        context.user_data.pop("settings_substate", None)
        user = context.user_data.get("user")
        focus = ""
        if user:
            try:
                s = await db.load_settings(user["id"])
                focus = s.get("current_focus") or ""
            except Exception:
                focus = ""
        await update.message.reply_text("Отменено.", reply_markup=main_kb_saas(focus))
        return

    # 4) Reply-keyboard buttons.
    if text == BTN_DIGEST:
        await digest_surface.send_digest(update, context)
        return
    if text == BTN_SUBSCRIPTION:
        await subscription_surface.show_subscription(update, context)
        return
    if text == BTN_PROFILE:
        await profile_surface.show_profile(update, context)
        return
    if text == BTN_SETTINGS:
        await settings_surface.show_settings(update, context)
        return
    if text.startswith("🎯"):
        await settings_surface.show_focus_prompt(update, context)
        return
    if text == BTN_HISTORY:
        await history_surface.show_history(update, context)
        return

    # 5) Free-text → chat-with-digest agent (per-user thread + user-scoped tools).
    user = context.user_data.get("user")
    if user:
        await _chat_with_digest(update, context, user)
        return

    # No resolved user (defensive — middleware normally guarantees one). Re-show
    # the menu so the user is never stuck.
    await update.message.reply_text(FALLBACK, reply_markup=main_kb_saas(""))


async def _chat_with_digest(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict) -> None:
    """Route a free-text message to the conversational agent for THIS user.

    The agent runs on a MemorySaver thread keyed by the user's tg id (so tenants
    never share conversation state) and with tools scoped to the user's own
    digests/focus. The chat_turns_per_month quota is enforced from the DB
    (get_effective_limit) before any LLM spend; over-limit users get a friendly
    capped reply and the agent is never invoked.
    """
    user_id = user["id"]
    tg_user_id = user["tg_user_id"]
    text = (update.message.text or "").strip()

    # Quota gate — limit from DB, current usage from the per-user monthly counter.
    cap = await db.get_effective_limit(user_id, "chat_turns_per_month", _CHAT_TURNS_FALLBACK)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = _CHAT_TURNS_FALLBACK
    used = await db.count_chat_turns_this_month(user_id)
    if cap >= 0 and used >= cap:
        await update.message.reply_text(CHAT_LIMIT_HIT.format(cap=cap))
        return

    checkpointer = context.application.bot_data.get("checkpointer")
    if checkpointer is None:
        logger.warning("chat agent: no checkpointer in bot_data; cannot run turn")
        await update.message.reply_text(CHAT_ERROR)
        return

    status_msg = await update.message.reply_text(CHAT_THINKING)
    try:
        reply = await run_chat_turn(
            tg_user_id, text, checkpointer, scope_user_id=user_id
        )
        # Count the turn only after a real invocation (gate failures don't burn quota).
        try:
            await db.record_chat_turn(user_id)
        except Exception as exc:
            logger.warning("record_chat_turn failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("chat agent turn failed for %s: %s", tg_user_id, exc)
        try:
            await status_msg.edit_text(CHAT_ERROR)
        except Exception:
            await update.message.reply_text(CHAT_ERROR)
        return

    try:
        await status_msg.edit_text(reply)
    except Exception:
        # Edit failed (e.g. message too old) — send a fresh plain message so the
        # user still gets the answer. Plain text matches the legacy owner path.
        await update.message.reply_text(reply)
