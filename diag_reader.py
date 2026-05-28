"""Diagnostic: per-channel distribution of scraped posts, external_urls and
triage selections. Answers "why does extract-mode skew the digest toward one
channel". Scrape + ONE triage call only — no digest generation, no chat send."""
import asyncio
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
for _p in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
    os.environ.pop(_p, None)

import db
from scraper import scrape_channel
from reader import triage_links
from personalization import load_personalization
from pipeline_config import build_pipeline_config, make_openrouter_client


async def main():
    await db.init_supabase(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    data = await db.load()
    chans = list(data.get("channels", []))
    if "hacker_news_feed" not in chans:
        chans.append("hacker_news_feed")

    results = await asyncio.gather(*[scrape_channel(c) for c in chans], return_exceptions=True)
    posts = []
    per_posts, per_links, per_with_links = Counter(), Counter(), Counter()
    chan_of = []
    for c, r in zip(chans, results):
        if isinstance(r, Exception):
            print(f"  {c}: SCRAPE ERROR {r}")
            continue
        for p in r:
            posts.append(p)
            chan_of.append(c)
        per_posts[c] = len(r)
        per_links[c] = sum(len(p.get("external_urls", [])) for p in r)
        per_with_links[c] = sum(1 for p in r if p.get("external_urls"))

    print("\n=== SCRAPE: per-channel posts / posts-with-links / total external_urls ===")
    for c in chans:
        print(f"  {c:22} posts={per_posts[c]:3}  with_links={per_with_links[c]:3}  links={per_links[c]:3}")
    print(f"  TOTAL posts={len(posts)}")

    cfg = build_pipeline_config(data, load_personalization(), read_mode="extract")
    client = make_openrouter_client(os.getenv("OPENROUTER_KEY"))
    sel = await triage_links(posts, client=client, model=cfg.models["triage"].model_id)

    sel_links, sel_posts = Counter(), Counter()
    for pid, urls in sel.items():
        ch = posts[int(pid)].get("channel", "?")
        sel_links[ch] += len(urls)
        sel_posts[ch] += 1
    print(f"\n=== TRIAGE: selected {sum(sel_links.values())} links across {len(sel)} posts ===")
    for c in chans:
        print(f"  {c:22} posts_selected={sel_posts[c]:3}  links_selected={sel_links[c]:3}")
    print(f"\n  per_channel_link_cap={cfg.per_channel_link_cap}  (links beyond this per channel get skipped)")

    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
