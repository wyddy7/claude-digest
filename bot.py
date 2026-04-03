import json
import logging
import os
from datetime import datetime, timedelta
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
from personalization import get_profile_description
from scraper import scrape_channel

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
OWNER_ID = CHAT_ID
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

_BROKEN_MODELS = {"google/gemini-3.1-flash-lite-preview", "google/gemini-3.1-flash"}
DEFAULT_MODEL = "anthropic/claude-3.5-haiku"


def _sanitize_data(data: dict) -> dict:
    clean = dict(data)
    clean.pop("openrouter_key", None)
    clean.pop("description", None)
    clean.setdefault("channels", DEFAULT_CHANNELS[:])
    clean.setdefault("focus_auto_reset", False)
    if clean.get("model") in _BROKEN_MODELS:
        clean["model"] = DEFAULT_MODEL
    return clean


def load() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Corrupt data.json, resetting to defaults: {e}")
            data = {}
        return _sanitize_data(data)
    return {
        "current_focus": "",
        "focus_auto_reset": False,
        "model": DEFAULT_MODEL,
        "channels": DEFAULT_CHANNELS[:],
        "last_digest": "",
        "last_digest_time": "",
        "interaction_history": [],
    }


def save(data: dict):
    content = json.dumps(_sanitize_data(data), ensure_ascii=False, indent=2)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(DATA_FILE)


def add_history(data: dict, entry: str):
    history = data.setdefault("interaction_history", [])
    history.append(f"{datetime.now().strftime('%d.%m %H:%M')} — {entry[:120]}")
    data["interaction_history"] = history[-20:]


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
    history.append(
        {
            "id": len(history) + 1,
            "date": datetime.now(MOSCOW).strftime("%Y-%m-%d"),
            "datetime": datetime.now(MOSCOW).isoformat(),
            "digest": digest,
            "posts_count": posts_count,
            "is_error": is_error,
        }
    )
    content = json.dumps(history, ensure_ascii=False, indent=2)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(HISTORY_FILE)


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


async def check_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_ID:
        logger.warning(f"Unauthorized access attempt from user {update.effective_user.id}")
        if update.callback_query:
            await _safe_answer(update.callback_query, "⛔ Нет доступа", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ Нет доступа")
        raise ApplicationHandlerStop


async def do_send_digest(bot, chat_id: int, status_msg=None):
    async def _update(text: str):
        nonlocal status_msg
        if status_msg is None:
            status_msg = await bot.send_message(chat_id, text)
        else:
            try:
                await status_msg.edit_text(text)
            except Exception:
                pass

    data = load()
    channels = data["channels"]

    all_posts = []
    for i, ch in enumerate(channels):
        await _update(f"⏳ Читаю каналы... {ch} ({i + 1}/{len(channels)})")
        posts = await scrape_channel(ch)
        all_posts.extend(posts)

    await _update(f"🤖 Формулирую дайджест... ({len(all_posts)} постов)")
    recent = load_history()[-3:]

    user_data_for_ai = data.copy()
    user_data_for_ai["openrouter_key"] = OPENROUTER_KEY

    digest_html, personal_html, stats_html = await generate_digest(
        all_posts,
        user_data_for_ai,
        recent_digests=recent,
    )

    raw_images = [post["image_bytes"] for post in all_posts if post.get("image_bytes")]
    approved = []
    if raw_images:
        await _update(f"🖼 Проверяю картинки... ({len(raw_images)} шт)")
        approved = await filter_images(raw_images, digest_html, OPENROUTER_KEY)
        logger.info(f"Approved images: {len(approved)}/{len(raw_images)}")

    await _update("✅ Готово")

    if data.get("focus_auto_reset") and data.get("current_focus"):
        data["current_focus"] = ""

    data["last_digest"] = digest_html
    data["last_digest_time"] = datetime.now().isoformat()
    add_history(data, f"Дайджест ({len(all_posts)} постов)")
    save(data)
    append_to_history(digest_html, len(all_posts))

    date_str = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    full_text = f"📰 <b>Дайджест {date_str}</b>\n\n{digest_html}"

    if approved:
        media = [InputMediaPhoto(BytesIO(image)) for image in approved[:10]]
        try:
            await bot.send_media_group(chat_id, media)
            logger.info(f"Sent {len(media)} images before digest")
        except Exception as e:
            logger.warning(f"send_media_group failed: {e}")

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
    data = load()
    data["model"] = model_id
    save(data)
    await _safe_answer(q, f"✅ {model_id}")
    await q.edit_message_reply_markup(
        reply_markup=settings_kb(model_id, data["channels"], data.get("focus_auto_reset", False))
    )


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
    channel = q.data.split("|", 1)[1]
    data = load()
    if channel in data["channels"]:
        data["channels"].remove(channel)
        save(data)
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
    item = history[idx]
    digest_id = item.get("id", idx + 1)
    text = f"📰 <b>Дайджест {item['date']} #{digest_id}</b>\n\n{item['digest']}"
    if len(text) > 4000:
        header = f"📰 <b>Дайджест {item['date']} #{digest_id}</b>\n\n"
        body = item["digest"]
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
    await q.edit_message_text("Профиль теперь редактируется в config/personalization.yaml")


KB_BUTTONS = {"📰 Дайджест", "📚 История", "👤 Профиль", "⚙️ Настройки"}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = load()
    focus = data.get("current_focus", "")

    if text == "/cancel":
        context.user_data.pop("state", None)
        await update.message.reply_text("Отменено.", reply_markup=main_kb(focus))
        return

    if text in KB_BUTTONS or text.startswith("🎯"):
        context.user_data.pop("state", None)

    if text == "📰 Дайджест":
        status = await update.message.reply_text("⏳ Читаю каналы...", reply_markup=main_kb(focus))
        await do_send_digest(context.bot, update.effective_chat.id, status_msg=status)
        return

    if text == "📚 История":
        history = load_history()
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
        add_history(data, f"Фокус: {text}")
        save(data)
        await update.message.reply_text(f"✅ Фокус: {text}", reply_markup=main_kb(text))
        return

    if state == "adding_channel":
        context.user_data.pop("state")
        channel = text.lstrip("@").strip()
        if channel and channel not in data["channels"]:
            data["channels"].append(channel)
            save(data)
            await update.message.reply_text(f"✅ Канал {channel} добавлен!", reply_markup=main_kb(focus))
        else:
            await update.message.reply_text(
                "Уже есть или некорректное имя.",
                reply_markup=main_kb(focus),
            )
        return

    add_history(data, f"Msg: {text[:80]}")
    save(data)
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    user_data_for_ai = data.copy()
    user_data_for_ai["openrouter_key"] = OPENROUTER_KEY

    reply = await chat_response(text, user_data_for_ai)
    data2 = load()
    add_history(data2, f"Bot: {reply[:80]}")
    save(data2)
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
        f"📅 Дайджест: {fmt(next_time(13, 0))}\n"
        f"💬 Чекин: {fmt(next_time(18, 0))}"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(TypeHandler(Update, check_owner), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("in", cmd_in))
    app.add_handler(CommandHandler("next", cmd_next))

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
