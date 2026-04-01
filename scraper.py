import re
import httpx
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}

# Messages within this window get grouped into one thread
_THREAD_GAP_HOURS = 3


def _extract_image_url(msg) -> str:
    for tag in msg.find_all(True, style=re.compile("background-image")):
        classes = tag.get("class", [])
        if "tgme_widget_message_photo_wrap" in classes:
            style = tag.get("style", "")
            m = re.search(r"background-image:url\(['\"]?([^'\")\s]+)['\"]?\)", style)
            if m:
                return m.group(1)
    return ""


def _extract_author(msg) -> str:
    """Extract sender name — present in group chats, absent in channels."""
    for cls in ("tgme_widget_message_author_name", "tgme_widget_message_from_author"):
        tag = msg.find(["a", "span"], class_=cls)
        if tag:
            return tag.get_text(strip=True)
    return ""


def _group_into_threads(raw: list[dict]) -> list[dict]:
    """
    For group chats (multiple distinct authors): merge messages that fall
    within _THREAD_GAP_HOURS of each other into one combined post.
    The AI then sees the full discussion and can judge by meaning.

    For channels (single author / no author): return as-is.
    """
    authors = {p["author"] for p in raw if p["author"]}
    if len(authors) <= 1:
        return raw  # regular channel, no grouping needed

    threads: list[list[dict]] = []
    current: list[dict] = []

    for post in raw:
        if not current:
            current = [post]
            continue
        try:
            last_t = datetime.fromisoformat(current[-1]["time"])
            this_t = datetime.fromisoformat(post["time"])
            gap_h = (this_t - last_t).total_seconds() / 3600
        except Exception:
            gap_h = float("inf")  # can't parse time — treat as separate thread

        if gap_h <= _THREAD_GAP_HOURS:
            current.append(post)
        else:
            threads.append(current)
            current = [post]

    if current:
        threads.append(current)

    result = []
    for thread in threads:
        if len(thread) == 1:
            result.append(thread[0])
            continue

        # Combine thread messages: label each by author
        parts = []
        for p in thread:
            label = f"[{p['author']}]" if p["author"] else "[?]"
            parts.append(f"{label}: {p['text']}")

        combined = "\n\n".join(parts)
        # Use the first message's metadata (link, time, image)
        merged = dict(thread[0])
        merged["text"] = combined[:2400]
        merged["is_thread"] = True
        merged["thread_size"] = len(thread)
        result.append(merged)
        logger.debug(f"[{thread[0]['channel']}] merged {len(thread)} msgs into thread")

    return result


async def scrape_channel(channel: str, hours_back: int = 26) -> list[dict]:
    url = f"https://t.me/s/{channel}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(f"[{channel}] HTTP {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            raw: list[dict] = []
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

            for msg in soup.find_all("div", class_="tgme_widget_message"):
                text_div = msg.find("div", class_="tgme_widget_message_text")
                if not text_div:
                    continue
                text = text_div.get_text(separator="\n", strip=True)
                if not text or len(text) < 30:
                    continue

                post_time = None
                time_tag = msg.find("time")
                if time_tag:
                    dt_str = time_tag.get("datetime", "")
                    try:
                        post_time = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    except Exception:
                        pass

                if post_time is None:
                    continue
                if post_time < cutoff:
                    continue

                link = ""
                link_tag = msg.find("a", class_="tgme_widget_message_date")
                if link_tag:
                    link = link_tag.get("href", "")

                author = _extract_author(msg)

                image_url = _extract_image_url(msg)
                image_bytes = None
                if image_url:
                    try:
                        img_resp = await client.get(image_url)
                        ct = img_resp.headers.get("content-type", "")
                        if img_resp.status_code == 200 and "image" in ct and len(img_resp.content) > 500:
                            image_bytes = img_resp.content
                    except Exception as e:
                        logger.warning(f"[{channel}] image fetch error: {e}")

                raw.append({
                    "channel": channel,
                    "author": author,
                    "text": text[:1200],
                    "link": link,
                    "image_url": image_url,
                    "image_bytes": image_bytes,
                    "time": post_time.isoformat(),
                })

    except Exception as e:
        logger.error(f"[{channel}] scrape error: {e}")
        return []

    posts = _group_into_threads(raw)
    imgs = sum(1 for p in posts if p.get("image_bytes"))
    threads = sum(1 for p in posts if p.get("is_thread"))
    logger.info(f"[{channel}] {len(raw)} msgs → {len(posts)} posts ({threads} threads, {imgs} with images)")
    return posts


async def scrape_all(channels: list[str]) -> list[dict]:
    all_posts = []
    for ch in channels:
        posts = await scrape_channel(ch)
        all_posts.extend(posts)
    return all_posts
