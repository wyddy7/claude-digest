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

# Fallback models. The digest default matches the pre-refactor generate_digest
# fallback so off-mode behavior is identical when user_state.model is unset.
DEFAULT_DIGEST_MODEL = "anthropic/claude-3.5-haiku"
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
    }
    return registry


def build_pipeline_config(
    user_data: dict, cfg_yaml: dict, read_mode: str = READ_MODE_OFF
) -> PipelineConfig:
    return PipelineConfig(
        read_mode=read_mode,
        models=build_registry_from_state(user_data, cfg_yaml),
    )


def make_openrouter_client(api_key: str) -> AsyncOpenAI:
    """Single place callsites build the OpenRouter-compatible LLM client."""
    if not api_key:
        raise RuntimeError("OPENROUTER_KEY env var is not set")
    return AsyncOpenAI(api_key=api_key, base_url=OPENROUTER_BASE)
