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
from personalization import get_profile_description
from scraper import scrape_all

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
    "рџљЂ Claude Sonnet 4.6": "anthropic/claude-sonnet-4-6",
    "вљЎ Claude 3.5 Haiku": "anthropic/claude-3.5-haiku",
    "рџ§  Claude 3.7 Sonnet": "anthropic/claude-3.7-sonnet",
    "вљЎ Gemini 2.0 Flash": "google/gemini-2.0-flash-001",
    "рџџў GPT-4o Mini": "openai/gpt-4o-mini",
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
    h = data.setdefault("interaction_history", [])
    h.append(f"{datetime.now().strftime('%d.%m %H:%M')} вЂ” {entry[:120]}")
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
    is_error = digest.startswith("РћС€РёР±РєР°") or digest.startswith("РќРµ РЅР°С€С‘Р»")
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
    focus_btn = f"рџЋЇ {focus[:28]}" if focus else "рџЋЇ Р—Р°РґР°С‚СЊ С„РѕРєСѓСЃ"
    return ReplyKeyboardMarkup(
        [
            ["рџ“° Р”Р°Р№РґР¶РµСЃС‚", "рџ“љ РСЃС‚РѕСЂРёСЏ"],
            ["рџ‘¤ РџСЂРѕС„РёР»СЊ", "вљ™пёЏ РќР°СЃС‚СЂРѕР№РєРё"],
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
        date_short = d["date"][5:].replace("-", ".")
        if d.get("is_error"):
            label = f"вќЊ {date_short} #{did}"
        else:
            label = f"рџ“° {date_short} #{did}  ({d['posts_count']} РїРѕСЃС‚РѕРІ)"
        rows.append([InlineKeyboardButton(label, callback_data=f"hv|{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("в¬…пёЏ", callback_data=f"hp|{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("вћЎпёЏ", callback_data=f"hp|{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def settings_kb(current_model: str, channels: list, auto_reset: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for label, model_id in MODELS.items():
        mark = "вњ… " if model_id == current_model else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"model|{model_id}")])
    rows.append([InlineKeyboardButton(f"рџ“Ў РљР°РЅР°Р»С‹ ({len(channels)})", callback_data="channels")])
    reset_label = (
        "рџ”„ РЎР±СЂРѕСЃ С„РѕРєСѓСЃР° РїРѕСЃР»Рµ РґР°Р№РґР¶РµСЃС‚Р°: Р’РљР› вњ…"
        if auto_reset
        else "рџ”„ РЎР±СЂРѕСЃ С„РѕРєСѓСЃР° РїРѕСЃР»Рµ РґР°Р№РґР¶РµСЃС‚Р°: Р’Р«РљР›"
    )
    rows.append([InlineKeyboardButton(reset_label, callback_data="toggle_autoreset")])
    return InlineKeyboardMarkup(rows)


def channels_kb(channels: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"вќЊ {ch}", callback_data=f"rmch|{ch}")] for ch in channels]
    rows.append([InlineKeyboardButton("вћ• Р”РѕР±Р°РІРёС‚СЊ РєР°РЅР°Р»", callback_data="addch")])
    rows.append([InlineKeyboardButton("в†ђ РќР°Р·Р°Рґ", callback_data="back_settings")])
    return InlineKeyboardMarkup(rows)


async def check_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_ID:
        logger.warning(f"Unauthorized access attempt from user {update.effective_user.id}")
        if update.callback_query:
            await _safe_answer(update.callback_query, "в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        elif update.message:
            await update.message.reply_text("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°")
        raise ApplicationHandlerStop


async def do_send_digest(bot, chat_id: int):
    data = load()
    posts = await scrape_all(data["channels"])
    recent = load_history()[-3:]

    user_data_for_ai = data.copy()
    user_data_for_ai["openrouter_key"] = OPENROUTER_KEY

    digest_html, personal_html, stats_html = await generate_digest(
        posts, user_data_for_ai, recent_digests=recent
    )

    if data.get("focus_auto_reset") and data.get("current_focus"):
        data["current_focus"] = ""

    data["last_digest"] = digest_html
    data["last_digest_time"] = datetime.now().isoformat()
    add_history(data, f"Р”Р°Р№РґР¶РµСЃС‚ ({len(posts)} РїРѕСЃС‚РѕРІ)")
    save(data)
    append_to_history(digest_html, len(posts))

    date_str = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    full_text = f"рџ“° <b>Р”Р°Р№РґР¶РµСЃС‚ {date_str}</b>\n\n{digest_html}"

    raw_images = [p["image_bytes"] for p in posts if p.get("image_bytes")]
    logger.info(f"Raw images found: {len(raw_images)}")

    approved = []
    if raw_images:
        approved = await filter_images(raw_images, digest_html, OPENROUTER_KEY)
        logger.info(f"Approved images: {len(approved)}/{len(raw_images)}")

    if approved:
        media = [InputMediaPhoto(BytesIO(b)) for b in approved[:10]]
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

    personal_parts = [p for p in [personal_html, stats_html] if p]
    if personal_parts:
        await bot.send_message(
            chat_id,
            "\n\n".join(personal_parts),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def job_digest(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(CHAT_ID, "вЏі РЎРѕР±РёСЂР°СЋ РґР°Р№РґР¶РµСЃС‚...")
    await do_send_digest(context.bot, CHAT_ID)


async def job_checkin(context: ContextTypes.DEFAULT_TYPE):
    data = load()
    focus = data.get("current_focus", "")
    focus_line = f" РљР°Рє РґРµР»Р° СЃ *{escape_markdown(focus, version=1)}*?" if focus else ""
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("вњ… РџСЂРѕС‡РёС‚Р°Р»", callback_data="ci_yes"),
            InlineKeyboardButton("вќЊ РќРµ СѓСЃРїРµР»", callback_data="ci_no"),
            InlineKeyboardButton("рџ’¬ РџРѕРіРѕРІРѕСЂРёС‚СЊ", callback_data="ci_talk"),
        ]]
    )
    await context.bot.send_message(
        CHAT_ID,
        f"Р­Р№, СѓСЃРїРµР» РіР»СЏРЅСѓС‚СЊ РґР°Р№РґР¶РµСЃС‚?{focus_line}",
        reply_markup=kb,
        parse_mode="Markdown",
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
    await _safe_answer(q, f"вњ… {model_id}")
    await q.edit_message_reply_markup(
        reply_markup=settings_kb(model_id, data["channels"], data.get("focus_auto_reset", False))
    )


async def cb_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    data = load()
    await q.edit_message_text(
        "рџ“Ў *РљР°РЅР°Р»С‹* вЂ” РЅР°Р¶РјРё вќЊ С‡С‚РѕР±С‹ СѓРґР°Р»РёС‚СЊ:",
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
    await _safe_answer(q, f"РЈРґР°Р»С‘РЅ: {ch}")
    await q.edit_message_text(
        "рџ“Ў *РљР°РЅР°Р»С‹*",
        reply_markup=channels_kb(data["channels"]),
        parse_mode="Markdown",
    )


async def cb_addch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    context.user_data["state"] = "adding_channel"
    await q.edit_message_text("Р’РІРµРґРё СЋР·РµСЂРЅРµР№Рј РєР°РЅР°Р»Р° Р±РµР· @:\n\n/cancel вЂ” РѕС‚РјРµРЅР°")


async def cb_back_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    data = load()
    await q.edit_message_text(
        "вљ™пёЏ *РќР°СЃС‚СЂРѕР№РєРё*",
        reply_markup=settings_kb(data["model"], data["channels"], data.get("focus_auto_reset", False)),
        parse_mode="Markdown",
    )


async def cb_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    page = int(q.data.split("|", 1)[1])
    history = load_history()
    await _safe_answer(q)
    await q.edit_message_text(
        f"рџ“љ *РСЃС‚РѕСЂРёСЏ* ({len(history)} РґР°Р№РґР¶РµСЃС‚РѕРІ)",
        reply_markup=history_kb(history, page),
        parse_mode="Markdown",
    )


async def cb_hv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    idx = int(q.data.split("|", 1)[1])
    history = load_history()
    await _safe_answer(q)
    if idx >= len(history):
        await q.edit_message_text("Р—Р°РїРёСЃСЊ РЅРµ РЅР°Р№РґРµРЅР°.")
        return
    d = history[idx]
    did = d.get("id", idx + 1)
    text = f"рџ“° <b>Р”Р°Р№РґР¶РµСЃС‚ {d['date']} #{did}</b>\n\n{d['digest']}"
    if len(text) > 4000:
        header = f"рџ“° <b>Р”Р°Р№РґР¶РµСЃС‚ {d['date']} #{did}</b>\n\n"
        body = d["digest"]
        truncated = header
        for para in body.split("\n\n"):
            candidate = truncated + ("\n\n" if truncated != header else "") + para
            if len(candidate) > 3900:
                break
            truncated = candidate
        text = truncated + "\n\n<i>(СЃРѕРєСЂР°С‰РµРЅРѕ)</i>"
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("в†ђ РЎРїРёСЃРѕРє", callback_data="hp|0")]])
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
        await _safe_answer(q, "РћРіРѕРЅСЊ! рџ”Ґ")
        await q.edit_message_text("РћРіРѕРЅСЊ! рџ”Ґ Р—Р°РІС‚СЂР° РІ 13:00.")
    elif action == "ci_no":
        await _safe_answer(q)
        if data["last_digest"]:
            await q.edit_message_text("Р”РµСЂР¶Рё РґР°Р№РґР¶РµСЃС‚ РµС‰С‘ СЂР°Р·:")
            await context.bot.send_message(
                q.message.chat_id,
                f"рџ“° <b>Р”Р°Р№РґР¶РµСЃС‚</b>\n\n{data['last_digest']}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await q.edit_message_text("Р”Р°Р№РґР¶РµСЃС‚ РµС‰С‘ РЅРµ Р·Р°РїСѓСЃРєР°Р»СЃСЏ.")
    elif action == "ci_talk":
        await _safe_answer(q)
        context.user_data["state"] = "chat"
        await q.edit_message_text("РџРёС€Рё, СЃР»СѓС€Р°СЋ рџ‘‡")


async def cb_toggle_autoreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = load()
    data["focus_auto_reset"] = not data.get("focus_auto_reset", False)
    save(data)
    status = "Р’РљР›" if data["focus_auto_reset"] else "Р’Р«РљР›"
    await _safe_answer(q, f"РђРІС‚Рѕ-СЃР±СЂРѕСЃ С„РѕРєСѓСЃР°: {status}")
    await q.edit_message_reply_markup(
        reply_markup=settings_kb(data["model"], data["channels"], data["focus_auto_reset"])
    )


async def cb_edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_answer(q)
    await q.edit_message_text("Profile editing moved to config/personalization.yaml")


KB_BUTTONS = {"рџ“° Р”Р°Р№РґР¶РµСЃС‚", "рџ“љ РСЃС‚РѕСЂРёСЏ", "рџ‘¤ РџСЂРѕС„РёР»СЊ", "вљ™пёЏ РќР°СЃС‚СЂРѕР№РєРё"}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = load()
    focus = data.get("current_focus", "")

    if text == "/cancel":
        context.user_data.pop("state", None)
        await update.message.reply_text("РћС‚РјРµРЅРµРЅРѕ.", reply_markup=main_kb(focus))
        return

    if text in KB_BUTTONS or text.startswith("рџЋЇ"):
        context.user_data.pop("state", None)

    if text == "рџ“° Р”Р°Р№РґР¶РµСЃС‚":
        await update.message.reply_text("вЏі Р§РёС‚Р°СЋ РєР°РЅР°Р»С‹...", reply_markup=main_kb(focus))
        await do_send_digest(context.bot, update.effective_chat.id)
        return

    if text == "рџ“љ РСЃС‚РѕСЂРёСЏ":
        history = load_history()
        if not history:
            await update.message.reply_text(
                "РСЃС‚РѕСЂРёСЏ РїСѓСЃС‚Р° вЂ” Р·Р°РїСѓСЃС‚Рё РїРµСЂРІС‹Р№ РґР°Р№РґР¶РµСЃС‚!",
                reply_markup=main_kb(focus),
            )
            return
        await update.message.reply_text(
            f"рџ“љ *РСЃС‚РѕСЂРёСЏ* ({len(history)} РґР°Р№РґР¶РµСЃС‚РѕРІ)",
            reply_markup=history_kb(history),
            parse_mode="Markdown",
        )
        return

    if text == "рџ‘¤ РџСЂРѕС„РёР»СЊ":
        description = escape_markdown(get_profile_description(), version=1)
        body = f"рџ‘¤ *РџСЂРѕС„РёР»СЊ*\n\n{description}"
        if focus:
            body += f"\n\nрџЋЇ *Р¤РѕРєСѓСЃ:* {escape_markdown(focus, version=1)}"
        model = escape_markdown(data["model"], version=1)
        channels = ", ".join(escape_markdown(ch, version=1) for ch in data["channels"])
        body += f"\n\nрџ¤– `{model}`\nрџ“Ў РљР°РЅР°Р»РѕРІ: {len(data['channels'])}: {channels}"
        body += "\n\n`config/personalization.yaml`"
        await update.message.reply_text(body, parse_mode="Markdown")
        return

    if text == "вљ™пёЏ РќР°СЃС‚СЂРѕР№РєРё":
        await update.message.reply_text(
            "вљ™пёЏ *РќР°СЃС‚СЂРѕР№РєРё*",
            reply_markup=settings_kb(data["model"], data["channels"], data.get("focus_auto_reset", False)),
            parse_mode="Markdown",
        )
        return

    if text.startswith("рџЋЇ"):
        context.user_data["state"] = "editing_focus"
        prompt_text = f"РўРµРєСѓС‰РёР№ С„РѕРєСѓСЃ: {focus}\n\n" if focus else ""
        await update.message.reply_text(
            f"{prompt_text}РќР° С‡С‚Рѕ С„РѕРєСѓСЃРёСЂРѕРІР°С‚СЊСЃСЏ РІ СЃР»РµРґСѓСЋС‰РµРј РґР°Р№РґР¶РµСЃС‚Рµ?\n/cancel вЂ” РѕС‚РјРµРЅР°",
        )
        return

    state = context.user_data.get("state")

    if state == "editing_focus":
        context.user_data.pop("state")
        data["current_focus"] = text
        add_history(data, f"Р¤РѕРєСѓСЃ: {text}")
        save(data)
        await update.message.reply_text(f"вњ… Р¤РѕРєСѓСЃ: {text}", reply_markup=main_kb(text))
        return

    if state == "adding_channel":
        context.user_data.pop("state")
        ch = text.lstrip("@").strip()
        if ch and ch not in data["channels"]:
            data["channels"].append(ch)
            save(data)
            await update.message.reply_text(f"вњ… РљР°РЅР°Р» {ch} РґРѕР±Р°РІР»РµРЅ!", reply_markup=main_kb(focus))
        else:
            await update.message.reply_text(
                "РЈР¶Рµ РµСЃС‚СЊ РёР»Рё РЅРµРєРѕСЂСЂРµРєС‚РЅРѕРµ РёРјСЏ.",
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
        "РџСЂРёРІРµС‚! РўРІРѕР№ РїРµСЂСЃРѕРЅР°Р»СЊРЅС‹Р№ РґР°Р№РґР¶РµСЃС‚-Р±РѕС‚ рџ¤–\n\n"
        "вЂў *13:00* вЂ” РґР°Р№РґР¶РµСЃС‚ РёР· РєР°РЅР°Р»РѕРІ\n"
        "вЂў *18:00* вЂ” С‡РµРєРёРЅ\n\n"
        "РљРЅРѕРїРєРё СЃРЅРёР·Сѓ рџ‘‡",
        reply_markup=main_kb(data.get("current_focus", "")),
        parse_mode="Markdown",
    )


if __name__ == "__main__":
    main()
