"""
Smoke tests - uv run test_smoke.py
"""
import asyncio
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

CHAT_ID = int(os.getenv("CHAT_ID"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")


async def test_scraper():
    print("scraper: cryptoEssay...", end=" ")
    from scraper import scrape_channel

    posts = await scrape_channel("cryptoEssay", hours_back=72)
    assert isinstance(posts, list)
    imgs = [p for p in posts if p.get("image_bytes")]
    print(f"OK ({len(posts)} posts, {len(imgs)} with images)")
    return posts


async def test_ai_digest(posts):
    print("ai.generate_digest...", end=" ")
    from ai import generate_digest

    data = {
        "current_focus": "",
        "model": "anthropic/claude-3.5-haiku",
        "openrouter_key": OPENROUTER_KEY,
        "interaction_history": [],
    }
    sample = posts[:3] if posts else [{"channel": "test", "text": "Test post about AI.", "link": "https://t.me/test/1"}]
    digest_html, personal_html, stats_html = await generate_digest(sample, data)
    assert not digest_html.startswith("Ошибка"), f"AI error: {digest_html[:200]}"
    assert len(digest_html) > 50
    has_links = "t.me" in digest_html or "http" in digest_html
    print(
        f"OK ({len(digest_html)} chars, links={'YES' if has_links else 'MISSING'}, "
        f"personal={'YES' if personal_html else 'NO'}, stats={'YES' if stats_html else 'NO'})"
    )
    print(f"  Preview: {digest_html[:180]}...")
    return digest_html


async def test_filter_images(posts, digest):
    print("ai.filter_images...", end=" ")
    from ai import filter_images

    raw = [p["image_bytes"] for p in posts if p.get("image_bytes")]
    if not raw:
        print("SKIP (no images in recent posts)")
        return []
    approved = await filter_images(raw, digest, OPENROUTER_KEY)
    print(f"OK ({len(approved)}/{len(raw)} approved)")
    return approved


async def test_bot_startup():
    print("bot startup check...", end=" ")
    import subprocess
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-c", "import bot; print('imports ok')"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent),
    )
    assert result.returncode == 0, result.stderr[:200]
    print("OK")


async def test_scheduler_startup():
    print("scheduler startup check...", end=" ")
    import queue
    import subprocess
    import threading
    import time
    from pathlib import Path

    proc = subprocess.Popen(
        [sys.executable, "scheduler.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(Path(__file__).parent),
    )
    output_q: queue.Queue = queue.Queue()

    def _reader(stream, q):
        for line in stream:
            q.put(line)
        q.put(None)

    t = threading.Thread(target=_reader, args=(proc.stdout, output_q), daemon=True)
    t.start()

    started = False
    deadline = time.monotonic() + 10
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = output_q.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            if "Scheduler started" in line:
                started = True
                break
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    assert started, "Scheduler did not emit 'Scheduler started' within 10s"
    print("OK")


async def test_send_images(approved_images):
    print("telegram.send_media_group...", end=" ")
    from io import BytesIO

    from telegram import Bot, InputMediaPhoto

    if not approved_images:
        print("SKIP (no approved images)")
        return
    bot = Bot(token=BOT_TOKEN)
    media = [InputMediaPhoto(BytesIO(b)) for b in approved_images[:3]]
    await bot.send_media_group(CHAT_ID, media)
    print(f"OK ({len(media)} images sent)")


async def test_telegram():
    print("telegram.send_message...", end=" ")
    from telegram import Bot

    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(CHAT_ID, "Smoke test done!")
    print("OK")


async def main():
    print("=" * 50)
    print("SMOKE TESTS")
    print("=" * 50)
    errors = []
    posts, digest, approved = [], "", []

    for label, coro in [
        ("scraper", lambda: test_scraper()),
        ("telegram", lambda: test_telegram()),
        ("bot_startup", lambda: test_bot_startup()),
        ("scheduler_startup", lambda: test_scheduler_startup()),
    ]:
        try:
            if label == "scraper":
                posts = await coro()
            else:
                await coro()
        except Exception as e:
            print(f"FAIL: {e}")
            errors.append(f"{label}: {e}")

    try:
        digest = await test_ai_digest(posts)
    except Exception as e:
        print(f"FAIL: {e}")
        errors.append(f"ai_digest: {e}")

    try:
        approved = await test_filter_images(posts, digest)
    except Exception as e:
        print(f"FAIL: {e}")
        errors.append(f"filter_images: {e}")

    try:
        await test_send_images(approved)
    except Exception as e:
        print(f"FAIL: {e}")
        errors.append(f"send_images: {e}")

    print("=" * 50)
    if errors:
        print(f"ERRORS ({len(errors)}):")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    print("All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
