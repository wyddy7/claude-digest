"""Offline unit tests for handlers/settings.py.

No network, no Supabase, no PTB application. db.* is monkeypatched; Telegram
types are minimal fakes. Synthetic user ids only (111111111-style).

Covers:
- show_settings renders the settings keyboard.
- show_focus_prompt sets the settings_substate.
- handle_text (editing_focus) saves focus and clears sub-state.
- handle_text (adding_channel) happy path.
- handle_text adding_channel rejects invalid name.
- handle_text adding_channel rejects duplicate.
- handle_text adding_channel enforces channels_max limit.
- cb(s|model|...) persists model and refreshes keyboard.
- cb(s|rmch|...) removes the channel and refreshes keyboard.
- cb(s|toggle_autoreset) flips the flag.
- cb(s|addch) sets the adding_channel sub-state.
- cb(s|back) re-renders the settings screen.
- cb(s|channels) renders the channels screen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import db as db_module
from handlers import settings as settings_mod

USER_ID = "uuid-test-user"
TG_USER_ID = 222222222

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
        self.answers: list = []
        self._edits: list = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))

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


def _fake_settings(channels=None, model=None, focus="", auto_reset=False):
    return {
        "user_id": USER_ID,
        "channels": channels if channels is not None else ["chan_a"],
        "model": model or db_module.DEFAULT_MODEL,
        "current_focus": focus,
        "focus_auto_reset": auto_reset,
        "limits": {},
    }


# ── show_settings ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_settings_renders(monkeypatch):
    settings = _fake_settings(channels=["chan_a", "chan_b"])
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))

    upd = make_update(text="⚙️ Настройки")
    ctx = FakeContext()
    await settings_mod.show_settings(upd, ctx)

    assert upd.effective_message.replies, "should have replied"
    text, kw = upd.effective_message.replies[0]
    assert "Настройки" in text
    assert kw.get("reply_markup") is not None


# ── show_focus_prompt ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_focus_prompt_sets_substate(monkeypatch):
    settings = _fake_settings(focus="AI агенты")
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))

    upd = make_update(text="🎯 AI агенты")
    ctx = FakeContext()
    await settings_mod.show_focus_prompt(upd, ctx)

    assert ctx.user_data["settings_substate"] == "editing_focus"
    assert upd.effective_message.replies, "should have replied"
    # Current focus should appear in the prompt
    reply_text = upd.effective_message.replies[0][0]
    assert "AI агенты" in reply_text


# ── handle_text: editing_focus ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_text_editing_focus_saves_and_clears(monkeypatch):
    saved: list = []
    monkeypatch.setattr(
        db_module, "save_settings",
        AsyncMock(side_effect=lambda uid, fields: saved.append(fields) or {})
    )

    upd = make_update(text="LLM инфра")
    ctx = FakeContext()
    ctx.user_data["settings_substate"] = "editing_focus"

    consumed = await settings_mod.handle_text(upd, ctx)
    assert consumed is True
    assert "settings_substate" not in ctx.user_data
    assert saved[0]["current_focus"] == "LLM инфра"


@pytest.mark.asyncio
async def test_handle_text_not_consumed_when_no_substate():
    upd = make_update(text="random text")
    ctx = FakeContext()
    consumed = await settings_mod.handle_text(upd, ctx)
    assert consumed is False


# ── handle_text: adding_channel — happy path ─────────────────────────────────


@pytest.mark.asyncio
async def test_handle_text_adding_channel_ok(monkeypatch):
    existing = ["chan_a"]
    settings = _fake_settings(channels=list(existing))
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=15))
    saved: list = []
    monkeypatch.setattr(
        db_module, "save_settings",
        AsyncMock(side_effect=lambda uid, fields: saved.append(fields) or {})
    )

    upd = make_update(text="@new_channel")
    ctx = FakeContext()
    ctx.user_data["settings_substate"] = "adding_channel"

    consumed = await settings_mod.handle_text(upd, ctx)
    assert consumed is True
    assert "settings_substate" not in ctx.user_data
    assert saved[0]["channels"] == ["chan_a", "new_channel"]


# ── handle_text: adding_channel — invalid name ───────────────────────────────


@pytest.mark.asyncio
async def test_handle_text_adding_channel_invalid_name(monkeypatch):
    settings = _fake_settings(channels=[])
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=15))
    save_mock = AsyncMock()
    monkeypatch.setattr(db_module, "save_settings", save_mock)

    upd = make_update(text="x!x")  # invalid: contains non-permitted chars
    ctx = FakeContext()
    ctx.user_data["settings_substate"] = "adding_channel"

    await settings_mod.handle_text(upd, ctx)
    save_mock.assert_not_called()


# ── handle_text: adding_channel — duplicate ───────────────────────────────────


@pytest.mark.asyncio
async def test_handle_text_adding_channel_duplicate(monkeypatch):
    settings = _fake_settings(channels=["chan_a"])
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=15))
    save_mock = AsyncMock()
    monkeypatch.setattr(db_module, "save_settings", save_mock)

    upd = make_update(text="chan_a")
    ctx = FakeContext()
    ctx.user_data["settings_substate"] = "adding_channel"

    await settings_mod.handle_text(upd, ctx)
    save_mock.assert_not_called()
    reply_text = upd.message.replies[0][0]
    assert "уже" in reply_text.lower()


# ── handle_text: adding_channel — limit cap ──────────────────────────────────


@pytest.mark.asyncio
async def test_handle_text_adding_channel_limit_hit(monkeypatch):
    """When channels_max is reached, the add is rejected with the limit message."""
    settings = _fake_settings(channels=["chan_a", "chan_b"])
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=2))
    save_mock = AsyncMock()
    monkeypatch.setattr(db_module, "save_settings", save_mock)

    upd = make_update(text="chan_c")
    ctx = FakeContext()
    ctx.user_data["settings_substate"] = "adding_channel"

    await settings_mod.handle_text(upd, ctx)
    save_mock.assert_not_called()
    reply_text = upd.message.replies[0][0]
    assert "2" in reply_text  # cap value echoed in message


# ── cb: model ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_model_saves_and_refreshes_keyboard(monkeypatch):
    saved: list = []
    settings = _fake_settings(model=db_module.DEFAULT_MODEL)
    monkeypatch.setattr(
        db_module, "save_settings",
        AsyncMock(side_effect=lambda uid, fields: saved.append(fields) or {})
    )
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))

    upd = make_update(callback_data="s|model|anthropic/claude-sonnet-4.6")
    ctx = FakeContext()
    await settings_mod.cb(upd, ctx)

    assert saved[0]["model"] == "anthropic/claude-sonnet-4.6"
    # keyboard refresh was called
    q = upd.callback_query
    assert any(e[0] == "markup" for e in q._edits)


# ── cb: rmch ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_rmch_removes_channel(monkeypatch):
    settings = _fake_settings(channels=["chan_a", "chan_b"])
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))
    saved: list = []
    monkeypatch.setattr(
        db_module, "save_settings",
        AsyncMock(side_effect=lambda uid, fields: saved.append(fields) or {})
    )

    upd = make_update(callback_data="s|rmch|chan_a")
    ctx = FakeContext()
    await settings_mod.cb(upd, ctx)

    assert saved[0]["channels"] == ["chan_b"]


# ── cb: toggle_autoreset ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_toggle_autoreset_flips(monkeypatch):
    settings_off = _fake_settings(auto_reset=False)
    settings_after = _fake_settings(auto_reset=True)

    load_calls: list = []
    async def _load(uid):
        load_calls.append(uid)
        # First call returns off, second call (after save) returns on.
        return settings_off if len(load_calls) == 1 else settings_after

    monkeypatch.setattr(db_module, "load_settings", _load)
    saved: list = []
    monkeypatch.setattr(
        db_module, "save_settings",
        AsyncMock(side_effect=lambda uid, fields: saved.append(fields) or {})
    )

    upd = make_update(callback_data="s|toggle_autoreset")
    ctx = FakeContext()
    await settings_mod.cb(upd, ctx)

    assert saved[0]["focus_auto_reset"] is True
    q = upd.callback_query
    assert any(e[0] == "markup" for e in q._edits)


# ── cb: addch ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_addch_sets_substate():
    upd = make_update(callback_data="s|addch")
    ctx = FakeContext()
    await settings_mod.cb(upd, ctx)

    assert ctx.user_data.get("settings_substate") == "adding_channel"
    q = upd.callback_query
    # Message should have been edited to show the prompt
    assert q._edits


# ── cb: channels ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_channels_renders_list(monkeypatch):
    settings = _fake_settings(channels=["chan_a", "chan_b"])
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))

    upd = make_update(callback_data="s|channels")
    ctx = FakeContext()
    await settings_mod.cb(upd, ctx)

    q = upd.callback_query
    assert any(e[0] == "text" for e in q._edits)


# ── cb: back ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cb_back_renders_settings(monkeypatch):
    settings = _fake_settings()
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value=settings))

    upd = make_update(callback_data="s|back")
    ctx = FakeContext()
    await settings_mod.cb(upd, ctx)

    q = upd.callback_query
    assert any(e[0] == "text" for e in q._edits)
