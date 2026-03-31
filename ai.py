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

class Insight(BaseModel):
    """Один инсайт из дайджеста."""
    title: str    # 3-6 слов, название инсайта
    channel: str  # username канала без @
    url: Optional[str] = None  # полная ссылка https://t.me/channel/123 — ТОЛЬКО из списка постов
    what: str     # что это — одно предложение
    how: str      # как конкретно применить — команда/шаг/инструмент

    @field_validator("url", mode="before")
    @classmethod
    def ensure_url(cls, v: str, info) -> str:
        if isinstance(v, str) and v.startswith("https://t.me/"):
            return v
        channel = (info.data or {}).get("channel", "")
        return f"https://t.me/{channel}" if channel else "https://t.me"


class DigestResult(BaseModel):
    """Структурированный дайджест Telegram-каналов."""
    insights: list[Insight]  # 3-6 инсайтов
    personal: list[str]      # 2-3 пункта лично для Дании
    today: str               # одно конкретное действие сегодня


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
        items = [f"- {d['date']}: {d['digest'][:120]}…" for d in recent_digests[-3:]]
        prev = "\nПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ (не повторяй те же инсайты):\n" + "\n".join(items)

    return f"""Ты персональный ИИ-ассистент Дании. По-русски, без воды, конкретно.

ПРОФИЛЬ:
{desc}

ТЕКУЩИЙ ФОКУС: {focus if focus else "не задан"}

{prev}
ИСТОРИЯ ВЗАИМОДЕЙСТВИЙ:
{history_text}

Правило: только конкретные команды/файлы/шаги. Никаких "можно изучить X" или "открывает горизонты"."""


# ── Digest generation via PydanticAI ─────────────────────────────────────────

def _format_posts(posts: list[dict]) -> str:
    parts = []
    for p in posts:
        link = p.get("link") or f"https://t.me/{p['channel']}"
        parts.append(f"КАНАЛ: {p['channel']}\nССЫЛКА: {link}\nТЕКСТ: {p['text'][:800]}")
    return "\n\n---\n\n".join(parts)


def _to_html(d: DigestResult) -> str:
    lines = ["<b>Топ инсайтов:</b>\n"]
    for ins in d.insights:
        url = ins.url or f"https://t.me/{ins.channel}"
        lines.append(
            f'• <b>{escape(ins.title)}</b> <a href="{url}">{escape(ins.channel)}</a>\n'
            f"  {escape(ins.what)}\n"
            f"  <i>{escape(ins.how)}</i>\n"
        )
    if d.personal:
        lines.append("<b>Лично тебе:</b>")
        for p in d.personal:
            lines.append(f"• {escape(p)}")
    lines.append(f"\n<b>Сделай сегодня:</b>\n{escape(d.today)}")
    return "\n".join(lines)


async def generate_digest(
    posts: list[dict],
    user_data: dict,
    recent_digests: list[dict] | None = None,
) -> str:
    if not posts:
        return "Не нашёл новых постов за последние 24 часа."

    model_id = user_data.get("model", "anthropic/claude-3.5-haiku")
    api_key = user_data["openrouter_key"]
    focus = user_data.get("current_focus", "")
    focus_line = f"\nФОКУС: «{focus}» — приоритизируй посты про это" if focus else ""

    posts_text = _format_posts(posts)
    prompt = (
        f"Посты из Telegram-каналов за последние 24 часа:{focus_line}\n\n"
        f"{posts_text}\n\n---\n"
        "Выбери 3-6 самых важных инсайтов для Дании. "
        "Для каждого используй ТОЧНУЮ ссылку из поля ССЫЛКА выше — не придумывай URL."
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
        return _to_html(result.output)
    except Exception as e:
        logger.error(f"pydantic-ai digest error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Ошибка генерации дайджеста: {e}"


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
