"""
Pipeline configuration + per-stage model registry for the digest pipeline.

The bot UI and the scheduler are just two builders of a PipelineConfig; the
pipeline itself is driven by an explicit config + injected collaborators (LLM
client, db module, fetcher), so it stays testable offline without the Telegram
layer. See docs/digest-bot-reader-plan.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# read_mode states for the reader layer (Grade A).
# "agentic" is a reserved stub raised as NotImplementedError (Grade B, P7).
READ_MODE_OFF = "off"
READ_MODE_EXTRACT = "extract"
READ_MODE_AGENTIC = "agentic"

# Fallback models. Keep in sync with db.DEFAULT_MODEL — both must name a model
# OpenRouter currently serves, or an unset user_state.model 404s the pipeline.
DEFAULT_DIGEST_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_CHEAP_MODEL = "deepseek/deepseek-chat"


@dataclass
class StageModel:
    """One pipeline stage's model + human-readable metadata.

    tier/rationale/task feed both the bot settings UI and the docs, so the
    registry is the single source of truth (no drift). P2 may leave the
    metadata blank; P3 populates it from the yaml mapping form.
    """

    model_id: str
    tier: str = ""
    rationale: str = ""
    task: str = ""


ModelRegistry = dict[str, StageModel]


@dataclass
class PipelineConfig:
    read_mode: str = READ_MODE_OFF
    models: ModelRegistry = field(default_factory=dict)
    per_channel_link_cap: int = 20
    dedup_enabled: bool = True
    dedup_window_days: int = 7
    tenant_id: Optional[str] = None  # reserved — SaaS seam, unused
    # Per-user pipeline inputs. When `user_data` is set the pipeline runs in
    # multi-tenant mode: it sources channels + profile + recent digests from
    # THIS config (the caller's user), never the legacy global db.load() row.
    # Left empty by legacy/test callers, which keep the db_module.load() path.
    channels: list = field(default_factory=list)
    user_data: dict = field(default_factory=dict)
    recent_digests: list = field(default_factory=list)
    # RESOLVED per-user personalization (profile + prompt rules) — set by
    # build_pipeline_config from the cfg_yaml the caller resolved via
    # personalization.resolve_personalization. The pipeline passes it to
    # ai.generate_digest → build_system_prompt. Empty dict (legacy/test
    # callers) → build_system_prompt falls back to the NEUTRAL default
    # template, never the owner's yaml.
    personalization: dict = field(default_factory=dict)


def stage_from_yaml(
    entry,
    *,
    default_model: str = "",
    default_tier: str = "",
    default_rationale: str = "",
    default_task: str = "",
) -> StageModel:
    """Normalize a yaml `models:` entry (bare string OR mapping) to a StageModel.

    Bare string -> model_id only (back-compat). Mapping -> full metadata.
    """
    if isinstance(entry, dict):
        return StageModel(
            model_id=str(entry.get("model_id", default_model)),
            tier=str(entry.get("tier", default_tier)),
            rationale=str(entry.get("rationale", default_rationale)),
            task=str(entry.get("task", default_task)),
        )
    if entry is None:
        return StageModel(model_id=default_model, tier=default_tier,
                          rationale=default_rationale, task=default_task)
    return StageModel(model_id=str(entry), tier=default_tier,
                      rationale=default_rationale, task=default_task)


def build_registry_from_state(user_data: dict, cfg_yaml: dict) -> ModelRegistry:
    """Build the per-stage model registry from runtime state + yaml.

    digest -> user_state.model (runtime-selectable) takes precedence.
    cheap stages -> yaml `models:`, falling back to deepseek/deepseek-chat.
    """
    models_cfg = (cfg_yaml or {}).get("models", {}) or {}

    digest_model = user_data.get("model") or stage_from_yaml(
        models_cfg.get("digest"), default_model=DEFAULT_DIGEST_MODEL
    ).model_id or DEFAULT_DIGEST_MODEL

    registry: ModelRegistry = {
        "digest": StageModel(
            model_id=digest_model,
            tier="premium",
            rationale="Main synthesis — quality of the final digest depends on it.",
            task="Group filtered posts into the structured digest.",
        ),
        "ad_filter": stage_from_yaml(
            models_cfg.get("ad_filter"),
            default_model=DEFAULT_CHEAP_MODEL,
            default_tier="cheap",
            default_rationale="High-volume binary classification — cost-sensitive.",
            default_task="Drop pure-ad posts before synthesis.",
        ),
        "triage": stage_from_yaml(
            models_cfg.get("triage"),
            default_model=DEFAULT_CHEAP_MODEL,
            default_tier="cheap",
            default_rationale="One bounded decision over a link list — a cheap model is enough.",
            default_task="Pick which external links in a post are worth opening (reader layer).",
        ),
        "summarize_link": stage_from_yaml(
            models_cfg.get("summarize_link"),
            default_model=DEFAULT_CHEAP_MODEL,
            default_tier="cheap",
            default_rationale="Reserved for Grade-B per-link summarization; cheap by default.",
            default_task="Summarize fetched article content (not used in the Grade-A MVP).",
        ),
    }
    return registry


def describe_registry(registry: ModelRegistry) -> str:
    """Render the per-stage registry as human-readable text (feeds bot UI + docs)."""
    lines = []
    for stage, sm in registry.items():
        tier = f" [{sm.tier}]" if sm.tier else ""
        lines.append(f"• {stage}{tier}: {sm.model_id}")
        if sm.task:
            lines.append(f"    {sm.task}")
        if sm.rationale:
            lines.append(f"    ↳ {sm.rationale}")
    return "\n".join(lines)


def build_pipeline_config(
    user_data: dict, cfg_yaml: dict, read_mode: str | None = None,
    recent_digests: list | None = None,
) -> PipelineConfig:
    """Build the pipeline config. read_mode resolution order:
    explicit arg > `read_mode:` in personalization.yaml > off (default).
    This is the runtime switch for the reader layer — flip it in the yaml to
    turn on extract-mode without a code change or redeploy.

    `user_data` carries the caller's channels + profile + focus + interaction
    history; they are stored on the config so the pipeline reads THIS user's
    inputs (multi-tenant) instead of the legacy global db.load() row. Pass
    `recent_digests` (the user's last few digest rows) for de-duplication
    context — scope it per user (db.load_user_history)."""
    if read_mode is None:
        read_mode = (cfg_yaml or {}).get("read_mode", READ_MODE_OFF)
    user_data = user_data or {}
    return PipelineConfig(
        read_mode=read_mode,
        models=build_registry_from_state(user_data, cfg_yaml),
        channels=list(user_data.get("channels") or []),
        user_data=dict(user_data),
        recent_digests=list(recent_digests or []),
        personalization=dict(cfg_yaml or {}),
    )


def make_openrouter_client(api_key: str) -> AsyncOpenAI:
    """Single place callsites build the OpenRouter-compatible LLM client."""
    if not api_key:
        raise RuntimeError("OPENROUTER_KEY env var is not set")
    return AsyncOpenAI(api_key=api_key, base_url=OPENROUTER_BASE)
