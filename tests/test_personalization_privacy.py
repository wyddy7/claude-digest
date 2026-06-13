"""Regression guard for the per-user personalization privacy leak.

Before the fix, ai.build_system_prompt always read config/personalization.yaml
(the OWNER's private profile: bio, style rules, stop-words) — so EVERY tenant's
digest and chat ran with the owner's personal context baked into the system
prompt. The handler-level fallback `load_personalization_db(...) or
load_personalization()` had the same hole.

These tests pin the new contract (personalization.resolve_personalization):
  - a tenant with empty/usage-only DB personalization gets the NEUTRAL
    committed default (config/personalization.default.yaml), never the owner's
    yaml;
  - the owner (env CHAT_ID) keeps the rich private yaml;
  - per-user DB overrides merge OVER the neutral base and the reserved
    "_usage" counter namespace is ignored, not treated as a profile;
  - the digest pipeline and the chat agent both receive the resolved per-user
    personalization (fail-closed to neutral), end to end.

Synthetic tg ids only (111111111-style) — never real user data.
"""
import asyncio

import pytest

import digest_bot.agent as agent
import digest_bot.db as db_module
import digest_bot.personalization as pers
from digest_bot.ai import build_system_prompt
from digest_bot.pipeline_config import build_pipeline_config

OWNER_TG_ID = 111111111
TENANT_TG_ID = 222222222

# Sentinels standing in for the owner's private yaml content. If any of these
# ever shows up in a tenant-facing prompt/config, the leak is back.
OWNER_BIO = "OWNER-PRIVATE-BIO-SENTINEL"
OWNER_STOP_WORD = "owner-private-stop-word"


def _owner_yaml_cfg() -> dict:
    """A stand-in for the owner's gitignored personalization.yaml."""
    cfg = pers.load_default_personalization()
    cfg["profile"]["description"] = OWNER_BIO
    cfg["prompt"]["stop_words"] = [OWNER_STOP_WORD]
    return cfg


@pytest.fixture(autouse=True)
def owner_env(monkeypatch):
    """Pin the owner identity and make the 'owner yaml' carry sentinels, so any
    leak into a tenant path is detectable regardless of the machine's local
    config/personalization.yaml."""
    monkeypatch.setenv("CHAT_ID", str(OWNER_TG_ID))
    monkeypatch.setattr(pers, "load_personalization", _owner_yaml_cfg)


# ── resolver: the privacy boundary itself ─────────────────────────────────────

def test_tenant_with_empty_db_personalization_gets_neutral_default():
    resolved = pers.resolve_personalization({}, TENANT_TG_ID)
    assert OWNER_BIO not in str(resolved)
    assert OWNER_STOP_WORD not in str(resolved)
    # Coherent prompt config, not a broken stub.
    assert resolved["prompt"]["system_template"].strip()
    assert resolved["profile"]["description"].strip()
    default = pers.load_default_personalization()
    assert resolved["profile"]["description"] == default["profile"]["description"]


def test_usage_counter_blob_is_not_a_profile():
    """db.record_chat_turn stores {'_usage': ...} in the same JSONB. A tenant
    whose ONLY personalization content is the usage counter must still resolve
    to the neutral default (the old `or`-fallback treated any truthy blob as a
    full profile)."""
    blob = {"_usage": {"chat_turns": {"2026-06": 7}}}
    resolved = pers.resolve_personalization(blob, TENANT_TG_ID)
    assert OWNER_BIO not in str(resolved)
    assert "_usage" not in resolved
    assert resolved["prompt"]["system_template"].strip()


def test_owner_keeps_rich_yaml_profile():
    resolved = pers.resolve_personalization(
        {"_usage": {"chat_turns": {"2026-06": 3}}}, OWNER_TG_ID
    )
    assert resolved["profile"]["description"] == OWNER_BIO
    assert resolved["prompt"]["stop_words"] == [OWNER_STOP_WORD]


def test_tenant_db_overrides_merge_over_neutral_base():
    """A future per-user profile (e.g. seeded by an optional onboarding step)
    overlays the neutral base without clobbering the rest of the prompt config
    and without ever pulling in the owner's yaml."""
    blob = {
        "profile": {"description": "tenant own bio"},
        "_usage": {"chat_turns": {"2026-06": 1}},
    }
    resolved = pers.resolve_personalization(blob, TENANT_TG_ID)
    assert resolved["profile"]["description"] == "tenant own bio"
    assert OWNER_BIO not in str(resolved)
    default = pers.load_default_personalization()
    assert resolved["prompt"]["system_template"] == default["prompt"]["system_template"]
    assert resolved["prompt"]["style_rules"] == default["prompt"]["style_rules"]


def test_unset_chat_id_means_nobody_is_owner(monkeypatch):
    monkeypatch.delenv("CHAT_ID", raising=False)
    resolved = pers.resolve_personalization({}, OWNER_TG_ID)
    assert OWNER_BIO not in str(resolved)  # fail-closed


# ── build_system_prompt: fail-closed default ──────────────────────────────────

def test_build_system_prompt_without_personalization_is_neutral():
    """A call site that forgets to resolve personalization must degrade to the
    generic template — never to the owner's yaml (the pre-fix behavior)."""
    prompt = build_system_prompt({"current_focus": "agents"})
    assert OWNER_BIO not in prompt
    assert OWNER_STOP_WORD not in prompt
    default_desc_first_line = (
        pers.load_default_personalization()["profile"]["description"].splitlines()[0].strip()
    )
    assert default_desc_first_line in prompt


def test_build_system_prompt_uses_explicit_resolved_config():
    tenant_cfg = pers.resolve_personalization(
        {"profile": {"description": "tenant own bio"}}, TENANT_TG_ID
    )
    prompt = build_system_prompt({"current_focus": ""}, personalization=tenant_cfg)
    assert "tenant own bio" in prompt
    assert OWNER_BIO not in prompt


# ── digest pipeline: resolved personalization reaches generation ──────────────

def test_pipeline_passes_tenant_personalization_to_generation(monkeypatch):
    captured: dict = {}

    async def fake_scrape(ch):
        return [{"channel": ch, "text": "post", "links": []}]

    async def fake_filter(posts, *, client, model, usage_log):
        return posts

    async def fake_generate(filtered, user_data, *, client, model,
                            recent_digests=None, usage_log=None, personalization=None):
        captured["personalization"] = personalization
        return ("<b>digest</b>", "", "")

    monkeypatch.setattr(agent, "scrape_channel", fake_scrape)
    monkeypatch.setattr(agent, "filter_ads", fake_filter)
    monkeypatch.setattr(agent, "generate_digest", fake_generate)

    tenant_cfg = pers.resolve_personalization({}, TENANT_TG_ID)
    cfg = build_pipeline_config(
        {"channels": ["userchan_a"], "current_focus": "ml",
         "model": "anthropic/claude-3.5-haiku", "interaction_history": []},
        tenant_cfg,
        recent_digests=[],
    )
    assert OWNER_BIO not in str(cfg.personalization)

    result = asyncio.run(agent.run_digest_pipeline(cfg, llm_client=object()))
    assert result["digest_html"] == "<b>digest</b>"
    sent = captured["personalization"]
    assert sent is not None, "pipeline must forward the resolved personalization"
    assert OWNER_BIO not in str(sent)
    assert sent["prompt"]["system_template"].strip()


# ── chat agent: tenant system prompt is owner-free ─────────────────────────────

class _FakeChatAgent:
    async def aget_state(self, config):  # compaction probe → skip
        return None

    def astream_events(self, *args, **kwargs):
        async def _gen():
            if False:  # pragma: no cover - empty async generator
                yield {}
        return _gen()


def _patch_chat_db(monkeypatch, personalization_blob):
    async def fake_load_settings(user_id):
        return {
            "user_id": user_id,
            "current_focus": "",
            "interaction_history": [],
            "model": "test/per-user-model",
            "personalization": personalization_blob,
        }

    async def fake_load_user_history(user_id, limit=0):
        return []

    monkeypatch.setattr(db_module, "load_settings", fake_load_settings)
    monkeypatch.setattr(db_module, "load_user_history", fake_load_user_history)


def test_chat_turn_tenant_system_prompt_is_owner_free(monkeypatch):
    _patch_chat_db(monkeypatch, personalization_blob={"_usage": {"chat_turns": {}}})
    captured: dict = {}

    def fake_create_chat_agent(system_prompt, checkpointer, user_id, model_id=None):
        captured["system_prompt"] = system_prompt
        captured["model_id"] = model_id
        return _FakeChatAgent()

    monkeypatch.setattr(agent, "create_chat_agent", fake_create_chat_agent)

    asyncio.run(agent.run_chat_turn(
        TENANT_TG_ID, "привет", object(), scope_user_id="tenant-uuid"
    ))
    sysp = captured["system_prompt"]
    assert OWNER_BIO not in sysp
    assert OWNER_STOP_WORD not in sysp
    # Fix A: the tenant's chat runs on THEIR selected model, not the owner yaml's.
    assert captured["model_id"] == "test/per-user-model"


def test_chat_turn_owner_keeps_yaml_profile(monkeypatch):
    _patch_chat_db(monkeypatch, personalization_blob={})
    captured: dict = {}

    def fake_create_chat_agent(system_prompt, checkpointer, user_id, model_id=None):
        captured["system_prompt"] = system_prompt
        captured["model_id"] = model_id
        return _FakeChatAgent()

    monkeypatch.setattr(agent, "create_chat_agent", fake_create_chat_agent)

    asyncio.run(agent.run_chat_turn(
        OWNER_TG_ID, "привет", object(), scope_user_id="owner-uuid"
    ))
    assert OWNER_BIO in captured["system_prompt"]
