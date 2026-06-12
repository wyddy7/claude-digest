import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from html import escape
from io import BytesIO

# psycopg3 requires SelectorEventLoop on Windows (incompatible with ProactorEventLoop)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytz
from dotenv import load_dotenv
from telegram import (
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import httpx

import db
from agent import run_digest_pipeline, run_chat_turn
from personalization import get_profile_description, load_personalization
from pipeline_config import (
    build_pipeline_config,
    build_registry_from_state,
    describe_registry,
    make_openrouter_client,
)

load_dotenv()
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
# Keep noisy libraries at INFO even in DEBUG mode
for _lib in ("httpx", "httpcore", "telegram", "apscheduler"):
    logging.getLogger(_lib).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
OWNER_ID = CHAT_ID
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HTTPS_PROXY = os.getenv("HTTPS_PROXY")
MOSCOW = pytz.timezone("Europe/Moscow")

_polling_blips: list[float] = []
_BLIP_WINDOW_SEC = 60.0
_BLIP_WARN_THRESHOLD = 5


def _mask_proxy(url: str | None) -> str:
    if not url:
        return "none"
    try:
        scheme, rest = url.split("://", 1)
        host = rest.split("@", 1)[-1]
        return f"{scheme}://***@{host}"
    except ValueError:
        return "malformed"

# Schedule — single source of truth for both bot.py and scheduler.py
DIGEST_HOUR, DIGEST_MINUTE = 13, 0
CHECKIN_HOUR, CHECKIN_MINUTE = 18, 0

DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]

MODELS = {
    "🚀 Claude Sonnet 4.6": "anthropic/claude-sonnet-4.6",
    "⚡ Claude Haiku 4.5": "anthropic/claude-haiku-4.5",
    "🧠 Claude 3.7 Sonnet": "anthropic/claude-3.7-sonnet",
    "⚡ Claude 3.5 Haiku": "anthropic/claude-3.5-haiku",
    "🟢 GPT-4o Mini": "openai/gpt-4o-mini",
}

_BROKEN_MODELS = {"google/gemini-3.1-flash-lite-preview", "google/gemini-3.1-flash"}
DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]


def main_kb(focus: str = "") -> ReplyKeyboardMarkup:
    focus_btn = f"🎯 {focus[:28]}" if focus else "🎯 Задать фокус"
    return ReplyKeyboardMarkup(
        [
            ["📰 Дайджест", "📚 История"],
            ["👤 Профиль", "⚙️ Настройки"],
            [focus_btn],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def history_kb(history: list, page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    total = len(history)
    start = page * per_page
    end = min(start + per_page, total)
    rows = []
    for i in range(start, end):
        item = history[i]
        digest_id = item.get("id", i + 1)
        date_short = item["date"][5:].replace("-", ".")
        if item.get("is_error"):
            label = f"❌ {date_short} #{digest_id}"
        else:
            label = f"📰 {date_short} #{digest_id} ({item['posts_count']} постов)"
        rows.append([InlineKeyboardButton(label, callback_data=f"hv|{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"hp|{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"hp|{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def settings_kb(current_model: str, channels: list, auto_reset: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for label, model_id in MODELS.items():
        mark = "✅ " if model_id == current_model else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"model|{model_id}")])
    rows.append([InlineKeyboardButton(f"📡 Каналы ({len(channels)})", callback_data="channels")])
    reset_label = (
        "🔄 Сброс фокуса после дайджеста: ВКЛ ✅"
        if auto_reset
        else "🔄 Сброс фокуса после дайджеста: ВЫКЛ"
    )
    rows.append([InlineKeyboardButton(reset_label, callback_data="toggle_autoreset")])
    return InlineKeyboardMarkup(rows)


def channels_kb(channels: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"❌ {channel}", callback_data=f"rmch|{channel}")] for channel in channels]
    rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="addch")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(rows)


async def cmd_stages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read-only view of the per-stage model registry (which model runs each
    pipeline stage, and why). Metadata is sourced from the registry — the same
    source the pipeline uses — so the UI never drifts from runtime behavior."""
    data = await db.load()
    registry = build_registry_from_state(data, load_personalization())
    text = "🧩 <b>Модели по этапам пайплайна</b>\n\n" + escape(describe_registry(registry))
    await update.message.reply_text(text, parse_mode="HTML")


async def do_send_digest(bot, chat_id: int, status_msg=None):
    async def _update(text: str):
        nonlocal status_msg
        if status_msg is None:
            status_msg = await bot.send_message(chat_id, text)
        else:
            try:
                await status_msg.edit_text(text)
            except Exception as e:
                logger.warning(f"status edit failed: {e}")

    await _update("⏳ Агент собирает дайджест...")

    cfg_data = await db.load()
    cfg_yaml = load_personalization()
    config = build_pipeline_config(cfg_data, cfg_yaml)
    llm_client = make_openrouter_client(OPENROUTER_KEY)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as fetcher:
        result = await run_digest_pipeline(
            config, llm_client=llm_client, fetcher=fetcher, on_status=_update
        )

    digest_html = result["digest_html"]
    personal_html = result.get("personal_html", "")
    stats_html = result.get("stats_html", "")
    posts_count = result.get("posts_count", 0)

    await _update("✅ Готово")

    data = await db.load()
    if data.get("focus_auto_reset") and data.get("current_focus"):
        data["current_focus"] = ""
    data["last_digest"] = digest_html
    data["last_digest_time"] = datetime.now().isoformat()
    await db.save(data)
    await db.add_history(f"Дайджест ({posts_count} постов)")
    await db.append_to_history(digest_html, posts_count)

    date_str = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    full_text = f"📰 <b>Дайджест {date_str}</b>\n\n{digest_html}"

    max_len = 4096
    if len(full_text) <= max_len:
        chunks = [full_text]
    else:
        paragraphs = full_text.split("\n\n")
        chunks = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 <= max_len:
                current = current + ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                while len(para) > max_len:
                    chunks.append(para[:max_len])
                    para = para[max_len:]
                current = para
        if current:
            chunks.append(current)

    for chunk in chunks:
        await bot.send_message(
            chat_id,
            chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    personal_parts = [part for part in [personal_html, stats_html] if part]
    if personal_parts:
        await bot.send_message(
            chat_id,
            "\n\n".join(personal_parts),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )



async def _safe_answer(q, text: str = "", show_alert: bool = False):
    try:
        await q.answer(text, show_alert=show_alert)
    except BadRequest as e:
        logger.warning(f"callback answer failed (query expired): {e}")


async def cb_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    model_id = q.data.split("|", 1)[1]
    data = await db.load()
    data["model"] = model_id
    await db.save(data)
    await _safe_answer(q, f"✅ {model_id}")
    await q.edit_message_reply_markup(
        reply_markup=settings_kb(model_id, data["channels"], data.get("focus_auto_reset", False))
    )


async def cb_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    data = await db.load()
    await q.edit_message_text(
        "📡 *Каналы* — нажми ❌ чтобы удалить:",
        reply_markup=channels_kb(data["channels"]),
        parse_mode="Markdown",
    )


async def cb_rmch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    channel = q.data.split("|", 1)[1]
    data = await db.load()
    if channel in data["channels"]:
        data["channels"].remove(channel)
        await db.save(data)
    await _safe_answer(q, f"Удалён: {channel}")
    await q.edit_message_text(
        "📡 *Каналы*",
        reply_markup=channels_kb(data["channels"]),
        parse_mode="Markdown",
    )


async def cb_addch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    context.user_data["state"] = "adding_channel"
    await q.edit_message_text("Введи юзернейм канала без @:\n\n/cancel — отмена")


async def cb_back_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    data = await db.load()
    await q.edit_message_text(
        "⚙️ *Настройки*",
        reply_markup=settings_kb(data["model"], data["channels"], data.get("focus_auto_reset", False)),
        parse_mode="Markdown",
    )


async def cb_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    page = int(q.data.split("|", 1)[1])
    history = await db.load_history()
    await _safe_answer(q)
    await q.edit_message_text(
        f"📚 *История* ({len(history)} дайджестов)",
        reply_markup=history_kb(history, page),
        parse_mode="Markdown",
    )


async def cb_hv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    idx = int(q.data.split("|", 1)[1])
    history = await db.load_history()
    await _safe_answer(q)
    if idx >= len(history):
        await q.edit_message_text("Запись не найдена.")
        return
    item = history[idx]
    digest_id = item.get("id", idx + 1)
    text = f"📰 <b>Дайджест {item['date']} #{digest_id}</b>\n\n{item.get('digest_html', '')}"
    if len(text) > 4000:
        header = f"📰 <b>Дайджест {item['date']} #{digest_id}</b>\n\n"
        body = item.get("digest_html", "")
        truncated = header
        for para in body.split("\n\n"):
            candidate = truncated + ("\n\n" if truncated != header else "") + para
            if len(candidate) > 3900:
                break
            truncated = candidate
        text = truncated + "\n\n<i>(сокращено)</i>"
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Список", callback_data="hp|0")]])
    await q.edit_message_text(
        text,
        reply_markup=back_kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cb_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action = q.data
    data = await db.load()
    if action == "ci_yes":
        await _safe_answer(q, "Огонь! 🔥")
        await q.edit_message_text(f"Огонь! 🔥 Завтра в {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}.")
    elif action == "ci_no":
        await _safe_answer(q)
        if data["last_digest"]:
            await q.edit_message_text("Держи дайджест ещё раз:")
            await context.bot.send_message(
                q.message.chat_id,
                f"📰 <b>Дайджест</b>\n\n{data['last_digest']}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await q.edit_message_text("Дайджест ещё не запускался.")
    elif action == "ci_talk":
        await _safe_answer(q)
        context.user_data["state"] = "chat"
        await q.edit_message_text("Пиши, слушаю 👇")


async def cb_toggle_autoreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = await db.load()
    data["focus_auto_reset"] = not data.get("focus_auto_reset", False)
    await db.save(data)
    status = "ВКЛ" if data["focus_auto_reset"] else "ВЫКЛ"
    await _safe_answer(q, f"Авто-сброс фокуса: {status}")
    await q.edit_message_reply_markup(
        reply_markup=settings_kb(data["model"], data["channels"], data["focus_auto_reset"])
    )


async def cb_edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    await q.edit_message_text("Профиль теперь редактируется в config/personalization.yaml")


KB_BUTTONS = {"📰 Дайджест", "📚 История", "👤 Профиль", "⚙️ Настройки"}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    uid = update.effective_user.id if update.effective_user else "?"
    logger.info(f"[handle_text] uid={uid} text={text!r}")
    data = await db.load()
    logger.debug(f"[handle_text] db.load() ok | channels={len(data.get('channels',[]))} focus={data.get('current_focus','')!r}")
    focus = data.get("current_focus", "")

    if text == "/cancel":
        context.user_data.pop("state", None)
        await update.message.reply_text("Отменено.", reply_markup=main_kb(focus))
        return

    if text in KB_BUTTONS or text.startswith("🎯"):
        context.user_data.pop("state", None)

    if text == "📰 Дайджест":
        # Send status as plain bot.send_message (not a quoted reply) so it can be edited later.
        # PTB 21 reply_text() creates a quoted reply which Telegram marks as non-editable.
        await do_send_digest(context.bot, update.effective_chat.id)
        return

    if text == "📚 История":
        history = await db.load_history()
        if not history:
            await update.message.reply_text(
                "История пуста — запусти первый дайджест!",
                reply_markup=main_kb(focus),
            )
            return
        await update.message.reply_text(
            f"📚 *История* ({len(history)} дайджестов)",
            reply_markup=history_kb(history),
            parse_mode="Markdown",
        )
        return

    if text == "👤 Профиль":
        description = escape_markdown(get_profile_description(), version=1)
        body = f"👤 *Профиль*\n\n{description}"
        if focus:
            body += f"\n\n🎯 *Фокус:* {escape_markdown(focus, version=1)}"
        model = escape_markdown(data["model"], version=1)
        channels = ", ".join(escape_markdown(ch, version=1) for ch in data["channels"])
        body += f"\n\n🤖 `{model}`\n📡 Каналов: {len(data['channels'])}: {channels}"
        body += "\n\n`config/personalization.yaml`"
        await update.message.reply_text(body, parse_mode="Markdown")
        return

    if text == "⚙️ Настройки":
        await update.message.reply_text(
            "⚙️ *Настройки*",
            reply_markup=settings_kb(data["model"], data["channels"], data.get("focus_auto_reset", False)),
            parse_mode="Markdown",
        )
        return

    if text.startswith("🎯"):
        context.user_data["state"] = "editing_focus"
        prompt_text = f"Текущий фокус: {focus}\n\n" if focus else ""
        await update.message.reply_text(
            f"{prompt_text}На что фокусироваться в следующем дайджесте?\n/cancel — отмена",
        )
        return

    state = context.user_data.get("state")

    if state == "editing_focus":
        context.user_data.pop("state")
        data["current_focus"] = text
        await db.save(data)
        await db.add_history(f"Фокус: {text}")
        await update.message.reply_text(f"✅ Фокус: {text}", reply_markup=main_kb(text))
        return

    if state == "adding_channel":
        context.user_data.pop("state")
        channel = text.lstrip("@").strip()
        if channel and channel not in data["channels"]:
            data["channels"].append(channel)
            await db.save(data)
            await update.message.reply_text(f"✅ Канал {channel} добавлен!", reply_markup=main_kb(focus))
        else:
            await update.message.reply_text(
                "Уже есть или некорректное имя.",
                reply_markup=main_kb(focus),
            )
        return

    await db.add_history(f"Msg: {text[:80]}")
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    checkpointer = context.application.bot_data.get("checkpointer")
    reply = await run_chat_turn(update.effective_user.id, text, checkpointer)
    await db.add_history(f"Bot: {reply[:80]}")
    await update.message.reply_text(reply, reply_markup=main_kb(focus))


async def cmd_in(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /in <минуты>\nПример: /in 2")
        return
    minutes = int(args[0])
    if minutes < 1 or minutes > 60:
        await update.message.reply_text("Укажи от 1 до 60 минут.")
        return
    fire_at = datetime.now(MOSCOW) + timedelta(minutes=minutes)
    time_str = fire_at.strftime("%H:%M МСК")
    target_chat_id = update.effective_chat.id

    async def _job(ctx: ContextTypes.DEFAULT_TYPE):
        await do_send_digest(ctx.bot, target_chat_id)

    context.job_queue.run_once(_job, when=minutes * 60, name=f"test_digest_{minutes}m")
    await update.message.reply_text(f"⏰ Дайджест запланирован через {minutes} мин (в {time_str})")


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(MOSCOW)

    def next_time(hour: int, minute: int) -> datetime:
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t

    def fmt(t: datetime) -> str:
        diff = t - now
        total_min = int(diff.total_seconds() // 60)
        h, m = divmod(total_min, 60)
        label = "сегодня" if t.date() == now.date() else "завтра"
        when = f"через {h}ч {m}м" if h else f"через {m}м"
        return f"{label} в {t.strftime('%H:%M')} МСК ({when})"

    await update.message.reply_text(
        f"📅 Дайджест: {fmt(next_time(DIGEST_HOUR, DIGEST_MINUTE))}\n"
        f"💬 Чекин: {fmt(next_time(CHECKIN_HOUR, CHECKIN_MINUTE))}"
    )


async def _post_init(app: Application) -> None:
    """Initialize supabase client and in-memory checkpointer at startup."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL or SUPABASE_KEY not set")
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required")

    await db.init_supabase(SUPABASE_URL, SUPABASE_KEY)
    await db.ensure_owner_user()

    from langgraph.checkpoint.memory import MemorySaver
    app.bot_data["checkpointer"] = MemorySaver()
    logger.info("DB ready (supabase-py), checkpointer ready (MemorySaver)")


async def _post_shutdown(app: Application) -> None:
    """Close DB pool on shutdown."""
    await db.close_pool()
    logger.info("DB connections closed")


def main():
    req = HTTPXRequest(
        proxy=HTTPS_PROXY,
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    get_updates_req = HTTPXRequest(
        proxy=HTTPS_PROXY,
        connect_timeout=10.0,
        read_timeout=40.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    logger.info("[transport] proxy=%s read_to=20s get_updates_read_to=40s",
                _mask_proxy(HTTPS_PROXY))

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(get_updates_req)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # resolve_user replaces the single-owner check_owner: owner falls through to
    # the legacy handlers below; invited non-owners are dispatched to the
    # handlers/ package and stopped; un-invited users get the invite-only reply.
    from handlers.middleware import resolve_user
    from handlers import onboarding as onboarding_surface
    from handlers import subscription as subscription_surface
    from handlers import admin as admin_surface

    app.add_handler(TypeHandler(Update, resolve_user), group=-1)

    from handlers import settings as settings_surface

    # Non-owner callback surfaces (patterns the owner UI never emits).
    app.add_handler(CallbackQueryHandler(onboarding_surface.cb, pattern=r"^onb\|"))
    app.add_handler(CallbackQueryHandler(subscription_surface.cb_buy, pattern=r"^buy\|"))
    app.add_handler(CallbackQueryHandler(settings_surface.cb, pattern=r"^s\|"))

    from handlers import history as history_surface
    app.add_handler(CallbackQueryHandler(history_surface.cb, pattern=r"^h\|"))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("in", cmd_in))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("stages", cmd_stages))

    app.add_handler(CallbackQueryHandler(cb_model, pattern=r"^model\|"))
    app.add_handler(CallbackQueryHandler(cb_channels, pattern="^channels$"))
    app.add_handler(CallbackQueryHandler(cb_rmch, pattern=r"^rmch\|"))
    app.add_handler(CallbackQueryHandler(cb_addch, pattern="^addch$"))
    app.add_handler(CallbackQueryHandler(cb_back_settings, pattern="^back_settings$"))
    app.add_handler(CallbackQueryHandler(cb_hp, pattern=r"^hp\|"))
    app.add_handler(CallbackQueryHandler(cb_hv, pattern=r"^hv\|"))
    from handlers.checkin import cb_checkin as checkin_cb
    app.add_handler(CallbackQueryHandler(checkin_cb, pattern=r"^ci_"))
    app.add_handler(CallbackQueryHandler(cb_toggle_autoreset, pattern="^toggle_autoreset$"))
    app.add_handler(CallbackQueryHandler(cb_edit_profile, pattern="^edit_profile$"))

    # --- payments (Stars) ---
    app.add_handler(CommandHandler("buy", subscription_surface.cmd_buy))
    app.add_handler(PreCheckoutQueryHandler(subscription_surface.pre_checkout))
    app.add_handler(MessageHandler(
        filters.SUCCESSFUL_PAYMENT, subscription_surface.successful_payment
    ))
    # --- admin (ADMIN_ID-gated; non-admins silently ignored) ---
    app.add_handler(CommandHandler("give_pro", admin_surface.cmd_give_pro))
    app.add_handler(CommandHandler("revoke_pro", admin_surface.cmd_revoke_pro))
    app.add_handler(CommandHandler("grant_trial", admin_surface.cmd_grant_trial))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)) and not isinstance(update, Update):
        now = asyncio.get_event_loop().time()
        _polling_blips.append(now)
        cutoff = now - _BLIP_WINDOW_SEC
        while _polling_blips and _polling_blips[0] < cutoff:
            _polling_blips.pop(0)
        count = len(_polling_blips)
        logger.info("[polling] transient %s (blip #%d in last %ds), PTB will retry",
                    type(err).__name__, count, int(_BLIP_WINDOW_SEC))
        if count >= _BLIP_WARN_THRESHOLD:
            logger.warning("[polling] proxy unstable: %d blips in %ds — see %s",
                           count, int(_BLIP_WINDOW_SEC), _mask_proxy(HTTPS_PROXY))
        return

    logger.error("Unhandled exception", exc_info=err)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Что-то пошло не так. Попробуй ещё раз.",
            )
        except Exception:
            pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Команды*\n\n"
        "/help — это сообщение\n"
        "/next — когда следующий дайджест и чекин\n"
        "/in `<минуты>` — запустить дайджест через N минут (1–60)\n"
        "/menu — показать главное меню\n\n"
        "*Кнопки*\n\n"
        "📰 *Дайджест* — запустить сейчас\n"
        "📚 *История* — предыдущие дайджесты\n"
        "👤 *Профиль* — профиль, модель, каналы\n"
        "⚙️ *Настройки* — выбор модели, управление каналами, авто-сброс фокуса\n"
        "🎯 *Фокус* — задать приоритет для следующего дайджеста\n\n"
        "*Расписание*\n\n"
        f"• {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} МСК — автодайджест\n"
        f"• {CHECKIN_HOUR:02d}:{CHECKIN_MINUTE:02d} МСК — чекин",
        parse_mode="Markdown",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await db.load()
    await update.message.reply_text(
        "Привет! Твой персональный дайджест-бот 🤖\n\n"
        f"• *{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}* — дайджест из каналов\n"
        f"• *{CHECKIN_HOUR:02d}:{CHECKIN_MINUTE:02d}* — чекин\n\n"
        "Кнопки снизу 👇",
        reply_markup=main_kb(data.get("current_focus", "")),
        parse_mode="Markdown",
    )


if __name__ == "__main__":
    main()
