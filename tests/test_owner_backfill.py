"""
Offline unit tests for db.ensure_owner_user().

No network, no Supabase. db.* internals are patched via monkeypatch so no
real tg id is ever used — only synthetic 111111111-style ids. The env var
CHAT_ID is set to the synthetic value for each test.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock

import pytest

import digest_bot.db as db_module

# Synthetic owner id — never a real telegram id.
_OWNER_TG_ID = 111111111
_OWNER_UUID = "aaaaaaaa-0000-0000-0000-000000000001"


# ── in-memory DB shim ──────────────────────────────────────────────────────────

class FakeTable:
    """Minimal fake that records inserts and supports reads by primary key."""

    def __init__(self):
        # tg_user_id → row for users; user_id → row for user_settings
        self.rows: dict = {}
        self._insert_calls: list[dict] = []

    def insert(self, row: dict) -> dict:
        self._insert_calls.append(copy.deepcopy(row))
        return copy.deepcopy(row)


class FakeSupabaseState:
    """Holds in-memory state shared across all patched db functions."""

    def __init__(
        self,
        owner_present: bool = False,
        legacy_state: Optional[dict] = None,
        pro_limits: Optional[dict] = None,
    ):
        # users table keyed by tg_user_id
        self.users: dict[int, dict] = {}
        if owner_present:
            self.users[_OWNER_TG_ID] = {
                "id": _OWNER_UUID,
                "tg_user_id": _OWNER_TG_ID,
                "tier": "pro",
                "onboarding_state": "done",
            }

        # user_settings keyed by user_id (UUID)
        self.user_settings: dict[str, dict] = {}

        # legacy user_state row (id=1) — None means table is empty
        self.legacy_state = legacy_state

        # pro tier limits
        self.pro_limits = pro_limits or {"max_channels": 20}

        # call tracking
        self.users_inserted: list[dict] = []
        self.settings_inserted: list[dict] = []

    # ── db surface ─────────────────────────────────────────────────────────────

    async def get_user_by_tg_id(self, tg_user_id: int) -> Optional[dict]:
        return copy.deepcopy(self.users.get(tg_user_id))

    async def _do_insert_user(self, payload: dict) -> dict:
        row = {**payload, "id": _OWNER_UUID}
        self.users[payload["tg_user_id"]] = row
        self.users_inserted.append(copy.deepcopy(row))
        return row

    async def _do_insert_settings(self, payload: dict) -> None:
        self.user_settings[payload["user_id"]] = copy.deepcopy(payload)
        self.settings_inserted.append(copy.deepcopy(payload))

    async def get_tier_limits(self, tier: str) -> dict:
        if tier == "pro":
            return copy.deepcopy(self.pro_limits)
        return {}

    async def _load_legacy(self) -> Optional[dict]:
        return copy.deepcopy(self.legacy_state)


def _patch_db(monkeypatch, state: FakeSupabaseState, env_chat_id: str = str(_OWNER_TG_ID)):
    """Wire the fake state into db module functions used by ensure_owner_user."""
    monkeypatch.setenv("CHAT_ID", env_chat_id)

    monkeypatch.setattr(db_module, "get_user_by_tg_id",
                        AsyncMock(side_effect=state.get_user_by_tg_id))
    monkeypatch.setattr(db_module, "get_tier_limits",
                        AsyncMock(side_effect=state.get_tier_limits))

    # Patch the internal _get_client path by replacing the table calls
    # ensure_owner_user uses directly on _get_client().table(...).insert(...).execute().
    # We replace _get_client with a fake that intercepts table().insert().execute().

    class _FakeExecuteResult:
        def __init__(self, row):
            self.data = [row]

    class _FakeInsertChain:
        def __init__(self, table_name, state):
            self._table = table_name
            self._state = state

        async def execute(self):
            if self._table == "users":
                row = await state._do_insert_user(self._payload)
                return _FakeExecuteResult(row)
            elif self._table == "user_settings":
                await state._do_insert_settings(self._payload)
                return _FakeExecuteResult(self._payload)
            raise RuntimeError(f"unexpected table: {self._table}")

        def __call__(self, payload):
            self._payload = payload
            return self

    class _FakeSelectChain:
        def __init__(self, table_name, state):
            self._table = table_name
            self._state = state
            self._filters: dict = {}

        def select(self, *a): return self
        def eq(self, col, val):
            self._filters[col] = val
            return self

        async def execute(self):
            if self._table == "user_state":
                if self._filters.get("id") == 1 and state.legacy_state is not None:
                    # Simulate a user_state row by reconstructing what _row_to_state expects
                    legacy = state.legacy_state
                    raw_row = {
                        "id": 1,
                        "channels": legacy.get("channels", db_module.DEFAULT_CHANNELS[:]),
                        "current_focus": legacy.get("current_focus", ""),
                        "focus_auto_reset": legacy.get("focus_auto_reset", False),
                        "model": legacy.get("model", db_module.DEFAULT_MODEL),
                        "last_digest": legacy.get("last_digest", ""),
                        "last_digest_time": legacy.get("last_digest_time", ""),
                        "interaction_history": legacy.get("interaction_history", []),
                    }
                    return _FakeExecuteResult(raw_row)
                return type("R", (), {"data": []})()
            raise RuntimeError(f"unexpected select on table: {self._table}")

    class _FakeTable:
        def __init__(self, name):
            self._name = name
            self._state = state

        def insert(self, payload):
            chain = _FakeInsertChain(self._name, self._state)
            chain._payload = payload
            return chain

        def select(self, *a):
            return _FakeSelectChain(self._name, self._state)

    class _FakeClient:
        def table(self, name):
            return _FakeTable(name)

    monkeypatch.setattr(db_module, "_client", _FakeClient())


# ── tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_creates_row_when_absent_no_legacy(monkeypatch):
    """When no users row exists and no legacy user_state, backfill from pro defaults."""
    state = FakeSupabaseState(owner_present=False, legacy_state=None)
    _patch_db(monkeypatch, state)

    await db_module.ensure_owner_user()

    assert len(state.users_inserted) == 1
    inserted = state.users_inserted[0]
    assert inserted["tg_user_id"] == _OWNER_TG_ID
    assert inserted["tier"] == "pro"
    assert inserted["onboarding_state"] == "done"
    assert inserted["trial_used"] is True
    assert inserted["is_active"] is True

    # pro_until is far in the future (at least 99 years from now)
    pro_until = datetime.fromisoformat(inserted["pro_until"])
    delta = pro_until - datetime.now(timezone.utc)
    assert delta.days > 365 * 99, f"pro_until too close: {pro_until}"

    # user_settings seeded from pro defaults
    assert len(state.settings_inserted) == 1
    settings = state.settings_inserted[0]
    assert settings["user_id"] == _OWNER_UUID
    assert "limits" in settings
    # Must be written explicitly — omitting it hands the row to the frozen
    # column server_default, which is how the 2026-08-04 dead-model bug worked.
    assert settings["model"] == db_module.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_creates_row_when_absent_with_legacy(monkeypatch):
    """When legacy user_state exists, user_settings is seeded from it."""
    legacy = {
        "channels": ["chan_a", "chan_b"],
        "current_focus": "AI research",
        "model": "anthropic/claude-3.5-haiku",
        "last_digest": "<b>old digest</b>",
        "last_digest_time": "2026-01-01T10:00:00",
        "interaction_history": ["entry1"],
        "focus_auto_reset": False,
    }
    state = FakeSupabaseState(owner_present=False, legacy_state=legacy)
    _patch_db(monkeypatch, state)

    await db_module.ensure_owner_user()

    assert len(state.users_inserted) == 1
    assert len(state.settings_inserted) == 1

    settings = state.settings_inserted[0]
    assert settings["channels"] == ["chan_a", "chan_b"]
    assert settings["current_focus"] == "AI research"
    assert settings["model"] == "anthropic/claude-3.5-haiku"
    assert settings["last_digest"] == "<b>old digest</b>"
    assert settings["interaction_history"] == ["entry1"]
    # When seeded from legacy, no "limits" key — uses the actual field columns
    assert "limits" not in settings


@pytest.mark.asyncio
async def test_no_op_when_row_already_present(monkeypatch):
    """If the owner row already exists, ensure_owner_user does nothing."""
    state = FakeSupabaseState(owner_present=True)
    _patch_db(monkeypatch, state)

    await db_module.ensure_owner_user()

    # No inserts should have happened
    assert len(state.users_inserted) == 0
    assert len(state.settings_inserted) == 0


@pytest.mark.asyncio
async def test_idempotent_called_twice(monkeypatch):
    """Calling ensure_owner_user twice is safe — second call is a no-op."""
    state = FakeSupabaseState(owner_present=False, legacy_state=None)
    _patch_db(monkeypatch, state)

    await db_module.ensure_owner_user()
    # After first call the owner row is in state.users; second call finds it.
    await db_module.ensure_owner_user()

    assert len(state.users_inserted) == 1  # only inserted once
    assert len(state.settings_inserted) == 1


@pytest.mark.asyncio
async def test_no_real_id_hardcoded(monkeypatch):
    """The owner tg_user_id is taken from CHAT_ID env, never hardcoded."""
    state = FakeSupabaseState(owner_present=False, legacy_state=None)
    _patch_db(monkeypatch, state, env_chat_id="999888777")

    await db_module.ensure_owner_user()

    assert len(state.users_inserted) == 1
    assert state.users_inserted[0]["tg_user_id"] == 999888777


@pytest.mark.asyncio
async def test_zero_chat_id_is_skipped(monkeypatch):
    """CHAT_ID=0 (unset) must not insert anything."""
    state = FakeSupabaseState(owner_present=False)
    _patch_db(monkeypatch, state, env_chat_id="0")

    await db_module.ensure_owner_user()

    assert len(state.users_inserted) == 0
