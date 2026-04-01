import base64
import logging
from html import escape
from typing import Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, field_validator
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
CHEAP_VISION_MODEL = "openai/gpt-4o-mini"

# ── Structured output schema ──────────────────────────────────────────────────

class SourceBlock(BaseModel):
    """Один источник (пост/тред) с набором инсайтов."""
    channel: str        # username без @
    url: str            # ссылка на пост https://t.me/channel/123
    post_date: str = "" # дата поста DD.MM.YYYY — из поля ДАТА
    bullets: list[str]  # 1-5 коротких фактов, одна строка каждый
    example: str = ""   # необязательно: простая аналогия/объяснение как первокурснику

    @field_validator("url", mode="before")
    @classmethod
    def ensure_url(cls, v: str, info) -> str:
        if isinstance(v, str) and v.startswith("https://t.me/"):
            return v
        channel = (info.data or {}).get("channel", "")
        return f"https://t.me/{channel}" if channel else "https://t.me"


class DigestResult(BaseModel):
    """Структурированный дайджест Telegram-каналов."""
    sources: list[SourceBlock]  # группировка по источникам
    personal: list[str]         # 2-3 пункта для закрепления в памяти


# ── Model factory ─────────────────────────────────────────────────────────────

def _make_openrouter_model(api_key: str, model_id: str) -> OpenRouterModel:
    return OpenRouterModel(
        model_id,
        provider=OpenRouterProvider(api_key=api_key),
    )


def _get_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=OPENROUTER_BASE)


# ── Prompts ───────────────────────────────────────────────────────────────────

def build_system_prompt(user_data: dict, recent_digests: list[dict] | None = None) -> str:
    desc = user_data.get("description", "")
    focus = user_data.get("current_focus", "")
    history = user_data.get("interaction_history", [])[-5:]
    history_text = "\n".join(f"- {h}" for h in history) if history else "нет"

    prev = ""
    if recent_digests:
        import re
        def _strip_html(s: str) -> str:
            return re.sub(r"<[^>]+>", "", s).strip()

        prev_lines = []
        for d in recent_digests[-3:]:
            if d.get("is_error"):
                continue
            clean = _strip_html(d["digest"])
            prev_lines.append(f"[{d['date']}]\n{clean[:600]}")
        if prev_lines:
            prev = "\nПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ (не повторяй те же инсайты — сравни заголовки и факты):\n" + "\n\n".join(prev_lines) + "\n"

    return f"""Ты — редактор персонального дайджеста AI/tech новостей. Пишешь по-русски, сухо, конкретно.

ПРОФИЛЬ:
{desc}

ТЕКУЩИЙ ФОКУС: {focus if focus else "не задан"}
{prev}
ПОСЛЕДНИЕ ВЗАИМОДЕЙСТВИЯ:
{history_text}

ЯЗЫК: Все поля — только на русском. Никакого английского.

ФИЛЬТР РЕКЛАМЫ (по смыслу, не по словам):
- Некоторые посты — треды: оригинальный пост + комментарии участников. Оцени ВЕСЬ тред целиком.
- Если тред начинается с рекламного поста, но комментарии содержат реальную дискуссию — возьми инсайт из дискуссии, проигнорируй рекламную часть.
- Если основная цель поста — продать конкретный сервис/курс без реального инсайта — пропусти полностью.

ЖЁСТКИЕ ПРАВИЛА:
1. `bullets` = список коротких фактов из поста. Одна строка — один факт. Что вышло/изменилось/появилось. Без воды.
2. `example` = заполняй ТОЛЬКО если концепт нетривиальный. Простая аналогия или объяснение в одно предложение, как первокурснику.
3. `personal` = 2-3 пункта, каждый повторяет конкретный инсайт из дайджеста в контексте профиля пользователя — для закрепления в памяти, не новая информация.
4. `post_date` = дата из поля ДАТА в исходных данных, формат DD.MM.YYYY.
5. `url` = ТОЛЬКО ссылки из поля ССЫЛКА. Не придумывай.

КОЛИЧЕСТВО ИСТОЧНИКОВ:
- 4-6 источников — только самые информативные посты.
- Один источник может давать 1-5 bullets — перечисляй все факты из поста.
- Малоинформативные посты пропускай полностью.

ЭТАЛОННЫЙ ПРИМЕР (структура и плотность):
  channel="cryptoEssay" url="https://t.me/cryptoEssay/2932" post_date="01.04.2026"
  bullets=["ИИ не учится по одному примеру — нужны миллиарды токенов для переобучения", "Агент не помнит между запусками — без внешнего хранилища память обнуляется", "Уточнения в чате не закрепляются — после сессии агент снова ошибётся"]
  example="Как если бы у тебя каждое утро стиралась память — агент так и работает"

  channel="ai_newz" url="https://t.me/ai_newz/4500" post_date="31.03.2026"
  bullets=["OpenAI: раунд $122 млрд, оценка $852 млрд — деньги идут на датацентры"]
  example=""

СТОП-СЛОВА (за их использование — неправильный ответ): "открывает горизонты", "даст преимущество", "критически важен", "удваивай ставку", "не распыляйся", "инвестируй время"."""


# ── Digest generation via PydanticAI ─────────────────────────────────────────

def _format_posts(posts: list[dict]) -> str:
    parts = []
    for p in posts:
        link = p.get("link") or f"https://t.me/{p['channel']}"
        # Parse time to human-readable date for AI
        post_time_raw = p.get("time", "")
        date_str = ""
        if post_time_raw:
            try:
                from datetime import datetime, timezone
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
        for b in src.bullets:
            lines.append(f"— {escape(b)}")
        if src.example:
            lines.append(f"💡 {escape(src.example)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _to_html_personal(d: DigestResult) -> str | None:
    if not d.personal:
        return None
    lines = ["<b>Лично тебе:</b>"]
    for p in d.personal:
        lines.append(f"• {escape(p)}")
    return "\n".join(lines)


def _to_html_stats(posts_checked: int, channels_count: int, sources_selected: int) -> str:
    return (
        f"<i>📊 Проверено {posts_checked} постов из {channels_count} каналов, "
        f"выбрано {sources_selected} источников</i>"
    )


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
    prompt = (
        f"Посты из Telegram-каналов:{focus_line}\n\n"
        f"{posts_text}\n\n---\n"
        "Сгруппируй по источникам (4-6 самых информативных постов). "
        "Для каждого источника:\n"
        "- url: ТОЧНАЯ ссылка из поля ССЫЛКА (не придумывай)\n"
        "- post_date: дата из поля ДАТА (формат DD.MM.YYYY)\n"
        "- bullets: все факты из поста, одна строка — один факт\n"
        "- example: только если концепт нетривиальный — одно предложение как первокурснику"
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
        return f"Ошибка генерации дайджеста: {e}", None, ""


# ── Image filtering ───────────────────────────────────────────────────────────

async def filter_images(images: list[bytes], digest_text: str, api_key: str) -> list[bytes]:
    """Фильтрует изображения, оставляя только тематически подходящие к дайджесту."""
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
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Ты — визуальный редактор. Твоя задача: отобрать картинки для дайджеста об AI и технологиях.\n"
                            f"ТЕКСТ ДАЙДЖЕСТА:\n{context}\n\n"
                            "Картинка УМЕСТНА, если она:\n"
                            "1. Иллюстрирует один из пунктов дайджеста.\n"
                            "2. Является скриншотом нового инструмента/интерфейса.\n"
                            "3. Это качественное тематическое фото (роботы, код, чипы).\n"
                            "Картинка НЕУМЕСТНА, если это: реклама, мем не по теме, личное фото, мусорный скриншот.\n\n"
                            "Ответь только одним словом: YES или NO."
                        )},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
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


# ── Free chat ─────────────────────────────────────────────────────────────────

async def chat_response(user_message: str, user_data: dict) -> str:
    client = _get_client(user_data["openrouter_key"])
    model = user_data.get("model", "anthropic/claude-3.5-haiku")
    last_digest = user_data.get("last_digest", "")
    digest_ctx = f"\nПОСЛЕДНИЙ ДАЙДЖЕСТ:\n{last_digest[:800]}" if last_digest else ""
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
        return f"Ошибка: {e}"
