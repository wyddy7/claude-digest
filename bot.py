import json
import logging
import os
from datetime import datetime, time
from io import BytesIO
from pathlib import Path

import pytz
from dotenv import load_dotenv
from telegram import (
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from ai import chat_response, filter_images, generate_digest
from scraper import scrape_all

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
OWNER_ID = CHAT_ID  # единственный разрешённый пользователь — берётся из CHAT_ID в .env
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
DATA_FILE = _DATA_DIR / "data.json"
HISTORY_FILE = _DATA_DIR / "digests_history.json"
MOSCOW = pytz.timezone("Europe/Moscow")

DEFAULT_CHANNELS = ["cryptoEssay", "llm_notes", "ai_newz", "y_everyday", "eaccchat"]

MODELS = {
    "🚀 Claude Sonnet 4.6": "anthropic/claude-sonnet-4-6",
    "⚡ Claude 3.5 Haiku": "anthropic/claude-3.5-haiku",
    "🧠 Claude 3.7 Sonnet": "anthropic/claude-3.7-sonnet",
    "⚡ Gemini 2.0 Flash": "google/gemini-2.0-flash-001",
    "🟢 GPT-4o Mini": "openai/gpt-4o-mini",
}


DEFAULT_PROFILE = """Даня, студент 4 курса, ~1 год опыта с ИИ-агентами и LLM.
12 месяцев работал в Cursor, сейчас переходит на Claude Code.
Хорошая чуйка на продукт, дизайн/эстетику. Любит строить бизнес и агентов.
В экзистенциальном кризисе, ищет себя. Финансово ограничен.
Главный интерес: инсайты для улучшения воркфлоу, возможности Claude Code, новые AI-инструменты."""


# ── Data ──────────────────────────────────────────────────────────────────────

_BROKEN_MODELS = {"google/gemini-3.1-flash-lite-preview", "google/gemini-3.1-flash"}
DEFAULT_MODEL = "anthropic/claude-3.5-haiku"


def load() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Corrupt data.json, resetting to defaults: {e}")
            data = {}
        data.setdefault("channels", DEFAULT_CHANNELS[:])
        data.setdefault("focus_auto_reset", False)
        # Remove any leaked key from the data file
        data.pop("openrouter_key", None)
        # Migrate away from broken/non-existent models
        if data.get("model") in _BROKEN_MODELS:
            data["model"] = DEFAULT_MODEL
        return data
    return {
        "description": DEFAULT_PROFILE,
        "current_focus": "",
        "focus_auto_reset": False,
        "model": DEFAULT_MODEL,
        "channels": DEFAULT_CHANNELS[:],
        "last_digest": "",
        "last_digest_time": "",
        "interaction_history": [],
    }


def save(data: dict):
    content = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(DATA_FILE)


def add_history(data: dict, entry: str):
    h = data.setdefault("interaction_history", [])
    h.append(f"{datetime.now().strftime('%d.%m %H:%M')} — {entry[:120]}")
    data["interaction_history"] = h[-20:]


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_to_history(digest: str, posts_count: int):
    history = load_history()
    is_error = digest.startswith("Ошибка") or digest.startswith("Не нашёл")
    history.append({
        "id": len(history) + 1,
        "date": datetime.now(MOSCOW).strftime("%Y-%m-%d"),
        "datetime": datetime.now(MOSCOW).isoformat(),
        "digest": digest,
        "posts_count": posts_count,
        "is_error": is_error,
    })
    content = json.dumps(history, ensure_ascii=False, indent=2)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(HISTORY_FILE)


# ── Keyboards ─────────────────────────────────────────────────────────────────

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
        d = history[i]
        did = d.get("id", i + 1)
        date_short = d["date"][5:].replace("-", ".")  # MM.DD
        if d.get("is_error"):
            label = f"❌ {date_short} #{did}"
        else:
            label = f"📰 {date_short} #{did}  ({d['posts_count']} постов)"
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
    reset_label = "🔄 Сброс фокуса после дайджеста: ВКЛ ✅" if auto_reset else "🔄 Сброс фокуса после дайджеста: ВЫКЛ"
    rows.append([InlineKeyboardButton(reset_label, callback_data="toggle_autoreset")])
    return InlineKeyboardMarkup(rows)


def channels_kb(channels: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"❌ {ch}", callback_data=f"rmch|{ch}")] for ch in channels]
    rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="addch")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_settings")])
    return InlineKeyboardMarkup(rows)


# ── Auth ──────────────────────────────────────────────────────────────────────

async def check_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_ID:
        logger.warning(f"Unauthorized access attempt from user {update.effective_user.id}")
        if update.callback_query:
            await _safe_answer(update.callback_query, "⛔ Нет доступа", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ Нет доступа")
        raise ApplicationHandlerStop


# ── Core ──────────────────────────────────────────────────────────────────────

async def do_send_digest(bot, chat_id: int):
    data = load()
    posts = await scrape_all(data["channels"])
    recent = load_history()[-3:]

    # Explicitly pass the key from environment, not from data dict
    user_data_for_ai = data.copy()
    user_data_for_ai["openrouter_key"] = OPENROUTER_KEY

    digest_html, personal_html = await generate_digest(posts, user_data_for_ai, recent_digests=recent)

    # Auto-reset focus after digest if enabled
    if data.get("focus_auto_reset") and data.get("current_focus"):
        data["current_focus"] = ""

    data["last_digest"] = digest_html
    data["last_digest_time"] = datetime.now().isoformat()
    add_history(data, f"Дайджест ({len(posts)} постов)")
    save(data)
    append_to_history(digest_html, len(posts))

    date_str = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    full_text = f"📰 <b>Дайджест {date_str}</b>\n\n{digest_html}"

    # Filter images
    raw_images = [p["image_bytes"] for p in posts if p.get("image_bytes")]
    logger.info(f"Raw images found: {len(raw_images)}")

    approved = []
    if raw_images:
        approved = await filter_images(raw_images, digest_html, OPENROUTER_KEY)
        logger.info(f"Approved images: {len(approved)}/{len(raw_images)}")

    # Step 1: images FIRST (before text)
    if approved:
        media = [InputMediaPhoto(BytesIO(b)) for b in approved[:10]]
        try:
            await bot.send_media_group(chat_id, media)
            logger.info(f"Sent {len(media)} images before digest")
        except Exception as e:
            logger.warning(f"send_media_group failed: {e}")

    # Step 2: digest text
    MAX_LEN = 4096
    if len(full_text) <= MAX_LEN:
        chunks = [full_text]
    else:
        paragraphs = full_text.split("\n\n")
        chunks = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 <= MAX_LEN:
                current = current + ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                while len(para) > MAX_LEN:
                    chunks.append(para[:MAX_LEN])
                    para = para[MAX_LEN:]
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

    # Step 3: personal section as separate message
    if personal_html:
        await bot.send_message(
            chat_id,
            personal_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def job_digest(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(CHAT_ID, "⏳ Собираю дайджест...")
    await do_send_digest(context.bot, CHAT_ID)


async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = load()
    focus = data.get("current_focus", "")
    focus_line = f" Как дела с *{escape_markdown(focus, version=1)}*?" if focus else ""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Прочитал", callback_data="ci_yes"),
        InlineKeyboardButton("❌ Не успел", callback_data="ci_no"),
        InlineKeyboardButton("💬 Поговорить", callback_data="ci_talk"),
    ]])
    await context.bot.send_message(
        CHAT_ID,
        f"Эй, успел глянуть дайджест?{focus_line}",
        reply_markup=kb,
        parse_mode="Markdown",
    )


# ── Callback helpers ──────────────────────────────────────────────────────────

async def _safe_answer(q, text: str = "", show_alert: bool = False):
    """Answer a callback query, ignoring 'query too old' Telegram errors."""
    try:
        await q.answer(text, show_alert=show_alert)
    except BadRequest as e:
        logger.warning(f"callback answer failed (query expired): {e}")


# ── Callback handlers ─────────────────────────────────────────────────────────

async def cb_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    model_id = q.data.split("|", 1)[1]
    data = load()
    data["model"] = model_id
    save(data)
    await _safe_answer(q, f"✅ {model_id}")
    await q.edit_message_reply_markup(reply_markup=settings_kb(model_id, data["channels"], data.get("focus_auto_reset", False)))


async def cb_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    data = load()
    await q.edit_message_text(
        "📡 *Каналы* — нажми ❌ чтобы удалить:",
        reply_markup=channels_kb(data["channels"]),
        parse_mode="Markdown",
    )


async def cb_rmch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ch = q.data.split("|", 1)[1]
    data = load()
    if ch in data["channels"]:
        data["channels"].remove(ch)
        save(data)
    await _safe_answer(q, f"Удалён: {ch}")
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
    data = load()
    await q.edit_message_text(
        "⚙️ *Настройки*",
        reply_markup=settings_kb(data["model"], data["channels"], data.get("focus_auto_reset", False)),
        parse_mode="Markdown",
    )


async def cb_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    page = int(q.data.split("|", 1)[1])
    history = load_history()
    await _safe_answer(q)
    await q.edit_message_text(
        f"📚 *История* ({len(history)} дайджестов)",
        reply_markup=history_kb(history, page),
        parse_mode="Markdown",
    )


async def cb_hv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    idx = int(q.data.split("|", 1)[1])
    history = load_history()
    await _safe_answer(q)
    if idx >= len(history):
        await q.edit_message_text("Запись не найдена.")
        return
    d = history[idx]
    did = d.get("id", idx + 1)
    text = f"📰 <b>Дайджест {d['date']} #{did}</b>\n\n{d['digest']}"
    if len(text) > 4000:
        # Truncate by paragraphs to avoid splitting mid-HTML-tag
        header = f"📰 <b>Дайджест {d['date']} #{did}</b>\n\n"
        body = d["digest"]
        truncated = header
        for para in body.split("\n\n"):
            candidate = truncated + ("\n\n" if truncated != header else "") + para
            if len(candidate) > 3900:
                break
            truncated = candidate
        text = truncated + "\n\n<i>(сокращено)</i>"
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Список", callback_data="hp|0")]])
    await q.edit_message_text(
        text, reply_markup=back_kb, parse_mode="HTML", disable_web_page_preview=True
    )


async def cb_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action = q.data
    data = load()
    if action == "ci_yes":
        await _safe_answer(q, "Огонь! 🔥")
        await q.edit_message_text("Огонь! 🔥 Завтра в 13:00.")
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
    data = load()
    data["focus_auto_reset"] = not data.get("focus_auto_reset", False)
    save(data)
    status = "ВКЛ" if data["focus_auto_reset"] else "ВЫКЛ"
    await _safe_answer(q, f"Авто-сброс фокуса: {status}")
    await q.edit_message_reply_markup(
        reply_markup=settings_kb(data["model"], data["channels"], data["focus_auto_reset"])
    )


async def cb_edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    data = load()
    context.user_data["state"] = "editing_profile"
    await q.edit_message_text(
        f"*Текущий профиль:*\n_{data['description']}_\n\nНапиши новый (/cancel — отмена):",
        parse_mode="Markdown",
    )


# ── Text handler ──────────────────────────────────────────────────────────────

# Keyboard buttons that always take priority over any state
KB_BUTTONS = {"📰 Дайджест", "📚 История", "👤 Профиль", "⚙️ Настройки"}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = load()
    focus = data.get("current_focus", "")

    if text == "/cancel":
        context.user_data.pop("state", None)
        await update.message.reply_text("Отменено.", reply_markup=main_kb(focus))
        return

    # ── Keyboard buttons always win, clear any pending state ─────────────────
    if text in KB_BUTTONS or text.startswith("🎯"):
        context.user_data.pop("state", None)

    if text == "📰 Дайджест":
        await update.message.reply_text("⏳ Читаю каналы...", reply_markup=main_kb(focus))
        await do_send_digest(context.bot, update.effective_chat.id)
        return

    if text == "📚 История":
        history = load_history()
        if not history:
            await update.message.reply_text("История пуста — запусти первый дайджест!", reply_markup=main_kb(focus))
            return
        await update.message.reply_text(
            f"📚 *История* ({len(history)} дайджестов)",
            reply_markup=history_kb(history),
            parse_mode="Markdown",
        )
        return

    if text == "👤 Профиль":
        description = escape_markdown(data["description"], version=1)
        body = f"👤 *Профиль*\n\n{description}"
        if focus:
            body += f"\n\n🎯 *Фокус:* {escape_markdown(focus, version=1)}"
        model = escape_markdown(data["model"], version=1)
        channels = ", ".join(escape_markdown(ch, version=1) for ch in data["channels"])
        body += f"\n\n🤖 `{model}`\n📡 Каналов: {len(data['channels'])}: {channels}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Изменить", callback_data="edit_profile")]])
        await update.message.reply_text(body, reply_markup=kb, parse_mode="Markdown")
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

    # ── State handlers (only reached for free text, not keyboard buttons) ────
    state = context.user_data.get("state")

    if state == "editing_profile":
        context.user_data.pop("state")
        data["description"] = text
        add_history(data, "Обновил профиль")
        save(data)
        await update.message.reply_text("✅ Профиль обновлён!", reply_markup=main_kb(focus))
        return

    if state == "editing_focus":
        context.user_data.pop("state")
        data["current_focus"] = text
        add_history(data, f"Фокус: {text}")
        save(data)
        await update.message.reply_text(f"✅ Фокус: {text}", reply_markup=main_kb(text))
        return

    if state == "adding_channel":
        context.user_data.pop("state")
        ch = text.lstrip("@").strip()
        if ch and ch not in data["channels"]:
            data["channels"].append(ch)
            save(data)
            await update.message.reply_text(f"✅ Канал {ch} добавлен!", reply_markup=main_kb(focus))
        else:
            await update.message.reply_text("Уже есть или некорректное имя.", reply_markup=main_kb(focus))
        return

    # Free AI chat
    add_history(data, f"Msg: {text[:80]}")
    save(data)
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    # Explicitly pass the key from environment
    user_data_for_ai = data.copy()
    user_data_for_ai["openrouter_key"] = OPENROUTER_KEY

    reply = await chat_response(text, user_data_for_ai)
    data2 = load()
    add_history(data2, f"Bot: {reply[:80]}")
    save(data2)
    await update.message.reply_text(reply, reply_markup=main_kb(focus))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(TypeHandler(Update, check_owner), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))

    app.add_handler(CallbackQueryHandler(cb_model, pattern=r"^model\|"))
    app.add_handler(CallbackQueryHandler(cb_channels, pattern="^channels$"))
    app.add_handler(CallbackQueryHandler(cb_rmch, pattern=r"^rmch\|"))
    app.add_handler(CallbackQueryHandler(cb_addch, pattern="^addch$"))
    app.add_handler(CallbackQueryHandler(cb_back_settings, pattern="^back_settings$"))
    app.add_handler(CallbackQueryHandler(cb_hp, pattern=r"^hp\|"))
    app.add_handler(CallbackQueryHandler(cb_hv, pattern=r"^hv\|"))
    app.add_handler(CallbackQueryHandler(cb_checkin, pattern=r"^ci_"))
    app.add_handler(CallbackQueryHandler(cb_toggle_autoreset, pattern="^toggle_autoreset$"))
    app.add_handler(CallbackQueryHandler(cb_edit_profile, pattern="^edit_profile$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    jq = app.job_queue
    jq.run_daily(job_digest, time=time(13, 0, tzinfo=MOSCOW), name="daily_digest")
    jq.run_daily(job_checkin, time=time(18, 0, tzinfo=MOSCOW), name="daily_checkin")

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load()
    await update.message.reply_text(
        "Привет! Твой персональный дайджест-бот 🤖\n\n"
        "• *13:00* — дайджест из каналов\n"
        "• *18:00* — чекин\n\n"
        "Кнопки снизу 👇",
        reply_markup=main_kb(data.get("current_focus", "")),
        parse_mode="Markdown",
    )


if __name__ == "__main__":
    main()
