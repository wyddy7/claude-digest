"""Identity resolution, invite gate, and the @requires_tier decorator.

`resolve_user` replaces the legacy single-owner `check_owner` TypeHandler. It:
  * always lets the owner (OWNER_ID) through to the legacy single-tenant path
    in bot.py — that path is unchanged;
  * for every other user, looks up the `users` row by numeric tg_user_id and
    enforces the invite gate (no row -> "invite-only" reply + stop);
  * attaches the resolved row to context.user_data["user"] for downstream
    handlers and the @requires_tier decorator.

No PTB business logic beyond routing lives here. Subscription activeness is
ALWAYS computed at runtime from pro_until/trial_ends_at via subscriptions.py —
never read off a stored tier string.
"""

import logging
import os

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

import db
import subscriptions

logger = logging.getLogger(__name__)

OWNER_ID = int(os.getenv("CHAT_ID", "0"))

INVITE_ONLY_TEXT = (
    "🔒 Бот работает по приглашению. "
    "Напиши владельцу, чтобы получить доступ."
)


def is_owner(tg_user_id: int) -> bool:
    """The owner keeps the legacy single-tenant path and bypasses all gating."""
    return OWNER_ID != 0 and tg_user_id == OWNER_ID


async def resolve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """group=-1 middleware. Owner -> fall through to legacy handlers. Non-owner ->
    invite-gate + attach the users row to context.user_data["user"]."""
    user = update.effective_user
    if not user:
        return  # service updates without a user — let them pass
    tg_user_id = user.id

    if is_owner(tg_user_id):
        context.user_data["is_owner"] = True
        return  # legacy owner path in bot.py handles everything

    context.user_data["is_owner"] = False
    row = await db.get_user_by_tg_id(tg_user_id)
    if not row:
        # No invite row exists. Politely refuse and stop — no row is created.
        if update.callback_query:
            await update.callback_query.answer(INVITE_ONLY_TEXT, show_alert=True)
        elif update.message:
            await update.message.reply_text(INVITE_ONLY_TEXT)
        raise ApplicationHandlerStop

    context.user_data["user"] = row

    # Non-owner message/command dispatch happens here, then we stop so the legacy
    # owner handlers in bot.py never see this update. Callback updates (onb|/buy|)
    # fall through to their dedicated CallbackQueryHandlers (patterns the owner UI
    # never emits), so they are NOT dispatched here.
    if update.callback_query:
        return
    if update.message:
        from handlers import onboarding as onboarding_surface
        from handlers import chat as chat_surface

        msg_text = update.message.text or ""
        state = row.get("onboarding_state") or "new"
        if msg_text.startswith("/start") or state not in {"done", "active"}:
            # Wizard owns everything until onboarding completes (and /start always).
            if msg_text.startswith("/start"):
                await onboarding_surface.start(update, context)
            elif context.user_data.get("onb_substate"):
                await chat_surface.route_text(update, context)
            else:
                await onboarding_surface.start(update, context)
        else:
            await chat_surface.route_text(update, context)
        raise ApplicationHandlerStop


def _effective_tier_active(user_row: dict) -> bool:
    """Whether the user currently has trial-or-paid access. Computed from the row
    fields the same way subscriptions.is_subscription_active does, but synchronously
    off an already-loaded row (avoids a second DB read inside the decorator)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for key in ("pro_until", "trial_ends_at"):
        ts = subscriptions._parse_ts(user_row.get(key))
        if ts and ts > now:
            return True
    return False


def requires_tier(level: str):
    """Decorator gating a handler on subscription eligibility.

    level: "trial_or_paid" — any active trial OR paid subscription.
           "pro" / "power"  — reserved; treated as trial_or_paid for the MVP
                              (the single paid tier is `pro`; finer feature gates
                              are enforced at the call site via get_effective_limit,
                              not here).

    The owner always bypasses. On failure the single path is the paywall message
    (subscription surface), never a bare "no access". Numeric caps (channel count,
    chat turns, history depth) are NOT checked here — they are read per-user at the
    call site via db.get_effective_limit.
    """

    def decorator(handler):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if context.user_data.get("is_owner"):
                return await handler(update, context)

            user_row = context.user_data.get("user")
            if user_row and _effective_tier_active(user_row):
                return await handler(update, context)

            # Gated: route to the paywall (lazy import avoids a cycle).
            from handlers import subscription as subscription_surface

            await subscription_surface.show_gate(update, context)
            return None

        wrapper.__name__ = getattr(handler, "__name__", "wrapped")
        wrapper.__doc__ = handler.__doc__
        return wrapper

    return decorator
