"""Guards against popularity-metric noise in the digest.

Measured over 162 prod digests (2026-08-16): 70 bullets/tail lines out of 3231
carried Hacker News popularity stats ("Score 151+ за 4 часа на HN"), 28 digests
affected. The number is not even a measurement — hacker_news_feed appends its own
publication threshold to the title, so 63 leaked values ranged 150..225 with a
median of 153 and 21 of them exactly 150/151.

Two defences, one tested here (deterministic), one in the prompt yamls:
the suffix is stripped from the post text before it reaches the model, so the
model cannot quote a number it was never shown. Benchmark numbers in prose must
survive — they are substantive facts, not popularity.
"""

from digest_bot.ai import _format_posts, strip_feed_engagement

# Verified live against t.me/s/hacker_news_feed on 2026-08-16.
REAL_FEED_POSTS = [
    "AI in drug discovery – what it is, where we stand and the path forward "
    "(Score: 150+ in 17 hours)\n\nLink: https://readhacker.news/s/72NVR",
    "Claude: System Prompts (🔥 Score: 160+ in 2 hours)\n\n"
    "Link: https://readhacker.news/s/72QUn",
    "Firefox for iOS now has a native adblocker (🔥 Score: 155+ in 2 hours)",
    "Patterns and problems in emerging multi-agent systems (Score: 152+ in 12 hours)",
]


def test_strips_real_feed_suffixes():
    for text in REAL_FEED_POSTS:
        cleaned = strip_feed_engagement(text)
        assert "Score" not in cleaned, cleaned
        assert "150+" not in cleaned and "160+" not in cleaned


def test_keeps_the_title_intact():
    cleaned = strip_feed_engagement("Claude: System Prompts (🔥 Score: 160+ in 2 hours)")
    assert cleaned == "Claude: System Prompts"


def test_handles_variants():
    variants = [
        "Some title (Score 151+ in 4 hours)",
        "Some title [Score: 151+ in 4 hours]",
        "Some title (score: 151 in 3 hours)",
        "Some title (163+ points in 1 day)",
        "Some title (163+ points)",
        "Some title (upvotes: 200+)",
        "Заголовок (Score: 150+ за 3 часа)",
    ]
    for text in variants:
        assert strip_feed_engagement(text).strip() == (
            "Заголовок" if text.startswith("Заголовок") else "Some title"
        ), text


def test_benchmark_numbers_survive():
    """Substantive metrics must never be touched — only the bracketed feed suffix."""
    keep = [
        "Grok 4.6 набрал 61 балл на AA Intelligence Index",
        "GPT-5.6 Sol в Terminal Bench 2.1 с режимом Ultra набрал 91.9%",
        "Цена $0.04 против $0.21 (gpt-2-image high)",
        "Модель решила 48% задач на FrontierMath Tier 4",
        "Контекст 1M токенов (200k на выходе)",
        "3-е место на LMArena (1280 очков), позади gpt-image-2 (1385)",
        "Итоговый балл (1385 points) на арене",
        # No "+" and no rate clause → a benchmark reading, not a feed threshold.
        "Модель на LMArena (score: 1280)",
        "Результат (points: 340) в внутреннем прогоне",
    ]
    for text in keep:
        assert strip_feed_engagement(text) == text


def test_format_posts_hides_the_score_from_the_prompt():
    posts = [
        {
            "channel": "hacker_news_feed",
            "text": REAL_FEED_POSTS[1],
            "link": "https://t.me/hacker_news_feed/130921",
            "time": "2026-08-16T08:00:00+00:00",
        }
    ]
    rendered = _format_posts(posts)
    assert "Score" not in rendered
    assert "Claude: System Prompts" in rendered


def test_prompt_rule_present_in_every_personalization_template():
    """The deterministic strip only covers the source suffix; the model's own
    editorializing ("#1 on HN", "went viral") is covered by a prompt rule that
    must exist in every template, owner-private and neutral alike."""
    import yaml

    from digest_bot.paths import CONFIG_DIR

    # Only the committed templates: personalization.yaml and the locale variants
    # (en) are gitignored, so CI never sees them.
    for name in ("personalization.default.yaml", "personalization.example.yaml"):
        path = CONFIG_DIR / name
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        rules = " ".join(cfg["prompt"]["hard_rules"]).lower()
        assert "popularity metrics" in rules, name
        assert "upvote" in rules, name
