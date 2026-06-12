"""Offline unit tests for the B2 onboarding / gating surface.

No network, no Supabase, no PTB application. db.* is monkeypatched; the Telegram
Update/CallbackQuery/Message/Bot are minimal fakes that record calls. Synthetic
tg ids only (111111111-style).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db as db_module
import subscriptions as subs_module
from handlers import middleware as mw
from handlers import onboarding as onb

OWNER = 111111111
USER = 222222222


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.successful_payment = None

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))
        return SimpleNamespace(edit_text=AsyncMock())


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.message.replies.append((text, kw))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class FakeContext:
    def __init__(self, bot=None):
        self.user_data = {}
        self.bot = bot or FakeBot()


def make_update(*, tg_user_id=USER, text=None, callback_data=None):
    msg = FakeMessage(text or "")
    q = FakeQuery(callback_data, msg) if callback_data is not None else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=tg_user_id),
        message=msg if callback_data is None else None,
        callback_query=q,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=tg_user_id),
    )


# ── invite gate ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invite_gate_blocks_unknown_user(monkeypatch):
    monkeypatch.setattr(db_module, "get_user_by_tg_id", AsyncMock(return_value=None))
    upd = make_update(text="/start")
    ctx = FakeContext()
    from telegram.ext import ApplicationHandlerStop

    with pytest.raises(ApplicationHandlerStop):
        await mw.resolve_user(upd, ctx)
    assert "приглашению" in upd.message.replies[0][0]
    assert "user" not in ctx.user_data


@pytest.mark.asyncio
async def test_owner_flows_through_unified_path(monkeypatch):
    """After the cutover the owner is a normal users row (state='done'). They are
    NOT special-cased: resolve_user attaches the row and dispatches a non-/start
    message into the chat router (no is_owner flag anywhere)."""
    owner_row = {"id": "uuid-owner", "tg_user_id": OWNER, "onboarding_state": "done"}
    monkeypatch.setattr(db_module, "get_user_by_tg_id", AsyncMock(return_value=owner_row))
    from handlers import chat as chat_surface
    route = AsyncMock()
    monkeypatch.setattr(chat_surface, "route_text", route)

    upd = make_update(tg_user_id=OWNER, text="привет")
    ctx = FakeContext()
    from telegram.ext import ApplicationHandlerStop
    with pytest.raises(ApplicationHandlerStop):
        await mw.resolve_user(upd, ctx)

    assert ctx.user_data["user"] is owner_row
    assert "is_owner" not in ctx.user_data
    route.assert_awaited_once()


# ── requires_tier ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_requires_tier_allows_active_trial():
    calls = []

    @mw.requires_tier("trial_or_paid")
    async def handler(update, context):
        calls.append(True)

    ctx = FakeContext()
    ctx.user_data["user"] = {
        "tg_user_id": USER,
        "trial_ends_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    }
    await handler(make_update(text="x"), ctx)
    assert calls == [True]


@pytest.mark.asyncio
async def test_requires_tier_gates_expired(monkeypatch):
    gate = AsyncMock()
    import handlers.subscription as sub
    monkeypatch.setattr(sub, "show_gate", gate)
    calls = []

    @mw.requires_tier("trial_or_paid")
    async def handler(update, context):
        calls.append(True)

    ctx = FakeContext()
    ctx.user_data["user"] = {
        "tg_user_id": USER,
        "trial_ends_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "pro_until": None,
    }
    await handler(make_update(text="x"), ctx)
    assert calls == []  # body never ran (no LLM spend)
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_passes_gate_as_active_pro():
    """The owner has no bypass after the cutover — they pass the gate the same
    way any active pro user does, off a far-future pro_until on their row."""
    calls = []

    @mw.requires_tier("trial_or_paid")
    async def handler(update, context):
        calls.append(True)

    ctx = FakeContext()
    ctx.user_data["user"] = {
        "tg_user_id": OWNER,
        "pro_until": (datetime.now(timezone.utc) + timedelta(days=365 * 100)).isoformat(),
    }
    await handler(make_update(text="x"), ctx)
    assert calls == [True]


# ── topic merge / cap ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_topic_toggle_merges_and_dedups(monkeypatch):
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=15))
    # Seed topics deterministically.
    monkeypatch.setattr(onb, "_TOPICS", {"ai": ["a", "b"], "dev": ["b", "c"]})
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}

    upd = make_update(callback_data="onb|topic|ai")
    await onb._toggle_topic(upd, ctx, "ai")
    assert ctx.user_data["onb_channels"] == ["a", "b"]

    upd2 = make_update(callback_data="onb|topic|dev")
    await onb._toggle_topic(upd2, ctx, "dev")
    assert ctx.user_data["onb_channels"] == ["a", "b", "c"]  # b deduped


@pytest.mark.asyncio
async def test_topic_merge_respects_cap(monkeypatch):
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=2))
    monkeypatch.setattr(onb, "_TOPICS", {"ai": ["a", "b", "c", "d"]})
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    upd = make_update(callback_data="onb|topic|ai")
    await onb._toggle_topic(upd, ctx, "ai")
    assert ctx.user_data["onb_channels"] == ["a", "b"]  # truncated to cap


@pytest.mark.asyncio
async def test_channels_done_requires_one(monkeypatch):
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    ctx.user_data["onb_channels"] = []
    upd = make_update(callback_data="onb|ch_done")
    await onb._channels_done(upd, ctx)
    assert upd.callback_query.answers[-1][1] is True  # show_alert popup, no advance


@pytest.mark.asyncio
async def test_channels_done_persists_and_advances(monkeypatch):
    save = AsyncMock()
    upd_fields = AsyncMock()
    monkeypatch.setattr(db_module, "save_settings", save)
    monkeypatch.setattr(db_module, "update_user_fields", upd_fields)
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    ctx.user_data["onb_channels"] = ["chan_a", "chan_b"]
    upd = make_update(callback_data="onb|ch_done")
    await onb._channels_done(upd, ctx)
    save.assert_awaited_once()
    assert save.await_args.args[1]["channels"] == ["chan_a", "chan_b"]
    upd_fields.assert_awaited_once()
    assert upd_fields.await_args.args[1]["onboarding_state"] == onb.ST_FOCUS


# ── channel free-text ingest (validation) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_channels_validates_and_caps(monkeypatch):
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=3))
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    ctx.user_data["onb_substate"] = "typing_channels"
    upd = make_update(text="@good_one bad! second_good third_one fourth_one")
    await onb._ingest_channels(upd, ctx)
    cand = ctx.user_data["onb_channels"]
    assert "good_one" in cand and "second_good" in cand
    assert "bad!" not in cand
    assert len(cand) <= 3  # capped


# ── N4: lenient @username parse ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_channels_strips_at_sign(monkeypatch):
    """Leading @ is stripped so @mychannel and mychannel are accepted identically."""
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=10))
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    upd = make_update(text="@first_chan second_chan")
    await onb._ingest_channels(upd, ctx)
    cand = ctx.user_data["onb_channels"]
    assert "first_chan" in cand   # @ was stripped
    assert "second_chan" in cand


@pytest.mark.asyncio
async def test_ingest_channels_newline_separated(monkeypatch):
    """Newline-separated input is accepted (same as space-separated)."""
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=10))
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    upd = make_update(text="chan_alpha\nchan_beta\nchan_gamma")
    await onb._ingest_channels(upd, ctx)
    cand = ctx.user_data["onb_channels"]
    assert "chan_alpha" in cand
    assert "chan_beta" in cand
    assert "chan_gamma" in cand


@pytest.mark.asyncio
async def test_ingest_channels_deduplicates(monkeypatch):
    """A duplicate username (with or without @) is added only once."""
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=10))
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    upd = make_update(text="my_channel @my_channel my_channel")
    await onb._ingest_channels(upd, ctx)
    cand = ctx.user_data["onb_channels"]
    assert cand.count("my_channel") == 1


@pytest.mark.asyncio
async def test_ingest_channels_ignores_empty_tokens(monkeypatch):
    """Empty tokens (multiple spaces, leading/trailing whitespace) are silently ignored."""
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=10))
    ctx = FakeContext()
    ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
    # Extra spaces and a lone @ produce empty tokens after stripping.
    upd = make_update(text="  valid_chan   @   another_val  ")
    await onb._ingest_channels(upd, ctx)
    cand = ctx.user_data["onb_channels"]
    assert "valid_chan" in cand
    assert "another_val" in cand
    # Empty string must never appear in candidates.
    assert "" not in cand


# ── N3: only curated AI/LLM topic surfaced in onboarding ──────────────────────

def test_only_ai_topic_in_labels():
    """_TOPIC_LABELS must contain exactly the curated AI/LLM key(s) — no unverified
    topic templates (dev, crypto, startup, science, business) surfaced in onboarding."""
    assert "ai" in onb._TOPIC_LABELS
    for unverified in ("dev", "crypto", "startup", "science", "business"):
        assert unverified not in onb._TOPIC_LABELS, (
            f"unverified topic '{unverified}' must not appear in _TOPIC_LABELS"
        )


# ── N2: preview passes on_status to deliver_digest ────────────────────────────

@pytest.mark.asyncio
async def test_run_preview_passes_on_status(monkeypatch):
    """_run_preview must pass an on_status callback to deliver_digest so that
    pipeline stage updates stream to the user (parity with the 📰 button)."""
    from unittest.mock import patch, AsyncMock as AM

    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value={"current_focus": ""}))
    monkeypatch.setattr(db_module, "update_user_fields", AsyncMock())

    captured = {}

    async def fake_deliver(bot, user, *, on_status=None):
        captured["on_status"] = on_status
        return 3  # posts_count

    with patch("handlers.digest.deliver_digest", fake_deliver):
        ctx = FakeContext()
        ctx.user_data["user"] = {"id": "uuid-1", "tg_user_id": USER}
        upd = make_update()
        upd.effective_chat = type("C", (), {"id": USER})()
        await onb._run_preview(upd, ctx)

    assert captured.get("on_status") is not None, (
        "deliver_digest must receive an on_status callback from _run_preview"
    )
    assert callable(captured["on_status"])


# ── first /start grants trial + advances ──────────────────────────────────────

@pytest.mark.asyncio
async def test_first_start_grants_trial(monkeypatch):
    grant = AsyncMock(return_value=True)
    upd_fields = AsyncMock()
    monkeypatch.setattr(subs_module, "grant_trial", grant)
    monkeypatch.setattr(db_module, "update_user_fields", upd_fields)
    ctx = FakeContext()
    ctx.user_data["user"] = {"tg_user_id": USER, "id": "uuid-1", "onboarding_state": "invited"}
    upd = make_update(text="/start")
    await onb.start(upd, ctx)
    grant.assert_awaited_once_with(USER)
    assert upd_fields.await_args.args[1]["onboarding_state"] == onb.ST_CHANNELS
    assert any("пробный доступ Pro" in r[0] for r in upd.message.replies)


@pytest.mark.asyncio
async def test_done_user_no_wizard_restart(monkeypatch):
    grant = AsyncMock()
    monkeypatch.setattr(subs_module, "grant_trial", grant)
    monkeypatch.setattr(db_module, "load_settings", AsyncMock(return_value={"current_focus": ""}))
    ctx = FakeContext()
    ctx.user_data["user"] = {"tg_user_id": USER, "id": "uuid-1", "onboarding_state": "done"}
    upd = make_update(text="/start")
    await onb.start(upd, ctx)
    grant.assert_not_called()  # trial not re-armed
