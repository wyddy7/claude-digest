# Digest Bot

Personal Telegram bot that scrapes selected public channels and generates an AI-personalized digest.

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
