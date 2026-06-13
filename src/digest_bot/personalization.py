"""Personalization config resolution — the per-user privacy boundary.

Three sources, in resolution order (resolve_personalization):

  1. per-user overrides   user_settings.personalization (JSONB, via db) — minus
                          reserved namespaces like the "_usage" counter
  2. owner yaml           config/personalization.yaml (gitignored, bind-mounted)
                          — ONLY for the owner (env CHAT_ID), never tenants
  3. neutral default      config/personalization.default.yaml (committed,
                          owner-free) — the base every non-owner starts from

HARD RULE: the owner's yaml carries the owner's personal bio / style / stop
words. It must never reach another tenant's prompt. Any new call site that
builds a system prompt must go through resolve_personalization (or pass an
explicitly resolved dict), not load_personalization().
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import yaml

from digest_bot.paths import CONFIG_DIR
PERSONALIZATION_FILE = CONFIG_DIR / "personalization.yaml"
PERSONALIZATION_EXAMPLE_FILE = CONFIG_DIR / "personalization.example.yaml"
PERSONALIZATION_DEFAULT_FILE = CONFIG_DIR / "personalization.default.yaml"

# Reserved namespaces inside user_settings.personalization that are NOT prompt
# config: db.record_chat_turn keeps the monthly chat-turn counter under
# "_usage". Resolution must ignore them (a usage blob alone is NOT a profile)
# and writers must never clobber them (read-merge-write, see db.record_chat_turn).
RESERVED_DB_KEYS = frozenset({"_usage"})

_PROMPT_LIST_KEYS = (
    "style_rules",
    "ad_filter_rules",
    "hard_rules",
    "source_selection_rules",
    "canonical_examples",
    "stop_words",
)


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Personalization config must be a mapping: {path}")
    return data


def _normalize(data: dict) -> dict:
    """Coerce a personalization dict to the shape ai.build_system_prompt expects:
    profile.description string, prompt.system_template string, rule fields lists
    of strings. Raises on structurally wrong rule fields."""
    profile = data.setdefault("profile", {})
    prompt = data.setdefault("prompt", {})
    profile.setdefault("description", "")
    prompt.setdefault("system_template", "")
    for key in _PROMPT_LIST_KEYS:
        value = prompt.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"Personalization config field must be a list: prompt.{key}")
        prompt[key] = [str(item) for item in value]
    return data


def _pick_config_path() -> Path:
    if PERSONALIZATION_FILE.exists():
        return PERSONALIZATION_FILE
    if PERSONALIZATION_EXAMPLE_FILE.exists():
        return PERSONALIZATION_EXAMPLE_FILE
    raise FileNotFoundError(
        "Missing personalization config. Expected "
        f"{PERSONALIZATION_FILE} or {PERSONALIZATION_EXAMPLE_FILE}."
    )


def load_personalization() -> dict:
    """OWNER-ONLY: the operator's private yaml (or the committed example on a
    fresh clone). Never feed this to a non-owner tenant — use
    resolve_personalization(), which gates it on is_owner()."""
    return _normalize(_load_yaml(_pick_config_path()))


def load_default_personalization() -> dict:
    """The neutral, owner-free template (committed to the repo). The fail-closed
    base for every tenant without a saved per-user profile."""
    return _normalize(_load_yaml(PERSONALIZATION_DEFAULT_FILE))


def get_profile_description() -> str:
    """OWNER-ONLY convenience (reads the owner yaml)."""
    profile = load_personalization().get("profile", {})
    return str(profile.get("description", "")).strip()


def is_owner(tg_user_id) -> bool:
    """True iff tg_user_id is the operator (env CHAT_ID) — the same anchor
    db.ensure_owner_user seeds the owner row from. Unset CHAT_ID → nobody is
    the owner (fail-closed: nobody inherits the private yaml)."""
    raw = os.getenv("CHAT_ID", "").strip()
    return bool(raw) and str(tg_user_id) == raw


def user_overrides(db_personalization: dict | None) -> dict:
    """Strip reserved namespaces (e.g. the _usage counter) from a raw
    user_settings.personalization blob, leaving only real prompt overrides."""
    return {
        k: v for k, v in (db_personalization or {}).items() if k not in RESERVED_DB_KEYS
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge: override wins per-key; nested dicts merge, anything
    else (lists, scalars) is replaced wholesale. Inputs are not mutated."""
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def resolve_personalization(db_personalization: dict | None, tg_user_id) -> dict:
    """The single per-user resolver (privacy boundary).

    base   = owner yaml IF tg_user_id is the operator, ELSE the neutral default
    result = base deep-merged with the user's own DB overrides (reserved
             namespaces like _usage stripped first — a usage counter is not a
             profile and must never force-skip the neutral base).

    A tenant therefore can NEVER see the owner's profile/stop-words/style; the
    owner keeps the rich gitignored yaml exactly as before the multi-tenant
    cutover.
    """
    base = load_personalization() if is_owner(tg_user_id) else load_default_personalization()
    overrides = user_overrides(db_personalization)
    if not overrides:
        return base
    return _normalize(_deep_merge(base, overrides))
