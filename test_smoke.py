"""
Smoke tests — uv run test_smoke.py
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
    print("ai.generate_digest (pydantic-ai)...", end=" ")
    from ai import generate_digest
    data = {
        "description": "Test user",
        "current_focus": "",
        "model": "anthropic/claude-3.5-haiku",
        "openrouter_key": OPENROUTER_KEY,
        "interaction_history": [],
    }
    sample = posts[:3] if posts else [{"channel": "test", "text": "Test post about AI.", "link": "https://t.me/test/1"}]
    result = await generate_digest(sample, data)
    assert not result.startswith("Ошибка"), f"AI error: {result[:200]}"
    assert len(result) > 50
    # Check that links are present (pydantic-ai guarantees structured output with URLs)
    has_links = "t.me" in result or "http" in result
    print(f"OK ({len(result)} chars, links={'YES' if has_links else 'MISSING'})")
    print(f"  Preview: {result[:180]}...")
    return result


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
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-c", "import bot; print('imports ok')"],
        capture_output=True, text=True,
        cwd="D:/D/Papka/Личное/claude/digest_bot"
    )
    assert result.returncode == 0, result.stderr[:200]
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
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
