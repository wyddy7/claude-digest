"""Offline unit tests for the multi-tenant chat-with-digest agent.

No network, no Supabase, no PTB application, no real LLM. db functions are
monkeypatched; agent.run_chat_turn is stubbed where we test the router. Telegram
types are minimal fakes. Synthetic user ids only.

Covers:
- agent._make_user_scoped_tools binds THIS user's id into every tool: the tools
  read db.load_user_history / db.load_settings scoped by user_id, never the
  legacy global db.load()/db.load_history(). Two different users never see each
  other's data through the same tool names.
- chat._chat_with_digest routes free text to run_chat_turn with the per-user
  thread key (tg id) + scope_user_id (uuid) and records the turn.
- the chat_turns_per_month quota is enforced from db.get_effective_limit BEFORE
  the agent runs: over-limit users get the capped message and the agent is never
  invoked (no LLM spend).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import digest_bot.agent as agent_mod
import digest_bot.db as db_module
from digest_bot.handlers import chat as chat_mod
from digest_bot.handlers.strings import CHAT_LIMIT_HIT

USER_A = "uuid-user-a"
USER_B = "uuid-user-b"
TG_USER_A = 444444444


def _active_user() -> dict:
    """A user row with an active subscription (passes the chat gate)."""
    return {
        "id": USER_A, "tg_user_id": TG_USER_A,
        "pro_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }


# ── fakes ─────────────────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))
        sent = FakeMessage()
        sent.edits: list = []

        async def _edit(t, **k):
            sent.edits.append((t, k))

        sent.edit_text = _edit
        return sent


class FakeContext:
    def __init__(self, user: dict):
        self.user_data: dict = {"user": user}
        self.application = SimpleNamespace(bot_data={"checkpointer": object()})


def make_update(text: str, tg_id: int = TG_USER_A):
    msg = FakeMessage(text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=tg_id),
        message=msg,
        callback_query=None,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=tg_id),
    )


# ── per-user scoped tools read the right user's data ──────────────────────────


@pytest.mark.asyncio
async def test_scoped_tools_bind_user_id(monkeypatch):
    """Each scoped tool must query db.load_user_history / db.load_settings with
    the user_id it was built for — never the legacy global db.load()/
    db.load_history()."""
    history_calls: list = []
    settings_calls: list = []

    async def _fake_load_user_history(user_id, limit=0):
        history_calls.append((user_id, limit))
        return [{"id": 1, "date": "2026-06-01", "digest_html": "wispr launched", "is_error": False}]

    async def _fake_load_settings(user_id):
        settings_calls.append(user_id)
        return {"current_focus": f"focus-of-{user_id}"}

    def _explode(*a, **k):  # global readers must NOT be touched by scoped tools
        raise AssertionError("scoped tool reached a global db reader")

    monkeypatch.setattr(db_module, "load_user_history", _fake_load_user_history)
    monkeypatch.setattr(db_module, "load_settings", _fake_load_settings)
    monkeypatch.setattr(db_module, "load", AsyncMock(side_effect=_explode))
    monkeypatch.setattr(db_module, "load_history", AsyncMock(side_effect=_explode))

    tools = agent_mod._make_user_scoped_tools(USER_A)
    by_name = {t.name: t for t in tools}

    # search_digest_history → scoped history
    res = await by_name["search_digest_history"].ainvoke({"query": "wispr"})
    assert res and res[0]["date"] == "2026-06-01"

    # get_recent_digests → scoped history with limit
    await by_name["get_recent_digests"].ainvoke({"n": 3})

    # get_current_focus → scoped settings
    focus = await by_name["get_current_focus"].ainvoke({})
    assert focus == f"focus-of-{USER_A}"

    assert {c[0] for c in history_calls} == {USER_A}, "history must be scoped to USER_A only"
    assert set(settings_calls) == {USER_A}, "settings must be scoped to USER_A only"
    assert (USER_A, 3) in history_calls, "get_recent_digests must thread the limit through"


@pytest.mark.asyncio
async def test_scoped_tools_isolate_two_users(monkeypatch):
    """Building tools for USER_A vs USER_B must produce tools that read different
    scopes — no shared/global state leaks one user's data into the other's."""
    seen: list = []

    async def _fake_load_settings(user_id):
        seen.append(user_id)
        return {"current_focus": f"focus-{user_id}"}

    monkeypatch.setattr(db_module, "load_settings", _fake_load_settings)

    tools_a = {t.name: t for t in agent_mod._make_user_scoped_tools(USER_A)}
    tools_b = {t.name: t for t in agent_mod._make_user_scoped_tools(USER_B)}

    focus_a = await tools_a["get_current_focus"].ainvoke({})
    focus_b = await tools_b["get_current_focus"].ainvoke({})

    assert focus_a == f"focus-{USER_A}"
    assert focus_b == f"focus-{USER_B}"
    assert focus_a != focus_b
    assert seen == [USER_A, USER_B]


# ── router threads per-user thread key + scope, records the turn ──────────────


@pytest.mark.asyncio
async def test_chat_routes_with_per_user_thread_and_scope(monkeypatch):
    captured: dict = {}

    async def _fake_run_chat_turn(user_id, message, checkpointer, *, scope_user_id=None):
        captured["thread_key"] = user_id
        captured["scope_user_id"] = scope_user_id
        captured["message"] = message
        return "ответ ассистента"

    recorded: list = []

    monkeypatch.setattr(chat_mod, "run_chat_turn", _fake_run_chat_turn)
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=50))
    monkeypatch.setattr(db_module, "count_chat_turns_this_month", AsyncMock(return_value=0))
    monkeypatch.setattr(
        db_module, "record_chat_turn", AsyncMock(side_effect=lambda uid: recorded.append(uid))
    )

    user = _active_user()
    upd = make_update("что было про wispr?")
    ctx = FakeContext(user)
    await chat_mod._chat_with_digest(upd, ctx, user)

    # thread keyed on the numeric tg id; tools/system-prompt scoped on the uuid.
    assert captured["thread_key"] == TG_USER_A
    assert captured["scope_user_id"] == USER_A
    assert captured["message"] == "что было про wispr?"
    # the turn was counted exactly once, for this user.
    assert recorded == [USER_A]


# ── quota gate: over-limit blocks the agent ───────────────────────────────────


@pytest.mark.asyncio
async def test_chat_over_limit_blocks_agent(monkeypatch):
    """When the month's usage has hit chat_turns_per_month, the agent must never
    be invoked and the user gets the capped message (limit value read from DB)."""

    async def _must_not_run(*a, **k):
        raise AssertionError("run_chat_turn invoked despite over-limit")

    monkeypatch.setattr(chat_mod, "run_chat_turn", _must_not_run)
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=50))
    monkeypatch.setattr(db_module, "count_chat_turns_this_month", AsyncMock(return_value=50))
    monkeypatch.setattr(db_module, "record_chat_turn", AsyncMock())

    user = _active_user()
    upd = make_update("ещё вопрос")
    ctx = FakeContext(user)
    await chat_mod._chat_with_digest(upd, ctx, user)

    assert upd.message.replies, "should have replied with the capped message"
    text, _ = upd.message.replies[0]
    assert text == CHAT_LIMIT_HIT.format(cap=50)
    # record_chat_turn must NOT be called when the gate blocks.
    db_module.record_chat_turn.assert_not_called()


# ── subscription gate: expired user can't reach the LLM ───────────────────────


@pytest.mark.asyncio
async def test_chat_expired_user_hits_paywall_not_agent(monkeypatch):
    """An expired/unpaid user typing free text must hit the paywall, never the
    chat agent (no LLM spend). This is the leak test2 caught — only 📰 was gated."""
    async def _must_not_run(*a, **k):
        raise AssertionError("run_chat_turn invoked for an expired user")

    gate = AsyncMock()
    monkeypatch.setattr(chat_mod, "run_chat_turn", _must_not_run)
    monkeypatch.setattr(chat_mod.subscription_surface, "show_gate", gate)
    # If the gate let it through, these would be hit — assert they are not.
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(side_effect=AssertionError))
    monkeypatch.setattr(db_module, "count_chat_turns_this_month", AsyncMock(side_effect=AssertionError))

    user = {"id": USER_A, "tg_user_id": TG_USER_A}  # no pro_until / trial_ends_at → expired
    upd = make_update("дай дайджест за месяц")
    ctx = FakeContext(user)
    await chat_mod._chat_with_digest(upd, ctx, user)

    gate.assert_awaited_once()


# ── /in deferred job: subscription re-checked AT FIRE TIME ────────────────────


class _FakeJobQueue:
    """Records run_once callbacks so the test can fire them manually."""

    def __init__(self):
        self.jobs: list = []

    def run_once(self, callback, when, name=None):
        self.jobs.append((callback, when, name))


def _make_in_context(user: dict) -> FakeContext:
    ctx = FakeContext(user)
    ctx.job_queue = _FakeJobQueue()
    ctx.bot = object()
    return ctx


@pytest.mark.asyncio
async def test_in_job_expired_at_fire_time_skips_llm(monkeypatch):
    """/in scheduled while ACTIVE must NOT generate a digest if the subscription
    lapsed (trial expiry or admin /revoke_pro) before the job fires. The gate at
    schedule time is not enough — the job re-fetches the row and re-checks."""

    async def _must_not_run(*a, **k):
        raise AssertionError("deliver_digest fired for a user inactive at fire time")

    monkeypatch.setattr(chat_mod.digest_surface, "deliver_digest", _must_not_run)
    # /in is admin-only; the caller is the admin for this fire-time test.
    monkeypatch.setattr(chat_mod.admin_surface, "is_admin_id", lambda _id: True)

    user = _active_user()  # active at schedule time → /in is accepted
    upd = make_update("/in 5")
    ctx = _make_in_context(user)
    await chat_mod._cmd_in(upd, ctx, user)
    assert ctx.job_queue.jobs, "/in should have scheduled a job for an active admin"

    # Between scheduling and firing the subscription is revoked/expires:
    expired_row = {"id": USER_A, "tg_user_id": TG_USER_A,
                   "pro_until": None, "trial_ends_at": None}
    monkeypatch.setattr(db_module, "get_user_by_tg_id", AsyncMock(return_value=expired_row))

    job_cb = ctx.job_queue.jobs[0][0]
    await job_cb(SimpleNamespace(bot=object()))  # must skip silently, no raise


@pytest.mark.asyncio
async def test_in_job_active_at_fire_time_delivers_fresh_row(monkeypatch):
    """Companion: a still-active user IS delivered at fire time, and the job uses
    the freshly fetched row (not the dict captured at schedule time)."""
    delivered = {}

    async def _deliver(bot, row, **k):
        delivered["row"] = row

    monkeypatch.setattr(chat_mod.digest_surface, "deliver_digest", _deliver)
    monkeypatch.setattr(chat_mod.admin_surface, "is_admin_id", lambda _id: True)

    user = _active_user()
    upd = make_update("/in 5")
    ctx = _make_in_context(user)
    await chat_mod._cmd_in(upd, ctx, user)

    fresh_row = dict(_active_user(), marker="fresh")
    monkeypatch.setattr(db_module, "get_user_by_tg_id", AsyncMock(return_value=fresh_row))

    job_cb = ctx.job_queue.jobs[0][0]
    await job_cb(SimpleNamespace(bot=object()))
    assert delivered["row"] is fresh_row, "job must deliver with the re-fetched row"


@pytest.mark.asyncio
async def test_in_non_admin_refused_no_job(monkeypatch):
    """/in is admin-only. A non-admin user (even an active one) is refused before
    anything is scheduled and before the subscription gate is consulted — no job,
    no spend, no paywall."""
    gate = AsyncMock()
    monkeypatch.setattr(chat_mod.subscription_surface, "show_gate", gate)
    monkeypatch.setattr(chat_mod.admin_surface, "is_admin_id", lambda _id: False)

    user = _active_user()  # active, but not admin
    upd = make_update("/in 5")
    ctx = _make_in_context(user)
    await chat_mod._cmd_in(upd, ctx, user)

    assert not ctx.job_queue.jobs, "no job may be scheduled for a non-admin"
    gate.assert_not_awaited()
    assert upd.message.replies, "non-admin should get a refusal reply"
    assert upd.message.replies[-1][0] == chat_mod.IN_ADMIN_ONLY
