"""Regression guard for the multi-tenant channel-sourcing bug.

Before the fix, run_digest_pipeline always read channels + profile from the
legacy global db.load() (user_state id=1), so EVERY tenant's digest was built
from the owner's channels instead of their own. These tests pin that the
pipeline now sources per-user inputs from the PipelineConfig and never touches
the global db row in multi-tenant mode.
"""
import asyncio

import pytest

import agent
from pipeline_config import build_pipeline_config


def test_build_pipeline_config_carries_per_user_inputs():
    user_data = {"channels": ["userchan_a", "userchan_b"], "current_focus": "ml",
                 "model": "anthropic/claude-3.5-haiku", "interaction_history": []}
    cfg = build_pipeline_config(user_data, {}, recent_digests=[{"digest_html": "prev"}])
    assert cfg.channels == ["userchan_a", "userchan_b"]
    assert cfg.user_data["current_focus"] == "ml"
    assert cfg.recent_digests == [{"digest_html": "prev"}]


class _ExplodingDB:
    """Any access to the legacy global row is a bug in multi-tenant mode."""
    DEFAULT_MODEL = "anthropic/claude-3.5-haiku"

    async def load(self):
        raise AssertionError("db.load() (global row) must NOT be called for a tenant")

    async def load_history(self, limit=0):
        raise AssertionError("db.load_history() (global) must NOT be called for a tenant")

    async def append_to_history(self, digest_html, posts_count):
        raise AssertionError("db.append_to_history() (global) must NOT be called for a tenant")


def test_pipeline_uses_config_channels_not_global(monkeypatch):
    scraped: list[str] = []

    async def fake_scrape(ch):
        scraped.append(ch)
        return [{"channel": ch, "text": "post", "links": []}]

    async def fake_filter(posts, *, client, model, usage_log):
        return posts

    async def fake_generate(filtered, user_data, *, client, model, recent_digests=None,
                            usage_log=None, personalization=None):
        # The per-user profile must reach generation, not the owner's.
        assert user_data.get("current_focus") == "ml"
        return ("<b>digest</b>", "", "")

    monkeypatch.setattr(agent, "scrape_channel", fake_scrape)
    monkeypatch.setattr(agent, "filter_ads", fake_filter)
    monkeypatch.setattr(agent, "generate_digest", fake_generate)

    cfg = build_pipeline_config(
        {"channels": ["userchan_a"], "current_focus": "ml", "model": _ExplodingDB.DEFAULT_MODEL,
         "interaction_history": []},
        {},
        recent_digests=[],
    )

    result = asyncio.run(agent.run_digest_pipeline(
        cfg, db_module=_ExplodingDB(), llm_client=object(),
    ))

    assert scraped == ["userchan_a"]          # the tenant's channel, not the owner's
    assert result["digest_html"] == "<b>digest</b>"
