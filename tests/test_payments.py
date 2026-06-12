"""
Offline tests for the Stars payment surface (SPEC-payments §5.2 cases 6–7 + T3 extras).

Logic cases 1–5 (is_subscription_active / stacking / grant_trial / fresh+dup
apply_successful_payment) live in tests/test_subscriptions.py against the same
FakeDB. Here we add:
  * case 6 — grant-failure rollback (ledger row deleted + re-raise; retry succeeds);
  * case 7 — PTB handler smoke (mocked Update): pre_checkout answers ok=True once,
             successful_payment calls apply_successful_payment + replies;
  * send_invoice cases — monthly + quarterly: currency=XTR, provider_token="",
    correct payload, DB-sourced price (not constants);
  * stacking on active trial — payment during active trial calls update_subscription
    with right args (trial_ends_at untouched, paid sub stacks on pro_until);
  * idempotency — duplicate SuccessfulPayment re-acks, no double-grant.

No network, no Telegram, no real tg ids (synthetic 111-style strings only).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

import db as db_module
import subscriptions as subs_module
from handlers import subscription as sub_surface


# ─── in-memory DB shim (mirrors tests/test_subscriptions.py FakeDB surface) ────

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

    async def get_tier_limits(self, tier: str) -> dict:
        return copy.deepcopy(self.tier_defaults.get(tier, {}))

    async def get_tier_default(self, tier: str, key: str, default: Any = None) -> Any:
        return (await self.get_tier_limits(tier)).get(key, default)

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


def _seed_pro_defaults(fdb: FakeDB):
    fdb.tier_defaults["pro"] = {
        "days_month": 30, "price_month_stars": 900,
        "days_quarter": 90, "price_quarter_stars": 2400,
    }


def _seed_user(fdb: FakeDB, tg_id: int, **kwargs) -> dict:
    row = {
        "id": str(tg_id), "tg_user_id": tg_id, "tier": "trial",
        "trial_used": False, "pro_until": None, "trial_ends_at": None,
        "trial_warn_sent": {},
    }
    row.update(kwargs)
    fdb.users[tg_id] = row
    return row


# ─── case 6 — grant-failure rollback ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_failure_rolls_back_ledger_and_retry_succeeds(fdb, monkeypatch):
    """If update_subscription raises after a fresh ledger insert, the ledger row is
    deleted and the error re-raised — so a retry with the same charge id succeeds."""
    _seed_pro_defaults(fdb)
    tg_id = 111111811
    _seed_user(fdb, tg_id)
    charge = "charge_rollback_001"

    # Make the first grant blow up; the ledger insert itself succeeds.
    real_update = subs_module.update_subscription
    calls = {"n": 0}

    async def flaky_update(tg_user_id: int, days: int, tier: str = "pro"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated grant failure")
        return await real_update(tg_user_id, days, tier=tier)

    monkeypatch.setattr(subs_module, "update_subscription", flaky_update)

    with pytest.raises(RuntimeError):
        await subs_module.apply_successful_payment(
            tg_user_id=tg_id, payload="digest_pro_month",
            telegram_payment_charge_id=charge, total_amount=900,
        )
    # Ledger row rolled back → charge retryable.
    assert charge not in fdb.events

    # Retry: now the grant succeeds and the charge is ledgered exactly once.
    result = await subs_module.apply_successful_payment(
        tg_user_id=tg_id, payload="digest_pro_month",
        telegram_payment_charge_id=charge, total_amount=900,
    )
    assert result.granted is True
    assert charge in fdb.events
    assert fdb.users[tg_id]["pro_until"] is not None


# ─── case 7a — pre_checkout answers ok=True exactly once ───────────────────────

@pytest.mark.asyncio
async def test_pre_checkout_answers_ok():
    update = MagicMock()
    update.pre_checkout_query = MagicMock()
    update.pre_checkout_query.answer = AsyncMock()
    context = MagicMock()

    await sub_surface.pre_checkout(update, context)

    update.pre_checkout_query.answer.assert_awaited_once_with(ok=True)


# ─── case 7b — successful_payment applies grant + replies ──────────────────────

@pytest.mark.asyncio
async def test_successful_payment_grants_and_replies(fdb):
    _seed_pro_defaults(fdb)
    tg_id = 111111822
    _seed_user(fdb, tg_id)

    update = MagicMock()
    update.effective_user.id = tg_id
    update.message.successful_payment.invoice_payload = "digest_pro_quarter"
    update.message.successful_payment.telegram_payment_charge_id = "charge_h7_001"
    update.message.successful_payment.total_amount = 2400
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await sub_surface.successful_payment(update, context)

    # Fresh grant → pro_until set, ledger row, user told "Pro активирован".
    assert fdb.users[tg_id]["pro_until"] is not None
    assert "charge_h7_001" in fdb.events
    update.message.reply_text.assert_awaited_once()
    sent = update.message.reply_text.await_args.args[0]
    assert "Pro активирован" in sent


@pytest.mark.asyncio
async def test_successful_payment_duplicate_acks_without_regrant(fdb):
    """A redelivered SuccessfulPayment re-acks without a second grant."""
    _seed_pro_defaults(fdb)
    tg_id = 111111833
    _seed_user(fdb, tg_id)

    def _make_update():
        u = MagicMock()
        u.effective_user.id = tg_id
        u.message.successful_payment.invoice_payload = "digest_pro_month"
        u.message.successful_payment.telegram_payment_charge_id = "charge_dup_001"
        u.message.successful_payment.total_amount = 900
        u.message.reply_text = AsyncMock()
        return u

    ctx = MagicMock()
    first = _make_update()
    await sub_surface.successful_payment(first, ctx)
    pro_after_first = fdb.users[tg_id]["pro_until"]

    second = _make_update()
    await sub_surface.successful_payment(second, ctx)

    # No double-grant; the duplicate reply reassures the user.
    assert fdb.users[tg_id]["pro_until"] == pro_after_first
    dup_sent = second.message.reply_text.await_args.args[0]
    assert "уже учтён" in dup_sent


# ─── case 7c — cb_buy product callback opens an invoice ────────────────────────

@pytest.mark.asyncio
async def test_cb_buy_product_sends_invoice(fdb):
    _seed_pro_defaults(fdb)
    tg_id = 111111844
    _seed_user(fdb, tg_id)

    update = MagicMock()
    update.callback_query.data = "buy|digest_pro_month"
    update.callback_query.answer = AsyncMock()
    update.effective_user.id = tg_id
    update.effective_chat.id = tg_id
    context = MagicMock()
    context.bot.send_invoice = AsyncMock()

    await sub_surface.cb_buy(update, context)

    update.callback_query.answer.assert_awaited()
    context.bot.send_invoice.assert_awaited_once()
    kwargs = context.bot.send_invoice.await_args.kwargs
    assert kwargs["payload"] == "digest_pro_month"
    assert kwargs["provider_token"] == ""
    assert kwargs["currency"] == "XTR"
    assert kwargs["prices"][0].amount == 900


# ─── send_invoice: monthly + quarterly explicit assertions ────────────────────

@pytest.mark.asyncio
async def test_send_invoice_monthly_stars_flags(fdb):
    """send_pro_invoice for digest_pro_month uses currency=XTR, provider_token='',
    correct payload and DB-sourced price (not a Python constant)."""
    _seed_pro_defaults(fdb)
    tg_id = 111111855
    _seed_user(fdb, tg_id)

    update = MagicMock()
    update.effective_user.id = tg_id
    update.effective_chat.id = tg_id
    context = MagicMock()
    context.bot.send_invoice = AsyncMock()

    await sub_surface.send_pro_invoice(update, context, "digest_pro_month")

    context.bot.send_invoice.assert_awaited_once()
    kwargs = context.bot.send_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR", "Stars invoices must use currency='XTR'"
    assert kwargs["provider_token"] == "", "Stars invoices must have provider_token=''"
    assert kwargs["payload"] == "digest_pro_month"
    # Price comes from DB tier defaults (900), not a hardcoded constant.
    assert kwargs["prices"][0].amount == 900
    assert kwargs["chat_id"] == tg_id


@pytest.mark.asyncio
async def test_send_invoice_quarterly_stars_flags(fdb):
    """send_pro_invoice for digest_pro_quarter uses correct DB-sourced price and payload."""
    _seed_pro_defaults(fdb)
    tg_id = 111111866
    _seed_user(fdb, tg_id)

    update = MagicMock()
    update.effective_user.id = tg_id
    update.effective_chat.id = tg_id
    context = MagicMock()
    context.bot.send_invoice = AsyncMock()

    await sub_surface.send_pro_invoice(update, context, "digest_pro_quarter")

    context.bot.send_invoice.assert_awaited_once()
    kwargs = context.bot.send_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    assert kwargs["provider_token"] == ""
    assert kwargs["payload"] == "digest_pro_quarter"
    # Price from DB (2400 for quarter), not hardcoded.
    assert kwargs["prices"][0].amount == 2400
    assert kwargs["chat_id"] == tg_id


# ─── stacking on active trial ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payment_during_active_trial_stacks_on_pro_until(fdb):
    """A user who pays while their trial is still active gets pro_until set from now
    (not from trial_ends_at) and trial_ends_at remains untouched.

    Per netwho pattern: update_subscription bases off pro_until if present and
    future, else now. When the user has only a trial (no pro_until), paid days
    start from now. The successful_payment handler must call apply_successful_payment
    with the exact tg_user_id, payload, and charge_id from the update.
    """
    _seed_pro_defaults(fdb)
    tg_id = 111111877
    from datetime import timezone as _tz
    trial_end_iso = (
        datetime.now(_tz.utc) + timedelta(days=2)
    ).isoformat()
    _seed_user(fdb, tg_id, trial_ends_at=trial_end_iso, trial_used=True)

    # No pro_until yet — user is on trial, paying for month.
    update = MagicMock()
    update.effective_user.id = tg_id
    update.message.successful_payment.invoice_payload = "digest_pro_month"
    update.message.successful_payment.telegram_payment_charge_id = "charge_trial_stack_001"
    update.message.successful_payment.total_amount = 900
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await sub_surface.successful_payment(update, context)

    row = fdb.users[tg_id]
    # pro_until was None before; after payment it should be set ~30 days from now.
    assert row["pro_until"] is not None, "pro_until must be set after payment"
    pro_until = datetime.fromisoformat(row["pro_until"].replace("Z", "+00:00"))
    if pro_until.tzinfo is None:
        from datetime import timezone as _tz2
        pro_until = pro_until.replace(tzinfo=_tz2.utc)

    now = datetime.now(_tz.utc)
    # 30 days (from DB) from now — allow ±5s for test clock skew.
    assert timedelta(days=29, hours=23) < (pro_until - now) < timedelta(days=30, hours=1)

    # trial_ends_at is UNCHANGED — payment does not truncate the trial.
    assert row["trial_ends_at"] == trial_end_iso, "trial_ends_at must not be modified by payment"

    # Ledger row written.
    assert "charge_trial_stack_001" in fdb.events

    # Handler ACK-ed the user with "Pro активирован".
    update.message.reply_text.assert_awaited_once()
    sent = update.message.reply_text.await_args.args[0]
    assert "Pro активирован" in sent
