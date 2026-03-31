import re
import httpx
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}


def _extract_image_url(msg) -> str:
    """Extract photo URL from a message block (background-image in photo_wrap)."""
    for tag in msg.find_all(True, style=re.compile("background-image")):
        classes = tag.get("class", [])
        if "tgme_widget_message_photo_wrap" in classes:
            style = tag.get("style", "")
            m = re.search(r"background-image:url\(['\"]?([^'\")\s]+)['\"]?\)", style)
            if m:
                return m.group(1)
    return ""


async def scrape_channel(channel: str, hours_back: int = 26) -> list[dict]:
    url = f"https://t.me/s/{channel}"
    try:
        # Single session: page fetch + image downloads share cookies (required for telesco.pe CDN)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(f"[{channel}] HTTP {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            posts = []
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
                        if post_time < cutoff:
                            continue
                    except Exception:
                        pass

                link = ""
                link_tag = msg.find("a", class_="tgme_widget_message_date")
                if link_tag:
                    link = link_tag.get("href", "")

                image_url = _extract_image_url(msg)
                image_bytes = None

                # Download image in the same session (shares stel_ssid cookie)
                if image_url:
                    try:
                        img_resp = await client.get(image_url)
                        ct = img_resp.headers.get("content-type", "")
                        if img_resp.status_code == 200 and "image" in ct and len(img_resp.content) > 500:
                            image_bytes = img_resp.content
                            logger.debug(f"[{channel}] image {len(image_bytes)} bytes")
                        else:
                            logger.debug(f"[{channel}] image skip: {img_resp.status_code} {ct}")
                    except Exception as e:
                        logger.warning(f"[{channel}] image fetch error: {e}")

                posts.append({
                    "channel": channel,
                    "text": text[:1200],
                    "link": link,
                    "image_url": image_url,
                    "image_bytes": image_bytes,
                    "time": post_time.isoformat() if post_time else "",
                })

    except Exception as e:
        logger.error(f"[{channel}] scrape error: {e}")
        return []

    return posts


async def scrape_all(channels: list[str]) -> list[dict]:
    all_posts = []
    for ch in channels:
        posts = await scrape_channel(ch)
        imgs = sum(1 for p in posts if p.get("image_bytes"))
        all_posts.extend(posts)
        logger.info(f"[{ch}] {len(posts)} posts, {imgs} with images")
    return all_posts
