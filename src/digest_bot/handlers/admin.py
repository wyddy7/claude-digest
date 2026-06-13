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
from digest_bot.subscriptions.py (DB tier defaults), never Python constants.
"""

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import digest_bot.db as db
import digest_bot.subscriptions as subscriptions
from digest_bot.handlers.strings import (
    ADMIN_RESET_USER_FULL_OK,
    ADMIN_RESET_USER_NOT_FOUND,
    ADMIN_RESET_USER_OK,
    ADMIN_RESET_USER_USAGE,
)

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


def is_admin_id(tg_user_id) -> bool:
    """True iff the given Telegram id is the configured admin (ADMIN_ID env).
    Unset ADMIN_ID (0) → nobody is admin (fail-closed). Shared so other surfaces
    (e.g. the admin-only /in) gate the same way without duplicating the check."""
    return bool(ADMIN_ID) and str(tg_user_id) == str(ADMIN_ID)


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user) and is_admin_id(update.effective_user.id)


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


def _fmt_usd(x) -> str:
    """Compact USD: $0.044, $1.83, +$9.87 (sign kept for margin)."""
    try:
        v = float(x or 0.0)
    except (TypeError, ValueError):
        return "$0"
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    a = abs(v)
    body = f"{a:.4f}".rstrip("0").rstrip(".") if a < 1 else f"{a:.2f}"
    return f"{sign}${body}"


def _format_dashboard(d: dict) -> str:
    """Render the 4-tier dashboard dict (db.aggregate_stats) as a Telegram-sized
    text report. Pure formatting — tolerant of missing keys (zeros)."""
    e = d.get("economics", {})
    a = d.get("activation", {})
    g = d.get("engagement", {})
    p = d.get("product", {})
    win = d.get("window_days", 30)

    by_model = e.get("cost_by_model", {})
    models_line = ", ".join(f"{m.split('/')[-1]} {_fmt_usd(c)}" for m, c in list(by_model.items())[:5]) or "—"
    tiers = a.get("tier_counts", {})
    tiers_line = ", ".join(f"{t} {n}" for t, n in sorted(tiers.items())) or "—"

    L = [
        f"📊 <b>Статистика</b> · окно {win}д",
        "",
        "💰 <b>Юнит-экономика</b>",
        f"• Дайджестов: {e.get('digests', 0)}",
        f"• Себестоимость: {_fmt_usd(e.get('total_cost_usd'))} (⌀ {_fmt_usd(e.get('cost_per_digest_usd'))}/дайдж)",
        f"• ⌀ на юзера: {_fmt_usd(e.get('cost_per_user_usd'))}",
        f"• Выручка: {e.get('revenue_stars', 0)}⭐ ≈ {_fmt_usd(e.get('revenue_usd'))}",
        f"• <b>Маржа: {_fmt_usd(e.get('gross_margin_usd'))}</b> · платящих: {e.get('paying_users', 0)}",
        f"• По моделям: {models_line}",
        "",
        "📈 <b>Активация</b>",
        f"• Юзеров: {a.get('users_total', 0)} (новых за окно: {a.get('signups_in_window', 0)})",
        f"• Онбординг: {a.get('onboarded_in_window', 0)} · первый дайджест: {a.get('first_digest_users', 0)}",
        f"• DAU/WAU/MAU: {a.get('dau', 0)} / {a.get('wau', 0)} / {a.get('mau', 0)}",
        f"• Тиры: {tiers_line} (актив pro: {a.get('active_pro', 0)})",
        f"• Конверсия signup→paid: {a.get('signup_to_paid_pct', 0)}%",
        "",
        "💬 <b>Вовлечённость</b>",
        f"• Чат-тёрнов: {g.get('chat_turns', 0)} (юзеров {g.get('chat_users', 0)})",
        f"• Упёрлись в лимит: {g.get('quota_hits', 0)} (юзеров {g.get('quota_hit_users', 0)})",
        f"• Ошибки дайджестов: {g.get('digest_error_rate_pct', 0)}% ({g.get('digest_errors', 0)})",
        "",
        "🔧 <b>Продукт</b> (read_mode)",
    ]
    rm = p.get("read_mode_cost", {})
    if rm:
        for mode, v in rm.items():
            L.append(f"• {mode}: {v.get('digests', 0)} дайдж · ⌀ {_fmt_usd(v.get('avg_cost_usd'))}")
    else:
        L.append("• —")
    return "\n".join(L)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats [days] — admin 4-tier dashboard: unit economics (cost/digest, cost/
    user, Stars revenue, gross margin — USD straight from OpenRouter's usage.cost),
    activation (DAU/WAU/MAU, funnel, tier mix), engagement (quota hits, error rate),
    and product (read_mode cost delta). Default window 30d; `/stats 7` for 7d. Reads
    only usage_events + subscription_events + the users snapshot — no profile data."""
    if not _is_admin(update):
        return
    days = 30
    if context.args:
        try:
            days = max(1, min(365, int(context.args[0])))
        except (TypeError, ValueError):
            pass
    stats = await db.aggregate_stats(days)
    await update.message.reply_text(_format_dashboard(stats), parse_mode="HTML")
