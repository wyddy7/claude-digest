"""Shared main reply keyboard for the unified multi-tenant path.

6 buttons per SPEC-ux §1: 📰 📚 👤 ⚙️ 🎯 💎. Every user — the owner included —
gets this keyboard; there is no separate single-tenant keyboard after the cutover.
"""

from telegram import ReplyKeyboardMarkup

# Button label constants — the text router matches on these exact strings.
BTN_DIGEST = "📰 Дайджест"
BTN_HISTORY = "📚 История"
BTN_PROFILE = "👤 Профиль"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SUBSCRIPTION = "💎 Подписка"

MENU_BUTTONS = {BTN_DIGEST, BTN_HISTORY, BTN_PROFILE, BTN_SETTINGS, BTN_SUBSCRIPTION}


def main_kb_saas(focus: str = "") -> ReplyKeyboardMarkup:
    focus_btn = f"🎯 {focus[:28]}" if focus else "🎯 Задать фокус"
    return ReplyKeyboardMarkup(
        [
            [BTN_DIGEST, BTN_HISTORY],
            [BTN_PROFILE, BTN_SETTINGS],
            [focus_btn, BTN_SUBSCRIPTION],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
