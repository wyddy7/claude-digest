"""Surface 7 — 💎 Подписка / paywall + Telegram Stars purchase flow.

Shows trial/sub status from subscriptions.py and the post-trial gate, then the
two-product buy block (monthly + quarterly). Tapping a buy button builds a
Telegram Stars invoice (currency XTR, provider_token="") whose payload is the
product key the success handler switches on. PreCheckout answers unconditionally;
the SuccessfulPayment handler applies an idempotent grant (see SPEC-payments §3).

User-facing strings are Russian per SPEC-ux §3. Star prices/anchors render from
DB tier defaults at build time (never inline constants). The only literals here
are payload identifiers and the XTR currency code.
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import ContextTypes

import db
import subscriptions
from handlers.strings import (
    SUB_BUY_BODY_HEADER,
    SUB_BUY_WALLET_TIP,
    SUB_GATE_EXPIRED,
    SUB_PAYMENT_DUPLICATE,
    SUB_PAYMENT_GRANTED,
    SUB_TRIAL_HEADER_TEMPLATE,
)

logger = logging.getLogger(__name__)


# ─── buy block (prices + anchors read from DB tier_defaults) ──────────────────

async def _buy_keyboard() -> InlineKeyboardMarkup:
    """Buy block. Star prices come from the DB pro-tier defaults (no constants).
    Buttons carry the product-key callbacks the invoice flow switches on."""
    price_month = await db.get_tier_default("pro", "price_month_stars", "—")
    price_quarter = await db.get_tier_default("pro", "price_quarter_stars", "—")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💎 Pro — месяц · {price_month}⭐",
            callback_data="buy|digest_pro_month",
        )],
        [InlineKeyboardButton(
            f"💎 Pro — 3 месяца · {price_quarter}⭐",
            callback_data="buy|digest_pro_quarter",
        )],
        [InlineKeyboardButton("ℹ️ Чем отличаются планы", callback_data="buy|info_tiers")],
        [InlineKeyboardButton("ℹ️ Как платить через Wallet", callback_data="buy|info_wallet")],
    ])


async def _buy_text() -> str:
    price_month = await db.get_tier_default("pro", "price_month_stars", "—")
    price_quarter = await db.get_tier_default("pro", "price_quarter_stars", "—")
    anchor_month = await db.get_tier_default("pro", "price_anchor_month_stars", None)
    anchor_quarter = await db.get_tier_default("pro", "price_anchor_quarter_stars", None)
    month_line = f"▸ Месяц — {price_month}⭐"
    if anchor_month:
        month_line += f"  (~~{anchor_month}~~)"
    quarter_line = f"▸ Квартал — {price_quarter}⭐  (выгоднее)"
    if anchor_quarter:
        quarter_line += f"  (~~{anchor_quarter}~~)"
    return (
        SUB_BUY_BODY_HEADER
        + f"{month_line}\n"
        + f"{quarter_line}\n\n"
        + SUB_BUY_WALLET_TIP
    )


# ─── status / gate surfaces ───────────────────────────────────────────────────

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
        header = SUB_TRIAL_HEADER_TEMPLATE.format(
            days_left=days_left,
            until_date=until.strftime("%Y-%m-%d"),
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
    decorator when a non-owner's trial/sub is inactive. Buy buttons are shown so
    the surface is the unlock path."""
    text = SUB_GATE_EXPIRED
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


# ─── invoice trigger (💎 buttons + /buy) ──────────────────────────────────────

async def send_pro_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           product_key: str) -> None:
    """Build + send a Telegram Stars invoice for product_key. Price and length
    come from DB tier defaults (subscriptions.product_invoice_fields). Stars flags:
    provider_token="" (empty, not None) + currency="XTR". payload == product_key —
    the success handler switches on it (never parses the amount)."""
    spec = subscriptions.PRODUCTS.get(product_key)
    if spec is None:
        logger.warning("send_pro_invoice: unknown product_key %r", product_key)
        return
    tg_user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    price_stars, _days, title, description = await subscriptions.product_invoice_fields(
        tg_user_id, spec["tier"], spec["period"]
    )

    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=product_key,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price_stars)],
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/buy [month|quarter] — invoice fallback. No arg → monthly. Unknown arg →
    list the two valid options."""
    args = context.args or []
    arg = args[0].lower() if args else "month"
    mapping = {"month": "digest_pro_month", "quarter": "digest_pro_quarter"}
    product_key = mapping.get(arg)
    if product_key is None:
        await update.effective_message.reply_text(
            "Доступно: /buy month или /buy quarter"
        )
        return
    await send_pro_invoice(update, context, product_key)


async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the buy| callbacks. Product-key callbacks open a Stars invoice; the
    info callbacks answer with a popup (no new screen)."""
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
    elif arg in subscriptions.PRODUCTS:
        await q.answer()
        await send_pro_invoice(update, context, arg)
    else:
        await q.answer()


# ─── Stars payment handlers ───────────────────────────────────────────────────

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer the pre-checkout query unconditionally — a subscription has no stock
    to verify, and the 10s Telegram window must be met."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply an idempotent grant on SuccessfulPayment and ACK the user either way.

    The ledger-insert-first / grant-second / rollback-on-failure ordering lives in
    subscriptions.apply_successful_payment (SPEC-payments §3). A duplicate delivery
    re-acks without a second grant; a hard DB failure propagates to error_handler
    so the un-ledgered charge retries cleanly on the next delivery."""
    sp = update.message.successful_payment
    tg_user_id = update.effective_user.id

    result = await subscriptions.apply_successful_payment(
        tg_user_id=tg_user_id,
        payload=sp.invoice_payload,
        telegram_payment_charge_id=sp.telegram_payment_charge_id,
        total_amount=sp.total_amount,
    )

    if result.granted:
        await update.message.reply_text(
            SUB_PAYMENT_GRANTED.format(active_until=result.active_until_human)
        )
    else:
        await update.message.reply_text(
            SUB_PAYMENT_DUPLICATE.format(active_until=result.active_until_human)
        )
