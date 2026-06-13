"""Surface 6 — 📚 История for every user (unified multi-tenant path).

Paginated list of the caller's digest history read from the `digests` table
filtered by user_id — users never see each other's rows.

Callback namespace: h| (history page = h|p|<page>, history view = h|v|<idx>).

All user-facing text is imported from handlers.strings.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import digest_bot.db as db
from digest_bot.handlers.strings import (
    HISTORY_EMPTY,
    HISTORY_HEADER_TEMPLATE,
    HISTORY_NOT_FOUND,
    HISTORY_TRUNCATED_SUFFIX,
)

logger = logging.getLogger(__name__)

_PER_PAGE = 5
_MAX_VIEWER_LEN = 4000  # leave headroom inside Telegram's 4096-char limit


# ─── inline keyboard ──────────────────────────────────────────────────────────

def _history_kb(rows: list[dict], page: int) -> InlineKeyboardMarkup:
    """Build the paginated history list keyboard from DB rows (newest-first)."""
    total = len(rows)
    start = page * _PER_PAGE
    end = min(start + _PER_PAGE, total)
    kb_rows = []
    for i in range(start, end):
        item = rows[i]
        date_short = (item.get("date") or "")[-5:].replace("-", ".")
        count = item.get("posts_count", 0)
        label = (
            f"❌ {date_short} #{i + 1}"
            if item.get("is_error")
            else f"📰 {date_short} #{i + 1} ({count} постов)"
        )
        kb_rows.append([InlineKeyboardButton(label, callback_data=f"h|v|{i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"h|p|{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"h|p|{page + 1}"))
    if nav:
        kb_rows.append(nav)
    return InlineKeyboardMarkup(kb_rows)


def _truncate_viewer(date: str, num: int, digest_html: str) -> str:
    """Return a viewer text that fits within Telegram's limit.
    If the full body fits, return it verbatim; otherwise trim at a paragraph
    boundary and append the truncation suffix."""
    header = f"📰 <b>Дайджест {date} #{num}</b>\n\n"
    full = header + digest_html
    if len(full) <= _MAX_VIEWER_LEN:
        return full
    truncated = header
    for para in digest_html.split("\n\n"):
        candidate = truncated + ("\n\n" if truncated != header else "") + para
        if len(candidate) + len(HISTORY_TRUNCATED_SUFFIX) > _MAX_VIEWER_LEN:
            break
        truncated = candidate
    return truncated + HISTORY_TRUNCATED_SUFFIX


# ─── entry points ─────────────────────────────────────────────────────────────

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📚 История — entry point from the reply-keyboard button."""
    user = context.user_data["user"]
    user_id = user["id"]

    rows = await db.load_user_history(user_id)
    if not rows:
        await update.effective_message.reply_text(HISTORY_EMPTY)
        return

    header = HISTORY_HEADER_TEMPLATE.format(count=len(rows))
    await update.effective_message.reply_text(
        header,
        reply_markup=_history_kb(rows, page=0),
        parse_mode="HTML",
    )


async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unified callback for h|p|<page> (list navigation) and h|v|<idx> (viewer).

    The callback_data encodes the action and parameter after a double-pipe:
      h|p|<page>  → refresh the paginated list at the given page
      h|v|<idx>   → show the digest at position <idx> in the current history
    """
    q = update.callback_query
    try:
        await q.answer()
    except Exception as e:
        logger.debug("history cb answer failed (non-fatal): %s", e)

    user = context.user_data.get("user")
    if not user:
        await q.edit_message_text(HISTORY_NOT_FOUND)
        return

    user_id = user["id"]
    # callback_data format: h|<action>|<param>
    parts = q.data.split("|", 2)
    action = parts[1] if len(parts) > 1 else ""
    param = parts[2] if len(parts) > 2 else "0"

    rows = await db.load_user_history(user_id)

    if action == "p":
        page = int(param)
        header = HISTORY_HEADER_TEMPLATE.format(count=len(rows))
        await q.edit_message_text(
            header,
            reply_markup=_history_kb(rows, page=page),
            parse_mode="HTML",
        )

    elif action == "v":
        idx = int(param)
        if idx >= len(rows):
            await q.edit_message_text(HISTORY_NOT_FOUND)
            return
        item = rows[idx]
        date_str = item.get("date") or ""
        num = idx + 1
        text = _truncate_viewer(date_str, num, item.get("digest_html") or "")
        back_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Список", callback_data="h|p|0")]]
        )
        await q.edit_message_text(
            text,
            reply_markup=back_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
