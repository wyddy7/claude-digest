"""Shared digest delivery helpers — one home for the Telegram send mechanics so
the interactive (button), onboarding-preview, and scheduled (cron fan-out) paths
all chunk + send identically.

Kept dependency-light (only the telegram Bot is touched) so both `handlers/` and
`scheduler.py` can import it without an import cycle.
"""

TELEGRAM_MAX_LEN = 4096


def _chunk(full_text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split a message body at the Telegram length limit, paragraph-aware."""
    if len(full_text) <= max_len:
        return [full_text]
    chunks: list[str] = []
    current = ""
    for para in full_text.split("\n\n"):
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
    return chunks


async def send_digest_chunks(bot, chat_id: int, full_text: str,
                             personal_html: str = "", stats_html: str = "") -> None:
    """Chunk + send a digest to one chat (Telegram 4096-char limit), then the
    personal/stats tail as a separate message. Shared by every delivery path."""
    for chunk in _chunk(full_text):
        await bot.send_message(chat_id, chunk, parse_mode="HTML",
                               disable_web_page_preview=True)

    personal_parts = [p for p in [personal_html, stats_html] if p]
    if personal_parts:
        await bot.send_message(
            chat_id, "\n\n".join(personal_parts),
            parse_mode="HTML", disable_web_page_preview=True,
        )


def make_status_updater(bot, chat_id: int):
    """Return an async `on_status(text)` callback that sends the first status
    message and edits it in place on every subsequent call. One implementation
    for the legacy, onboarding, and button paths (was copy-pasted in 3 places)."""
    state = {"msg": None}

    async def on_status(text: str) -> None:
        try:
            if state["msg"] is None:
                state["msg"] = await bot.send_message(chat_id, text)
            else:
                await state["msg"].edit_text(text)
        except Exception:
            # Status UI is best-effort; never let it break delivery.
            pass

    return on_status
