"""Surface 8 — admin grants. Gated by the ADMIN_ID env var (numeric Telegram id).

Commands (admin only; non-admins are silently ignored so the surface is invisible):
  /give_pro <tg_user_id> <days>  — comp Pro without payment (stacks, same path as
                                    a real payment, for testing the tier flip).
  /revoke_pro <tg_user_id>       — clear pro_until AND trial_ends_at (keeps
                                    trial_used so it does not re-arm a free trial).
  /grant_trial <tg_user_id>      — seed an invited users row (SPEC-ux §1.1 invite
                                    flow); the target's first /start runs onboarding
                                    and grants the trial.
  /reset_user <tg_user_id> [full] — re-arm onboarding without revoking invite or
                                    trial_used. Optional 'full' deletes both
                                    users + user_settings rows entirely.

Arguments are NUMERIC tg_user_ids only — never a @username. All limits/dates come
from subscriptions.py (DB tier defaults), never Python constants.
"""

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import db
import subscriptions
from handlers.strings import (
    ADMIN_RESET_USER_FULL_OK,
    ADMIN_RESET_USER_NOT_FOUND,
    ADMIN_RESET_USER_OK,
    ADMIN_RESET_USER_USAGE,
)

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


def _is_admin(update: Update) -> bool:
    return bool(
        ADMIN_ID
        and update.effective_user
        and update.effective_user.id == ADMIN_ID
    )


async def cmd_give_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/give_pro <tg_user_id> <days> — comp Pro. Stacks on any active remainder."""
    if not _is_admin(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit():
        await update.message.reply_text("Использование: /give_pro <tg_user_id> <days>")
        return
    target = int(args[0])
    days = int(args[1])
    # Provision a row defensively so /give_pro works on a not-yet-onboarded id.
    await db.get_or_create_user(target)
    new_until = await subscriptions.update_subscription(target, days)
    await update.message.reply_text(
        f"✅ Выдал Pro юзеру {target} на {days} дн. (до {new_until:%Y-%m-%d})."
    )
    try:
        await context.bot.send_message(target, f"🎁 Тебе выдан Pro на {days} дн.")
    except Exception:
        pass  # no chat yet / blocked — best-effort notify only


async def cmd_revoke_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/revoke_pro <tg_user_id> — clear pro_until AND trial_ends_at."""
    if not _is_admin(update):
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /revoke_pro <tg_user_id>")
        return
    target = int(args[0])
    await subscriptions.revoke_subscription(target)
    await update.message.reply_text(f"✅ Pro и Trial отозваны у юзера {target}.")


async def cmd_reset_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset_user <tg_user_id> [full]

    Soft reset (no 'full' arg): resets onboarding_state → 'invited' and clears
    user_settings.channels + current_focus so the target's next /start re-runs the
    onboarding wizard from scratch. Does NOT revoke trial_used or pro_until — the
    user's subscription state is preserved.

    Full reset ('full' as second arg): deletes both the users AND user_settings
    rows entirely. The target becomes a stranger; a subsequent /grant_trial +
    /start re-invites them from zero.
    """
    if not _is_admin(update):
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(ADMIN_RESET_USER_USAGE)
        return
    target = int(args[0])
    full = len(args) >= 2 and args[1].lower() == "full"

    if full:
        deleted = await db.delete_user_rows(target)
        if not deleted:
            await update.message.reply_text(ADMIN_RESET_USER_NOT_FOUND.format(target=target))
            return
        await update.message.reply_text(ADMIN_RESET_USER_FULL_OK.format(target=target))
    else:
        reset = await db.reset_user_onboarding(target)
        if not reset:
            await update.message.reply_text(ADMIN_RESET_USER_NOT_FOUND.format(target=target))
            return
        await update.message.reply_text(ADMIN_RESET_USER_OK.format(target=target))


async def cmd_grant_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grant_trial <tg_user_id> — seed an invited users row (SPEC-ux §1.1). The
    target's first /start runs onboarding, which grants the 3-day Pro trial."""
    if not _is_admin(update):
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /grant_trial <tg_user_id>")
        return
    target = int(args[0])
    await db.get_or_create_user(target)
    # Mark the row 'invited' so the invite gate admits the target and their first
    # /start enters the onboarding wizard (which grants the trial idempotently).
    await db.update_user_fields(target, {"onboarding_state": "invited"})
    await update.message.reply_text(
        f"✅ Юзер {target} приглашён. Пусть нажмёт /start у бота."
    )
    try:
        await context.bot.send_message(
            target,
            "🎉 Тебе открыли доступ к персональному AI-дайджесту. Нажми /start.",
        )
    except Exception:
        pass
