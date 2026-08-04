"""Guards against the 2026-08-04 dead-model incident.

Retired model ids (claude-3.5-haiku, claude-3.7-sonnet) sat in DEFAULT_MODEL and
in the /settings menu for months, 404-ing the first digest of every new user.
These are offline structural checks — they cannot know what OpenRouter serves
today, so they pin the invariants that made the bug survivable:
every offered model is priced, the default is offered, and known-retired ids
never come back.
"""

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
