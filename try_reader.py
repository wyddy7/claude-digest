"""
Manual reader-layer check (local, on-demand). Runs the digest pipeline ONCE
with a chosen read_mode against the live channels (plus an extra channel,
default hacker_news_feed), prints the cost_summary + digest to the terminal,
and sends the digest to your Telegram chat.

It does NOT write to digest history (read-only db wrapper) and does NOT start
polling, so it never conflicts with the running homelab bot. dedup is disabled
here so re-runs always re-fetch (repeatable manual testing); production runs on
homelab keep dedup on.

Usage (from digest_bot/):
    uv run python try_reader.py                 # read_mode=extract, +hacker_news_feed, send to chat
    uv run python try_reader.py off             # baseline (no reader), for comparison
    uv run python try_reader.py extract --no-send

Local note: the homelab HTTPS_PROXY is IP-bound and unreachable from a laptop,
so this script drops *_PROXY env vars and connects directly (Supabase,
OpenRouter, t.me and Telegram are all directly reachable from local).
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
for _p in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
    os.environ.pop(_p, None)

import httpx

import db
from agent import run_digest_pipeline
from personalization import load_personalization
from pipeline_config import build_pipeline_config, make_openrouter_client

EXTRA_CHANNEL = "hacker_news_feed"


class _ReadOnlyDB:
    """Proxies reads to the real db; swallows writes so the test never pollutes
    the digests history table."""

    def __init__(self, data):
        self._data = data

    async def load(self):
        return self._data

    async def load_history(self, limit=0):
        return await db.load_history(limit)

    async def append_to_history(self, *a, **k):
        print("[try_reader] append_to_history skipped (read-only test)")


async def main():
    read_mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "extract"
    send = "--no-send" not in sys.argv

    await db.init_supabase(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    data = await db.load()
    if EXTRA_CHANNEL and EXTRA_CHANNEL not in data.get("channels", []):
        data["channels"] = list(data.get("channels", [])) + [EXTRA_CHANNEL]
    print(f"[try_reader] read_mode={read_mode} channels={data['channels']}")

    cfg_yaml = load_personalization()
    config = build_pipeline_config(data, cfg_yaml, read_mode=read_mode)
    config.dedup_enabled = False  # repeatable manual runs — always re-fetch

    client = make_openrouter_client(os.getenv("OPENROUTER_KEY"))

    async def _status(t):
        print("  …", t)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as fetcher:
        result = await run_digest_pipeline(
            config, db_module=_ReadOnlyDB(data), llm_client=client,
            fetcher=fetcher, on_status=_status,
        )

    print("\n===== COST SUMMARY =====")
    print(result["cost_summary"])
    print("\n===== DIGEST (HTML) =====")
    print(result["digest_html"])
    if result.get("personal_html"):
        print("\n===== PERSONAL =====")
        print(result["personal_html"])

    if send:
        from telegram import Bot
        chat_id = int(os.getenv("CHAT_ID", "0"))
        text = f"🧪 <b>TEST read_mode={read_mode}</b>\n\n{result['digest_html']}"[:4096]
        async with Bot(os.getenv("BOT_TOKEN")) as bot:
            await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
        print(f"\n[try_reader] sent digest to chat {chat_id}")

    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
