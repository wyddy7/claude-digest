# digest_bot — project agent contract

> Project-local instructions for AI coding agents. Read before changing
> code in this repo.

## Stack lock

These are fixed — do not introduce alternatives without an explicit reason:

- `python-telegram-bot[job-queue]==21.6` — Telegram client (PTB).
- `deepagents>=0.1.0` (installed: 0.5.2) over `langgraph>=0.4.0` — chat agent only.
- `langchain-openai>=0.3.0` for `ChatOpenAI` pointed at OpenRouter.
- `openai>=1.54.5` (`AsyncOpenAI`) for direct OpenRouter calls in `ai.py`.
- `supabase>=2.10.0` (`create_async_client`) — sole DB layer.
- `httpx`, `beautifulsoup4` — scraper.

**Banned:**

- `psycopg`, `asyncpg`, `sqlalchemy` — proxy/event-loop deadlocks under
  PTB's `run_polling()` when `HTTPS_PROXY` is active.
- `response_format={"type": "json_object"}` for Anthropic models via
  OpenRouter — returns empty content. JSON is enforced via prompt text
  + `_parse_llm_json()`.
- Wrapping `run_digest_pipeline` in any agent framework. The digest
  pipeline is and stays a plain async function — adding tool-selection
  LLM calls to a deterministic 5-step flow is regression by design.
  - **Grade A reader is ALLOWED** (`read_mode=extract`): one deterministic
    triage LLM call + a fixed fetch→resolve-1-hop→trafilatura-extract
    sequence. It is a single bounded decision, not a tool-selection loop, so
    it stays on the allowed side of this invariant.
  - **Grade B is BANNED inside `run_digest_pipeline`** (`read_mode=agentic`):
    an iterative, model-driven fetch→read→fetch loop. It must live in a
    separate, explicitly flagged module. The pipeline raises
    `NotImplementedError` for `read_mode=agentic` rather than silently
    degrading. Do not bolt an agent loop onto the pipeline to implement it.

## Pre-push checklist

Run **before** every push to `master`:

```bash
uv run python test_ai_integration.py    # offline, ~5 sec, must be 100% pass
```

Covers schema/render/Dockerfile completeness/scraper/ad-filter/chat
compaction boundary logic. Section [10] specifically protects the chat
context compaction — if you touch `_find_safe_cut` or message reducer
behavior, run it first.

`test_smoke.py` is **not** part of the pre-push gate — requires live
`OPENROUTER_KEY` / `BOT_TOKEN` / Supabase credentials. Run only when
debugging end-to-end behavior locally.

## CI gate (automatic on push to master)

`.github/workflows/test.yml` runs:

1. Docker build (cached layers).
2. Import check (every module imports without crashing).
3. `uv run python test_ai_integration.py`.
4. Optionally `test_smoke.py` if secrets are configured.

`.github/workflows/deploy.yml` deploys after tests pass.

## What cannot be tested locally

- **Real chat behavior** — needs your bot token, real Telegram routing.
  Verify on first real chat session after deploy.
- **Real digest output quality** — runs daily on cron. If a generation
  regresses, the digest is already in the chat; rollback via
  `git revert` + push.
- **Long chat sessions for compaction** — synthetic test in
  `test_ai_integration.py [10]` covers cut-point logic, but the actual
  trigger (>30 messages in a single MemorySaver session) only happens
  in production. Watch for `[chat_agent] compacted N prior messages` in
  container logs after first few days of use.

## Stack-specific traps (cross-link to commit history)

- OpenRouter Anthropic models reject `response_format=json_object`
  (`84838f8`).
- All OpenRouter calls **must** set explicit `max_tokens` — worst-case
  cost estimation otherwise rejects the request with 402 (`c82fc78`).
- `MemorySaver` resets on container restart — thread-id state is
  intentionally ephemeral for this single-user bot (`d60e2dc`).
- `RemoveMessage(id=...)` requires `id` to be present; messages added
  via the `add_messages` reducer get auto-assigned uuid4 ids — so in
  prod state `m.id` is always set, but synthetic test fixtures may
  not. Compaction silently no-ops in that case.
- `aupdate_state(config, {...})` does **not** retrigger middleware —
  safe to call between turns to mutate state.
- Tool-call pairs (`AIMessage(tool_calls=...)` →
  `ToolMessage(tool_call_id=...)`) must never be split.
  `_find_safe_cut` only allows boundaries before a `HumanMessage`.
- `error_handler` filters `NetworkError`/`TimedOut` (without `update`)
  to a single INFO line + blip counter — these are long-poll proxy
  hiccups that PTB auto-retries internally. **Do not** revert to the
  generic `logger.error("Unhandled exception", exc_info=...)` for
  these — it floods logs with tracebacks for normal recovery. Real
  unhandled exceptions still get traceback (`c2d1d9b`).

## Compaction tuning knobs

`agent.py` constants — change if real usage shows under/over-trigger:

| Constant | Default | Effect when raised |
|---|---|---|
| `COMPACT_TRIGGER` | 30 | fewer summarizer calls, larger context allowed |
| `COMPACT_TARGET` | 10 | longer tail kept verbatim |
| `COMPACT_SLACK` | 5 | wider window to find a HumanMessage boundary |

Compaction cost: ~$0.001/run on `deepseek/deepseek-chat`. Trivial.
Compaction failure is non-fatal — chat continues with un-compacted state.

## Secrets hygiene

`config/personalization.yaml` and `.env` are gitignored. Do not commit
real values for `BOT_TOKEN`, `CHAT_ID`, `OPENROUTER_KEY`, `SUPABASE_*`,
`HTTPS_PROXY`. Only the `*.example.yaml` template lives in the repo.

## License

MIT — see `LICENSE`.
