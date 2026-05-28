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

from ai import _parse_llm_json

logger = logging.getLogger(__name__)

ENGINE = "grade_a"

TRIAGE_MAX_TOKENS = 500
EXTRACT_CHAR_BUDGET = 4000  # per-article cap folded into the digest prompt
FETCH_TIMEOUT = 15

# Hosts that wrap a canonical article behind a redirect page. httpx follows
# HTTP redirects transparently for every URL; this set marks wrappers that
# additionally need canonical-link extraction from the fetched HTML.
_WRAPPER_HOSTS = {"readhacker.news"}


async def triage_links(posts: list[dict], *, client, model: str) -> dict[str, list[str]]:
    """One batched LLM call -> {post_index: [urls worth opening]}.

    The result is intersected with each post's external_urls (provenance): the
    model can never introduce a URL that did not appear in a scraped post.
    """
    indexed = [(str(i), p) for i, p in enumerate(posts) if p.get("external_urls")]
    if not indexed:
        return {}

    lines = []
    for pid, p in indexed:
        title = (p.get("text") or "")[:200].replace("\n", " ")
        url_list = "\n".join(f"  - {u}" for u in p["external_urls"])
        lines.append(f"POST {pid} (канал {p.get('channel', '?')}): {title}\nСсылки:\n{url_list}")

    system = (
        "Ты — редактор технического дайджеста. Для каждого поста реши, какие "
        "внешние ссылки стоит открыть и прочитать, чтобы понять суть. Многие "
        "посты — это агрегаторы: только заголовок + ссылка, а реальный контент "
        "лежит за ссылкой, поэтому такие ссылки почти всегда стоит открыть. "
        "Выбирай ТОЛЬКО ссылки из предложенного списка — не придумывай новых. "
        "Если пост самодостаточен и ссылка ничего не добавит — верни для него "
        "пустой список.\n\n"
        'Ответь ТОЛЬКО валидным JSON по схеме: '
        '{"selections": [{"post_id": "0", "urls": ["https://..."]}]}'
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
        data = _parse_llm_json(resp.choices[0].message.content)
    except Exception as e:
        logger.warning(f"[reader] triage failed, opening nothing: {e}")
        return {}

    allow = {pid: set(p["external_urls"]) for pid, p in indexed}
    out: dict[str, list[str]] = {}
    for sel in data.get("selections", []):
        pid = str(sel.get("post_id", ""))
        if pid not in allow:
            continue
        # Provenance intersect — drop any URL the model invented.
        chosen = [u for u in sel.get("urls", []) if u in allow[pid]]
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
        logger.warning(f"[reader] extract failed: {e}")
        text = ""
    return text.strip()[:EXTRACT_CHAR_BUDGET]


async def read_posts(posts: list[dict], *, config, client, fetcher) -> dict:
    """Orchestrate the Grade-A reader. Mutates posts in place, attaching
    post["read_content"] = [{url, final_url, text, ok}] for triaged links.

    Returns stats: {attempted, ok, urls_skipped_dedup, urls_skipped_cap}.
    Dedup + per-channel cap filtering is layered in by P5.
    """
    stats = {"attempted": 0, "ok": 0, "urls_skipped_dedup": 0, "urls_skipped_cap": 0}

    selections = await triage_links(posts, client=client, model=config.models["triage"].model_id)
    if not selections:
        return stats

    for pid, urls in selections.items():
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
                read_content.append({"url": url, "final_url": final_url, "text": text, "ok": ok})
            except Exception as e:
                logger.warning(f"[reader] fetch/extract failed for {url}: {e}")
                read_content.append({"url": url, "final_url": url, "text": "", "ok": False})
        post["read_content"] = read_content

    logger.info(f"[reader] {ENGINE}: {stats}")
    return stats
