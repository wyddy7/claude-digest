"""Non-owner text router.

Dispatches the 6-button reply keyboard to its surface and owns the onboarding
free-text escape hatches. Surfaces not yet built out in this stage (📚 История,
⚙️ Настройки, 🎯 Фокус for multi-tenant) answer with a clear "coming soon"
placeholder so the menu is coherent and nothing crashes — they are wired in a
later stage. The owner's rich single-tenant router in bot.py is untouched.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import db
from handlers import digest as digest_surface
from handlers import onboarding as onboarding_surface
from handlers import profile as profile_surface
from handlers import subscription as subscription_surface
from handlers.menu import main_kb_saas
from handlers.strings import (
    BTN_DIGEST,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_SETTINGS,
    BTN_SUBSCRIPTION,
    FALLBACK,
    SOON,
)

logger = logging.getLogger(__name__)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry for non-owner text. Onboarding free-text is consumed first; then the
    menu buttons; then a polite fallback. The owner never reaches here (the
    middleware leaves owner updates for the legacy bot.py handlers)."""
    text = (update.message.text or "").strip()

    # 1) Onboarding escape-hatch free-text (channels / focus typing).
    if await onboarding_surface.handle_text(update, context):
        return

    # 2) Reply-keyboard buttons.
    if text == BTN_DIGEST:
        await digest_surface.send_digest(update, context)
        return
    if text == BTN_SUBSCRIPTION:
        await subscription_surface.show_subscription(update, context)
        return
    if text == BTN_PROFILE:
        await profile_surface.show_profile(update, context)
        return
    if text == BTN_HISTORY or text == BTN_SETTINGS or text.startswith("🎯"):
        await update.message.reply_text(SOON)
        return

    # 3) Fallback — re-show the menu so the user is never stuck.
    user = context.user_data.get("user")
    focus = ""
    if user:
        try:
            settings = await db.load_settings(user["id"])
            focus = settings.get("current_focus") or ""
        except Exception:
            focus = ""
    await update.message.reply_text(FALLBACK, reply_markup=main_kb_saas(focus))
