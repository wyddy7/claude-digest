"""Non-owner text router.

Dispatches the 6-button reply keyboard to its surface and owns the onboarding
free-text escape hatches. ⚙️ Настройки and 🎯 Фокус are wired to
handlers.settings; 📚 История is wired to handlers.history (paginated,
tenant-scoped). This is the single text router for EVERY user — the owner is a
normal users row and is dispatched here too (no legacy single-tenant path).

Utility commands (/next, /help, /stages, /in) are also handled here. They read
the caller's own DB row — never a global id=1 row.
"""

import logging
from datetime import datetime, timedelta
from html import escape

import pytz

from telegram import Update
from telegram.ext import ContextTypes

import db
# Schedule is single-sourced in bot.py (same import checkin.py uses) — never
# re-hardcode the hours here.
from bot import (
    DIGEST_HOUR as _DIGEST_HOUR,
    DIGEST_MINUTE as _DIGEST_MINUTE,
    CHECKIN_HOUR as _CHECKIN_HOUR,
    CHECKIN_MINUTE as _CHECKIN_MINUTE,
)
from agent import run_chat_turn, clear_chat_thread
from handlers.middleware import _effective_tier_active
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
    CHAT_CLEARED,
    CHAT_ERROR,
    CHAT_LIMIT_HIT,
    CHAT_THINKING,
    FALLBACK,
    ONBOARDING_MENU_READY as MENU_READY,
)

logger = logging.getLogger(__name__)

_MOSCOW = pytz.timezone("Europe/Moscow")

# DB fallback if chat_turns_per_month is somehow absent from tier_defaults.
_CHAT_TURNS_FALLBACK = 50


async def _cmd_next(update: Update) -> None:
    """Tenant-safe /next: shows schedule timing without DB access."""
    now = datetime.now(_MOSCOW)

    def _next_time(hour: int, minute: int) -> datetime:
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t

    def _fmt(t: datetime) -> str:
        diff = t - now
        total_min = int(diff.total_seconds() // 60)
        h, m = divmod(total_min, 60)
        label = "сегодня" if t.date() == now.date() else "завтра"
        when = f"через {h}ч {m}м" if h else f"через {m}м"
        return f"{label} в {t.strftime('%H:%M')} МСК ({when})"

    await update.message.reply_text(
        f"📅 Дайджест: {_fmt(_next_time(_DIGEST_HOUR, _DIGEST_MINUTE))}\n"
        f"💬 Чекин: {_fmt(_next_time(_CHECKIN_HOUR, _CHECKIN_MINUTE))}"
    )


async def _cmd_help(update: Update) -> None:
    """Tenant-safe /help."""
    await update.message.reply_text(
        "*Команды*\n\n"
        "/help — это сообщение\n"
        "/next — когда следующий дайджест и чекин\n"
        "/in `<минуты>` — запустить дайджест через N минут (1–60)\n"
        "/clear — очистить историю диалога с ассистентом\n\n"
        "*Кнопки*\n\n"
        "📰 *Дайджест* — запустить сейчас\n"
        "📚 *История* — предыдущие дайджесты\n"
        "👤 *Профиль* — профиль, модель, каналы\n"
        "⚙️ *Настройки* — выбор модели, управление каналами, авто-сброс фокуса\n"
        "🎯 *Фокус* — задать приоритет для следующего дайджеста\n\n"
        "*Расписание*\n\n"
        f"• {_DIGEST_HOUR:02d}:{_DIGEST_MINUTE:02d} МСК — автодайджест\n"
        f"• {_CHECKIN_HOUR:02d}:{_CHECKIN_MINUTE:02d} МСК — чекин",
        parse_mode="Markdown",
    )


async def _cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear — wipe the caller's chat-with-digest conversation memory so the next
    message starts a fresh thread. Free (no LLM, no subscription gate)."""
    tg_user_id = update.effective_user.id if update.effective_user else None
    checkpointer = context.application.bot_data.get("checkpointer")
    if checkpointer is not None and tg_user_id is not None:
        try:
            await clear_chat_thread(checkpointer, tg_user_id)
        except Exception as e:
            logger.warning("/clear failed for %s: %s", tg_user_id, e)
    await update.message.reply_text(CHAT_CLEARED)


async def _cmd_stages(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict) -> None:
    """Tenant-safe /stages: reads the caller's own settings row, not global state."""
    from pipeline_config import build_registry_from_state, describe_registry
    from personalization import load_personalization

    user_id = user["id"]
    settings = await db.load_settings(user_id)
    cfg_data = {
        "channels": settings.get("channels") or [],
        "current_focus": settings.get("current_focus") or "",
        "model": settings.get("model") or db.DEFAULT_MODEL,
    }
    cfg_yaml = await db.load_personalization_db(user_id) or load_personalization()
    registry = build_registry_from_state(cfg_data, cfg_yaml)
    text = "🧩 <b>Модели по этапам пайплайна</b>\n\n" + escape(describe_registry(registry))
    await update.message.reply_text(text, parse_mode="HTML")


async def _require_active(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict) -> bool:
    """Gate the LLM-spend surfaces (chat, /in) on an active trial/subscription.
    Returns True if allowed; otherwise shows the paywall and returns False. The
    📰 button is gated by @requires_tier — this is the same gate for the surfaces
    that aren't decorated handlers, so an expired user can't burn tokens."""
    if _effective_tier_active(user):
        return True
    await subscription_surface.show_gate(update, context)
    return False


async def _cmd_in(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict) -> None:
    """Tenant-safe /in <minutes>: schedules a per-user digest via job_queue."""
    if not await _require_active(update, context, user):
        return
    raw = (update.message.text or "").strip()
    parts = raw.split()
    args = parts[1:] if len(parts) > 1 else []

    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /in <минуты>\nПример: /in 2")
        return
    minutes = int(args[0])
    if minutes < 1 or minutes > 60:
        await update.message.reply_text("Укажи от 1 до 60 минут.")
        return

    fire_at = datetime.now(_MOSCOW) + timedelta(minutes=minutes)
    time_str = fire_at.strftime("%H:%M МСК")
    tg_user_id = user["tg_user_id"]

    async def _job(ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await digest_surface.deliver_digest(ctx.bot, user)
        except Exception as exc:
            logger.warning("scheduled /in digest for %s failed: %s", tg_user_id, exc)

    context.job_queue.run_once(_job, when=minutes * 60, name=f"scheduled_digest_{tg_user_id}_{minutes}m")
    await update.message.reply_text(f"⏰ Дайджест запланирован через {minutes} мин (в {time_str})")


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry for ALL user text (owner included — they are a normal users row).
    Onboarding free-text is consumed first; then settings sub-states; then
    utility commands; then the menu buttons; then the chat agent / a polite
    fallback."""
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

    # 4) Utility commands (tenant-safe: read caller's row, not global state).
    user = context.user_data.get("user")
    if text in ("/menu", "/menu@DigestBot"):
        focus = ""
        if user:
            try:
                s = await db.load_settings(user["id"])
                focus = s.get("current_focus") or ""
            except Exception:
                focus = ""
        await update.message.reply_text(MENU_READY, reply_markup=main_kb_saas(focus))
        return
    if text in ("/next", "/next@DigestBot"):
        await _cmd_next(update)
        return
    if text in ("/help", "/help@DigestBot"):
        await _cmd_help(update)
        return
    if text in ("/clear", "/clear@DigestBot"):
        await _cmd_clear(update, context)
        return
    if text.startswith("/stages") and user:
        await _cmd_stages(update, context, user)
        return
    if text.startswith("/in") and user:
        await _cmd_in(update, context, user)
        return

    # 5) Reply-keyboard buttons.
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

    # 6) Free-text → chat-with-digest agent (per-user thread + user-scoped tools).
    if user:
        await _chat_with_digest(update, context, user)
        return

    # No resolved user (defensive — middleware normally guarantees one). Re-show
    # the menu so the user is never stuck.
    await update.message.reply_text(FALLBACK, reply_markup=main_kb_saas(""))


async def _chat_with_digest(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict) -> None:
    """Route a free-text message to the conversational agent for THIS user.

    The agent runs on a MemorySaver thread keyed by the user's tg id (so tenants
    never share conversation state) and with tools scoped to the user's own
    digests/focus. The chat_turns_per_month quota is enforced from the DB
    (get_effective_limit) before any LLM spend; over-limit users get a friendly
    capped reply and the agent is never invoked.
    """
    user_id = user["id"]
    tg_user_id = user["tg_user_id"]
    text = (update.message.text or "").strip()

    # Subscription gate FIRST — an expired/unpaid user must not reach the LLM.
    if not await _require_active(update, context, user):
        return

    # Quota gate — limit from DB, current usage from the per-user monthly counter.
    cap = await db.get_effective_limit(user_id, "chat_turns_per_month", _CHAT_TURNS_FALLBACK)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = _CHAT_TURNS_FALLBACK
    used = await db.count_chat_turns_this_month(user_id)
    if cap >= 0 and used >= cap:
        await update.message.reply_text(CHAT_LIMIT_HIT.format(cap=cap))
        return

    checkpointer = context.application.bot_data.get("checkpointer")
    if checkpointer is None:
        logger.warning("chat agent: no checkpointer in bot_data; cannot run turn")
        await update.message.reply_text(CHAT_ERROR)
        return

    status_msg = await update.message.reply_text(CHAT_THINKING)
    try:
        reply = await run_chat_turn(
            tg_user_id, text, checkpointer, scope_user_id=user_id
        )
        # Count the turn only after a real invocation (gate failures don't burn quota).
        try:
            await db.record_chat_turn(user_id)
        except Exception as exc:
            logger.warning("record_chat_turn failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("chat agent turn failed for %s: %s", tg_user_id, exc)
        try:
            await status_msg.edit_text(CHAT_ERROR)
        except Exception:
            await update.message.reply_text(CHAT_ERROR)
        return

    try:
        await status_msg.edit_text(reply)
    except Exception:
        # Edit failed (e.g. message too old) — send a fresh plain message so the
        # user still gets the answer. Plain text matches the legacy owner path.
        await update.message.reply_text(reply)
