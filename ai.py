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
    title: str       # 3-6 слов, название инсайта
    channel: str     # username канала без @
    url: Optional[str] = None   # полная ссылка https://t.me/channel/123 — ТОЛЬКО из списка постов
    post_date: str = ""          # дата поста в формате DD.MM.YYYY — из поля ДАТА
    what: str        # что это — одно предложение, факт
    how: str         # конкретная команда/файл/шаг/инструмент — НЕ "изучи X"

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
    personal: list[str]      # 2-3 пункта лично для Дании — конкретные, не общие
    today: str               # ОДНО конкретное действие: глагол + инструмент/команда/файл


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
        prev = "\nПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ (не повторяй те же инсайты):\n" + "\n".join(items) + "\n"

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
- В `how` НИКОГДА не пиши рекламный призыв (ссылку на продукт, "запишись", "получи консультацию").

ЖЁСТКИЕ ПРАВИЛА:
1. `what` = один факт из поста (что именно появилось/изменилось/вышло).
2. `how` = конкретная команда, путь к файлу, URL, флаг CLI или название инструмента. Никогда не пиши "изучи X", "используй X для Y" без конкретного шага.
3. `today` = одно предложение: глагол в повелительном + конкретный инструмент/команда/файл. Пример: "Запусти `uv run test_smoke.py` и проверь логи scraper-а".
4. `personal` = 2-3 пункта, каждый привязан к конкретному инсайту из этого дайджеста, не к общим советам.
5. `post_date` = дата из поля ДАТА в исходных данных, формат DD.MM.YYYY.
6. `url` = ТОЛЬКО ссылки из поля ССЫЛКА. Не придумывай.

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


def _to_html(d: DigestResult) -> str:
    lines = ["<b>Топ инсайтов:</b>\n"]
    for ins in d.insights:
        url = ins.url or f"https://t.me/{ins.channel}"
        date_label = f" <i>{escape(ins.post_date)}</i>" if ins.post_date else ""
        lines.append(
            f'• <b>{escape(ins.title)}</b> <a href="{url}">{escape(ins.channel)}</a>{date_label}\n'
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
        f"Посты из Telegram-каналов:{focus_line}\n\n"
        f"{posts_text}\n\n---\n"
        "Выбери 3-6 самых важных инсайтов. "
        "Для каждого:\n"
        "- url: ТОЧНАЯ ссылка из поля ССЫЛКА (не придумывай)\n"
        "- post_date: дата из поля ДАТА (формат DD.MM.YYYY)\n"
        "- how: конкретная команда/файл/инструмент, не общий совет\n"
        "- today: одно действие с конкретным инструментом или командой"
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
