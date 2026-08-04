"""
LLM cost → USD. Source of truth is OpenRouter, NOT this file.

We call OpenRouter exclusively, and OpenRouter returns the authoritative per-call
charge inline on every response as `usage.cost` (no request flag needed). ai.record_usage
captures it; agent._build_cost_summary sums it per stage as `api_cost_usd`; and
price_cost_summary below PREFERS that real cost. So in normal operation the dollars
come straight from the provider and never touch the table.

The token-rate table here is a LAST-RESORT FALLBACK only — used solely when a
response omitted usage.cost (a fake in tests, or a provider that didn't report it),
so we record an approximate cost instead of a silent $0 that would flatter the
margin. Rates are USD per 1,000,000 tokens (prompt, completion); they drift and are
not kept authoritative — fix the API capture, not this table, if costs look wrong.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# FALLBACK ONLY (see module docstring). USD per 1M tokens: id -> (prompt, completion).
# Just the models this bot actually defaults to; everything else hits _FALLBACK_RATE.
PRICE_PER_1M: dict[str, tuple[float, float]] = {
    # Offered in /settings (see handlers/settings.AVAILABLE_MODELS).
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "qwen/qwen3.8-max": (2.00, 6.00),
    "z-ai/glm-5.2": (0.76, 2.42),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "deepseek/deepseek-v4-pro": (0.435, 0.870),
    "deepseek/deepseek-v4-flash-0731": (0.09, 0.18),
    # Pipeline internals + models users may still have stored from before.
    # Re-checked against the live catalog 2026-08-04: was (0.28, 0.88), which
    # understated output by 17%. This is the ad_filter/triage model, so it prices
    # every digest's cheap stages.
    "deepseek/deepseek-chat": (0.2574, 1.0287),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
    "anthropic/claude-3-haiku": (0.25, 1.25),
}

# Used when a model id is not in the table. Deliberately not cheap — an unknown
# model costing $0 would make the margin look better than it is.
_FALLBACK_RATE: tuple[float, float] = (1.00, 3.00)

# Telegram Stars (XTR) -> USD. This is the DEVELOPER PAYOUT value (~$0.013/star
# after Telegram's cut), not the price the user pays (~$0.02). Margin is "what I
# net minus what I spend", so payout is the correct revenue basis. Tune here if
# Telegram changes the payout ratio.
STAR_USD: float = 0.013


def rate_for(model: str) -> tuple[float, float]:
    """Return (prompt_rate, completion_rate) USD/1M for a model id. Exact match,
    then longest-prefix match (so 'anthropic/claude-3.5-haiku:beta' resolves),
    then the conservative fallback (logged once-ish at debug)."""
    if not model:
        return _FALLBACK_RATE
    if model in PRICE_PER_1M:
        return PRICE_PER_1M[model]
    best = ""
    for known in PRICE_PER_1M:
        if model.startswith(known) and len(known) > len(best):
            best = known
    if best:
        return PRICE_PER_1M[best]
    logger.debug("pricing: no rate for model %s — using fallback %s", model, _FALLBACK_RATE)
    return _FALLBACK_RATE


def token_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost of one (model, prompt_tokens, completion_tokens) triple."""
    p_rate, c_rate = rate_for(model)
    return (prompt_tokens / 1_000_000.0) * p_rate + (completion_tokens / 1_000_000.0) * c_rate


def price_cost_summary(cost_summary: dict) -> dict:
    """Turn a pipeline cost_summary into a priced breakdown.

    Input: cost_summary from agent._build_cost_summary, whose `per_stage_tokens`
    is {stage: {model, prompt_tokens, completion_tokens, calls, [api_cost_usd]}}.

    Returns:
      {
        "total_cost_usd": float,
        "by_stage":  {stage: {"model", "prompt_tokens", "completion_tokens",
                              "calls", "cost_usd"}},
        "by_model":  {model: cost_usd},
        "read_mode": str,
      }
    Prefers a provider-reported api_cost_usd per stage when present, else prices
    from the local table.
    """
    per_stage = (cost_summary or {}).get("per_stage_tokens") or {}
    by_stage: dict[str, dict] = {}
    by_model: dict[str, float] = {}
    total = 0.0
    for stage, s in per_stage.items():
        model = s.get("model") or ""
        pt = int(s.get("prompt_tokens", 0) or 0)
        ct = int(s.get("completion_tokens", 0) or 0)
        api_cost = s.get("api_cost_usd")
        cost = float(api_cost) if api_cost not in (None, 0) else token_cost_usd(model, pt, ct)
        by_stage[stage] = {
            "model": model,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "calls": int(s.get("calls", 0) or 0),
            "cost_usd": round(cost, 6),
        }
        by_model[model] = round(by_model.get(model, 0.0) + cost, 6)
        total += cost
    return {
        "total_cost_usd": round(total, 6),
        "by_stage": by_stage,
        "by_model": by_model,
        "read_mode": (cost_summary or {}).get("read_mode", ""),
    }
