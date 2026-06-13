"""Surface 1 — onboarding wizard (callback-driven, DB-persisted state machine).

A 3-step first-touch flow: pick channels via topic templates → optional focus →
an immediate preview digest. State lives in users.onboarding_state (persisted
after each step) so the flow survives a container restart — NOT a PTB
ConversationHandler (whose in-process state would reset, the documented
MemorySaver class of bug).

The 3-day Pro trial is granted at the FIRST /start (entry), so the preview digest
runs under real Pro limits. Idempotent via subscriptions.grant_trial (trial_used).

Topic → channel seed sets are EDITABLE SEED DATA in config/onboarding_topics.yaml,
loaded at import time — never hardcoded here. All user-facing strings are Russian.
"""

import logging
import re
import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import digest_bot.db as db
import digest_bot.delivery as delivery
import digest_bot.subscriptions as subscriptions
from digest_bot.paths import CONFIG_DIR
from digest_bot.handlers.menu import main_kb_saas
from digest_bot.handlers.middleware import _effective_tier_active
from digest_bot.handlers.strings import (
    ONBOARDING_CHANNELS_MIN_ERROR,
    ONBOARDING_FOCUS,
    ONBOARDING_FOCUS_OWN_PROMPT,
    ONBOARDING_MENU_READY,
    ONBOARDING_OWN_CHANNELS_PROMPT,
    ONBOARDING_PREVIEW_CLOSE,
    ONBOARDING_PREVIEW_FAIL,
    ONBOARDING_PREVIEW_PRE,
    ONBOARDING_WELCOME,
)

logger = logging.getLogger(__name__)

# ── onboarding states (users.onboarding_state) ────────────────────────────────
ST_INVITED = "invited"
ST_NEW = "new"  # db.get_or_create_user default; treated like 'invited' for entry
ST_CHANNELS = "collecting_channels"
ST_FOCUS = "collecting_focus"
ST_PREVIEW = "preview"
ST_DONE = "done"
# Legacy/migration rows may carry 'active' as a done-equivalent.
_DONE_STATES = {ST_DONE, "active"}
_ENTRY_STATES = {ST_INVITED, ST_NEW}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

# ── topic seed (EDITABLE SEED DATA) ───────────────────────────────────────────
_TOPICS_PATH = CONFIG_DIR / "onboarding_topics.yaml"

_TOPIC_LABELS = {
    # N3: only the curated, owner-verified AI/LLM set is offered.
    # Other topic templates remain in the YAML for future curation, but are
    # not surfaced in onboarding until they are verified by the owner.
    "ai": "🤖 AI и LLM",
}


def _load_topics() -> dict:
    """topic_key -> [channels]. Loaded from the editable YAML seed; empty on error
    (the ✏️ Свои каналы escape hatch still lets the user proceed)."""
    try:
        with open(_TOPICS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {k: list(v or []) for k, v in data.items()}
    except Exception as e:  # pragma: no cover - file present in repo
        logger.warning("onboarding topics seed load failed: %s", e)
        return {}


_TOPICS = _load_topics()

FOCUS_CHIPS = {
    "agents": "Claude Code и агенты",
    "oss": "Локальные / open-source LLM",
    "prod": "AI для продакшна / инфра",
}

# TODO(personalization, optional step 3): a skippable «пара слов о себе» step
# that seeds user_settings.personalization["profile"]["description"]. Writer
# contract: read settings → overlay {"profile": {"description": ...}} into the
# existing personalization blob → save (NEVER replace the whole JSONB — the
# reserved "_usage" chat-turn counter lives there, see db.record_chat_turn).
# Not load-bearing for the privacy fix: a tenant without a profile gets the
# neutral config/personalization.default.yaml via
# personalization.resolve_personalization(). Copy goes to handlers/strings.py.


# ── candidate-set helpers (within-step working set in user_data) ──────────────

def _candidate(context) -> list:
    return context.user_data.setdefault("onb_channels", [])


def _selected_topics(context) -> set:
    return context.user_data.setdefault("onb_topics", set())


async def _channel_cap(user_id: str) -> int:
    cap = await db.get_effective_limit(user_id, "channels_max", 15)
    try:
        return int(cap)
    except (TypeError, ValueError):
        return 15


# ── step renderers ────────────────────────────────────────────────────────────

def _welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Поехали →", callback_data="onb|start")],
        [InlineKeyboardButton("ℹ️ Что внутри", callback_data="onb|info_intro")],
    ])


WELCOME_TEXT = ONBOARDING_WELCOME  # canonical text lives in handlers.strings


def _channels_kb(context) -> InlineKeyboardMarkup:
    selected = _selected_topics(context)
    rows = []
    for key, label in _TOPIC_LABELS.items():
        mark = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"onb|topic|{key}")])
    rows.append([InlineKeyboardButton("✏️ Свои каналы", callback_data="onb|own")])
    rows.append([
        InlineKeyboardButton("ℹ️ Зачем это", callback_data="onb|info_ch"),
        InlineKeyboardButton("Далее →", callback_data="onb|ch_done"),
    ])
    return InlineKeyboardMarkup(rows)


async def _channels_text(context, user_id: str, *, truncated: bool = False) -> str:
    cap = await _channel_cap(user_id)
    n_topics = len(_selected_topics(context))
    n_channels = len(_candidate(context))
    footer = f"\n\nВыбрано тем: {n_topics} • каналов: {n_channels} (лимит твоего тарифа: {cap})"
    if truncated:
        footer += f"\n⚠️ Взял первые {cap}. Остальное добавишь после — в ⚙️ Настройках."
    return (
        "Шаг 1 из 2 — Каналы *\n\n"
        "Выбери темы — я подставлю проверенные каналы по каждой.\n"
        "Можно выбрать несколько. Отметишь тут, потом всё отредактируешь." + footer
    )


def _focus_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text, callback_data=f"onb|focus|{key}")]
            for key, text in FOCUS_CHIPS.items()]
    rows.append([
        InlineKeyboardButton("✏️ Свой фокус", callback_data="onb|focus_own"),
        InlineKeyboardButton("Пропустить →", callback_data="onb|focus_skip"),
    ])
    rows.append([InlineKeyboardButton("ℹ️ Что такое фокус", callback_data="onb|info_focus")])
    return InlineKeyboardMarkup(rows)


FOCUS_TEXT = ONBOARDING_FOCUS  # canonical text lives in handlers.strings


# ── entry: /start ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start for an invited non-owner. Routes by onboarding_state; on first entry
    grants the 3-day Pro trial, then shows Step 0. A 'done' user gets the normal
    menu, never a wizard restart."""
    user = context.user_data["user"]
    state = user.get("onboarding_state") or ST_NEW

    if state in _DONE_STATES:
        await _show_menu(update, context)
        return

    if state in _ENTRY_STATES:
        # Grant trial once (idempotent), flip into the wizard.
        await subscriptions.grant_trial(user["tg_user_id"])
        await db.update_user_fields(user["tg_user_id"], {"onboarding_state": ST_CHANNELS})
        await update.effective_message.reply_text(WELCOME_TEXT, reply_markup=_welcome_kb())
        return

    # Resume an in-flight step from DB-saved partial state.
    await _resume(update, context, state)


async def _resume(update, context, state: str):
    user = context.user_data["user"]
    if state == ST_CHANNELS:
        # Re-hydrate candidate set from any partially-saved channels.
        try:
            settings = await db.load_settings(user["id"])
            saved = settings.get("channels") or []
        except Exception:
            saved = []
        context.user_data["onb_channels"] = list(saved)
        await update.effective_message.reply_text(
            await _channels_text(context, user["id"]), reply_markup=_channels_kb(context)
        )
    elif state == ST_FOCUS:
        await update.effective_message.reply_text(FOCUS_TEXT, reply_markup=_focus_kb())
    elif state == ST_PREVIEW:
        await _run_preview(update, context)
    else:
        await update.effective_message.reply_text(WELCOME_TEXT, reply_markup=_welcome_kb())


async def _show_menu(update, context):
    user = context.user_data["user"]
    try:
        settings = await db.load_settings(user["id"])
        focus = settings.get("current_focus") or ""
    except Exception:
        focus = ""
    await update.effective_message.reply_text(
        ONBOARDING_MENU_READY, reply_markup=main_kb_saas(focus)
    )


# ── callback router (onb| namespace) ──────────────────────────────────────────

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    user = context.user_data["user"]

    # Any onb| callback clears the ephemeral typing sub-state.
    context.user_data.pop("onb_substate", None)

    # Replay guard: a finished user can re-tap an old onboarding inline button
    # still living in their chat history (Telegram keeps keyboards alive). Without
    # this, "Пропустить →" / a topic chip would re-enter the wizard, re-trigger
    # the preview pipeline (bypassing the daily cap) AND clobber current_focus.
    # Info popups are harmless and stay allowed; everything state-changing stops.
    if user.get("onboarding_state") == ST_DONE and action not in {
        "info_intro", "info_ch", "info_focus",
    }:
        await q.answer("Онбординг уже завершён — пользуйся кнопками меню снизу 👇")
        return

    if action == "info_intro":
        await q.answer(
            "Бот читает публичные Telegram-каналы, которые ты выберешь, и раз в "
            "день шлёт структурированный дайджест. Реклама режется автоматически. "
            "Пробный период — 3 дня полного Pro.",
            show_alert=True,
        )
        return
    if action == "info_ch":
        await q.answer(
            "Дайджест собирается ТОЛЬКО из этих каналов. Это и есть твоя "
            "персонализация — выбирай, что реально читаешь. Менять можно когда угодно.",
            show_alert=True,
        )
        return
    if action == "info_focus":
        await q.answer(
            "Фокус — это приоритет отбора на сегодня. Например «агенты» поднимет "
            "посты про агентов выше. Это не меняет формат дайджеста, только что в "
            "него попадёт.",
            show_alert=True,
        )
        return

    if action == "start":
        await q.answer()
        # Hydrate candidate set in case of resume.
        context.user_data.setdefault("onb_channels", [])
        await q.message.reply_text(
            await _channels_text(context, user["id"]), reply_markup=_channels_kb(context)
        )
        return

    if action == "topic":
        await _toggle_topic(update, context, parts[2] if len(parts) > 2 else "")
        return

    if action == "own":
        await q.answer()
        context.user_data["onb_substate"] = "typing_channels"
        await q.message.reply_text(
            ONBOARDING_OWN_CHANNELS_PROMPT,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← К темам", callback_data="onb|back_topics")]]
            ),
        )
        return

    if action == "back_topics":
        await q.answer()
        await q.message.reply_text(
            await _channels_text(context, user["id"]), reply_markup=_channels_kb(context)
        )
        return

    if action == "ch_done":
        await _channels_done(update, context)
        return

    if action == "focus":
        await _set_focus(update, context, FOCUS_CHIPS.get(parts[2] if len(parts) > 2 else "", ""))
        return

    if action == "focus_own":
        await q.answer()
        context.user_data["onb_substate"] = "typing_focus"
        await q.message.reply_text(ONBOARDING_FOCUS_OWN_PROMPT)
        return

    if action == "focus_skip":
        await _set_focus(update, context, "")
        return

    await q.answer()


async def _toggle_topic(update, context, key: str):
    q = update.callback_query
    user = context.user_data["user"]
    selected = _selected_topics(context)
    candidate = _candidate(context)
    cap = await _channel_cap(user["id"])

    if key in selected:
        selected.discard(key)
        # Remove this topic's channels that no other selected topic provides.
        keep = set()
        for t in selected:
            keep.update(_TOPICS.get(t, []))
        context.user_data["onb_channels"] = [c for c in candidate if c in keep]
    else:
        selected.add(key)
        for ch in _TOPICS.get(key, []):
            if ch not in candidate:
                candidate.append(ch)

    truncated = False
    if len(context.user_data["onb_channels"]) > cap:
        context.user_data["onb_channels"] = context.user_data["onb_channels"][:cap]
        truncated = True

    await q.answer()
    try:
        await q.edit_message_text(
            await _channels_text(context, user["id"], truncated=truncated),
            reply_markup=_channels_kb(context),
        )
    except Exception:
        await q.message.reply_text(
            await _channels_text(context, user["id"], truncated=truncated),
            reply_markup=_channels_kb(context),
        )


async def _channels_done(update, context):
    q = update.callback_query
    user = context.user_data["user"]
    candidate = _candidate(context)
    if not candidate:
        await q.answer(ONBOARDING_CHANNELS_MIN_ERROR, show_alert=True)
        return
    await q.answer()
    await db.save_settings(user["id"], {"channels": candidate})
    await db.update_user_fields(user["tg_user_id"], {"onboarding_state": ST_FOCUS})
    await q.message.reply_text(FOCUS_TEXT, reply_markup=_focus_kb())


async def _set_focus(update, context, focus_text: str):
    q = update.callback_query
    user = context.user_data["user"]
    if q:
        await q.answer()
    await db.save_settings(user["id"], {"current_focus": focus_text})
    await db.update_user_fields(user["tg_user_id"], {"onboarding_state": ST_PREVIEW})
    await _run_preview(update, context)


# ── free-text (escape hatches) ───────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Owns onboarding free-text input. Returns True if it consumed the message
    (so the chat router does not also handle it)."""
    substate = context.user_data.get("onb_substate")
    if substate == "typing_channels":
        await _ingest_channels(update, context)
        return True
    if substate == "typing_focus":
        await _ingest_focus(update, context)
        return True
    return False


async def _ingest_channels(update, context):
    user = context.user_data["user"]
    cap = await _channel_cap(user["id"])
    candidate = _candidate(context)
    tokens = re.split(r"[\s,]+", update.message.text.strip())
    dropped = []
    for tok in tokens:
        tok = tok.lstrip("@").strip()
        if not tok:
            continue
        if not _USERNAME_RE.match(tok):
            dropped.append(tok)
            continue
        if tok not in candidate and len(candidate) < cap:
            candidate.append(tok)
    context.user_data.pop("onb_substate", None)
    truncated = len(candidate) >= cap and len(tokens) > 0
    note = ""
    if dropped:
        note = "\n(пропустил неподходящее: " + ", ".join(dropped[:5]) + ")"
    await update.message.reply_text(
        (await _channels_text(context, user["id"], truncated=truncated)) + note,
        reply_markup=_channels_kb(context),
    )


async def _ingest_focus(update, context):
    user = context.user_data["user"]
    context.user_data.pop("onb_substate", None)
    await _set_focus_from_text(update, context, update.message.text.strip())


async def _set_focus_from_text(update, context, focus_text: str):
    user = context.user_data["user"]
    await db.save_settings(user["id"], {"current_focus": focus_text})
    await db.update_user_fields(user["tg_user_id"], {"onboarding_state": ST_PREVIEW})
    await _run_preview(update, context)


# ── step 3: immediate preview digest ──────────────────────────────────────────

# Kept as module-level aliases for any external references; canonical text in handlers.strings.
PREVIEW_PRE = ONBOARDING_PREVIEW_PRE
PREVIEW_CLOSE = ONBOARDING_PREVIEW_CLOSE
PREVIEW_FAIL = ONBOARDING_PREVIEW_FAIL


async def _run_preview(update, context):
    """Generate the first digest now, then close onboarding. Always lands the user
    in 'done' (channels are saved) — a transient generation failure does not trap
    them in the wizard.

    N2: streams pipeline stage updates via make_status_updater, matching
    the live behaviour of the 📰 button (deliver_digest's on_status path)."""
    from digest_bot.handlers.digest import deliver_digest

    user = context.user_data["user"]
    chat = update.effective_chat
    tg_user_id = user["tg_user_id"]

    # Subscription gate BEFORE any LLM spend (fail-closed). The preview is
    # reachable outside the happy path: a /start resume on a stuck 'preview'
    # state, a replayed onb| inline button from old chat history, or a
    # /reset_user re-run after the trial expired. In the normal first-touch
    # flow the trial was granted at /start, so this always passes; an
    # expired/revoked/null-state user gets the paywall and NO pipeline run.
    if not _effective_tier_active(user):
        from digest_bot.handlers import subscription as subscription_surface

        await subscription_surface.show_gate(update, context)
        # Channels are already saved — land the user in 'done' so /start shows
        # the menu (with the paywall), not a wizard/preview retry loop.
        await db.update_user_fields(tg_user_id, {"onboarding_state": ST_DONE})
        for k in ("onb_channels", "onb_topics", "onb_substate"):
            context.user_data.pop(k, None)
        return

    await context.bot.send_message(chat.id, PREVIEW_PRE)
    on_status = delivery.make_status_updater(context.bot, tg_user_id)
    try:
        await deliver_digest(context.bot, user, on_status=on_status)
        await context.bot.send_message(
            chat.id, PREVIEW_CLOSE, reply_markup=main_kb_saas(
                (await db.load_settings(user["id"])).get("current_focus") or ""
            )
        )
    except Exception as e:
        logger.warning("onboarding preview failed for %s: %s", user.get("tg_user_id"), e)
        await context.bot.send_message(
            chat.id, PREVIEW_FAIL, reply_markup=main_kb_saas("")
        )
    finally:
        # Channels saved → always reach 'done'; do not trap on transient failure.
        await db.update_user_fields(user["tg_user_id"], {"onboarding_state": ST_DONE})
        await db.bump_usage(user["id"], "onboarding_done")
        for k in ("onb_channels", "onb_topics", "onb_substate"):
            context.user_data.pop(k, None)
