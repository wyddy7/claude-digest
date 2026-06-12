# Digest Bot

Multi-tenant Telegram bot that scrapes selected public channels and delivers an
AI-personalized daily digest. Invite-gated onboarding, per-user channels / focus /
model, a Pro trial, and Telegram Stars payments — every user runs through one
shared pipeline with their own settings and limits.

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

## Features

- **Onboarding**: invite gate → 3-step wizard (pick a curated channel set, set a
  focus) → an immediate preview digest. State persists in the DB and survives
  restarts.
- **Per-user everything**: channels, focus, digest model, history, and quotas are
  all keyed per user. Limits come from DB tier defaults, never hard-coded.
- **Surfaces**: 📰 digest · ⚙️ settings (channels / model / focus / auto-reset) ·
  👤 profile · 📚 history (paginated) · 💎 subscription · a chat-with-digest agent
  with per-user memory and user-scoped tools.
- **Subscriptions**: 3-day Pro trial, then Telegram Stars (XTR) payments with an
  idempotent payment ledger. Access is always computed at runtime from
  `pro_until` / `trial_ends_at`.
- **Scheduler**: a daily fan-out (13:00 MSK) delivers each active user their own
  digest; a check-in fan-out (18:00 MSK) nudges engagement.

## Architecture

- `python-telegram-bot` (polling) + a standalone `scheduler.py` process.
- **Supabase** (HTTP API via `supabase-py`) is the only datastore — no local
  state files. Schema is managed by Alembic migrations under `migrations/`.
- The digest pipeline (`agent.run_digest_pipeline`) is a plain deterministic
  async function (scrape → read → ad-filter → generate); per-user inputs ride on
  a `PipelineConfig`.
- The chat agent (`deepagents` + an in-memory checkpointer) is the only
  model-driven loop; its tools are closures scoped to one user.

## Setup

1. Install `uv`.
2. Create `.env`:

```env
BOT_TOKEN=...           # BotFather token
CHAT_ID=...             # owner's numeric Telegram id (seeded as a Pro user at startup)
ADMIN_ID=...            # numeric id allowed to run admin commands (usually = CHAT_ID; 0 disables them)
OPENROUTER_KEY=...      # LLM access (OpenRouter)
SUPABASE_URL=...        # Supabase project URL (HTTP API)
SUPABASE_KEY=...        # service-role key
HTTPS_PROXY=...         # optional, for restricted networks
LOG_LEVEL=INFO          # optional
LOG_DIR=logs            # optional — rotating file logs (gitignored); LOG_TO_FILE=0 to disable
```

3. Apply the schema to your Supabase project (Alembic is not wired to CI — run it
   manually against a dev/prod DB):

```bash
SUPABASE_DB_URL="postgresql+psycopg2://USER:PASS@HOST:5432/postgres" \
  uv run --with alembic --with sqlalchemy --with psycopg2-binary alembic upgrade head
```

4. Optionally edit `config/personalization.yaml` (profile + prompt tuning); it is
   the fallback when a user has no per-user personalization. Commit only
   `config/personalization.example.yaml`.

5. Install dependencies and run:

```bash
uv sync
uv run bot.py          # the bot (polling)
uv run scheduler.py    # the daily digest/check-in scheduler (separate process)
```

The owner (`CHAT_ID`) is backfilled into the `users` table as a Pro user on first
start, and any legacy single-tenant digest history is linked to them — nothing is
lost on the multi-tenant cutover.

## Admin commands

`ADMIN_ID`-gated (silently ignored for everyone else):

- `/grant_trial <tg_id>` — invite a user and grant the 3-day Pro trial.
- `/give_pro <tg_id> <days>` — comp Pro (stacks on any active remainder).
- `/revoke_pro <tg_id>` — clear Pro / trial access.
- `/reset_user <tg_id> [full]` — re-arm onboarding (or `full` to delete the rows).

## Tests

```bash
uv run python test_ai_integration.py   # offline, must be green before pushing
uv run pytest -q                       # offline unit suite
uv run test_smoke.py                   # live (needs real credentials)
```

## License

MIT.
