"""
Offline tests for admin grant/revoke surface (handlers/admin.py).

ADMIN_ID gate: non-admin numeric id is silently ignored (no reply, no DB write).
Admin id: grants Pro (update_subscription stacks) or revokes (clears pro_until +
trial_ends_at). All via fake supabase shim — no network, no real tg ids (synthetic
111-style only).
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db as db_module
import subscriptions as subs_module
from handlers import admin as admin_surface


# ─── in-memory DB shim ────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.users: dict[int, dict] = {}
        self.tier_defaults: dict[str, dict] = {}
        self.events: dict[str, dict] = {}

    async def get_user_by_tg_id(self, tg_user_id: int) -> Optional[dict]:
        return copy.deepcopy(self.users.get(tg_user_id))

    async def get_or_create_user(self, tg_user_id: int) -> dict:
        if tg_user_id not in self.users:
            self.users[tg_user_id] = {
                "id": str(tg_user_id),
                "tg_user_id": tg_user_id,
                "tier": "trial",
                "trial_used": False,
                "pro_until": None,
                "trial_ends_at": None,
                "trial_warn_sent": {},
            }
        return copy.deepcopy(self.users[tg_user_id])

    async def get_tier_default(self, tier: str, key: str, default: Any = None) -> Any:
        return self.tier_defaults.get(tier, {}).get(key, default)

    async def get_tier_limits(self, tier: str) -> dict:
        return copy.deepcopy(self.tier_defaults.get(tier, {}))

    async def update_user_fields(self, tg_user_id: int, fields: dict) -> bool:
        if tg_user_id not in self.users:
            return False
        self.users[tg_user_id].update(fields)
        return True

    async def update_subscription_row(self, tg_user_id: int, pro_until_iso: Optional[str]) -> bool:
        return await self.update_user_fields(tg_user_id, {"pro_until": pro_until_iso})

    async def grant_trial_row(self, tg_user_id: int, trial_ends_at_iso: str) -> bool:
        return await self.update_user_fields(tg_user_id, {
            "trial_ends_at": trial_ends_at_iso,
            "trial_used": True,
        })

    async def insert_subscription_event(
        self, user_id: str, event_type: str, payload: Optional[dict] = None,
        stars_amount: Optional[int] = None, telegram_payment_charge_id: Optional[str] = None,
    ) -> dict:
        if telegram_payment_charge_id and telegram_payment_charge_id in self.events:
            raise db_module.DuplicateCharge(telegram_payment_charge_id)
        row = {
            "user_id": user_id, "event_type": event_type, "payload": payload or {},
            "stars_amount": stars_amount,
            "telegram_payment_charge_id": telegram_payment_charge_id,
        }
        if telegram_payment_charge_id:
            self.events[telegram_payment_charge_id] = row
        return row

    async def delete_subscription_event(self, telegram_payment_charge_id: str) -> None:
        self.events.pop(telegram_payment_charge_id, None)


@pytest.fixture()
def fdb():
    return FakeDB()


@pytest.fixture(autouse=True)
def patch_db(fdb, monkeypatch):
    for name in (
        "get_user_by_tg_id", "get_or_create_user", "get_tier_default",
        "get_tier_limits", "update_user_fields", "update_subscription_row",
        "grant_trial_row", "insert_subscription_event", "delete_subscription_event",
    ):
        monkeypatch.setattr(db_module, name, getattr(fdb, name))


# ─── synthetic admin + target ids ─────────────────────────────────────────────
# Only synthetic ids (111-style), never real Telegram user ids.

_ADMIN_TG_ID = 111100001
_NON_ADMIN_TG_ID = 111100002
_TARGET_TG_ID = 111100003


def _seed_user(fdb: FakeDB, tg_id: int, **kwargs) -> dict:
    row = {
        "id": str(tg_id),
        "tg_user_id": tg_id,
        "tier": "trial",
        "trial_used": False,
        "pro_until": None,
        "trial_ends_at": None,
        "trial_warn_sent": {},
    }
    row.update(kwargs)
    fdb.users[tg_id] = row
    return row


def _make_update(user_id: int, cmd_args: list[str]) -> MagicMock:
    """Build a minimal fake Update for a command with args."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str]) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    ctx.bot.send_message = AsyncMock()
    return ctx


# ─── /give_pro — ADMIN_ID gate ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_give_pro_non_admin_silently_ignored(fdb, monkeypatch):
    """Non-admin tg id → cmd_give_pro returns immediately, no reply, no DB write."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)
    _seed_user(fdb, _TARGET_TG_ID)

    update = _make_update(_NON_ADMIN_TG_ID, [str(_TARGET_TG_ID), "30"])
    ctx = _make_context([str(_TARGET_TG_ID), "30"])

    await admin_surface.cmd_give_pro(update, ctx)

    # Silent ignore: no reply, no DB mutation.
    update.message.reply_text.assert_not_awaited()
    assert fdb.users[_TARGET_TG_ID]["pro_until"] is None


@pytest.mark.asyncio
async def test_give_pro_admin_grants_days(fdb, monkeypatch):
    """Admin /give_pro <target> <days> calls update_subscription and replies."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)
    _seed_user(fdb, _TARGET_TG_ID)

    update = _make_update(_ADMIN_TG_ID, [str(_TARGET_TG_ID), "7"])
    ctx = _make_context([str(_TARGET_TG_ID), "7"])

    await admin_surface.cmd_give_pro(update, ctx)

    # pro_until must now be set (~7 days from now).
    pro_until_iso = fdb.users[_TARGET_TG_ID]["pro_until"]
    assert pro_until_iso is not None, "pro_until must be set after /give_pro"
    pro_until = datetime.fromisoformat(pro_until_iso.replace("Z", "+00:00"))
    if pro_until.tzinfo is None:
        pro_until = pro_until.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < (pro_until - now) < timedelta(days=7, hours=1)

    # Admin gets a confirmation reply.
    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "Pro" in reply_text


@pytest.mark.asyncio
async def test_give_pro_admin_grants_stacks_on_active_sub(fdb, monkeypatch):
    """/give_pro on a user with an active pro_until stacks (does not reset)."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)
    remaining_days = 10
    future_iso = (
        datetime.now(timezone.utc) + timedelta(days=remaining_days)
    ).isoformat()
    _seed_user(fdb, _TARGET_TG_ID, pro_until=future_iso)

    update = _make_update(_ADMIN_TG_ID, [str(_TARGET_TG_ID), "30"])
    ctx = _make_context([str(_TARGET_TG_ID), "30"])

    await admin_surface.cmd_give_pro(update, ctx)

    pro_until_iso = fdb.users[_TARGET_TG_ID]["pro_until"]
    assert pro_until_iso is not None
    pro_until = datetime.fromisoformat(pro_until_iso.replace("Z", "+00:00"))
    if pro_until.tzinfo is None:
        pro_until = pro_until.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    # Must be ~40 days from now (10 remaining + 30 granted), not just 30.
    expected_low = now + timedelta(days=remaining_days + 30) - timedelta(seconds=5)
    expected_high = now + timedelta(days=remaining_days + 30) + timedelta(seconds=5)
    assert expected_low <= pro_until <= expected_high, (
        f"Expected ~{remaining_days + 30}d from now but got {pro_until}"
    )


@pytest.mark.asyncio
async def test_give_pro_missing_args_replies_usage(fdb, monkeypatch):
    """Admin /give_pro with missing args replies with usage hint."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)

    update = _make_update(_ADMIN_TG_ID, [])
    ctx = _make_context([])  # no args

    await admin_surface.cmd_give_pro(update, ctx)

    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "give_pro" in reply.lower() or "использование" in reply.lower()


# ─── /revoke_pro — ADMIN_ID gate ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revoke_pro_non_admin_silently_ignored(fdb, monkeypatch):
    """Non-admin → revoke_pro returns silently without touching the DB."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)
    future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _seed_user(fdb, _TARGET_TG_ID, pro_until=future_iso)

    update = _make_update(_NON_ADMIN_TG_ID, [str(_TARGET_TG_ID)])
    ctx = _make_context([str(_TARGET_TG_ID)])

    await admin_surface.cmd_revoke_pro(update, ctx)

    update.message.reply_text.assert_not_awaited()
    # pro_until must be unchanged.
    assert fdb.users[_TARGET_TG_ID]["pro_until"] == future_iso


@pytest.mark.asyncio
async def test_revoke_pro_admin_clears_pro_until(fdb, monkeypatch):
    """Admin /revoke_pro clears pro_until and trial_ends_at, replies to admin."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)
    future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    trial_iso = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _seed_user(fdb, _TARGET_TG_ID, pro_until=future_iso, trial_ends_at=trial_iso, trial_used=True)

    update = _make_update(_ADMIN_TG_ID, [str(_TARGET_TG_ID)])
    ctx = _make_context([str(_TARGET_TG_ID)])

    await admin_surface.cmd_revoke_pro(update, ctx)

    row = fdb.users[_TARGET_TG_ID]
    assert row["pro_until"] is None, "pro_until must be cleared after revoke"
    assert row["trial_ends_at"] is None, "trial_ends_at must be cleared after revoke"
    # trial_used NOT reset — revoke must not re-arm a free trial.
    assert row["trial_used"] is True, "trial_used must remain True after revoke"

    # Admin gets a confirmation reply.
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert str(_TARGET_TG_ID) in reply


@pytest.mark.asyncio
async def test_revoke_pro_missing_arg_replies_usage(fdb, monkeypatch):
    """Admin /revoke_pro with no args replies with usage hint."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", _ADMIN_TG_ID)

    update = _make_update(_ADMIN_TG_ID, [])
    ctx = _make_context([])

    await admin_surface.cmd_revoke_pro(update, ctx)

    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "revoke_pro" in reply.lower() or "использование" in reply.lower()


@pytest.mark.asyncio
async def test_revoke_pro_admin_id_zero_blocks_all(fdb, monkeypatch):
    """ADMIN_ID=0 (default/unset) disables admin commands — everyone is non-admin."""
    monkeypatch.setattr(admin_surface, "ADMIN_ID", 0)
    future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _seed_user(fdb, _TARGET_TG_ID, pro_until=future_iso)

    # Even if someone sends ADMIN_ID=0 as their own id (edge case), gate must block.
    update = _make_update(0, [str(_TARGET_TG_ID)])
    ctx = _make_context([str(_TARGET_TG_ID)])

    await admin_surface.cmd_revoke_pro(update, ctx)

    update.message.reply_text.assert_not_awaited()
    assert fdb.users[_TARGET_TG_ID]["pro_until"] == future_iso
