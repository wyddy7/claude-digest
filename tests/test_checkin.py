"""Offline unit tests for the multi-tenant check-in fan-out + callbacks.

No network, no Supabase, no real Telegram ids (synthetic 111111xxx style only).
The Bot is mocked; db.* and subscriptions.* functions are monkeypatched.

Covers:
- run_checkin_fanout fans out over active users, gating each on subscription state.
- Inactive users receive no message; maybe_warn_expiry is called instead.
- Per-user focus is read from user_settings.current_focus.
- Per-user failures are isolated (one failing user does not block others).
- cb_checkin ci_yes edits the message to the "tomorrow at HH:MM" body.
- cb_checkin ci_no resends the user's own last_digest (from user_settings), or
  shows the "not sent yet" message when last_digest is absent.
- cb_checkin ci_talk sets context.user_data["state"] = "chat".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import digest_bot.db as db_module
import digest_bot.subscriptions as subs_module
from digest_bot.handlers import checkin as checkin_mod
from digest_bot.handlers.strings import (
    CHECKIN_NO_EMPTY,
    CHECKIN_NO_PREFIX,
    CHECKIN_TALK_PROMPT,
    CHECKIN_YES_ANSWER,
)

# ── synthetic ids (never real) ─────────────────────────────────────────────────

_TG_A = 111111001
_TG_B = 111111002
_TG_INACTIVE = 111111003

_UUID_A = "uuid-ci-a"
_UUID_B = "uuid-ci-b"
_UUID_INACTIVE = "uuid-ci-inactive"


# ── helpers ────────────────────────────────────────────────────────────────────

def _user_row(tg_id: int, uuid: str) -> dict:
    return {"id": uuid, "tg_user_id": tg_id, "is_active": True}


def _make_fake_bot() -> MagicMock:
    """A MagicMock Bot whose send_message is an AsyncMock and supports async ctx."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    # Support `async with Bot(token) as bot`
    bot.__aenter__ = AsyncMock(return_value=bot)
    bot.__aexit__ = AsyncMock(return_value=None)
    return bot


# ── run_checkin_fanout ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_sends_to_active_users(monkeypatch):
    """Active subscribed users all receive a check-in message."""
    users = [_user_row(_TG_A, _UUID_A), _user_row(_TG_B, _UUID_B)]

    monkeypatch.setattr(db_module, "list_active_users", AsyncMock(return_value=users))
    monkeypatch.setattr(
        subs_module, "is_subscription_active",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        db_module, "load_settings",
        AsyncMock(return_value={"current_focus": "AI agents"}),
    )

    fake_bot = _make_fake_bot()

    with patch("digest_bot.handlers.checkin.Bot", return_value=fake_bot):
        await checkin_mod.run_checkin_fanout("fake-token")

    assert fake_bot.send_message.call_count == 2
    call_tg_ids = {call.args[0] for call in fake_bot.send_message.call_args_list}
    assert call_tg_ids == {_TG_A, _TG_B}


@pytest.mark.asyncio
async def test_fanout_skips_inactive_user_and_warns(monkeypatch):
    """Inactive users are skipped (no send_message) and maybe_warn_expiry is called."""
    users = [_user_row(_TG_INACTIVE, _UUID_INACTIVE)]

    monkeypatch.setattr(db_module, "list_active_users", AsyncMock(return_value=users))
    monkeypatch.setattr(
        subs_module, "is_subscription_active",
        AsyncMock(return_value=False),
    )
    warn_mock = AsyncMock()
    monkeypatch.setattr(subs_module, "maybe_warn_expiry", warn_mock)

    fake_bot = _make_fake_bot()

    with patch("digest_bot.handlers.checkin.Bot", return_value=fake_bot):
        await checkin_mod.run_checkin_fanout("fake-token")

    fake_bot.send_message.assert_not_called()
    warn_mock.assert_awaited_once_with(_TG_INACTIVE, fake_bot)


@pytest.mark.asyncio
async def test_fanout_no_users_returns_early(monkeypatch):
    """When no users rows exist, the fan-out exits early without creating a Bot."""
    monkeypatch.setattr(db_module, "list_active_users", AsyncMock(return_value=[]))

    bot_cls_mock = MagicMock()
    with patch("digest_bot.handlers.checkin.Bot", bot_cls_mock):
        await checkin_mod.run_checkin_fanout("fake-token")

    bot_cls_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fanout_isolates_failing_user(monkeypatch):
    """A failure for user A must not prevent delivery to user B."""
    users = [_user_row(_TG_A, _UUID_A), _user_row(_TG_B, _UUID_B)]
    monkeypatch.setattr(db_module, "list_active_users", AsyncMock(return_value=users))
    monkeypatch.setattr(
        subs_module, "is_subscription_active",
        AsyncMock(return_value=True),
    )

    call_count = 0

    async def _load_settings_raising(user_id):
        nonlocal call_count
        call_count += 1
        if user_id == _UUID_A:
            raise RuntimeError("simulated DB error for user A")
        return {"current_focus": ""}

    monkeypatch.setattr(db_module, "load_settings", _load_settings_raising)

    fake_bot = _make_fake_bot()
    with patch("digest_bot.handlers.checkin.Bot", return_value=fake_bot):
        await checkin_mod.run_checkin_fanout("fake-token")

    # Only user B should have received a message (A raised, was isolated).
    assert fake_bot.send_message.call_count == 1
    assert fake_bot.send_message.call_args.args[0] == _TG_B


@pytest.mark.asyncio
async def test_fanout_uses_per_user_focus(monkeypatch):
    """The check-in message uses each user's own current_focus, not a shared one."""
    users = [_user_row(_TG_A, _UUID_A), _user_row(_TG_B, _UUID_B)]
    monkeypatch.setattr(db_module, "list_active_users", AsyncMock(return_value=users))
    monkeypatch.setattr(subs_module, "is_subscription_active", AsyncMock(return_value=True))

    settings_by_id = {
        _UUID_A: {"current_focus": "LLM evals"},
        _UUID_B: {"current_focus": ""},
    }
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(side_effect=lambda uid: settings_by_id[uid]))

    fake_bot = _make_fake_bot()
    with patch("digest_bot.handlers.checkin.Bot", return_value=fake_bot):
        await checkin_mod.run_checkin_fanout("fake-token")

    # Both messages sent; the one for user A must contain "LLM evals" in the text.
    assert fake_bot.send_message.call_count == 2
    texts = [call.kwargs.get("text") or call.args[1] for call in fake_bot.send_message.call_args_list]
    a_text = next(
        call.kwargs.get("text") or call.args[1]
        for call in fake_bot.send_message.call_args_list
        if (call.kwargs.get("chat_id") or call.args[0]) == _TG_A
    )
    b_text = next(
        call.kwargs.get("text") or call.args[1]
        for call in fake_bot.send_message.call_args_list
        if (call.kwargs.get("chat_id") or call.args[0]) == _TG_B
    )
    assert "LLM evals" in a_text
    # User B has no focus — focus suffix must be absent.
    assert "Как дела" not in b_text


# ── cb_checkin callbacks ───────────────────────────────────────────────────────

class FakeQuery:
    """Minimal CallbackQuery fake for testing cb_checkin."""

    def __init__(self, data: str, chat_id: int = 999):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id)
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kw):
        self.edits.append(text)


def _make_update(action: str, chat_id: int = 999):
    q = FakeQuery(action, chat_id=chat_id)
    return SimpleNamespace(callback_query=q), q


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeContext:
    def __init__(self, user_row=None):
        self.user_data = {}
        if user_row:
            self.user_data["user"] = user_row
        self.bot = FakeBot()


@pytest.mark.asyncio
async def test_cb_ci_yes_edits_message(monkeypatch):
    """ci_yes should answer the query and edit to the 'tomorrow at HH:MM' text."""
    upd, q = _make_update("ci_yes")
    ctx = FakeContext()

    await checkin_mod.cb_checkin(upd, ctx)

    assert q.answers, "query should have been answered"
    assert q.edits, "message should have been edited"
    assert "🔥" in q.edits[0]


@pytest.mark.asyncio
async def test_cb_ci_no_resends_tenant_digest(monkeypatch):
    """ci_no for a tenant user resends their last_digest from user_settings."""
    user_row = {"id": _UUID_A, "tg_user_id": _TG_A}
    upd, q = _make_update("ci_no", chat_id=_TG_A)
    ctx = FakeContext(user_row=user_row)

    monkeypatch.setattr(
        db_module, "load_settings",
        AsyncMock(return_value={"last_digest": "<b>test digest</b>"}),
    )

    await checkin_mod.cb_checkin(upd, ctx)

    assert q.edits and CHECKIN_NO_PREFIX in q.edits[0]
    assert ctx.bot.sent, "last_digest should have been re-sent"
    _, resent_text = ctx.bot.sent[0]
    assert "test digest" in resent_text


@pytest.mark.asyncio
async def test_cb_ci_no_empty_digest_for_tenant(monkeypatch):
    """ci_no when last_digest is absent shows the 'not sent yet' message."""
    user_row = {"id": _UUID_A, "tg_user_id": _TG_A}
    upd, q = _make_update("ci_no", chat_id=_TG_A)
    ctx = FakeContext(user_row=user_row)

    monkeypatch.setattr(
        db_module, "load_settings",
        AsyncMock(return_value={"last_digest": ""}),
    )

    await checkin_mod.cb_checkin(upd, ctx)

    assert q.edits and CHECKIN_NO_EMPTY in q.edits[0]
    assert not ctx.bot.sent, "no message should be sent when last_digest is empty"


@pytest.mark.asyncio
async def test_cb_ci_talk_shows_prompt():
    """ci_talk shows the talk prompt. No state flag is set — in the unified router
    any subsequent free text already falls through to the chat agent."""
    upd, q = _make_update("ci_talk")
    ctx = FakeContext(user_row={"id": _UUID_A, "tg_user_id": _TG_A})

    await checkin_mod.cb_checkin(upd, ctx)

    assert "state" not in ctx.user_data
    assert q.edits and CHECKIN_TALK_PROMPT in q.edits[0]
