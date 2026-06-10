"""Surface 7 — 💎 Подписка / paywall (stub; invoices land in B3).

Shows trial/sub status from subscriptions.py and the post-trial gate message.
NO Telegram Stars invoice is built here yet — the buy buttons and PreCheckout /
successful_payment handlers are B3. Clear TODO seams mark where they attach.

User-facing strings are Russian per SPEC-ux §3. Prices shown in the buy screen
are resolved from DB tier defaults at render time (never inline constants).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
import subscriptions

logger = logging.getLogger(__name__)


# TODO(B3): replace these no-op buy buttons with callback_data="buy|digest_pro_month"
# / "buy|digest_pro_quarter" wired to the Stars invoice flow (PreCheckoutQuery +
# successful_payment handlers live in this module). For now they answer with an
# info popup so the surface is reachable and shows the plan without charging.
async def _buy_keyboard() -> InlineKeyboardMarkup:
    """Buy block. Reads star prices from the DB pro-tier defaults (no constants).
    Buttons are inert in this stub (callback buy|soon)."""
    price_month = await db.get_tier_default("pro", "price_month_stars", "—")
    price_quarter = await db.get_tier_default("pro", "price_quarter_stars", "—")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Месяц · {price_month}⭐", callback_data="buy|soon")],
        [InlineKeyboardButton(f"Квартал · {price_quarter}⭐", callback_data="buy|soon")],
        [InlineKeyboardButton("ℹ️ Чем отличаются планы", callback_data="buy|info_tiers")],
        [InlineKeyboardButton("ℹ️ Как платить через Wallet", callback_data="buy|info_wallet")],
    ])


async def _buy_text() -> str:
    price_month = await db.get_tier_default("pro", "price_month_stars", "—")
    price_quarter = await db.get_tier_default("pro", "price_quarter_stars", "—")
    return (
        "💎 <b>Оформить подписку</b>\n\n"
        "Pro — всё, что нужно для ежедневного дайджеста:\n"
        "• до 15 каналов • кастомный фокус • история без лимита\n\n"
        f"▸ Месяц — {price_month}⭐\n"
        f"▸ Квартал — {price_quarter}⭐  (выгоднее)\n\n"
        "💡 Дешевле всего через Telegram Wallet / TON — там нет наценки\n"
        "   App Store. Через iOS-приложение Stars дороже на ~30%."
    )


async def show_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """💎 Подписка entry. Branches by state: active trial/sub status, else buy."""
    user_row = context.user_data.get("user")
    tg_user_id = user_row["tg_user_id"] if user_row else update.effective_user.id

    active = await subscriptions.is_subscription_active(tg_user_id)
    until = await subscriptions.active_until(tg_user_id)

    message = update.effective_message
    if active and until:
        from datetime import datetime, timezone

        days_left = max(0, (until - datetime.now(timezone.utc)).days)
        header = (
            "💎 <b>Подписка</b>\n\n"
            "Статус: Pro-триал 🎁\n"
            f"Осталось: {days_left} дн. (до {until.strftime('%Y-%m-%d')})\n\n"
            "Всё открыто: 15 каналов, кастомный фокус, расширенная история.\n"
            "Когда триал закончится — выбери план, чтобы не потерять доступ."
        )
        await message.reply_text(
            header, parse_mode="HTML", reply_markup=await _buy_keyboard()
        )
        return

    # Inactive / expired → buy screen straight away.
    await message.reply_text(
        await _buy_text(), parse_mode="HTML", reply_markup=await _buy_keyboard()
    )


async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post-trial unpaid gate (SPEC-ux §3.5). Reachable from the @requires_tier
    decorator when a non-owner's trial/sub is inactive. Text only here — buy
    buttons are shown so the surface is the unlock path."""
    text = (
        "🔒 <b>Pro-триал закончился</b>\n\n"
        "Чтобы снова получать ежедневный дайджест, оформи подписку 👇\n"
        "(твои каналы и фокус сохранены)"
    )
    kb = await _buy_keyboard()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            text, parse_mode="HTML", reply_markup=kb
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=kb
        )


async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the buy| callbacks. In this stub the purchase buttons are inert —
    they explain that payments arrive next; the info popups are live."""
    q = update.callback_query
    arg = q.data.split("|", 1)[1] if "|" in q.data else ""
    if arg == "info_wallet":
        await q.answer(
            "Открой @wallet или TON Space, купи Stars там и оплати — цена без "
            "наценки App Store. На iOS Stars дороже, нам приходит столько же.",
            show_alert=True,
        )
    elif arg == "info_tiers":
        await q.answer(
            "Pro — до 15 каналов, кастомный фокус, история без лимита. "
            "Power (позже) — без лимита каналов, до 3 дайджестов, приватные каналы.",
            show_alert=True,
        )
    else:
        # TODO(B3): build the Telegram Stars invoice (currency XTR, payload encodes
        # tg_user_id + plan) here instead of this placeholder.
        await q.answer("Оплата подключается в ближайшем обновлении 🙌", show_alert=True)
