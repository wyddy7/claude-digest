"""Offline tests for handlers/strings.py.

Checks:
- All expected names are importable.
- BTN_* re-exports from handlers.menu are identical (not copied/diverged).
- Template strings contain their format keys so .format() works without KeyError.
- No string is empty (all names exported by the module have a non-empty value).
"""

from __future__ import annotations

import pytest

import digest_bot.handlers.strings as S
import digest_bot.handlers.menu as menu


# ── re-export consistency ─────────────────────────────────────────────────────

def test_btn_reexports_match_menu():
    """BTN_* in strings.py must be the identical objects from menu.py, not copies."""
    assert S.BTN_DIGEST is menu.BTN_DIGEST
    assert S.BTN_HISTORY is menu.BTN_HISTORY
    assert S.BTN_PROFILE is menu.BTN_PROFILE
    assert S.BTN_SETTINGS is menu.BTN_SETTINGS
    assert S.BTN_SUBSCRIPTION is menu.BTN_SUBSCRIPTION
    assert S.MENU_BUTTONS is menu.MENU_BUTTONS


# ── required names exist and are non-empty ────────────────────────────────────

REQUIRED_NAMES = [
    "INVITE_ONLY",
    "SOON",
    "FALLBACK",
    "ONBOARDING_WELCOME",
    "ONBOARDING_FOCUS",
    "ONBOARDING_MENU_READY",
    "ONBOARDING_OWN_CHANNELS_PROMPT",
    "ONBOARDING_FOCUS_OWN_PROMPT",
    "ONBOARDING_CHANNELS_MIN_ERROR",
    "ONBOARDING_PREVIEW_PRE",
    "ONBOARDING_PREVIEW_CLOSE",
    "ONBOARDING_PREVIEW_FAIL",
    "DIGEST_COLLECTING",
    "DIGEST_ERROR",
    "SUB_GATE_EXPIRED",
    "SUB_TRIAL_HEADER_TEMPLATE",
    "SUB_BUY_BODY_HEADER",
    "SUB_BUY_WALLET_TIP",
    "SUB_PAYMENT_GRANTED",
    "SUB_PAYMENT_DUPLICATE",
]


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_string_exists_and_nonempty(name):
    value = getattr(S, name, None)
    assert value is not None, f"handlers.strings.{name} is missing"
    assert isinstance(value, str), f"handlers.strings.{name} is not a str"
    assert value.strip(), f"handlers.strings.{name} is empty"


# ── template keys are present ─────────────────────────────────────────────────

def test_sub_trial_header_template_keys():
    result = S.SUB_TRIAL_HEADER_TEMPLATE.format(days_left=3, until_date="2026-07-01")
    assert "3" in result
    assert "2026-07-01" in result


def test_sub_payment_granted_key():
    result = S.SUB_PAYMENT_GRANTED.format(active_until="2026-07-01")
    assert "2026-07-01" in result


def test_sub_payment_duplicate_key():
    result = S.SUB_PAYMENT_DUPLICATE.format(active_until="2026-07-01")
    assert "2026-07-01" in result


# ── onboarding aliases in onboarding.py still match ──────────────────────────

def test_onboarding_module_aliases():
    """WELCOME_TEXT / FOCUS_TEXT / PREVIEW_* in onboarding.py must resolve to
    the same strings.py values (they are aliases, not independent copies)."""
    from digest_bot.handlers import onboarding as onb

    assert onb.WELCOME_TEXT == S.ONBOARDING_WELCOME
    assert onb.FOCUS_TEXT == S.ONBOARDING_FOCUS
    assert onb.PREVIEW_PRE == S.ONBOARDING_PREVIEW_PRE
    assert onb.PREVIEW_CLOSE == S.ONBOARDING_PREVIEW_CLOSE
    assert onb.PREVIEW_FAIL == S.ONBOARDING_PREVIEW_FAIL


def test_middleware_invite_only_alias():
    """INVITE_ONLY_TEXT in middleware.py must resolve to strings.INVITE_ONLY."""
    from digest_bot.handlers import middleware as mw

    assert mw.INVITE_ONLY_TEXT == S.INVITE_ONLY
