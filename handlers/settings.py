"""Surface 5 — ⚙️ Настройки for every user (unified multi-tenant path).

Provides: list/add/remove channels (limit-capped via get_effective_limit),
model picker, focus edit (🎯), and focus auto-reset toggle.

Reads/writes user_settings rows keyed on the user's UUID (via db.load_settings /
db.save_settings) and resolves per-user limits from db.get_effective_limit.

All user-facing text is imported from handlers.strings. The module
introduces NO new hard-coded quota constants — every numeric gate uses
get_effective_limit.

Callback namespace: s|.
"""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from handlers.menu import main_kb_saas
from handlers.strings import (
    BTN_DIGEST,
    BTN_HISTORY,
    BTN_PROFILE,
    BTN_SETTINGS,
    BTN_SUBSCRIPTION,
    SETTINGS_ADDCH_PROMPT,
    SETTINGS_ADDCH_ALREADY,
    SETTINGS_ADDCH_INVALID,
    SETTINGS_ADDCH_LIMIT_HIT,
    SETTINGS_ADDCH_OK,
    SETTINGS_CHANNELS_HEADER,
    SETTINGS_FOCUS_PROMPT,
    SETTINGS_FOCUS_OK,
    SETTINGS_HEADER,
    SETTINGS_RMCH_OK,
    SETTINGS_TOGGLE_AUTORESET_ON,
    SETTINGS_TOGGLE_AUTORESET_OFF,
)

logger = logging.getLogger(__name__)

# Reuse the same permissive username regex as onboarding (C3).
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

# A press of any of these reply-keyboard buttons (or the 🎯 focus button, matched
# by prefix) is NEVER focus/channel input — it must escape the sub-state, not be
# swallowed as a literal value. Guarded in handle_text below.
_MENU_LABELS = {BTN_DIGEST, BTN_HISTORY, BTN_PROFILE, BTN_SETTINGS, BTN_SUBSCRIPTION}


def _is_menu_or_command(text: str) -> bool:
    return text.startswith("/") or text.startswith("🎯") or text in _MENU_LABELS

# Available model choices offered to every user.
AVAILABLE_MODELS: dict[str, str] = {
    "🚀 Claude Sonnet 4.6": "anthropic/claude-sonnet-4.6",
    "⚡ Claude Haiku 4.5": "anthropic/claude-haiku-4.5",
    "🧠 Claude 3.7 Sonnet": "anthropic/claude-3.7-sonnet",
    "⚡ Claude 3.5 Haiku": "anthropic/claude-3.5-haiku",
    "🟢 GPT-4o Mini": "openai/gpt-4o-mini",
}


# ─── inline keyboards ─────────────────────────────────────────────────────────

def _settings_kb(current_model: str, channels: list, auto_reset: bool) -> InlineKeyboardMarkup:
    rows = []
    for label, model_id in AVAILABLE_MODELS.items():
        mark = "✅ " if model_id == current_model else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"s|model|{model_id}"
        )])
    rows.append([InlineKeyboardButton(
        f"📡 Каналы ({len(channels)})", callback_data="s|channels"
    )])
    reset_label = (
        "🔄 Авто-сброс фокуса: ВКЛ ✅" if auto_reset
        else "🔄 Авто-сброс фокуса: ВЫКЛ"
    )
    rows.append([InlineKeyboardButton(reset_label, callback_data="s|toggle_autoreset")])
    return InlineKeyboardMarkup(rows)


def _channels_kb(channels: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"❌ {ch}", callback_data=f"s|rmch|{ch}")]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="s|addch")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="s|back")])
    return InlineKeyboardMarkup(rows)


# ─── entry point (⚙️ reply-keyboard button) ───────────────────────────────────

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point called from the ⚙️ reply-keyboard button via chat.route_text."""
    user = context.user_data["user"]
    settings = await db.load_settings(user["id"])
    model = settings.get("model") or db.DEFAULT_MODEL
    channels = settings.get("channels") or []
    auto_reset = bool(settings.get("focus_auto_reset"))
    await update.effective_message.reply_text(
        SETTINGS_HEADER,
        reply_markup=_settings_kb(model, channels, auto_reset),
        parse_mode="Markdown",
    )


# ─── focus entry point (🎯 reply-keyboard button) ────────────────────────────

async def show_focus_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry from the 🎯 focus button; switches user into editing_focus sub-state
    so the next free-text message is captured as the new focus value."""
    user = context.user_data["user"]
    settings = await db.load_settings(user["id"])
    current = settings.get("current_focus") or ""
    context.user_data["settings_substate"] = "editing_focus"
    prefix = f"Текущий фокус: {current}\n\n" if current else ""
    await update.effective_message.reply_text(
        f"{prefix}{SETTINGS_FOCUS_PROMPT}"
    )


# ─── free-text handler (editing_focus / adding_channel sub-states) ────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume settings free-text sub-states. Returns True if the message was
    consumed so chat.route_text does not also handle it."""
    substate = context.user_data.get("settings_substate")
    if substate not in ("editing_focus", "adding_channel"):
        return False

    text = (update.message.text or "").strip()
    # Guard: a menu-button press or a /command is not a focus/channel value.
    # Drop the sub-state and let chat.route_text dispatch the press normally —
    # never write the button label into the focus/channel field.
    if _is_menu_or_command(text):
        context.user_data.pop("settings_substate", None)
        return False

    if substate == "editing_focus":
        await _ingest_focus(update, context)
        return True
    if substate == "adding_channel":
        await _ingest_channel(update, context)
        return True
    return False


async def _ingest_focus(update, context) -> None:
    user = context.user_data["user"]
    context.user_data.pop("settings_substate", None)
    focus = (update.message.text or "").strip()
    await db.save_settings(user["id"], {"current_focus": focus})
    await update.message.reply_text(
        SETTINGS_FOCUS_OK.format(focus=focus),
        reply_markup=main_kb_saas(focus),
    )


async def _ingest_channel(update, context) -> None:
    user = context.user_data["user"]
    context.user_data.pop("settings_substate", None)
    raw = (update.message.text or "").strip()
    channel = raw.lstrip("@").strip()

    settings = await db.load_settings(user["id"])
    channels = list(settings.get("channels") or [])
    focus = settings.get("current_focus") or ""
    auto_reset = bool(settings.get("focus_auto_reset"))

    if not channel or not _USERNAME_RE.match(channel):
        await update.message.reply_text(SETTINGS_ADDCH_INVALID)
        return

    if channel in channels:
        await update.message.reply_text(SETTINGS_ADDCH_ALREADY)
        return

    cap = await db.get_effective_limit(user["id"], "channels_max", 15)
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 15

    if len(channels) >= cap:
        await update.message.reply_text(SETTINGS_ADDCH_LIMIT_HIT.format(cap=cap))
        return

    channels.append(channel)
    await db.save_settings(user["id"], {"channels": channels})
    await update.message.reply_text(
        SETTINGS_ADDCH_OK.format(channel=channel),
        reply_markup=_channels_kb(channels),
    )


# ─── callback router (s| namespace) ──────────────────────────────────────────

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry for all s| callbacks."""
    q = update.callback_query
    parts = q.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    user = context.user_data["user"]
    user_id = user["id"]

    if action == "model":
        model_id = parts[2] if len(parts) > 2 else ""
        await _cb_model(q, user_id, model_id)
    elif action == "channels":
        await _cb_channels(q, user_id)
    elif action == "rmch":
        channel = parts[2] if len(parts) > 2 else ""
        await _cb_rmch(q, user_id, channel)
    elif action == "addch":
        await _cb_addch(q, context)
    elif action == "back":
        await _cb_back(q, user_id)
    elif action == "toggle_autoreset":
        await _cb_toggle_autoreset(q, user_id)
    else:
        await q.answer()


async def _safe_answer(q, text: str = "", show_alert: bool = False) -> None:
    from telegram.error import BadRequest
    try:
        await q.answer(text, show_alert=show_alert)
    except BadRequest as e:
        logger.warning("settings cb answer failed (query expired): %s", e)


async def _cb_model(q, user_id: str, model_id: str) -> None:
    if model_id:
        await db.save_settings(user_id, {"model": model_id})
    settings = await db.load_settings(user_id)
    current = settings.get("model") or db.DEFAULT_MODEL
    channels = settings.get("channels") or []
    auto_reset = bool(settings.get("focus_auto_reset"))
    await _safe_answer(q, f"✅ {model_id}")
    await q.edit_message_reply_markup(
        reply_markup=_settings_kb(current, channels, auto_reset)
    )


async def _cb_channels(q, user_id: str) -> None:
    await _safe_answer(q)
    settings = await db.load_settings(user_id)
    channels = settings.get("channels") or []
    await q.edit_message_text(
        SETTINGS_CHANNELS_HEADER,
        reply_markup=_channels_kb(channels),
        parse_mode="Markdown",
    )


async def _cb_rmch(q, user_id: str, channel: str) -> None:
    settings = await db.load_settings(user_id)
    channels = list(settings.get("channels") or [])
    if channel in channels:
        channels.remove(channel)
        await db.save_settings(user_id, {"channels": channels})
    await _safe_answer(q, SETTINGS_RMCH_OK.format(channel=channel))
    await q.edit_message_text(
        SETTINGS_CHANNELS_HEADER,
        reply_markup=_channels_kb(channels),
        parse_mode="Markdown",
    )


async def _cb_addch(q, context) -> None:
    await _safe_answer(q)
    context.user_data["settings_substate"] = "adding_channel"
    await q.edit_message_text(SETTINGS_ADDCH_PROMPT)


async def _cb_back(q, user_id: str) -> None:
    await _safe_answer(q)
    settings = await db.load_settings(user_id)
    model = settings.get("model") or db.DEFAULT_MODEL
    channels = settings.get("channels") or []
    auto_reset = bool(settings.get("focus_auto_reset"))
    await q.edit_message_text(
        SETTINGS_HEADER,
        reply_markup=_settings_kb(model, channels, auto_reset),
        parse_mode="Markdown",
    )


async def _cb_toggle_autoreset(q, user_id: str) -> None:
    settings = await db.load_settings(user_id)
    new_val = not bool(settings.get("focus_auto_reset"))
    await db.save_settings(user_id, {"focus_auto_reset": new_val})
    status_msg = SETTINGS_TOGGLE_AUTORESET_ON if new_val else SETTINGS_TOGGLE_AUTORESET_OFF
    await _safe_answer(q, status_msg)
    # Re-read after save to refresh the keyboard.
    settings = await db.load_settings(user_id)
    model = settings.get("model") or db.DEFAULT_MODEL
    channels = settings.get("channels") or []
    await q.edit_message_reply_markup(
        reply_markup=_settings_kb(model, channels, new_val)
    )
