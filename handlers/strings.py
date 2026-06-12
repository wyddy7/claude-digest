"""Single source of truth for all user-facing text in the multi-tenant handlers.

Import rule: every user-visible string (message templates, button labels, status
messages, paywall copy, onboarding copy) lives here. Handlers import by name —
never define an inline string that a user will read.

Exceptions (not moved here):
  - Short inline format strings that embed runtime values and are used only once
    (e.g. f"✅ выдал Pro юзеру {target} на {days} дн." in admin.py which is
    admin-only tooling, not a user surface).
  - tooltip/alert pop-up answers that are single-use (cb_buy info popups).
  - The buy-text assembler in subscription.py which builds the body dynamically
    from DB prices — the template skeletons remain there.

All wording is identical to the original; this is a mechanical extraction.
"""

# ── menu button labels (re-exported from handlers/menu.py as the canonical home) ──
# NOTE: menu.py still defines these; strings.py re-exports so callers that want
# only labels can import from a single module without pulling in telegram types.
from handlers.menu import (  # noqa: F401  re-export
    BTN_DIGEST,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_SETTINGS,
    BTN_SUBSCRIPTION,
    MENU_BUTTONS,
)

# ── middleware / invite gate ──────────────────────────────────────────────────
INVITE_ONLY = (
    "🔒 Бот работает по приглашению. "
    "Напиши владельцу, чтобы получить доступ."
)

# ── chat router ───────────────────────────────────────────────────────────────
SOON = (
    "Этот раздел скоро откроется здесь. "
    "Пока доступны 📰 Дайджест, 👤 Профиль и 💎 Подписка."
)
FALLBACK = "Не понял команду. Меню снизу 👇"

# ── onboarding ────────────────────────────────────────────────────────────────
ONBOARDING_WELCOME = (
    "Привет 👋 Это твой персональный AI-дайджест.\n\n"
    "Каждый день в 13:00 он собирает посты из выбранных каналов, "
    "отфильтровывает рекламу и шум, и присылает выжимку «по делу» — "
    "плюс блок «Лично тебе»: что из новостей касается твоей работы.\n\n"
    "Тебе активирован пробный доступ Pro на 3 дня. Настроим за 2 шага 👇"
)

ONBOARDING_FOCUS = (
    "Шаг 2 из 2 — Фокус (необязательно)\n\n"
    "Есть тема, на которой держать акцент? Я подниму такие посты выше.\n"
    "Можно пропустить — тогда приоритет по общей значимости."
)

ONBOARDING_MENU_READY = "Меню снизу 👇"

ONBOARDING_OWN_CHANNELS_PROMPT = (
    "Пришли юзернеймы каналов через пробел или с новой строки, без @.\n"
    "Пример: durov_russia data_secrets\n\n"
    "Или вернись к темам:"
)

ONBOARDING_FOCUS_OWN_PROMPT = "Напиши свой фокус одной фразой:"

ONBOARDING_CHANNELS_MIN_ERROR = "Выбери хотя бы одну тему или добавь свой канал 🙂"

ONBOARDING_PREVIEW_PRE = (
    "Готово ✅ Собираю первый дайджест по твоим каналам за последние 24 часа.\n"
    "Это займёт ~30–60 секунд…"
)
ONBOARDING_PREVIEW_CLOSE = (
    "Вот так это выглядит каждый день в 13:00 (МСК) 📰\n\n"
    "Сейчас у тебя Pro-триал — 3 дня всё открыто.\n"
    "Меню снизу 👇  • Каналы и модели — в ⚙️  • Подписка — в 💎"
)
ONBOARDING_PREVIEW_FAIL = (
    "⚠️ За последние 24 часа в выбранных каналах пусто или случилась ошибка. "
    "Каналы уже сохранены — основной дайджест придёт в 13:00, "
    "или жми 📰 в любой момент."
)

# ── digest surface ────────────────────────────────────────────────────────────
DIGEST_COLLECTING = "⏳ Собираю дайджест по твоим каналам…"
DIGEST_ERROR = "⚠️ Не удалось собрать дайджест — попробуй ещё раз чуть позже."

# ── subscription / paywall ────────────────────────────────────────────────────
SUB_GATE_EXPIRED = (
    "🔒 <b>Pro-триал закончился</b>\n\n"
    "Чтобы снова получать ежедневный дайджест, оформи подписку 👇\n"
    "(твои каналы и фокус сохранены)"
)

SUB_TRIAL_HEADER_TEMPLATE = (
    "💎 <b>Подписка</b>\n\n"
    "Статус: Pro-триал 🎁\n"
    "Осталось: {days_left} дн. (до {until_date})\n\n"
    "Всё открыто: 15 каналов, кастомный фокус, расширенная история.\n"
    "Когда триал закончится — выбери план, чтобы не потерять доступ."
)

SUB_BUY_BODY_HEADER = (
    "💎 <b>Оформить подписку</b>\n\n"
    "Pro — всё, что нужно для ежедневного дайджеста:\n"
    "• до 15 каналов • кастомный фокус • история без лимита\n\n"
)
SUB_BUY_WALLET_TIP = (
    "💡 Дешевле всего через Telegram Wallet / TON — там нет наценки\n"
    "   App Store. Через iOS-приложение Stars дороже на ~30%."
)

SUB_PAYMENT_GRANTED = (
    "🎉 Pro активирован.\n"
    "Подписка активна до {active_until}."
)
SUB_PAYMENT_DUPLICATE = (
    "Платёж уже учтён ✅\n"
    "Подписка активна до {active_until}."
)
