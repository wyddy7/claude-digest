"""
Offline tests for:
  - db.count_user_digests_today (N6 building block)
  - db.reset_user_onboarding / db.delete_user_rows (N1 building blocks)
  - scheduler.run_digest_fanout digests_per_day enforcement (N6 gate)

No network, no Supabase, no real tg ids (synthetic 111-style only).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db as db_module


# ─── synthetic ids ────────────────────────────────────────────────────────────
_USER_TG_ID = 111200001
_USER_ID = str(_USER_TG_ID)


# ─── FakeDB for db.* unit tests ───────────────────────────────────────────────

class FakeDB:
    """Minimal in-memory DB shim for testing the new db helper functions."""

    def __init__(self):
        self.users: dict[int, dict] = {}
        self.user_settings: dict[str, dict] = {}
        # digests: list of {"user_id": str, "date": str, ...}
        self.digests: list[dict] = []

    async def get_user_by_tg_id(self, tg_user_id: int) -> Optional[dict]:
        return copy.deepcopy(self.users.get(tg_user_id))

    async def update_user_fields(self, tg_user_id: int, fields: dict) -> bool:
        if tg_user_id not in self.users:
            return False
        self.users[tg_user_id].update(fields)
        return True

    async def save_settings(self, user_id: str, fields: dict) -> dict:
        row = self.user_settings.setdefault(user_id, {})
        row.update(fields)
        return copy.deepcopy(row)

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def count_user_digests_today_fake(self, user_id: str) -> int:
        today = self._today_utc()
        return sum(
            1 for d in self.digests
            if d.get("user_id") == user_id and d.get("date") == today
        )


def _seed_user(fdb: FakeDB, tg_id: int, **kwargs) -> dict:
    row = {
        "id": str(tg_id),
        "tg_user_id": tg_id,
        "tier": "trial",
        "onboarding_state": "done",
        "trial_used": False,
        "pro_until": None,
        "trial_ends_at": None,
    }
    row.update(kwargs)
    fdb.users[tg_id] = row
    return row


# ─── db.reset_user_onboarding ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reset_user_onboarding_sets_invited_and_clears(monkeypatch):
    """reset_user_onboarding: sets onboarding_state=invited and clears channels+focus."""
    fdb = FakeDB()
    _seed_user(fdb, _USER_TG_ID, onboarding_state="done", trial_used=True)
    fdb.user_settings[_USER_ID] = {"channels": ["llm_notes"], "current_focus": "ai"}

    monkeypatch.setattr(db_module, "get_user_by_tg_id", fdb.get_user_by_tg_id)
    monkeypatch.setattr(db_module, "update_user_fields", fdb.update_user_fields)
    monkeypatch.setattr(db_module, "save_settings", fdb.save_settings)

    result = await db_module.reset_user_onboarding(_USER_TG_ID)

    assert result is True
    assert fdb.users[_USER_TG_ID]["onboarding_state"] == "invited"
    assert fdb.user_settings[_USER_ID]["channels"] == []
    assert fdb.user_settings[_USER_ID]["current_focus"] == ""
    # trial_used must NOT be touched — it was True before and must stay True.
    assert fdb.users[_USER_TG_ID]["trial_used"] is True


@pytest.mark.asyncio
async def test_reset_user_onboarding_missing_user_returns_false(monkeypatch):
    """reset_user_onboarding: returns False when the user row doesn't exist."""
    fdb = FakeDB()

    monkeypatch.setattr(db_module, "get_user_by_tg_id", fdb.get_user_by_tg_id)
    monkeypatch.setattr(db_module, "update_user_fields", fdb.update_user_fields)
    monkeypatch.setattr(db_module, "save_settings", fdb.save_settings)

    result = await db_module.reset_user_onboarding(_USER_TG_ID)

    assert result is False


# ─── db.delete_user_rows ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_user_rows_removes_both_rows(monkeypatch):
    """delete_user_rows: deletes users + user_settings rows when user exists."""
    fdb = FakeDB()
    _seed_user(fdb, _USER_TG_ID)
    fdb.user_settings[_USER_ID] = {"channels": ["ch1"]}

    # Patch the internals that delete_user_rows calls (get_user_by_tg_id + client ops).
    # We test the actual db module function; patch the supabase client calls.
    _deleted_tables: list[str] = []

    async def _fake_get_user(tg_user_id):
        return copy.deepcopy(fdb.users.get(tg_user_id))

    class _FakeQuery:
        def __init__(self, table):
            self._table = table
        def delete(self):
            return self
        def eq(self, *a, **kw):
            return self
        async def execute(self):
            _deleted_tables.append(self._table)
            return MagicMock(data=[])

    class _FakeClient:
        def table(self, name):
            return _FakeQuery(name)

    monkeypatch.setattr(db_module, "get_user_by_tg_id", _fake_get_user)
    monkeypatch.setattr(db_module, "_get_client", lambda: _FakeClient())

    result = await db_module.delete_user_rows(_USER_TG_ID)

    assert result is True
    assert "user_settings" in _deleted_tables
    assert "users" in _deleted_tables


@pytest.mark.asyncio
async def test_delete_user_rows_missing_user_returns_false(monkeypatch):
    """delete_user_rows: returns False immediately when user doesn't exist."""
    fdb = FakeDB()

    monkeypatch.setattr(db_module, "get_user_by_tg_id", fdb.get_user_by_tg_id)

    result = await db_module.delete_user_rows(_USER_TG_ID)
    assert result is False


# ─── scheduler digests_per_day gate (N6) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_fanout_skips_user_when_daily_cap_reached():
    """run_digest_fanout skips a user who has already received digests_per_day today."""
    import scheduler as sched_module

    user = {
        "id": _USER_ID,
        "tg_user_id": _USER_TG_ID,
        "is_active": True,
    }

    calls: list = []

    async def _fake_list_active_users():
        return [user]

    async def _fake_is_sub_active(tg_id):
        return True  # subscription is active

    async def _fake_get_effective_limit(uid, key, default=None):
        if key == "digests_per_day":
            return 1  # cap is 1 per day
        return default

    async def _fake_count_today(uid):
        return 1  # already sent 1 today (= cap)

    async def _fake_deliver(*a, **kw):
        calls.append(1)
        return 0

    with (
        patch.object(db_module, "list_active_users", _fake_list_active_users),
        patch("scheduler.subscriptions.is_subscription_active", _fake_is_sub_active),
        patch("scheduler.db.get_effective_limit", _fake_get_effective_limit),
        patch("scheduler.db.count_user_digests_today", _fake_count_today),
        patch("scheduler._deliver_user_digest", _fake_deliver),
    ):
        # Provide a fake Bot context manager.
        mock_bot = AsyncMock()
        mock_bot.__aenter__ = AsyncMock(return_value=mock_bot)
        mock_bot.__aexit__ = AsyncMock(return_value=False)
        with patch("scheduler.Bot", return_value=mock_bot):
            await sched_module.run_digest_fanout()

    # No delivery should have happened (cap reached).
    assert calls == [], f"Expected no deliveries but got {len(calls)}"


@pytest.mark.asyncio
async def test_fanout_delivers_when_under_daily_cap():
    """run_digest_fanout delivers when today's count is below digests_per_day."""
    import scheduler as sched_module

    user = {
        "id": _USER_ID,
        "tg_user_id": _USER_TG_ID,
        "is_active": True,
    }

    delivered: list = []

    async def _fake_list_active_users():
        return [user]

    async def _fake_is_sub_active(tg_id):
        return True

    async def _fake_get_effective_limit(uid, key, default=None):
        if key == "digests_per_day":
            return 1
        return default

    async def _fake_count_today(uid):
        return 0  # none sent today → under cap

    async def _fake_deliver(*a, **kw):
        delivered.append(1)
        return 5

    with (
        patch.object(db_module, "list_active_users", _fake_list_active_users),
        patch("scheduler.subscriptions.is_subscription_active", _fake_is_sub_active),
        patch("scheduler.db.get_effective_limit", _fake_get_effective_limit),
        patch("scheduler.db.count_user_digests_today", _fake_count_today),
        patch("scheduler._deliver_user_digest", _fake_deliver),
    ):
        mock_bot = AsyncMock()
        mock_bot.__aenter__ = AsyncMock(return_value=mock_bot)
        mock_bot.__aexit__ = AsyncMock(return_value=False)
        with patch("scheduler.Bot", return_value=mock_bot):
            await sched_module.run_digest_fanout()

    assert len(delivered) == 1, f"Expected 1 delivery but got {len(delivered)}"


@pytest.mark.asyncio
async def test_fanout_delivers_when_no_cap_set():
    """run_digest_fanout delivers when digests_per_day is None (no limit set)."""
    import scheduler as sched_module

    user = {
        "id": _USER_ID,
        "tg_user_id": _USER_TG_ID,
        "is_active": True,
    }

    delivered: list = []

    async def _fake_list_active_users():
        return [user]

    async def _fake_is_sub_active(tg_id):
        return True

    async def _fake_get_effective_limit(uid, key, default=None):
        return None  # no cap configured

    async def _fake_count_today(uid):
        return 99  # even if many already sent, no cap → deliver

    async def _fake_deliver(*a, **kw):
        delivered.append(1)
        return 3

    with (
        patch.object(db_module, "list_active_users", _fake_list_active_users),
        patch("scheduler.subscriptions.is_subscription_active", _fake_is_sub_active),
        patch("scheduler.db.get_effective_limit", _fake_get_effective_limit),
        patch("scheduler.db.count_user_digests_today", _fake_count_today),
        patch("scheduler._deliver_user_digest", _fake_deliver),
    ):
        mock_bot = AsyncMock()
        mock_bot.__aenter__ = AsyncMock(return_value=mock_bot)
        mock_bot.__aexit__ = AsyncMock(return_value=False)
        with patch("scheduler.Bot", return_value=mock_bot):
            await sched_module.run_digest_fanout()

    assert len(delivered) == 1, "Expected delivery when no cap is configured"


# ─── manual 📰 digests_per_day gate (parity with cron) ───────────────────────

from datetime import timedelta  # noqa: E402


class _FakeMsg:
    def __init__(self):
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kw):
        return self


class _FakeUpdate:
    def __init__(self):
        self.effective_message = _FakeMsg()


def _active_ctx(user: dict):
    return SimpleNamespace(user_data={"user": user}, bot=object())


def _active_user(**kw) -> dict:
    row = {
        "id": _USER_ID,
        "tg_user_id": _USER_TG_ID,
        "pro_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_manual_digest_capped_for_active_user(monkeypatch):
    """An active (non-owner) user pressing 📰 after hitting digests_per_day gets the
    cap message and the pipeline never runs (no LLM spend)."""
    from handlers import digest as digest_mod

    delivered: list = []

    async def _deliver(*a, **kw):
        delivered.append(1)

    monkeypatch.setattr(digest_mod, "is_owner", lambda _id: False)
    monkeypatch.setattr(digest_mod, "deliver_digest", _deliver)
    monkeypatch.setattr(digest_mod.db, "get_effective_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(digest_mod.db, "count_user_digests_today", AsyncMock(return_value=1))

    upd, ctx = _FakeUpdate(), _active_ctx(_active_user())
    await digest_mod.send_digest(upd, ctx)

    assert delivered == [], "pipeline must NOT run when the daily cap is reached"
    from handlers.strings import DIGEST_DAILY_CAP
    assert upd.effective_message.replies == [DIGEST_DAILY_CAP]


@pytest.mark.asyncio
async def test_manual_digest_owner_exempt_from_cap(monkeypatch):
    """The owner is exempt from the manual cap — testing isn't blocked even at cap."""
    from handlers import digest as digest_mod

    delivered: list = []

    async def _deliver(*a, **kw):
        delivered.append(1)

    monkeypatch.setattr(digest_mod, "is_owner", lambda _id: True)
    monkeypatch.setattr(digest_mod, "deliver_digest", _deliver)
    monkeypatch.setattr(digest_mod.db, "get_effective_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(digest_mod.db, "count_user_digests_today", AsyncMock(return_value=99))

    upd, ctx = _FakeUpdate(), _active_ctx(_active_user())
    await digest_mod.send_digest(upd, ctx)

    assert delivered == [1], "owner must be delivered even above the cap"


@pytest.mark.asyncio
async def test_manual_digest_delivers_under_cap(monkeypatch):
    """Under the daily cap, a normal active user's 📰 runs the pipeline."""
    from handlers import digest as digest_mod

    delivered: list = []

    async def _deliver(*a, **kw):
        delivered.append(1)

    monkeypatch.setattr(digest_mod, "is_owner", lambda _id: False)
    monkeypatch.setattr(digest_mod, "deliver_digest", _deliver)
    monkeypatch.setattr(digest_mod.db, "get_effective_limit", AsyncMock(return_value=1))
    monkeypatch.setattr(digest_mod.db, "count_user_digests_today", AsyncMock(return_value=0))

    upd, ctx = _FakeUpdate(), _active_ctx(_active_user())
    await digest_mod.send_digest(upd, ctx)

    assert delivered == [1], "under cap, the digest pipeline should run"
