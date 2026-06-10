"""
Subscription / trial / expiry logic (no PTB imports).

Pure logic + supabase-py I/O via the `db` module. All datetimes are
timezone-aware UTC, stored as ISO-8601 strings. Subscription activeness is
ALWAYS computed from pro_until/trial_ends_at at runtime — never stored as a
denormalized boolean (avoids the is_premium drift problem).

The DB accessor is the module-level `db` import; offline tests monkeypatch
`db.*` functions, so this module stays trivially testable without a real DB or
PTB. Every limit/date/price is read from DB tier defaults — no Python constants
for quotas, days, or prices (only payload string identifiers + currency live as
literals here).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import db

logger = logging.getLogger(__name__)

# Product key → meaning. A code fact (string→tier/period), not a tunable quota,
# so a literal map is allowed. Days and star prices are looked up in the DB
# (tier_defaults) at invoice/grant time, keyed by (tier, period). Never inline.
PRODUCTS = {
    "digest_pro_month": {"tier": "pro", "period": "month"},
    "digest_pro_quarter": {"tier": "pro", "period": "quarter"},
}

# Per-period DB keys for (days, price) inside the tier limits blob.
_PERIOD_KEYS = {
    "month": {"days": "days_month", "price": "price_month_stars"},
    "quarter": {"days": "days_quarter", "price": "price_quarter_stars"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (or passthrough a datetime) to an aware UTC
    datetime. None/empty → None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value)
        # PostgREST returns '...+00:00' or '...Z'; normalise the Z form.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt(dt: Optional[datetime]) -> str:
    """Human-readable subscription end for ACK messages."""
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d")


@dataclass
class PaymentResult:
    granted: bool
    active_until_human: str


# ─── subscription state ───────────────────────────────────────────────────────

async def is_subscription_active(tg_user_id: int) -> bool:
    """True iff (pro_until > now) OR (trial_ends_at > now). Computed, not stored."""
    row = await db.get_user_by_tg_id(tg_user_id)
    if not row:
        return False
    now = _now()
    pro_until = _parse_ts(row.get("pro_until"))
    trial_ends = _parse_ts(row.get("trial_ends_at"))
    if pro_until and pro_until > now:
        return True
    if trial_ends and trial_ends > now:
        return True
    return False


async def active_until(tg_user_id: int) -> Optional[datetime]:
    """Latest of (pro_until, trial_ends_at) that is in the future, for display."""
    row = await db.get_user_by_tg_id(tg_user_id)
    if not row:
        return None
    now = _now()
    candidates = [
        ts for ts in (_parse_ts(row.get("pro_until")), _parse_ts(row.get("trial_ends_at")))
        if ts and ts > now
    ]
    return max(candidates) if candidates else None


# ─── mutators ─────────────────────────────────────────────────────────────────

async def update_subscription(tg_user_id: int, days: int, tier: str = "pro") -> datetime:
    """Extend or set the PAID subscription. STACKS on the active remainder: if
    pro_until is still in the future, add `days` to it; else start from now.
    `days` is supplied by the caller from DB tier defaults — never a constant.
    Also flips users.tier to the paid bundle name so get_effective_limit
    resolves the paid tier's defaults (activeness itself stays computed from
    timestamps, never from tier). Returns the new pro_until."""
    row = await db.get_user_by_tg_id(tg_user_id)
    now = _now()
    pro_until = _parse_ts(row.get("pro_until")) if row else None
    base = pro_until if (pro_until and pro_until > now) else now
    new_until = base + timedelta(days=days)
    await db.update_subscription_row(tg_user_id, new_until.isoformat(), tier=tier)
    return new_until


async def grant_trial(tg_user_id: int) -> bool:
    """Grant the trial ONCE per user. Reads the trial length from the DB tier
    defaults (tier_defaults['trial']['days']). No-op (returns False) if
    trial_used is already True. Sets trial_ends_at + trial_used in one update."""
    row = await db.get_user_by_tg_id(tg_user_id)
    if not row or row.get("trial_used"):
        return False
    trial_days = await db.get_tier_default("trial", "days")
    trial_end = _now() + timedelta(days=int(trial_days))
    await db.grant_trial_row(tg_user_id, trial_end.isoformat())
    return True


async def revoke_subscription(tg_user_id: int) -> bool:
    """Clear BOTH pro_until and trial_ends_at. Leaves trial_used = True so
    revoking does not re-arm a fresh trial."""
    return await db.update_user_fields(tg_user_id, {
        "pro_until": None,
        "trial_ends_at": None,
    })


# ─── invoice fields ───────────────────────────────────────────────────────────

async def product_invoice_fields(tg_user_id: int, tier: str, period: str) -> tuple[int, int, str, str]:
    """Return (price_stars, days, title, description) for an invoice — all read
    from the tier_defaults DB row for (tier, period). No constants."""
    keys = _PERIOD_KEYS[period]
    price_stars = int(await db.get_tier_default(tier, keys["price"]))
    days = int(await db.get_tier_default(tier, keys["days"]))
    period_label = "1 month" if period == "month" else "3 months"
    title = f"Digest Pro — {period_label}"
    description = f"Digest Pro подписка на {period_label}."
    return price_stars, days, title, description


# ─── idempotent successful-payment application ────────────────────────────────

async def apply_successful_payment(
    tg_user_id: int,
    payload: str,
    telegram_payment_charge_id: str,
    total_amount: int,
) -> PaymentResult:
    """Idempotent grant keyed on telegram_payment_charge_id.

    Insert the ledger row FIRST (the unique-charge gate), THEN grant days. A
    duplicate Telegram delivery hits the UNIQUE violation and re-acks without a
    second grant. If the grant raises after a fresh insert, roll the ledger row
    back so the charge is retryable on the next delivery.
    """
    spec = PRODUCTS.get(payload)
    if spec is None:
        # Unknown payload — do not grant; re-ack current state so the user is
        # never left hanging on a charge we can't map.
        until = await active_until(tg_user_id)
        logger.warning("apply_successful_payment: unknown payload %r", payload)
        return PaymentResult(granted=False, active_until_human=_fmt(until or _now()))

    # Resolve the user UUID (defensive: a payer without a row is an upstream bug;
    # never drop their money silently — provision the row first).
    user = await db.get_or_create_user(tg_user_id)
    user_id = user["id"]

    # 1. Gate insert (idempotency anchor) — DuplicateCharge => already processed.
    try:
        await db.insert_subscription_event(
            user_id=user_id,
            event_type="payment_succeeded",
            payload={"payload": payload, "tg_user_id": tg_user_id},
            stars_amount=total_amount,
            telegram_payment_charge_id=telegram_payment_charge_id,
        )
    except db.DuplicateCharge:
        until = await active_until(tg_user_id)
        return PaymentResult(granted=False, active_until_human=_fmt(until or _now()))

    # 2. Fresh charge: grant days. On failure, roll back the gate and re-raise.
    keys = _PERIOD_KEYS[spec["period"]]
    days = int(await db.get_tier_default(spec["tier"], keys["days"]))
    try:
        new_until = await update_subscription(tg_user_id, days, tier=spec["tier"])
    except Exception:
        await db.delete_subscription_event(telegram_payment_charge_id)
        raise
    return PaymentResult(granted=True, active_until_human=_fmt(new_until))


# ─── expiry warning (debounced, T-24h) ────────────────────────────────────────

async def maybe_warn_expiry(tg_user_id: int, bot) -> None:
    """Fire a one-time T-24h expiry warning per subscription period. Debounced via
    users.trial_warn_sent JSONB (state, not a limit): we store the `end` we warned
    for, so pushing `end` further out (update_subscription/grant_trial) re-arms.
    Expired users get the paywall on next interaction, not spam here."""
    row = await db.get_user_by_tg_id(tg_user_id)
    if not row:
        return
    end = await active_until(tg_user_id)
    if not end:
        return  # None or already expired → no warn here
    now = _now()
    delta = end - now
    if not (timedelta(0) < delta <= timedelta(hours=24)):
        return

    warned_state = row.get("trial_warn_sent") or {}
    warned_for = _parse_ts(warned_state.get("end")) if isinstance(warned_state, dict) else None
    # Already warned for this (or a later) end → debounce.
    if warned_for and warned_for >= end:
        return

    try:
        await bot.send_message(
            tg_user_id,
            "⏳ Подписка Digest Pro заканчивается завтра.\n"
            "Продли, чтобы дайджест продолжал приходить: /buy",
        )
    except Exception as e:
        logger.debug("maybe_warn_expiry: send failed (non-fatal): %s", e)
        return
    await db.update_user_fields(tg_user_id, {"trial_warn_sent": {"end": end.isoformat()}})
