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
    FALLBACK,
)

logger = logging.getLogger(__name__)


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

    # 5) Fallback — re-show the menu so the user is never stuck.
    user = context.user_data.get("user")
    focus = ""
    if user:
        try:
            settings = await db.load_settings(user["id"])
            focus = settings.get("current_focus") or ""
        except Exception:
            focus = ""
    await update.message.reply_text(FALLBACK, reply_markup=main_kb_saas(focus))
