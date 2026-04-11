import asyncio
import base64
import json
import logging
from html import escape
from typing import Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, field_validator

from personalization import load_personalization

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CHEAP_VISION_MODEL = "openai/gpt-4o-mini"
AD_FILTER_MODEL = "deepseek/deepseek-chat"


# ─── Pydantic schemas ────────────────────────────────────────────────────────

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


class PostAdLabel(BaseModel):
    index: int
    is_ad: bool


class AdBatchResult(BaseModel):
    posts: list[PostAdLabel]


# ─── Client factory ──────────────────────────────────────────────────────────

def _get_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=OPENROUTER_BASE)


# ─── Prompt helpers ──────────────────────────────────────────────────────────

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
            clean = _strip_html(digest.get("digest_html", ""))
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
        prefix = "ТРЕД" if p.get("is_thread") else "ПОСТ"
        parts.append(
            f"{prefix}: {p['channel']}\n"
            f"ДАТА: {date_str}\n"
            f"ССЫЛКА: {link}\n"
            f"ТЕКСТ:\n{p['text'][:1600]}"
        )
    return "\n\n---\n\n".join(parts)


def _to_html_digest(d: DigestResult) -> str:
    blocks = []
    for src in d.sources:
        date_label = f" [{escape(src.post_date)}]" if src.post_date else ""
        header = f'<a href="{src.url}">{escape(src.channel)}</a>{date_label}'
        lines = [header]
        for bullet in src.bullets:
            lines.append(f"— {escape(bullet)}")
        if src.example:
            lines.append(f"💡 {escape(src.example)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _to_html_personal(d: DigestResult) -> str | None:
    if not d.personal:
        return None
    lines = ["<b>Лично тебе:</b>"]
    for item in d.personal:
        lines.append(f"• {escape(item)}")
    return "\n".join(lines)


def _to_html_stats(posts_checked: int, channels_count: int, sources_selected: int) -> str:
    return (
        f"<i>📊 Проверено {posts_checked} постов из {channels_count} каналов, "
        f"выбрано {sources_selected} среди них</i>"
    )


# ─── AI functions ─────────────────────────────────────────────────────────────

async def filter_ads(posts: list[dict], api_key: str, batch_size: int = 3) -> list[dict]:
    """Pre-filter posts: drop pure ads, keep posts with real signal."""
    if not posts:
        return []

    client = _get_client(api_key)
    ad_system = (
        "Ты — строгий редактор технического дайджеста. "
        "Твоя задача: определить, является ли пост чистой рекламой.\n\n"
        "РЕКЛАМА (is_ad=true): пост продаёт курс, сервис или событие БЕЗ реального контента — "
        "только призыв купить/зарегистрироваться/подписаться, без конкретных фактов или объяснений.\n\n"
        "НЕ РЕКЛАМА (is_ad=false): пост содержит реальные инсайты, факты, анализ или примеры, "
        "даже если упоминает конкретный продукт или компанию. "
        "Фраза 'не реклама' в тексте — подсказка, но не решающий фактор, смотри на содержание.\n\n"
        "ГРАНИЧНЫЕ СЛУЧАИ — НЕ РЕКЛАМА:\n"
        "- Событие/вебинар с конкретной программой, спикерами или разбором темы → НЕ реклама\n"
        "- Продуктовый апдейт с описанием новых фич или архитектурных решений → НЕ реклама\n"
        "- Пост упоминает продукт, но основную часть занимает анализ, список фактов или пример → НЕ реклама\n\n"
        "РЕКЛАМА — только если: нет фактического контента, только CTA или восклицания типа "
        "'купи', 'зарегистрируйся', 'успей до конца недели', 'скидка 50%'.\n\n"
        f"Отвечай ТОЛЬКО валидным JSON по схеме: {AdBatchResult.model_json_schema()}"
    )

    kept: list[dict] = []
    for batch_start in range(0, len(posts), batch_size):
        batch = posts[batch_start: batch_start + batch_size]
        lines = []
        for i, p in enumerate(batch):
            lines.append(f"[{i}] КАНАЛ: {p['channel']}\nТЕКСТ:\n{p['text'][:800]}")
        prompt = (
            "Оцени каждый пост: is_ad=true если чистая реклама без сигнала, is_ad=false если есть реальный контент.\n\n"
            + "\n\n---\n\n".join(lines)
        )
        try:
            resp = await client.chat.completions.create(
                model=AD_FILTER_MODEL,
                messages=[
                    {"role": "system", "content": ad_system},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            data = json.loads(resp.choices[0].message.content)
            result = AdBatchResult.model_validate(data)
            labels = {lbl.index: lbl.is_ad for lbl in result.posts}
            for i, post in enumerate(batch):
                if not labels.get(i, False):
                    kept.append(post)
                else:
                    logger.info(f"Ad-filter dropped: [{post['channel']}] {post['text'][:60]!r}")
        except Exception as e:
            logger.warning(f"Ad-filter batch failed, keeping all: {e}")
            kept.extend(batch)

    logger.info(f"Ad-filter: {len(posts)} posts → {len(kept)} kept")
    return kept


async def generate_digest(
    posts: list[dict],
    user_data: dict,
    recent_digests: list[dict] | None = None,
) -> tuple[str, str | None, str]:
    if not posts:
        return "Не нашёл новых постов за последние 24 часа.", None, ""

    model_id = user_data.get("model", "anthropic/claude-3.5-haiku")
    api_key = user_data["openrouter_key"]
    focus = user_data.get("current_focus", "")
    focus_line = f"\nФОКУС: «{focus}» — приоритизируй посты про это" if focus else ""

    posts_text = _format_posts(posts)
    schema_hint = DigestResult.model_json_schema()
    prompt = (
        f"Посты из Telegram-каналов:{focus_line}\n\n"
        f"{posts_text}\n\n---\n"
        "Сгруппируй по источникам (4-6 самых информативных постов). "
        "Для каждого источника:\n"
        "- url: ТОЧНАЯ ссылка из поля ССЫЛКА (не придумывай)\n"
        "- post_date: дата из поля ДАТА (формат DD.MM.YYYY)\n"
        "- bullets: все факты из поста, одна строка — один факт\n"
        "- example: только если концепт нетривиальный — одно предложение как первокурснику\n\n"
        f"Отвечай ТОЛЬКО валидным JSON по схеме: {schema_hint}"
    )

    system = build_system_prompt(user_data, recent_digests)
    client = _get_client(api_key)

    try:
        resp = await client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        result = DigestResult.model_validate(data)
        stats = _to_html_stats(
            posts_checked=len(posts),
            channels_count=len({p["channel"] for p in posts}),
            sources_selected=len(result.sources),
        )
        return _to_html_digest(result), _to_html_personal(result), stats
    except Exception as e:
        logger.error(f"digest generation error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Ошибка генерации дайджеста: {e}", None, ""


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
                                    "Ты — визуальный редактор. Твоя задача: отобрать картинки для дайджеста об AI и технологиях.\n"
                                    f"ТЕКСТ ДАЙДЖЕСТА:\n{context}\n\n"
                                    "Картинка УМЕСТНА, если она:\n"
                                    "1. Иллюстрирует один из пунктов дайджеста.\n"
                                    "2. Является скриншотом нового инструмента/интерфейса.\n"
                                    "3. Это качественное тематическое фото (роботы, код, чипы).\n"
                                    "Картинка НЕУМЕСТНА, если это: реклама, мем не по теме, личное фото, мусорный скриншот.\n\n"
                                    "Ответить только одним словом: YES или NO."
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


async def generate_weekly_recap(history_7days: list[dict]) -> str:
    """Generate weekly insights recap for Sunday 18:00 job."""
    if not history_7days:
        return ""
    total = len(history_7days)
    total_posts = sum(h.get("posts_count", 0) for h in history_7days)
    digests_text = "\n\n".join(
        f"[{h['date']}]\n{h['digest'][:400]}"
        for h in history_7days
        if not h.get("is_error")
    )
    return f"📅 За эту неделю: {total} дайджестов, {total_posts} постов проверено.\n\n{digests_text[:1200]}"
