"""Guards against the 2026-08-04 dead-model incident.

Retired model ids (claude-3.5-haiku, claude-3.7-sonnet) sat in DEFAULT_MODEL and
in the /settings menu for months, 404-ing the first digest of every new user.
These are offline structural checks — they cannot know what OpenRouter serves
today, so they pin the invariants that made the bug survivable:
every offered model is priced, the default is offered, and known-retired ids
never come back.
"""

import digest_bot.ai as ai
import digest_bot.db as db_module
import digest_bot.pipeline_config as pipeline_config
import digest_bot.pricing as pricing
from digest_bot.handlers.settings import AVAILABLE_MODELS

# Confirmed absent from OpenRouter's catalog on 2026-08-04. Extend, never shrink.
RETIRED_MODEL_IDS = {
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-3.7-sonnet",
}


def test_default_model_is_not_retired():
    assert db_module.DEFAULT_MODEL not in RETIRED_MODEL_IDS
    assert pipeline_config.DEFAULT_DIGEST_MODEL not in RETIRED_MODEL_IDS


def test_settings_menu_offers_no_retired_models():
    offered = set(AVAILABLE_MODELS.values())
    assert offered.isdisjoint(RETIRED_MODEL_IDS), offered & RETIRED_MODEL_IDS


def test_default_model_is_selectable_in_the_menu():
    """A default the user cannot re-pick after switching away is a trap."""
    assert db_module.DEFAULT_MODEL in set(AVAILABLE_MODELS.values())


def test_every_offered_model_has_a_real_price():
    """Unpriced models fall back to a made-up rate, so /stats margin lies."""
    missing = [m for m in AVAILABLE_MODELS.values() if m not in pricing.PRICE_PER_1M]
    assert not missing, f"add to pricing.PRICE_PER_1M: {missing}"


def test_default_and_digest_fallback_agree():
    assert db_module.DEFAULT_MODEL == pipeline_config.DEFAULT_DIGEST_MODEL


# ── Guards added 2026-08-04 after the first Sonnet 5 prod digest FAILED ──────
# Being live in the catalog is not the same as being usable: the digest call
# must get non-empty content back within DIGEST_MAX_TOKENS.

# Rejects reasoning.enabled=false with HTTP 400 ("Reasoning is mandatory for this
# endpoint") AND returns content="" with reasoning on. Unusable either way.
REASONING_MANDATORY_MODEL_IDS = {"qwen/qwen3.8-max"}

# Highest single-call digest completion ever observed in prod usage_events
# (Sonnet 4.6, 46 successful runs). The cap must clear it with real headroom —
# 3500 gave only 14% and Sonnet 5 truncated through it twice on 2026-08-04.
OBSERVED_PEAK_DIGEST_COMPLETION_TOKENS = 3077


def test_settings_menu_offers_no_reasoning_mandatory_models():
    offered = set(AVAILABLE_MODELS.values())
    clash = offered & REASONING_MANDATORY_MODEL_IDS
    assert not clash, f"these return empty content in the digest call: {clash}"


def test_digest_max_tokens_clears_the_observed_peak():
    assert ai.DIGEST_MAX_TOKENS >= 2 * OBSERVED_PEAK_DIGEST_COMPLETION_TOKENS


def test_digest_call_disables_reasoning():
    """Reasoning tokens are billed as output and eat max_tokens before any
    content is emitted — leaving this on silently empties the digest."""
    assert ai.DIGEST_REASONING == {"enabled": False}
