import base64
import logging
from html import escape
from typing import Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, field_validator
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from personalization import load_personalization

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CHEAP_VISION_MODEL = "openai/gpt-4o-mini"


class SourceBlock(BaseModel):
    channel: str
    url: str
    post_date: str = ""
    bullets: list[str]
    example: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def ensure_url(cls, v: str, info) -> str:
        if isinstance(v, str) and v.startswith("https://t.me/"):
            return v
        channel = (info.data or {}).get("channel", "")
        return f"https://t.me/{channel}" if channel else "https://t.me"


class DigestResult(BaseModel):
    sources: list[SourceBlock]
    personal: list[str]


def _make_openrouter_model(api_key: str, model_id: str) -> OpenRouterModel:
    return OpenRouterModel(
        model_id,
        provider=OpenRouterProvider(api_key=api_key),
    )


def _get_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=OPENROUTER_BASE)


def _render_rule_block(items: list[str], empty_value: str = "- нет") -> str:
    if not items:
        return empty_value
    return "\n".join(f"- {item}" for item in items)


def _render_example_block(items: list[str]) -> str:
    if not items:
        return ""
    return "\n\n".join(items)


def build_system_prompt(user_data: dict, recent_digests: list[dict] | None = None) -> str:
    cfg = load_personalization()
    profile_cfg = cfg.get("profile", {})
    prompt_cfg = cfg.get("prompt", {})

    profile_description = user_data.get("description") or profile_cfg.get("description", "")
    focus = user_data.get("current_focus", "") or prompt_cfg.get("empty_focus_text", "не задан")
    history = user_data.get("interaction_history", [])[-5:]
    history_text = "\n".join(f"- {h}" for h in history) if history else "нет"

    prev = prompt_cfg.get("empty_recent_digest_text", "")
    if recent_digests:
        import re

        def _strip_html(text: str) -> str:
            return re.sub(r"<[^>]+>", "", text).strip()

        prev_lines = []
        for digest in recent_digests[-3:]:
            if digest.get("is_error"):
                continue
            clean = _strip_html(digest["digest"])
            prev_lines.append(f"[{digest['date']}]\n{clean[:600]}")
        if prev_lines:
            prev = "ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ:\n" + "\n\n".join(prev_lines)

    template = prompt_cfg.get("system_template", "").strip()
    if not template:
        raise ValueError("Missing prompt.system_template in personalization config")

    return template.format(
        profile_description=profile_description,
        focus=focus,
        recent_digest_block=prev,
        interaction_history=history_text,
        style_rules=_render_rule_block(prompt_cfg.get("style_rules", [])),
        ad_filter_rules=_render_rule_block(prompt_cfg.get("ad_filter_rules", [])),
        hard_rules=_render_rule_block(prompt_cfg.get("hard_rules", [])),
        source_selection_rules=_render_rule_block(prompt_cfg.get("source_selection_rules", [])),
        canonical_examples=_render_example_block(prompt_cfg.get("canonical_examples", [])),
        stop_words=", ".join(f'"{item}"' for item in prompt_cfg.get("stop_words", [])),
    )


def _format_posts(posts: list[dict]) -> str:
    parts = []
    for p in posts:
        link = p.get("link") or f"https://t.me/{p['channel']}"
        post_time_raw = p.get("time", "")
        date_str = ""
        if post_time_raw:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(post_time_raw)
                date_str = dt.strftime("%d.%m.%Y %H:%M UTC")
            except Exception:
                date_str = post_time_raw[:10]
        prefix = "РўР Р•Р”" if p.get("is_thread") else "РџРћРЎРў"
        parts.append(
            f"{prefix}: {p['channel']}\n"
            f"Р”РђРўРђ: {date_str}\n"
            f"РЎРЎР«Р›РљРђ: {link}\n"
            f"РўР•РљРЎРў:\n{p['text'][:1600]}"
        )
    return "\n\n---\n\n".join(parts)


def _to_html_digest(d: DigestResult) -> str:
    blocks = []
    for src in d.sources:
        date_label = f" [{escape(src.post_date)}]" if src.post_date else ""
        header = f'<a href="{src.url}">{escape(src.channel)}</a>{date_label}'
        lines = [header]
        for bullet in src.bullets:
            lines.append(f"вЂ” {escape(bullet)}")
        if src.example:
            lines.append(f"рџ’Ў {escape(src.example)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _to_html_personal(d: DigestResult) -> str | None:
    if not d.personal:
        return None
    lines = ["<b>Р›РёС‡РЅРѕ С‚РµР±Рµ:</b>"]
    for item in d.personal:
        lines.append(f"вЂў {escape(item)}")
    return "\n".join(lines)


def _to_html_stats(posts_checked: int, channels_count: int, sources_selected: int) -> str:
    return (
        f"<i>рџ“Љ РџСЂРѕРІРµСЂРµРЅРѕ {posts_checked} РїРѕСЃС‚РѕРІ РёР· {channels_count} РєР°РЅР°Р»РѕРІ, "
        f"РІС‹Р±СЂР°РЅРѕ {sources_selected} РёСЃС‚РѕС‡РЅРёРєРѕРІ</i>"
    )


async def generate_digest(
    posts: list[dict],
    user_data: dict,
    recent_digests: list[dict] | None = None,
) -> tuple[str, str | None, str]:
    if not posts:
        return "РќРµ РЅР°С€С‘Р» РЅРѕРІС‹С… РїРѕСЃС‚РѕРІ Р·Р° РїРѕСЃР»РµРґРЅРёРµ 24 С‡Р°СЃР°.", None, ""

    model_id = user_data.get("model", "anthropic/claude-3.5-haiku")
    api_key = user_data["openrouter_key"]
    focus = user_data.get("current_focus", "")
    focus_line = f"\nР¤РћРљРЈРЎ: В«{focus}В» вЂ” РїСЂРёРѕСЂРёС‚РёР·РёСЂСѓР№ РїРѕСЃС‚С‹ РїСЂРѕ СЌС‚Рѕ" if focus else ""

    posts_text = _format_posts(posts)
    prompt = (
        f"РџРѕСЃС‚С‹ РёР· Telegram-РєР°РЅР°Р»РѕРІ:{focus_line}\n\n"
        f"{posts_text}\n\n---\n"
        "РЎРіСЂСѓРїРїРёСЂСѓР№ РїРѕ РёСЃС‚РѕС‡РЅРёРєР°Рј (4-6 СЃР°РјС‹С… РёРЅС„РѕСЂРјР°С‚РёРІРЅС‹С… РїРѕСЃС‚РѕРІ). "
        "Р”Р»СЏ РєР°Р¶РґРѕРіРѕ РёСЃС‚РѕС‡РЅРёРєР°:\n"
        "- url: РўРћР§РќРђРЇ СЃСЃС‹Р»РєР° РёР· РїРѕР»СЏ РЎРЎР«Р›РљРђ (РЅРµ РїСЂРёРґСѓРјС‹РІР°Р№)\n"
        "- post_date: РґР°С‚Р° РёР· РїРѕР»СЏ Р”РђРўРђ (С„РѕСЂРјР°С‚ DD.MM.YYYY)\n"
        "- bullets: РІСЃРµ С„Р°РєС‚С‹ РёР· РїРѕСЃС‚Р°, РѕРґРЅР° СЃС‚СЂРѕРєР° вЂ” РѕРґРёРЅ С„Р°РєС‚\n"
        "- example: С‚РѕР»СЊРєРѕ РµСЃР»Рё РєРѕРЅС†РµРїС‚ РЅРµС‚СЂРёРІРёР°Р»СЊРЅС‹Р№ вЂ” РѕРґРЅРѕ РїСЂРµРґР»РѕР¶РµРЅРёРµ РєР°Рє РїРµСЂРІРѕРєСѓСЂСЃРЅРёРєСѓ"
    )

    system = build_system_prompt(user_data, recent_digests)

    try:
        model = _make_openrouter_model(api_key, model_id)
        agent = Agent(
            model,
            output_type=PromptedOutput(DigestResult),
            system_prompt=system,
            retries=2,
        )
        result = await agent.run(prompt)
        stats = _to_html_stats(
            posts_checked=len(posts),
            channels_count=len({p["channel"] for p in posts}),
            sources_selected=len(result.output.sources),
        )
        return _to_html_digest(result.output), _to_html_personal(result.output), stats
    except Exception as e:
        logger.error(f"pydantic-ai digest error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return f"РћС€РёР±РєР° РіРµРЅРµСЂР°С†РёРё РґР°Р№РґР¶РµСЃС‚Р°: {e}", None, ""


async def filter_images(images: list[bytes], digest_text: str, api_key: str) -> list[bytes]:
    if not images:
        return []

    client = _get_client(api_key)
    approved = []
    context = digest_text[:1500]

    for img_bytes in images[:10]:
        b64 = base64.b64encode(img_bytes).decode()
        try:
            resp = await client.chat.completions.create(
                model=CHEAP_VISION_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "РўС‹ вЂ” РІРёР·СѓР°Р»СЊРЅС‹Р№ СЂРµРґР°РєС‚РѕСЂ. РўРІРѕСЏ Р·Р°РґР°С‡Р°: РѕС‚РѕР±СЂР°С‚СЊ РєР°СЂС‚РёРЅРєРё РґР»СЏ РґР°Р№РґР¶РµСЃС‚Р° РѕР± AI Рё С‚РµС…РЅРѕР»РѕРіРёСЏС….\n"
                                    f"РўР•РљРЎРў Р”РђР™Р”Р–Р•РЎРўРђ:\n{context}\n\n"
                                    "РљР°СЂС‚РёРЅРєР° РЈРњР•РЎРўРќРђ, РµСЃР»Рё РѕРЅР°:\n"
                                    "1. РР»Р»СЋСЃС‚СЂРёСЂСѓРµС‚ РѕРґРёРЅ РёР· РїСѓРЅРєС‚РѕРІ РґР°Р№РґР¶РµСЃС‚Р°.\n"
                                    "2. РЇРІР»СЏРµС‚СЃСЏ СЃРєСЂРёРЅС€РѕС‚РѕРј РЅРѕРІРѕРіРѕ РёРЅСЃС‚СЂСѓРјРµРЅС‚Р°/РёРЅС‚РµСЂС„РµР№СЃР°.\n"
                                    "3. Р­С‚Рѕ РєР°С‡РµСЃС‚РІРµРЅРЅРѕРµ С‚РµРјР°С‚РёС‡РµСЃРєРѕРµ С„РѕС‚Рѕ (СЂРѕР±РѕС‚С‹, РєРѕРґ, С‡РёРїС‹).\n"
                                    "РљР°СЂС‚РёРЅРєР° РќР•РЈРњР•РЎРўРќРђ, РµСЃР»Рё СЌС‚Рѕ: СЂРµРєР»Р°РјР°, РјРµРј РЅРµ РїРѕ С‚РµРјРµ, Р»РёС‡РЅРѕРµ С„РѕС‚Рѕ, РјСѓСЃРѕСЂРЅС‹Р№ СЃРєСЂРёРЅС€РѕС‚.\n\n"
                                    "РћС‚РІРµС‚СЊ С‚РѕР»СЊРєРѕ РѕРґРЅРёРј СЃР»РѕРІРѕРј: YES РёР»Рё NO."
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ],
                    }
                ],
                max_tokens=5,
                temperature=0.0,
            )
            answer = resp.choices[0].message.content.strip().upper()
            logger.info(f"Image filter verdict: {answer}")
            if "YES" in answer:
                approved.append(img_bytes)
        except Exception as e:
            logger.warning(f"image filter error: {e}")
            continue

    return approved


async def chat_response(user_message: str, user_data: dict) -> str:
    client = _get_client(user_data["openrouter_key"])
    model = user_data.get("model", "anthropic/claude-3.5-haiku")
    last_digest = user_data.get("last_digest", "")
    digest_ctx = f"\nРџРћРЎР›Р•Р”РќРР™ Р”РђР™Р”Р–Р•РЎРў:\n{last_digest[:800]}" if last_digest else ""
    system = build_system_prompt(user_data) + digest_ctx
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            max_tokens=700,
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.error(f"chat error: {e}")
        return f"РћС€РёР±РєР°: {e}"
