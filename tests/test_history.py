"""Offline unit tests for handlers/history.py.

No network, no Supabase, no PTB application. db.load_user_history is
monkeypatched; Telegram types are minimal fakes. Synthetic user ids only.

Covers:
- show_history with empty history replies with the empty-state message.
- show_history with rows sends the header + inline keyboard.
- cb(h|p|0) re-renders the paginated list header with the keyboard.
- cb(h|v|0) renders the digest viewer for index 0.
- cb(h|v|<out-of-range>) replies with the not-found string.
- _truncate_viewer short body returned verbatim.
- _truncate_viewer long body trimmed with the truncation suffix.
- _history_kb navigation buttons appear when total > _PER_PAGE.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import db as db_module
from handlers import history as history_mod
from handlers.strings import (
    HISTORY_EMPTY,
    HISTORY_NOT_FOUND,
    HISTORY_TRUNCATED_SUFFIX,
)

USER_ID = "uuid-history-test"
TG_USER_ID = 333333333


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))
        return SimpleNamespace(edit_text=AsyncMock())


class FakeQuery:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage()
        self._edits: list = []

    async def answer(self, text="", show_alert=False):
        pass

    async def edit_message_text(self, text, **kw):
        self._edits.append(("text", text, kw))

    async def edit_message_reply_markup(self, **kw):
        self._edits.append(("markup", kw))


class FakeContext:
    def __init__(self):
        self.user_data: dict = {
            "user": {"id": USER_ID, "tg_user_id": TG_USER_ID},
        }


def make_update(*, text=None, callback_data=None):
    msg = FakeMessage(text or "")
    q = FakeQuery(callback_data, msg) if callback_data is not None else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=TG_USER_ID),
        message=msg if callback_data is None else None,
        callback_query=q,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=TG_USER_ID),
    )


def _fake_row(idx: int, *, is_error: bool = False) -> dict:
    return {
        "id": idx + 1,
        "user_id": USER_ID,
        "date": f"2026-06-{idx + 1:02d}",
        "digest_html": f"<b>Дайджест {idx + 1}</b>\n\nТекст #{idx + 1}",
        "posts_count": 5 + idx,
        "is_error": is_error,
    }


# ── show_history: empty ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_history_empty(monkeypatch):
    monkeypatch.setattr(db_module, "load_user_history", AsyncMock(return_value=[]))

    upd = make_update(text="📚 История")
    ctx = FakeContext()
    await history_mod.show_history(upd, ctx)

    assert upd.effective_message.replies, "should have replied"
    text, _ = upd.effective_message.replies[0]
    assert text == HISTORY_EMPTY


# ── show_history: rows present ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_history_with_rows(monkeypatch):
    rows = [_fake_row(i) for i in range(3)]
    monkeypatch.setattr(db_module, "load_user_history", AsyncMock(return_value=rows))

    upd = make_update(text="📚 История")
    ctx = FakeContext()
    await history_mod.show_history(upd, ctx)

    assert upd.effective_message.replies
    text, kw = upd.effective_message.replies[0]
    assert "3" in text  # row count shown
    assert kw.get("reply_markup") is not None


# ── cb: list navigation (h|p|0) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_page_rerenders_list(monkeypatch):
    rows = [_fake_row(i) for i in range(3)]
    monkeypatch.setattr(db_module, "load_user_history", AsyncMock(return_value=rows))

    upd = make_update(callback_data="h|p|0")
    ctx = FakeContext()
    await history_mod.cb(upd, ctx)

    q = upd.callback_query
    assert any(e[0] == "text" for e in q._edits), "should have edited the message text"
    edit_text = next(e[1] for e in q._edits if e[0] == "text")
    assert "3" in edit_text


# ── cb: viewer (h|v|0) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_viewer_shows_digest(monkeypatch):
    rows = [_fake_row(0)]
    monkeypatch.setattr(db_module, "load_user_history", AsyncMock(return_value=rows))

    upd = make_update(callback_data="h|v|0")
    ctx = FakeContext()
    await history_mod.cb(upd, ctx)

    q = upd.callback_query
    assert any(e[0] == "text" for e in q._edits)
    edit_text = next(e[1] for e in q._edits if e[0] == "text")
    assert "Дайджест" in edit_text
    assert "2026-06-01" in edit_text


# ── cb: viewer out-of-range ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_viewer_out_of_range(monkeypatch):
    rows = [_fake_row(0)]
    monkeypatch.setattr(db_module, "load_user_history", AsyncMock(return_value=rows))

    upd = make_update(callback_data="h|v|99")
    ctx = FakeContext()
    await history_mod.cb(upd, ctx)

    q = upd.callback_query
    edit_text = next(e[1] for e in q._edits if e[0] == "text")
    assert edit_text == HISTORY_NOT_FOUND


# ── _truncate_viewer: short body verbatim ────────────────────────────────────


def test_truncate_viewer_short_body():
    body = "<b>Дайджест</b>\n\nКороткий текст."
    result = history_mod._truncate_viewer("2026-06-01", 1, body)
    assert body in result
    assert HISTORY_TRUNCATED_SUFFIX not in result


# ── _truncate_viewer: long body trimmed ──────────────────────────────────────


def test_truncate_viewer_long_body():
    # Build a body that exceeds _MAX_VIEWER_LEN when combined with header
    para = "Абзац с содержимым дайджеста. " * 20  # ~600 chars per para
    body = "\n\n".join([para] * 20)  # ~12000+ chars
    result = history_mod._truncate_viewer("2026-06-01", 1, body)
    assert len(result) <= history_mod._MAX_VIEWER_LEN + len(HISTORY_TRUNCATED_SUFFIX)
    assert result.endswith(HISTORY_TRUNCATED_SUFFIX)


# ── _history_kb: nav buttons appear when > PER_PAGE ──────────────────────────


def test_history_kb_next_button_appears():
    rows = [_fake_row(i) for i in range(history_mod._PER_PAGE + 2)]
    kb = history_mod._history_kb(rows, page=0)
    # Flatten all button texts from the keyboard
    all_labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("➡️" in label for label in all_labels), "next button should appear"


def test_history_kb_prev_button_appears():
    rows = [_fake_row(i) for i in range(history_mod._PER_PAGE + 2)]
    kb = history_mod._history_kb(rows, page=1)
    all_labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("⬅️" in label for label in all_labels), "prev button should appear"


# ── history rows are scoped to caller's user_id ───────────────────────────────


@pytest.mark.asyncio
async def test_load_user_history_called_with_user_id(monkeypatch):
    """Verify that load_user_history is called with the caller's user_id, not
    a global query — this is the tenant-scoping guarantee."""
    captured_ids: list = []

    async def _fake_load(user_id, limit=0):
        captured_ids.append(user_id)
        return []

    monkeypatch.setattr(db_module, "load_user_history", _fake_load)

    upd = make_update(text="📚 История")
    ctx = FakeContext()
    await history_mod.show_history(upd, ctx)

    assert captured_ids == [USER_ID], (
        "load_user_history must be called with the caller's user_id"
    )
