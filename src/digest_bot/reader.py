"""
Reader layer (Grade A): deterministic deep-read behind post links.

ONE cheap "triage" LLM call decides which of a post's external_urls are worth
opening; the chosen links are fetched (1 content-hop, wrapper redirects
resolved) and extracted with trafilatura. There is NO open-ended agent loop —
this is a single bounded decision, which keeps it on the allowed side of the
run_digest_pipeline "no agent framework" invariant (see digest_bot/CLAUDE.md).

A future Grade B (iterative model-driven fetch->read->fetch loop) MUST live in
a separate, explicitly flagged module behind read_mode=agentic — never bolted
onto this engine.
"""
import logging
from urllib.parse import urlparse

import trafilatura
from bs4 import BeautifulSoup

from digest_bot.ai import _parse_llm_json, record_usage, strip_feed_engagement

logger = logging.getLogger(__name__)

ENGINE = "grade_a"

TRIAGE_MAX_TOKENS = 800
EXTRACT_CHAR_BUDGET = 1500  # per-article cap folded into the digest prompt — the
# main cost lever: full bodies × many links blow up the digest prompt. The lead
# ~300 words carry enough gist for digest bullets.
FETCH_TIMEOUT = 15

# Hosts that wrap a canonical article behind a redirect page. httpx follows
# HTTP redirects transparently for every URL; this set marks wrappers that
# additionally need canonical-link extraction from the fetched HTML.
_WRAPPER_HOSTS = {"readhacker.news"}


async def triage_links(posts: list[dict], *, client, model: str, usage_log=None) -> dict[str, list[str]]:
    """One batched LLM call -> {post_index: [urls worth opening]}.

    The result is intersected with each post's external_urls (provenance): the
    model can never introduce a URL that did not appear in a scraped post.
    """
    indexed = [(str(i), p) for i, p in enumerate(posts) if p.get("external_urls")]
    if not indexed:
        return {}

    lines = []
    for pid, p in indexed:
        title = strip_feed_engagement(p.get("text") or "")[:200].replace("\n", " ")
        link_lines = "\n".join(f"    [{j}] {u}" for j, u in enumerate(p["external_urls"]))
        lines.append(f"POST {pid} (канал {p.get('channel', '?')}): {title}\nLINKS:\n{link_lines}")

    system = (
        "Ты — редактор технического дайджеста. Для каждого поста реши, какие "
        "внешние ссылки стоит открыть и прочитать, чтобы понять суть. Многие "
        "посты — это агрегаторы: только заголовок + ссылка, а реальный контент "
        "лежит за ссылкой, поэтому такие ссылки почти всегда стоит открыть. "
        "Возвращай НОМЕРА ссылок (индексы из списка LINKS), а не сами URL. "
        "Только номера из списка. Если пост самодостаточен и ссылка ничего не "
        "добавит — верни для него пустой список.\n\n"
        'Ответь ТОЛЬКО валидным JSON по схеме: '
        '{"selections": [{"post_id": "0", "links": [0, 1]}]}'
    )
    user = "Посты:\n\n" + "\n\n---\n\n".join(lines)

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=TRIAGE_MAX_TOKENS,
            temperature=0.0,
        )
        record_usage(usage_log, "triage", model, resp)
        data = _parse_llm_json(resp.choices[0].message.content)
    except Exception as e:
        logger.warning("[reader] triage failed, opening nothing: %s: %s", type(e).__name__, e)
        return {}

    by_pid = {pid: p["external_urls"] for pid, p in indexed}
    out: dict[str, list[str]] = {}
    for sel in data.get("selections", []):
        pid = str(sel.get("post_id", ""))
        urls = by_pid.get(pid)
        if urls is None:
            continue
        # Index-based provenance: only in-range indices map to real URLs; the
        # model cannot introduce a URL it wasn't shown.
        chosen = []
        for idx in sel.get("links", []):
            if isinstance(idx, bool):
                continue
            if isinstance(idx, int) and 0 <= idx < len(urls):
                chosen.append(urls[idx])
        if chosen:
            out[pid] = chosen
    return out


def _canonical_from_html(html: str) -> str | None:
    """Best-effort canonical link for wrapper pages: <link rel=canonical> or a
    meta-refresh target. Returns None if neither is present."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    link = soup.find("link", rel="canonical", href=True)
    if link and link["href"].startswith(("http://", "https://")):
        return link["href"]
    meta = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "refresh"})
    if meta and meta.get("content"):
        content = meta["content"]
        if "url=" in content.lower():
            target = content[content.lower().index("url=") + 4:].strip().strip("'\"")
            if target.startswith(("http://", "https://")):
                return target
    return None


async def resolve_one_hop(url: str, *, fetcher) -> tuple[str, str]:
    """Fetch a URL following HTTP redirects -> (final_url, html). For known
    wrapper hosts, extract the canonical link and fetch it once more. Hard stop
    at 1 content hop (no recursive crawling)."""
    resp = await fetcher.get(url, follow_redirects=True, timeout=FETCH_TIMEOUT)
    html = resp.text
    final_url = str(resp.url)
    host = (urlparse(final_url).netloc or "").lower()
    if any(w in host for w in _WRAPPER_HOSTS):
        canonical = _canonical_from_html(html)
        if canonical and canonical != final_url:
            resp2 = await fetcher.get(canonical, follow_redirects=True, timeout=FETCH_TIMEOUT)
            return str(resp2.url), resp2.text
    return final_url, html


def extract_content(html: str) -> str:
    """Deterministic main-content extraction (no network). Truncated to budget."""
    if not html:
        return ""
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception as e:
        logger.warning("[reader] extract failed: %s: %s", type(e).__name__, e)
        text = ""
    return text.strip()[:EXTRACT_CHAR_BUDGET]


def _apply_cap(selections: dict, posts: list[dict], cap: int, stats: dict) -> dict:
    """Enforce a per-channel link cap across the triaged selections (cost
    guardrail). Truncated links are counted in stats['urls_skipped_cap']."""
    from collections import defaultdict

    per_channel = defaultdict(int)
    capped: dict[str, list[str]] = {}
    for pid, urls in selections.items():
        channel = posts[int(pid)].get("channel", "?")
        kept = []
        for u in urls:
            if per_channel[channel] >= cap:
                stats["urls_skipped_cap"] += 1
                continue
            per_channel[channel] += 1
            kept.append(u)
        if kept:
            capped[pid] = kept
    return capped


async def _apply_dedup(capped: dict, *, db_module, window_days: int, stats: dict) -> dict:
    """Drop links already fetched within the dedup window (cost guardrail).
    Single batched query; failures are non-fatal (fetch proceeds)."""
    all_urls = [u for urls in capped.values() for u in urls]
    if not all_urls:
        return capped
    url_to_hash = {u: db_module._url_hash(u) for u in all_urls}
    try:
        seen = await db_module.get_seen_urls(list(url_to_hash.values()), window_days)
    except Exception as e:
        logger.warning("[reader] dedup lookup failed, proceeding without it: %s: %s", type(e).__name__, e)
        return capped
    fresh: dict[str, list[str]] = {}
    for pid, urls in capped.items():
        kept = []
        for u in urls:
            if url_to_hash[u] in seen:
                stats["urls_skipped_dedup"] += 1
            else:
                kept.append(u)
        if kept:
            fresh[pid] = kept
    return fresh


async def read_posts(posts: list[dict], *, config, client, fetcher, db_module=None, usage_log=None) -> dict:
    """Orchestrate the Grade-A reader. Mutates posts in place, attaching
    post["read_content"] = [{url, final_url, text, ok}] for triaged links.

    Pipeline: triage (provenance) → per-channel cap → dedup → fetch (1 hop) →
    extract. db_module (injected) backs the dedup cache; omit it to disable
    dedup (offline tests, or dedup_enabled=False).

    Returns stats: {attempted, ok, urls_skipped_dedup, urls_skipped_cap}.
    """
    stats = {"attempted": 0, "ok": 0, "urls_skipped_dedup": 0, "urls_skipped_cap": 0}

    selections = await triage_links(
        posts, client=client, model=config.models["triage"].model_id, usage_log=usage_log
    )
    if not selections:
        return stats

    capped = _apply_cap(selections, posts, config.per_channel_link_cap, stats)

    if config.dedup_enabled and db_module is not None:
        capped = await _apply_dedup(
            capped, db_module=db_module, window_days=config.dedup_window_days, stats=stats
        )

    fetched_ok: list[str] = []
    for pid, urls in capped.items():
        post = posts[int(pid)]
        read_content = []
        for url in urls:
            stats["attempted"] += 1
            try:
                final_url, html = await resolve_one_hop(url, fetcher=fetcher)
                text = extract_content(html)
                ok = bool(text)
                if ok:
                    stats["ok"] += 1
                    fetched_ok.append(url)
                read_content.append({"url": url, "final_url": final_url, "text": text, "ok": ok})
            except Exception as e:
                logger.warning("[reader] fetch/extract failed for %s: %s: %s", url, type(e).__name__, e)
                read_content.append({"url": url, "final_url": url, "text": "", "ok": False})
        post["read_content"] = read_content

    if config.dedup_enabled and db_module is not None and fetched_ok:
        try:
            await db_module.mark_urls_fetched(fetched_ok)
        except Exception as e:
            logger.warning("[reader] mark_urls_fetched failed (non-fatal): %s: %s", type(e).__name__, e)

    logger.info("[reader] %s: %s", ENGINE, stats)
    return stats
