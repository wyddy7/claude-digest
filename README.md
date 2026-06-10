# Digest Bot

Personal Telegram bot that scrapes selected public channels and generates an AI-personalized digest.

## Sample Output

A real digest the bot produced (2026-05-28). The pipeline scrapes a set of
public Telegram channels, reads the linked articles, filters ads, and writes a
tiered Russian-language digest with per-source fairness and a short "more" tail.

> The channel set and the per-user "for you" section are configurable; both are
> stripped from this sample. Costs below are for one full run.

| Stage     | Prompt tokens | Completion tokens | Calls |
|-----------|--------------:|------------------:|------:|
| triage    |         3 650 |               539 |     1 |
| ad-filter |        10 791 |             2 929 |    13 |
| digest    |        22 915 |             1 972 |     1 |

`read_mode=extract` · 34 URLs attempted, 29 extracted · 21 skipped (dedup cap)

```html
<a href="https://t.me/naebnet/15845">naebnet</a> [28.05.2026 17:20 UTC]
— Claude Opus 4.8 набирает 69,2% на SWE-Bench Pro и 57,9% на Humanity's Last Exam
— Dynamic workflows в Claude Code: агент меняет план действий на ходу при смене контекста
— Модель лучше работает на длинных задачах и чаще обнаруживает собственные ошибки

<a href="https://t.me/seeallochnaya/3660">seeallochnaya</a> [28.05.2026 16:53 UTC]
— Fast режим Opus 4.8 теперь в 3 раза дешевле прошлых моделей: было в 6× дороже базового, стало в 2×
— Гранулярная разбивка длины рассуждений — как у ChatGPT o-серии, пользователь управляет глубиной
— Анонс нового класса моделей выше Opus по интеллекту — «в ближайшие недели»

<a href="https://t.me/ai_newz/4595">ai_newz</a> [28.05.2026 17:29 UTC]
— Лимиты токенов в Claude Code увеличены синхронно с ростом потребления на более высоких уровнях усилий
— Модель декларируется более честной: реже срезает углы, чаще признаёт незнание

<a href="https://t.me/cryptoEssay/3012">cryptoEssay</a> [28.05.2026 17:59 UTC]
— Dynamic workflows — 90% рабочего использования Claude: один промпт типа «прочитай А и Б, сделай В, проверь Г, сохрани в Д, протестируй» без ручного сопровождения
— Задача на 30 минут (финансовый анализ + дашборд + имейл + проверка договора) выполняется агентом целиком самостоятельно

<a href="https://t.me/hacker_news_feed/128466">hacker_news_feed</a> [28.05.2026 19:40 UTC]
— Show HN: «Continue? Y/N» — 60-секундная игра про permission fatigue AI-агентов
💡 Permission fatigue — когда человек устаёт нажимать «разрешить» на каждый шаг агента и начинает аппрувить не глядя.

<i>ещё: Go proposal — generic methods (type parameters на конкретных методах) · Apple и Google как активные посредники в push-уведомлениях: суммаризация на устройстве · AMD изменила лицензирование Vivado для Linux без предупреждения</i>
```

## Local Setup

1. Install `uv`.
2. Create `.env`:

```env
BOT_TOKEN=...
CHAT_ID=...
OPENROUTER_KEY=...
```

3. Create local personalization config:

```bash
cd digest_bot
copy config\personalization.example.yaml config\personalization.yaml
```

4. Edit `config/personalization.yaml` with:
- private user profile details
- prompt style and digest preferences
- stop words and similar tuning rules

5. Install dependencies and run:

```bash
uv sync
uv run bot.py
```

## Data Layout

- `data/data.json` stores channels, focus, selected model, and lightweight interaction state.
- `data/digests_history.json` stores generated digest history.
- `config/personalization.yaml` stores private profile context and prompt tuning.

## Security Rules

- Keep secrets only in `.env` or deployment secrets.
- Keep sensitive or flexible prompt/profile data only in `config/personalization.yaml`.
- Commit only `config/personalization.example.yaml`.
- Do not commit runtime state JSON files.

## Tests

Offline integration checks:

```bash
uv run test_ai_integration.py
```

Smoke test:

```bash
uv run test_smoke.py
```
