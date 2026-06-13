"""
Offline unit tests for subscription logic (subscriptions.py) and DB helpers (db.py).

All DB access is intercepted via monkeypatching.  No network, no Supabase,
no real tg ids (synthetic 111111111-style only).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import digest_bot.db as db_module
import digest_bot.subscriptions as subs_module
from digest_bot.subscriptions import (
    PaymentResult,
    apply_successful_payment,
    grant_trial,
    is_subscription_active,
    revoke_subscription,
    update_subscription,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _utc(offset_seconds: float = 0) -> datetime:
    """now() ± offset_seconds in UTC."""
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ─── in-memory DB shim ────────────────────────────────────────────────────────
#
# A minimal fake that mirrors the db.py async surface used by subscriptions.py.
# Only stores rows used by the tested functions; irrelevant columns are absent.

class FakeDB:
    """In-memory replacement for db.* functions used by subscriptions.py."""

    def __init__(self):
        # users table: tg_user_id → row dict
        self.users: dict[int, dict] = {}
        # tier_defaults: tier → limits dict
        self.tier_defaults: dict[str, dict] = {}
        # user_settings: user_id (UUID string) → row dict
        self.user_settings: dict[str, dict] = {}
        # subscription_events: charge_id → row dict
        self.events: dict[str, dict] = {}

    # ── read ──────────────────────────────────────────────────────────────────

    async def get_user_by_tg_id(self, tg_user_id: int) -> Optional[dict]:
        return copy.deepcopy(self.users.get(tg_user_id))

    async def get_or_create_user(self, tg_user_id: int) -> dict:
        if tg_user_id not in self.users:
            row = {
                "id": str(tg_user_id),  # synthetic UUID-shaped string
                "tg_user_id": tg_user_id,
                "tier": "trial",
                "trial_used": False,
                "pro_until": None,
                "trial_ends_at": None,
                "trial_warn_sent": {},
            }
            self.users[tg_user_id] = row
        return copy.deepcopy(self.users[tg_user_id])

    async def get_tier_limits(self, tier: str) -> dict:
        return copy.deepcopy(self.tier_defaults.get(tier, {}))

    async def get_tier_default(self, tier: str, key: str, default: Any = None) -> Any:
        limits = await self.get_tier_limits(tier)
        return limits.get(key, default)

    async def load_settings(self, user_id: str) -> dict:
        if user_id not in self.user_settings:
            raise RuntimeError(f"user_settings missing for user_id={user_id}")
        return copy.deepcopy(self.user_settings[user_id])

    # ── write ─────────────────────────────────────────────────────────────────

    async def update_user_fields(self, tg_user_id: int, fields: dict) -> bool:
        if tg_user_id not in self.users:
            return False
        self.users[tg_user_id].update(fields)
        return True

    async def update_subscription_row(self, tg_user_id: int, pro_until_iso: Optional[str], tier: Optional[str] = None) -> bool:
        fields = {"pro_until": pro_until_iso}
        if tier:
            fields["tier"] = tier
        return await self.update_user_fields(tg_user_id, fields)

    async def grant_trial_row(self, tg_user_id: int, trial_ends_at_iso: str) -> bool:
        return await self.update_user_fields(tg_user_id, {
            "trial_ends_at": trial_ends_at_iso,
            "trial_used": True,
        })

    # ── events ────────────────────────────────────────────────────────────────

    async def insert_subscription_event(
        self,
        user_id: str,
        event_type: str,
        payload: Optional[dict] = None,
        stars_amount: Optional[int] = None,
        telegram_payment_charge_id: Optional[str] = None,
    ) -> dict:
        if telegram_payment_charge_id and telegram_payment_charge_id in self.events:
            raise db_module.DuplicateCharge(telegram_payment_charge_id)
        row = {
            "user_id": user_id,
            "event_type": event_type,
            "payload": payload or {},
            "stars_amount": stars_amount,
            "telegram_payment_charge_id": telegram_payment_charge_id,
        }
        if telegram_payment_charge_id:
            self.events[telegram_payment_charge_id] = row
        return row

    async def delete_subscription_event(self, telegram_payment_charge_id: str) -> None:
        self.events.pop(telegram_payment_charge_id, None)

    async def record_payment_event(
        self,
        user_id: str,
        event_type: str,
        payload: Optional[dict] = None,
        stars_amount: Optional[int] = None,
        telegram_payment_charge_id: Optional[str] = None,
    ) -> bool:
        try:
            await self.insert_subscription_event(
                user_id=user_id,
                event_type=event_type,
                payload=payload,
                stars_amount=stars_amount,
                telegram_payment_charge_id=telegram_payment_charge_id,
            )
            return True
        except db_module.DuplicateCharge:
            return False

    async def get_effective_limit(self, user_id: str, key: str, fallback: Any = None) -> Any:
        settings = await self.load_settings(user_id)
        overrides = settings.get("limits") or {}
        if key in overrides:
            return overrides[key]
        # resolve the user's tier — find the user by id
        user_row = None
        for u in self.users.values():
            if u.get("id") == user_id:
                user_row = u
                break
        if user_row:
            tier_limits = await self.get_tier_limits(user_row["tier"])
            if key in tier_limits:
                return tier_limits[key]
        return fallback


@pytest.fixture()
def fdb():
    """A fresh FakeDB instance for each test."""
    return FakeDB()


@pytest.fixture(autouse=True)
def patch_db(fdb, monkeypatch):
    """Redirect all db.* calls used by subscriptions.py to the FakeDB."""
    monkeypatch.setattr(db_module, "get_user_by_tg_id", fdb.get_user_by_tg_id)
    monkeypatch.setattr(db_module, "get_or_create_user", fdb.get_or_create_user)
    monkeypatch.setattr(db_module, "get_tier_default", fdb.get_tier_default)
    monkeypatch.setattr(db_module, "get_tier_limits", fdb.get_tier_limits)
    monkeypatch.setattr(db_module, "update_user_fields", fdb.update_user_fields)
    monkeypatch.setattr(db_module, "update_subscription_row", fdb.update_subscription_row)
    monkeypatch.setattr(db_module, "grant_trial_row", fdb.grant_trial_row)
    monkeypatch.setattr(db_module, "insert_subscription_event", fdb.insert_subscription_event)
    monkeypatch.setattr(db_module, "delete_subscription_event", fdb.delete_subscription_event)
    monkeypatch.setattr(db_module, "record_payment_event", fdb.record_payment_event)
    monkeypatch.setattr(db_module, "load_settings", fdb.load_settings)
    monkeypatch.setattr(db_module, "get_effective_limit", fdb.get_effective_limit)


# ─── helper: seed a user row directly ─────────────────────────────────────────

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


# ─── is_subscription_active ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_active_no_user(fdb):
    """No user row → inactive."""
    assert not await is_subscription_active(111111111)


@pytest.mark.asyncio
async def test_active_pro_until_future(fdb):
    future = _iso(_utc(+86400))
    _seed_user(fdb, 111111112, pro_until=future)
    assert await is_subscription_active(111111112)


@pytest.mark.asyncio
async def test_active_pro_until_past(fdb):
    past = _iso(_utc(-1))
    _seed_user(fdb, 111111113, pro_until=past)
    assert not await is_subscription_active(111111113)


@pytest.mark.asyncio
async def test_active_trial_ends_future(fdb):
    future = _iso(_utc(+3600))
    _seed_user(fdb, 111111114, trial_ends_at=future)
    assert await is_subscription_active(111111114)


@pytest.mark.asyncio
async def test_active_trial_ends_past(fdb):
    past = _iso(_utc(-1))
    _seed_user(fdb, 111111115, trial_ends_at=past)
    assert not await is_subscription_active(111111115)


@pytest.mark.asyncio
async def test_active_both_timestamps_past(fdb):
    """Both timestamps expired → inactive."""
    past = _iso(_utc(-1))
    _seed_user(fdb, 111111116, pro_until=past, trial_ends_at=past)
    assert not await is_subscription_active(111111116)


@pytest.mark.asyncio
async def test_active_pro_active_trial_expired(fdb):
    """pro_until future even if trial expired → still active."""
    future = _iso(_utc(+86400))
    past = _iso(_utc(-1))
    _seed_user(fdb, 111111117, pro_until=future, trial_ends_at=past)
    assert await is_subscription_active(111111117)


# ─── update_subscription ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_subscription_starts_from_now_when_expired(fdb):
    """pro_until in the past → base = now() → new_until ≈ now + days."""
    past = _iso(_utc(-3600))  # 1 h ago
    _seed_user(fdb, 222222221, pro_until=past)

    before = _utc()
    new_until = await update_subscription(222222221, days=30)
    after = _utc()

    # Should be ~30 days from now, not from an expired past date.
    expected_low = before + timedelta(days=30)
    expected_high = after + timedelta(days=30)
    assert expected_low <= new_until <= expected_high


@pytest.mark.asyncio
async def test_update_subscription_stacks_on_active_remainder(fdb):
    """pro_until in the future → stack days on top of the remainder."""
    remaining = timedelta(days=10)
    future = _iso(_utc(remaining.total_seconds()))
    _seed_user(fdb, 222222222, pro_until=future)

    before = _utc()
    new_until = await update_subscription(222222222, days=30)
    after = _utc()

    # new_until must be roughly existing_pro_until + 30 days.
    expected_low = before + remaining + timedelta(days=30) - timedelta(seconds=2)
    expected_high = after + remaining + timedelta(days=30) + timedelta(seconds=2)
    assert expected_low <= new_until <= expected_high


@pytest.mark.asyncio
async def test_update_subscription_writes_back(fdb):
    """Verify the DB row is actually updated."""
    _seed_user(fdb, 222222223)
    new_until = await update_subscription(222222223, days=7)
    stored = fdb.users[222222223]["pro_until"]
    assert stored is not None
    stored_dt = datetime.fromisoformat(stored.replace("Z", "+00:00"))
    if stored_dt.tzinfo is None:
        stored_dt = stored_dt.replace(tzinfo=timezone.utc)
    assert abs((stored_dt - new_until).total_seconds()) < 1


# ─── grant_trial ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_trial_once(fdb):
    """First call returns True and sets trial_ends_at + trial_used."""
    fdb.tier_defaults["trial"] = {"days": 3}
    _seed_user(fdb, 333333331, trial_used=False)

    result = await grant_trial(333333331)

    assert result is True
    row = fdb.users[333333331]
    assert row["trial_used"] is True
    assert row["trial_ends_at"] is not None
    # trial_ends_at should be ~3 days in the future
    ends_dt = datetime.fromisoformat(row["trial_ends_at"].replace("Z", "+00:00"))
    if ends_dt.tzinfo is None:
        ends_dt = ends_dt.replace(tzinfo=timezone.utc)
    diff = ends_dt - _utc()
    assert timedelta(days=2, hours=23) < diff < timedelta(days=3, hours=1)


@pytest.mark.asyncio
async def test_grant_trial_idempotent_noop(fdb):
    """Second call returns False and does NOT update trial_ends_at again."""
    fdb.tier_defaults["trial"] = {"days": 3}
    future = _iso(_utc(+86400))
    _seed_user(fdb, 333333332, trial_used=True, trial_ends_at=future)

    result = await grant_trial(333333332)

    assert result is False
    # trial_ends_at should remain unchanged
    assert fdb.users[333333332]["trial_ends_at"] == future


@pytest.mark.asyncio
async def test_grant_trial_no_user_returns_false(fdb):
    """No user row → False, no crash."""
    fdb.tier_defaults["trial"] = {"days": 3}
    result = await grant_trial(333333399)
    assert result is False


# ─── revoke_subscription ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revoke_clears_both_timestamps(fdb):
    future = _iso(_utc(+86400))
    _seed_user(fdb, 444444441, pro_until=future, trial_ends_at=future, trial_used=True)

    ok = await revoke_subscription(444444441)

    assert ok is True
    row = fdb.users[444444441]
    assert row["pro_until"] is None
    assert row["trial_ends_at"] is None


@pytest.mark.asyncio
async def test_revoke_keeps_trial_used(fdb):
    """Revoke does NOT reset trial_used — the user cannot re-arm a free trial."""
    future = _iso(_utc(+86400))
    _seed_user(fdb, 444444442, pro_until=future, trial_used=True)

    await revoke_subscription(444444442)

    # trial_used is NOT written by revoke; it should remain as-is from seed.
    assert fdb.users[444444442]["trial_used"] is True


@pytest.mark.asyncio
async def test_revoke_inactive_user_returns_false(fdb):
    """Revoking a non-existent user row returns False."""
    result = await revoke_subscription(444444499)
    assert result is False


# ─── get_effective_limit ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_effective_limit_user_override_wins(fdb):
    """User-level override in user_settings.limits beats tier default."""
    uid = "555555551"
    _seed_user(fdb, 555555551, tier="trial")
    fdb.tier_defaults["trial"] = {"digests_per_day": 1}
    fdb.user_settings[uid] = {"limits": {"digests_per_day": 99}}

    result = await db_module.get_effective_limit(uid, "digests_per_day", fallback=0)
    assert result == 99


@pytest.mark.asyncio
async def test_effective_limit_tier_default_used_when_no_override(fdb):
    """No user override → falls back to tier default."""
    uid = "555555552"
    _seed_user(fdb, 555555552, tier="pro")
    fdb.tier_defaults["pro"] = {"digests_per_day": 5}
    fdb.user_settings[uid] = {"limits": {}}

    result = await db_module.get_effective_limit(uid, "digests_per_day", fallback=0)
    assert result == 5


@pytest.mark.asyncio
async def test_effective_limit_fallback_used_when_no_tier_key(fdb):
    """Neither override nor tier row has the key → return the explicit fallback."""
    uid = "555555553"
    _seed_user(fdb, 555555553, tier="trial")
    fdb.tier_defaults["trial"] = {}  # key absent
    fdb.user_settings[uid] = {"limits": {}}

    result = await db_module.get_effective_limit(uid, "some_limit", fallback=42)
    assert result == 42


@pytest.mark.asyncio
async def test_effective_limit_user_zero_override_beats_tier(fdb):
    """User override of 0 (falsy) still beats tier default — falsy is valid."""
    uid = "555555554"
    _seed_user(fdb, 555555554, tier="pro")
    fdb.tier_defaults["pro"] = {"digests_per_day": 5}
    fdb.user_settings[uid] = {"limits": {"digests_per_day": 0}}

    result = await db_module.get_effective_limit(uid, "digests_per_day", fallback=99)
    assert result == 0


# ─── record_payment_event idempotency ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_payment_event_first_insert_returns_true(fdb):
    result = await db_module.record_payment_event(
        user_id="666666661",
        event_type="payment_succeeded",
        telegram_payment_charge_id="charge_abc_001",
        stars_amount=900,
    )
    assert result is True
    assert "charge_abc_001" in fdb.events


@pytest.mark.asyncio
async def test_record_payment_event_duplicate_returns_false(fdb):
    """Same charge_id submitted twice → second call returns False, row unchanged."""
    charge_id = "charge_abc_002"
    await db_module.record_payment_event(
        user_id="666666662",
        event_type="payment_succeeded",
        telegram_payment_charge_id=charge_id,
        stars_amount=900,
    )
    result = await db_module.record_payment_event(
        user_id="666666662",
        event_type="payment_succeeded",
        telegram_payment_charge_id=charge_id,
        stars_amount=900,
    )
    assert result is False
    # Still exactly one event row
    assert len([k for k in fdb.events if k == charge_id]) == 1


# ─── apply_successful_payment (full idempotency) ──────────────────────────────

@pytest.mark.asyncio
async def test_apply_payment_grants_once(fdb):
    """First successful payment → granted=True, pro_until extended."""
    fdb.tier_defaults["pro"] = {
        "days_month": 31,
        "price_month_stars": 900,
        "days_quarter": 93,
        "price_quarter_stars": 2400,
    }
    tg_id = 777777771
    _seed_user(fdb, tg_id)

    result = await apply_successful_payment(
        tg_user_id=tg_id,
        payload="digest_pro_month",
        telegram_payment_charge_id="charge_pay_001",
        total_amount=900,
    )

    assert result.granted is True
    assert fdb.users[tg_id]["pro_until"] is not None
    assert "charge_pay_001" in fdb.events


@pytest.mark.asyncio
async def test_apply_payment_idempotent_duplicate(fdb):
    """Duplicate delivery of the same charge → granted=False, no second grant."""
    fdb.tier_defaults["pro"] = {
        "days_month": 31,
        "price_month_stars": 900,
        "days_quarter": 93,
        "price_quarter_stars": 2400,
    }
    tg_id = 777777772
    _seed_user(fdb, tg_id)

    await apply_successful_payment(
        tg_user_id=tg_id,
        payload="digest_pro_month",
        telegram_payment_charge_id="charge_pay_002",
        total_amount=900,
    )
    first_pro_until = fdb.users[tg_id]["pro_until"]

    # Duplicate delivery
    result2 = await apply_successful_payment(
        tg_user_id=tg_id,
        payload="digest_pro_month",
        telegram_payment_charge_id="charge_pay_002",
        total_amount=900,
    )

    assert result2.granted is False
    # pro_until must NOT have changed
    assert fdb.users[tg_id]["pro_until"] == first_pro_until


@pytest.mark.asyncio
async def test_apply_payment_unknown_payload(fdb):
    """Unknown payload → granted=False, no crash, no DB row written."""
    tg_id = 777777773
    _seed_user(fdb, tg_id)

    result = await apply_successful_payment(
        tg_user_id=tg_id,
        payload="digest_pro_unknown_xyz",
        telegram_payment_charge_id="charge_pay_003",
        total_amount=0,
    )

    assert result.granted is False
    assert "charge_pay_003" not in fdb.events


@pytest.mark.asyncio
async def test_apply_payment_quarter_grants_correct_days(fdb):
    """Quarter product uses days_quarter from tier_defaults."""
    fdb.tier_defaults["pro"] = {
        "days_month": 31,
        "price_month_stars": 900,
        "days_quarter": 93,
        "price_quarter_stars": 2400,
    }
    tg_id = 777777774
    _seed_user(fdb, tg_id)

    before = _utc()
    result = await apply_successful_payment(
        tg_user_id=tg_id,
        payload="digest_pro_quarter",
        telegram_payment_charge_id="charge_pay_004",
        total_amount=2400,
    )
    after = _utc()

    assert result.granted is True
    pro_until_str = fdb.users[tg_id]["pro_until"]
    pro_until = datetime.fromisoformat(pro_until_str.replace("Z", "+00:00"))
    if pro_until.tzinfo is None:
        pro_until = pro_until.replace(tzinfo=timezone.utc)

    expected_low = before + timedelta(days=93)
    expected_high = after + timedelta(days=93)
    assert expected_low <= pro_until <= expected_high


@pytest.mark.asyncio
async def test_buy_text_renders_html_strike_and_discount(monkeypatch):
    """Paywall anchors render as HTML <s> (the message is HTML — Markdown ~~ would
    show literal tildes) and the discount line appears; charged prices unchanged."""
    import digest_bot.handlers.subscription as sub

    vals = {
        ("pro", "price_month_stars"): 900,
        ("pro", "price_quarter_stars"): 2400,
        ("pro", "price_anchor_month_stars"): 1170,
        ("pro", "price_anchor_quarter_stars"): 3120,
    }

    async def _gtd(tier, key, default=None):
        return vals.get((tier, key), default)

    monkeypatch.setattr(db_module, "get_tier_default", _gtd)
    text = await sub._buy_text()

    assert "<s>1170</s>" in text and "<s>3120</s>" in text
    assert "~~" not in text                      # no Markdown strike in an HTML message
    assert "Скидка" in text or "скидк" in text.lower()
    assert "900" in text and "2400" in text      # charged prices intact
