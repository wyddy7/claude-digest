"""Offline acceptance tests for B2 onboarding wizard and invite/trial gating.

Tests exercise:
  1. Cold /start for an invited non-owner walks the wizard end-to-end:
       topic pick → focus skip → preview triggered with user's channels → 'done'.
     A fake in-memory DB shim verifies the users + user_settings rows exist.
  2. Trial is granted exactly once at first /start (idempotent guard).
  3. Preview digest is triggered at onboarding end; the pipeline call is mocked
     and asserted to be invoked with the user's saved channels.
  4. A non-invited tg_user_id gets the invite-only reply; no rows are created.
  5. A post-trial-expiry user requesting a digest gets the paywall message and
     the real digest pipeline is never invoked.

No network, no Supabase, no real Bot API.  Synthetic tg ids only (111111111-style).
db.* functions used by handlers are monkeypatched to an in-memory FakeDB.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest

import db as db_module
import subscriptions as subs_module
from handlers import middleware as mw
from handlers import onboarding as onb

# ── synthetic ids (never real user data) ──────────────────────────────────────
INVITED_USER = 222222222
UNINVITED_USER = 333333333


# ── minimal in-memory DB shim (mirrors FakeDB from test_subscriptions) ─────────

class FakeDB:
    def __init__(self):
        # users table: tg_user_id → row dict
        self.users: dict[int, dict] = {}
        # user_settings: user_id (str) → row dict
        self.user_settings: dict[str, dict] = {}
        # tier_defaults: tier name → limits dict
        self.tier_defaults: dict[str, dict] = {
            "trial": {"days": 3, "channels_max": 15},
            "pro": {"days_month": 31, "price_month_stars": 900,
                    "days_quarter": 93, "price_quarter_stars": 2400},
        }
        # subscription_events: charge_id → row
        self.events: dict[str, dict] = {}

    # ── read ──────────────────────────────────────────────────────────────────

    async def get_user_by_tg_id(self, tg_user_id: int) -> Optional[dict]:
        return copy.deepcopy(self.users.get(tg_user_id))

    async def get_or_create_user(self, tg_user_id: int) -> dict:
        if tg_user_id not in self.users:
            uid = str(tg_user_id)
            row = {
                "id": uid,
                "tg_user_id": tg_user_id,
                "tier": "trial",
                "onboarding_state": "new",
                "trial_used": False,
                "pro_until": None,
                "trial_ends_at": None,
                "trial_warn_sent": {},
            }
            self.users[tg_user_id] = row
            # seed a paired user_settings row
            self.user_settings[uid] = {"user_id": uid, "limits": {}, "channels": []}
        return copy.deepcopy(self.users[tg_user_id])

    async def load_settings(self, user_id: str) -> dict:
        if user_id not in self.user_settings:
            # auto-create so tests that only care about users don't crash
            self.user_settings[user_id] = {"user_id": user_id, "limits": {}, "channels": []}
        return copy.deepcopy(self.user_settings[user_id])

    async def get_tier_limits(self, tier: str) -> dict:
        return copy.deepcopy(self.tier_defaults.get(tier, {}))

    async def get_tier_default(self, tier: str, key: str, default: Any = None) -> Any:
        return self.tier_defaults.get(tier, {}).get(key, default)

    async def get_effective_limit(self, user_id: str, key: str, fallback: Any = None) -> Any:
        settings = await self.load_settings(user_id)
        overrides = settings.get("limits") or {}
        if key in overrides:
            return overrides[key]
        # find the user's tier
        for u in self.users.values():
            if u.get("id") == user_id:
                tier_limits = await self.get_tier_limits(u["tier"])
                if key in tier_limits:
                    return tier_limits[key]
        return fallback

    # ── write ─────────────────────────────────────────────────────────────────

    async def update_user_fields(self, tg_user_id: int, fields: dict) -> bool:
        if tg_user_id not in self.users:
            return False
        self.users[tg_user_id].update(fields)
        return True

    async def save_settings(self, user_id: str, fields: dict) -> dict:
        if user_id not in self.user_settings:
            self.user_settings[user_id] = {"user_id": user_id, "limits": {}}
        self.user_settings[user_id].update(fields)
        return copy.deepcopy(self.user_settings[user_id])

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

    async def insert_subscription_event(self, user_id, event_type, payload=None,
                                        stars_amount=None, telegram_payment_charge_id=None):
        if telegram_payment_charge_id and telegram_payment_charge_id in self.events:
            raise db_module.DuplicateCharge(telegram_payment_charge_id)
        row = {"user_id": user_id, "event_type": event_type}
        if telegram_payment_charge_id:
            self.events[telegram_payment_charge_id] = row
        return row

    async def delete_subscription_event(self, telegram_payment_charge_id: str) -> None:
        self.events.pop(telegram_payment_charge_id, None)

    async def record_payment_event(self, user_id, event_type, payload=None,
                                   stars_amount=None, telegram_payment_charge_id=None):
        try:
            await self.insert_subscription_event(
                user_id=user_id, event_type=event_type, payload=payload,
                stars_amount=stars_amount,
                telegram_payment_charge_id=telegram_payment_charge_id,
            )
            return True
        except db_module.DuplicateCharge:
            return False

    async def load_personalization_db(self, tenant_id: str) -> dict:
        return {}


def _seed_invited(fdb: FakeDB, tg_id: int = INVITED_USER, **kwargs) -> dict:
    """Place an existing invited user row (onboarding_state='invited')."""
    uid = str(tg_id)
    row = {
        "id": uid,
        "tg_user_id": tg_id,
        "tier": "trial",
        "onboarding_state": "invited",
        "trial_used": False,
        "pro_until": None,
        "trial_ends_at": None,
        "trial_warn_sent": {},
    }
    row.update(kwargs)
    fdb.users[tg_id] = row
    fdb.user_settings[uid] = {"user_id": uid, "limits": {}, "channels": []}
    return row


# ── PTB fake helpers ──────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))
        return SimpleNamespace(edit_text=AsyncMock())


class FakeQuery:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.message.replies.append((text, kw))


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class FakeContext:
    def __init__(self, bot: Optional[FakeBot] = None):
        self.user_data: dict = {}
        self.bot = bot or FakeBot()


def _make_update(*, tg_user_id: int = INVITED_USER, text: str = "",
                  callback_data: Optional[str] = None):
    msg = FakeMessage(text)
    q = FakeQuery(callback_data, msg) if callback_data is not None else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=tg_user_id),
        message=msg if callback_data is None else None,
        callback_query=q,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=tg_user_id),
    )


# ── shared monkeypatch fixture ────────────────────────────────────────────────

@pytest.fixture()
def fdb():
    return FakeDB()


@pytest.fixture(autouse=True)
def patch_db(fdb, monkeypatch):
    monkeypatch.setattr(db_module, "get_user_by_tg_id", fdb.get_user_by_tg_id)
    monkeypatch.setattr(db_module, "get_or_create_user", fdb.get_or_create_user)
    monkeypatch.setattr(db_module, "load_settings", fdb.load_settings)
    monkeypatch.setattr(db_module, "save_settings", fdb.save_settings)
    monkeypatch.setattr(db_module, "update_user_fields", fdb.update_user_fields)
    monkeypatch.setattr(db_module, "get_tier_limits", fdb.get_tier_limits)
    monkeypatch.setattr(db_module, "get_tier_default", fdb.get_tier_default)
    monkeypatch.setattr(db_module, "get_effective_limit", fdb.get_effective_limit)
    monkeypatch.setattr(db_module, "update_subscription_row", fdb.update_subscription_row)
    monkeypatch.setattr(db_module, "grant_trial_row", fdb.grant_trial_row)
    monkeypatch.setattr(db_module, "insert_subscription_event", fdb.insert_subscription_event)
    monkeypatch.setattr(db_module, "delete_subscription_event", fdb.delete_subscription_event)
    monkeypatch.setattr(db_module, "record_payment_event", fdb.record_payment_event)
    monkeypatch.setattr(db_module, "load_personalization_db", fdb.load_personalization_db)


# ── acceptance #4: non-invited user gets invite-only reply, no rows ───────────

@pytest.mark.asyncio
async def test_non_invited_gets_gate_no_row_created(fdb, monkeypatch):
    """A tg_user_id that has no row in users gets the invite-only reply.
    No users or user_settings row is created."""
    upd = _make_update(tg_user_id=UNINVITED_USER, text="/start")
    ctx = FakeContext()

    from telegram.ext import ApplicationHandlerStop
    with pytest.raises(ApplicationHandlerStop):
        await mw.resolve_user(upd, ctx)

    # Reply must mention invitation.
    assert upd.message.replies, "expected at least one reply"
    reply_text = upd.message.replies[0][0]
    assert "приглашению" in reply_text or "приглашение" in reply_text or "приглас" in reply_text

    # No row must have been written.
    assert UNINVITED_USER not in fdb.users
    assert str(UNINVITED_USER) not in fdb.user_settings


# ── acceptance #2: trial granted exactly once at first /start ─────────────────

@pytest.mark.asyncio
async def test_trial_granted_exactly_once_on_first_start(fdb, monkeypatch):
    """First /start grants the trial; a second /start on a 'done' user does not."""
    _seed_invited(fdb, INVITED_USER)
    grant = AsyncMock(return_value=True)
    monkeypatch.setattr(subs_module, "grant_trial", grant)

    ctx = FakeContext()
    ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
    upd = _make_update(text="/start")

    await onb.start(upd, ctx)

    # Trial must have been granted once.
    grant.assert_awaited_once_with(INVITED_USER)
    # onboarding_state must have advanced to ST_CHANNELS.
    stored_state = fdb.users[INVITED_USER].get("onboarding_state")
    assert stored_state == onb.ST_CHANNELS

    # Simulate the user completing onboarding → mark done.
    fdb.users[INVITED_USER]["onboarding_state"] = onb.ST_DONE
    ctx.user_data["user"]["onboarding_state"] = onb.ST_DONE

    # Second /start for a done user must NOT re-arm the trial.
    grant.reset_mock()
    monkeypatch.setattr(db_module, "load_settings", fdb.load_settings)
    await onb.start(upd, ctx)
    grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_idempotent_second_start_same_state(fdb, monkeypatch):
    """A user that already has trial_used=True cannot re-arm the trial via /start."""
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _seed_invited(fdb, INVITED_USER, trial_used=True, trial_ends_at=future,
                  onboarding_state="invited")

    grant = AsyncMock(return_value=False)  # subscriptions.grant_trial would return False too
    monkeypatch.setattr(subs_module, "grant_trial", grant)

    ctx = FakeContext()
    ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
    upd = _make_update(text="/start")
    await onb.start(upd, ctx)

    # grant_trial was delegated — the call went through.
    grant.assert_awaited_once_with(INVITED_USER)
    # The trial_ends_at is unchanged (grant_trial returned False = no-op).
    assert fdb.users[INVITED_USER]["trial_ends_at"] == future


# ── acceptance #1 + #3: full wizard walk, rows exist, preview triggered ────────

@pytest.mark.asyncio
async def test_full_wizard_walk_topic_focus_preview(fdb, monkeypatch):
    """Cold /start for an invited user walks the 3-step wizard:
    topic-pick → focus skip → preview triggered with user's channels → ST_DONE.

    Checks:
    - users row exists (onboarding_state=done after preview).
    - user_settings row exists (channels saved from topic pick).
    - deliver_digest was called exactly once with the correct user dict.
    - The real LLM pipeline is never invoked (deliver_digest is mocked).
    """
    # Seed invited user row.
    _seed_invited(fdb, INVITED_USER)

    # Mirror production grant_trial: it WRITES trial_ends_at to the row, which is
    # what the preview gate (no LLM spend without an active sub) checks later.
    async def _grant_trial(tg_id):
        fdb.users[tg_id]["trial_ends_at"] = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat()
        fdb.users[tg_id]["trial_used"] = True
        return True

    monkeypatch.setattr(subs_module, "grant_trial", _grant_trial)

    # ── Step 0: /start → wizard entry ─────────────────────────────────────────
    ctx = FakeContext()
    ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
    upd0 = _make_update(text="/start")
    await onb.start(upd0, ctx)

    # onboarding must have advanced to ST_CHANNELS.
    assert fdb.users[INVITED_USER]["onboarding_state"] == onb.ST_CHANNELS

    # ── Step 1: topic pick → "onb|topic|ai" ───────────────────────────────────
    TEST_CHANNELS = ["ai_news_a", "ai_news_b"]
    monkeypatch.setattr(onb, "_TOPICS", {"ai": TEST_CHANNELS})
    monkeypatch.setattr(db_module, "get_effective_limit", AsyncMock(return_value=15))

    ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
    upd1 = _make_update(callback_data="onb|topic|ai")
    await onb._toggle_topic(upd1, ctx, "ai")
    assert ctx.user_data["onb_channels"] == TEST_CHANNELS

    # ── Step 2: channels done → advances to ST_FOCUS ──────────────────────────
    ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
    ctx.user_data["onb_channels"] = TEST_CHANNELS[:]
    upd2 = _make_update(callback_data="onb|ch_done")
    await onb._channels_done(upd2, ctx)

    # channels must be saved to DB.
    saved_channels = fdb.user_settings[str(INVITED_USER)].get("channels")
    assert saved_channels == TEST_CHANNELS

    # onboarding_state must be ST_FOCUS.
    assert fdb.users[INVITED_USER]["onboarding_state"] == onb.ST_FOCUS

    # ── Step 3: focus skip → triggers _run_preview → deliver_digest called ─────
    deliver_mock = AsyncMock(return_value=5)  # simulate 5 posts returned
    with patch("handlers.digest.deliver_digest", deliver_mock):
        ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
        # Update the user dict to reflect the channels saved in settings.
        fdb.user_settings[str(INVITED_USER)]["channels"] = TEST_CHANNELS
        # load_settings is already patched to fdb.load_settings

        upd3 = _make_update(callback_data="onb|focus_skip")
        # _set_focus with empty string → saves focus, calls _run_preview.
        await onb._set_focus(upd3, ctx, "")

    # deliver_digest must have been invoked exactly once with the user's dict.
    deliver_mock.assert_awaited_once()
    called_user = deliver_mock.await_args[0][1]  # second positional arg = user
    assert called_user["tg_user_id"] == INVITED_USER

    # onboarding_state must reach ST_DONE (set in _run_preview finally block).
    assert fdb.users[INVITED_USER]["onboarding_state"] == onb.ST_DONE

    # users row must exist with expected shape.
    assert INVITED_USER in fdb.users
    user_row = fdb.users[INVITED_USER]
    assert user_row["tg_user_id"] == INVITED_USER

    # user_settings row must exist with the channels.
    assert str(INVITED_USER) in fdb.user_settings
    settings_row = fdb.user_settings[str(INVITED_USER)]
    assert settings_row["channels"] == TEST_CHANNELS


@pytest.mark.asyncio
async def test_preview_not_triggered_if_no_channels(fdb, monkeypatch):
    """_channels_done with an empty candidate set shows a popup and does NOT
    advance to ST_FOCUS (so the preview is never triggered)."""
    _seed_invited(fdb, INVITED_USER, onboarding_state=onb.ST_CHANNELS)
    deliver_mock = AsyncMock()
    with patch("handlers.digest.deliver_digest", deliver_mock):
        ctx = FakeContext()
        ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
        ctx.user_data["onb_channels"] = []
        upd = _make_update(callback_data="onb|ch_done")
        await onb._channels_done(upd, ctx)

    # Must have shown an alert popup.
    assert upd.callback_query.answers[-1][1] is True  # show_alert=True
    # onboarding_state unchanged.
    assert fdb.users[INVITED_USER]["onboarding_state"] == onb.ST_CHANNELS
    # deliver_digest not called.
    deliver_mock.assert_not_awaited()


# ── acceptance #5: expired trial → paywall shown, digest pipeline not invoked ──

@pytest.mark.asyncio
async def test_expired_trial_shows_paywall_no_pipeline(fdb, monkeypatch):
    """A user whose trial_ends_at is in the past gets the paywall message.
    The real digest pipeline (run_digest_pipeline) must never be invoked."""
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _seed_invited(fdb, INVITED_USER, trial_used=True, trial_ends_at=past,
                  pro_until=None, onboarding_state=onb.ST_DONE)

    pipeline_mock = AsyncMock()
    gate_mock = AsyncMock()

    # Patch the paywall show_gate so we can assert it fires.
    import handlers.subscription as sub_surface
    monkeypatch.setattr(sub_surface, "show_gate", gate_mock)
    # Also patch run_digest_pipeline in case the gate ever failed to block.
    with patch("agent.run_digest_pipeline", pipeline_mock):
        ctx = FakeContext()
        ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
        upd = _make_update(text="/start")

        from handlers.digest import send_digest
        await send_digest(upd, ctx)

    # The paywall show_gate must have fired.
    gate_mock.assert_awaited_once()
    # The real pipeline must never have been invoked.
    pipeline_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_trial_does_not_hit_paywall(fdb, monkeypatch):
    """A user with an active trial can request a digest without hitting the paywall.
    The pipeline is mocked so no LLM call is made."""
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _seed_invited(fdb, INVITED_USER, trial_used=True, trial_ends_at=future,
                  pro_until=None, onboarding_state=onb.ST_DONE)
    fdb.user_settings[str(INVITED_USER)]["channels"] = ["test_chan"]

    gate_mock = AsyncMock()
    import handlers.subscription as sub_surface
    monkeypatch.setattr(sub_surface, "show_gate", gate_mock)

    deliver_mock = AsyncMock(return_value=3)
    with patch("handlers.digest.deliver_digest", deliver_mock):
        ctx = FakeContext()
        ctx.user_data["user"] = copy.deepcopy(fdb.users[INVITED_USER])
        upd = _make_update(text="📰")

        from handlers.digest import send_digest
        await send_digest(upd, ctx)

    # Paywall must NOT fire.
    gate_mock.assert_not_awaited()
    # deliver_digest must have been called (subscription active).
    deliver_mock.assert_awaited_once()
