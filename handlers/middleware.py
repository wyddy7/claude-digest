"""Identity resolution, invite gate, and the @requires_tier decorator.

`resolve_user` is the single group=-1 middleware. For EVERY user (the owner
included — the owner is a normal `users` row seeded by db.ensure_owner_user) it:
  * looks up the `users` row by numeric tg_user_id and enforces the invite gate
    (no row -> "invite-only" reply + stop);
  * attaches the resolved row to context.user_data["user"] for downstream
    handlers and the @requires_tier decorator;
  * dispatches text/commands into the handlers/ package and stops, so there is
    exactly one multi-tenant code path.

No PTB business logic beyond routing lives here. Subscription activeness is
ALWAYS computed at runtime from pro_until/trial_ends_at via subscriptions.py —
never read off a stored tier string.
"""

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

import db
import subscriptions
from handlers.strings import INVITE_ONLY

logger = logging.getLogger(__name__)

# Keep the old name as an alias so any external references still work.
INVITE_ONLY_TEXT = INVITE_ONLY

# Commands that have their own CommandHandlers in bot.py (payments + admin).
# The middleware lets these fall through instead of routing them into the
# menu/chat dispatcher, so they reach the owner/admin like any other user.
_FALLTHROUGH_COMMANDS = ("/buy", "/give_pro", "/revoke_pro", "/grant_trial", "/reset_user")


async def resolve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """group=-1 middleware. Invite-gate + attach the users row to
    context.user_data["user"], then dispatch into the handlers/ package. The
    owner flows through the SAME path as everyone else (their seeded row carries
    onboarding_state='done' + pro, so they land on the menu, not the wizard)."""
    user = update.effective_user
    if not user:
        return  # service updates without a user — let them pass
    tg_user_id = user.id

    row = await db.get_user_by_tg_id(tg_user_id)
    if not row:
        # No invite row exists. Politely refuse and stop — no row is created.
        if update.callback_query:
            await update.callback_query.answer(INVITE_ONLY, show_alert=True)
        elif update.message:
            await update.message.reply_text(INVITE_ONLY)
        raise ApplicationHandlerStop

    context.user_data["user"] = row

    # Message/command dispatch happens here, then we stop. Callback updates
    # (onb|/buy|/s|/h|/ci_) fall through to their dedicated CallbackQueryHandlers,
    # so they are NOT dispatched here.
    if update.callback_query:
        return
    if update.message:
        from handlers import onboarding as onboarding_surface
        from handlers import chat as chat_surface

        # Payment + admin surfaces have their own CommandHandlers in bot.py — let
        # the Stars success service message and these commands fall through to
        # them instead of dispatching into the menu/chat router.
        msg_text_raw = update.message.text or ""
        if update.message.successful_payment or msg_text_raw.startswith(_FALLTHROUGH_COMMANDS):
            return

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

    On failure the single path is the paywall message (subscription surface),
    never a bare "no access". The owner's seeded row carries a far-future
    pro_until, so they pass this gate like any active pro user. Numeric caps
    (channel count, chat turns, history depth) are NOT checked here — they are
    read per-user at the call site via db.get_effective_limit.
    """

    def decorator(handler):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
