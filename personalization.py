from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).parent / "config"
PERSONALIZATION_FILE = CONFIG_DIR / "personalization.yaml"
PERSONALIZATION_EXAMPLE_FILE = CONFIG_DIR / "personalization.example.yaml"


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Personalization config must be a mapping: {path}")
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
    data = _load_yaml(_pick_config_path())
    profile = data.setdefault("profile", {})
    prompt = data.setdefault("prompt", {})
    profile.setdefault("description", "")
    prompt.setdefault("system_template", "")
    for key in (
        "style_rules",
        "ad_filter_rules",
        "hard_rules",
        "source_selection_rules",
        "canonical_examples",
        "stop_words",
    ):
        value = prompt.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"Personalization config field must be a list: prompt.{key}")
        prompt[key] = [str(item) for item in value]
    return data


def get_profile_description() -> str:
    profile = load_personalization().get("profile", {})
    return str(profile.get("description", "")).strip()
