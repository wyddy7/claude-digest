"""
Offline integration test for ai.py - no API calls.
"""
import io
import re
import sys
import traceback
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from bs4 import BeautifulSoup
from scraper import _extract_image_urls

from ai import (
    AdBatchResult,
    DigestResult,
    PostAdLabel,
    SourceBlock,
    _format_posts,
    _to_html_digest,
    _to_html_personal,
    _to_html_stats,
    build_system_prompt,
)
from personalization import load_personalization

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


print("\n[1] DigestResult / SourceBlock schema")
try:
    sources = [
        SourceBlock(
            channel=f"channel_{i}",
            url=f"https://t.me/channel_{i}/1",
            post_date="01.04.2026",
            bullets=[f"Fact {i}.1", f"Fact {i}.2"],
            example=f"Example {i}" if i % 2 == 0 else "",
        )
        for i in range(6)
    ]
    dr = DigestResult(sources=sources, personal=["Personal item 1", "Personal item 2"])
    check("schema_accepts_sources", len(dr.sources) == 6)
    check("schema_personal_field", len(dr.personal) == 2)
    check("sourceblock_bullets", len(dr.sources[0].bullets) == 2)
    check("sourceblock_url_validator", dr.sources[0].url.startswith("https://t.me/"))
except Exception as e:
    FAIL.append("schema")
    print(f"  FAIL  schema: {e}")
    traceback.print_exc()
    dr = None


print("\n[2] personalization config")
try:
    cfg = load_personalization()
    check("config_has_profile", "profile" in cfg)
    check("config_has_prompt", "prompt" in cfg)
    check("config_example_exists", Path("config/personalization.example.yaml").exists())
    check("config_has_template", bool(cfg["prompt"].get("system_template")))
except Exception as e:
    FAIL.append("personalization_config")
    print(f"  FAIL  personalization_config: {e}")
    traceback.print_exc()


print("\n[3] _to_html_digest / _to_html_personal rendering")
try:
    assert dr is not None, "dr fixture not built"
    html_digest = _to_html_digest(dr)
    html_personal = _to_html_personal(dr)
    check("digest_contains_channel_link", 'href="https://t.me/channel_0/1"' in html_digest)
    check("digest_contains_channel_name", "channel_0" in html_digest)
    check("digest_contains_post_date", "01.04.2026" in html_digest)
    check("digest_contains_example", "Example 0" in html_digest)
    check("digest_no_example_for_empty", "Example 1" not in html_digest)
    check("personal_not_none", html_personal is not None)
    check("personal_header", "<b>Лично тебе:</b>" in html_personal)
    check("personal_bullet", "Personal item 1" in html_personal)
    dr_empty = DigestResult(sources=[], personal=[])
    check("personal_none_when_empty", _to_html_personal(dr_empty) is None)
except Exception as e:
    FAIL.append("_to_html")
    print(f"  FAIL  _to_html: {e}")
    traceback.print_exc()


print("\n[4] build_system_prompt: sections and stripping")
try:
    html_digest_text = (
        "<b>Title</b>\n"
        "• <b>Insight</b> <a href='https://t.me/ch/1'>channel</a>\n"
        "  Fact.\n"
        "  <i>Command: pip install x</i>\n"
    )
    recent_digests = [{"date": "2026-03-30", "digest_html": html_digest_text, "is_error": False}]
    user_data = {"current_focus": "AI agents", "interaction_history": ["msg1", "msg2"]}
    prompt = build_system_prompt(user_data, recent_digests)
    prev_section_match = re.search(
        r"ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ.*?(?=\nRECENT INTERACTIONS:|$)",
        prompt,
        re.DOTALL,
    )
    assert prev_section_match, "previous digests section not found"
    prev_section = prev_section_match.group(0)
    has_html_tags = bool(re.search(r"<[^>]+>", prev_section))
    check("html_stripped_from_prev_digests", not has_html_tags)
    check("plain_text_survives_strip", "Insight" in prev_section)
    check("plain_text_command_survives", "pip install x" in prev_section)
    check("contains_style_rules", any(s in prompt for s in ("STYLE RULES:", "СТИЛЬ:")))
    check("contains_source_selection", any(s in prompt for s in ("SOURCE SELECTION:", "ОТБОР ИСТОЧНИКОВ:")))
    check("contains_stop_words", any(s in prompt for s in ("STOP WORDS:", "СТОП-СЛОВА:")))

    # Truncation regression check (chat-context-2): the latest digest must be
    # carried verbatim when its body is longer than the old 600-char cap.
    # Synthetic body: 2000 chars of filler with marker at position ~1100,
    # which the previous code would have chopped.
    long_filler = "ai_newz [03.05.2026]\n" + ("— filler line about models. " * 30)
    marker = "Wispr Flow поднял $80M"
    tail = " (more text continues to push body past the old 600-char cap.)"
    long_body = (long_filler + "\nseeallochnaya [03.05.2026]\n— " + marker + tail).ljust(2000, ".")
    assert long_body.find(marker) > 700, "marker should be past old 600-char cutoff"
    long_recent = [
        {"date": "2026-05-04", "digest_html": "<b>old</b> placeholder", "is_error": False},
        {"date": "2026-05-05", "digest_html": long_body, "is_error": False},
    ]
    long_prompt = build_system_prompt(user_data, long_recent)
    check("latest_digest_preserves_marker_past_700_chars", marker in long_prompt)
except Exception as e:
    FAIL.append("build_system_prompt")
    print(f"  FAIL  build_system_prompt: {e}")
    traceback.print_exc()


print("\n[5] _to_html_stats")
try:
    stats = _to_html_stats(posts_checked=15, channels_count=5, sources_selected=4)
    check("stats_contains_posts_count", "15" in stats)
    check("stats_contains_channels_count", "5" in stats)
    check("stats_contains_sources_count", "4" in stats)
    check("stats_is_italic", stats.startswith("<i>") and stats.endswith("</i>"))
except Exception as e:
    FAIL.append("_to_html_stats")
    print(f"  FAIL  _to_html_stats: {e}")
    traceback.print_exc()


print("\n[6] _format_posts")
try:
    posts = [
        {
            "channel": "test_ch",
            "text": "Short AI post.",
            "link": "https://t.me/test_ch/42",
            "time": "2026-03-31T10:00:00+00:00",
            "is_thread": False,
        },
        {
            "channel": "another_ch",
            "text": "Thread about models.",
            "link": "https://t.me/another_ch/99",
            "time": "2026-03-30T08:30:00+00:00",
            "is_thread": True,
        },
    ]
    formatted = _format_posts(posts)
    check("format_posts_post_label", "ПОСТ: test_ch" in formatted)
    check("format_posts_thread_label", "ТРЕД: another_ch" in formatted)
    check("format_posts_link_field", "https://t.me/test_ch/42" in formatted)
    check("format_posts_separator", "---" in formatted)
    check("format_posts_date_format", "31.03.2026" in formatted)
except Exception as e:
    FAIL.append("_format_posts")
    print(f"  FAIL  _format_posts: {e}")
    traceback.print_exc()


print("\n[7] Dockerfile: all local .py modules are COPYed")
try:
    import ast
    import re as _re

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    # Extract filenames from COPY lines
    copied_files: set[str] = set()
    for line in dockerfile.splitlines():
        line = line.strip()
        if line.startswith("COPY "):
            parts = line.split()
            # last token is destination, everything before is source
            for f in parts[1:-1]:
                copied_files.add(f)

    # Collect all local .py files referenced as imports across copied app modules
    local_modules = {f.stem for f in Path(".").glob("*.py") if not f.name.startswith("test_")}
    app_sources = [f for f in copied_files if f.endswith(".py")]

    missing = []
    for src in app_sources:
        src_path = Path(src)
        if not src_path.exists():
            continue
        source = src_path.read_text(encoding="utf-8", errors="replace")
        for m in _re.finditer(r"^(?:from|import)\s+(\w+)", source, _re.MULTILINE):
            mod = m.group(1)
            if mod in local_modules and f"{mod}.py" not in copied_files:
                missing.append(f"{mod}.py (imported by {src})")

    check("dockerfile_copies_all_local_imports", len(missing) == 0,
          f"missing: {missing}" if missing else "")

    # config/ directory must be copied so example yaml is available as fallback
    has_config_copy = any(
        "config" in p for p in copied_files
    )
    check("dockerfile_copies_config_dir", has_config_copy,
          "add 'COPY config/ config/' to Dockerfile")
except Exception as e:
    FAIL.append("dockerfile_check")
    print(f"  FAIL  dockerfile_check: {e}")
    traceback.print_exc()


print("\n[8] AdBatchResult / PostAdLabel schema")
try:
    lbl_ad = PostAdLabel(index=0, is_ad=True)
    lbl_ok = PostAdLabel(index=1, is_ad=False)
    batch = AdBatchResult(posts=[lbl_ad, lbl_ok])
    check("postadlabel_is_ad_true", lbl_ad.is_ad is True)
    check("postadlabel_is_ad_false", lbl_ok.is_ad is False)
    check("adbatchresult_two_posts", len(batch.posts) == 2)
    check("adbatchresult_index_preserved", batch.posts[0].index == 0 and batch.posts[1].index == 1)

    # Simulate filter_ads keep/drop logic (mirrors the real function)
    posts = [
        {"channel": "ch1", "text": "Реклама курса за 10к"},
        {"channel": "ch2", "text": "Разбор архитектуры AI-native компании с деталями"},
    ]
    labels = {lbl.index: lbl.is_ad for lbl in batch.posts}
    kept = [p for i, p in enumerate(posts) if not labels.get(i, False)]
    check("filter_logic_drops_ad", len(kept) == 1)
    check("filter_logic_keeps_signal", kept[0]["channel"] == "ch2")
except Exception as e:
    FAIL.append("ad_filter_schema")
    print(f"  FAIL  ad_filter_schema: {e}")
    traceback.print_exc()


print("\n[9] _extract_image_urls: multi-photo album")
try:
    # Simulate a message with two photo_wraps (Telegram album)
    html = """
    <div class="tgme_widget_message">
      <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.example.com/photo1.jpg')"></a>
      <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.example.com/photo2.jpg')"></a>
      <i class="emoji" style="background-image:url('https://telegram.org/img/emoji/1.png')"></i>
    </div>
    """
    msg = BeautifulSoup(html, "html.parser").find("div", class_="tgme_widget_message")
    urls = _extract_image_urls(msg)
    check("multi_photo_returns_two", len(urls) == 2)
    check("multi_photo_url_0_correct", urls[0] == "https://cdn.example.com/photo1.jpg")
    check("multi_photo_url_1_correct", urls[1] == "https://cdn.example.com/photo2.jpg")

    # Single photo
    html_single = """
    <div class="tgme_widget_message">
      <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.example.com/only.jpg')"></a>
      <i class="emoji" style="background-image:url('https://telegram.org/img/emoji/2.png')"></i>
    </div>
    """
    msg_single = BeautifulSoup(html_single, "html.parser").find("div", class_="tgme_widget_message")
    urls_single = _extract_image_urls(msg_single)
    check("single_photo_returns_one", len(urls_single) == 1)

    # No photo — emoji only
    html_none = """<div class="tgme_widget_message"><i class="emoji" style="background-image:url('e.png')"></i></div>"""
    msg_none = BeautifulSoup(html_none, "html.parser").find("div", class_="tgme_widget_message")
    check("no_photo_returns_empty", _extract_image_urls(msg_none) == [])
except Exception as e:
    FAIL.append("_extract_image_urls")
    print(f"  FAIL  _extract_image_urls: {e}")
    traceback.print_exc()


print("\n[11] _matches_query: fuzzy-token digest search")
try:
    from agent import _matches_query

    digest_text = (
        "ai_newz [04.05.2026]\n"
        "— Xiaomi MiMo 2.5: миллион токенов контекста, мультимодальность.\n\n"
        "seeallochnaya [05.05.2026]\n"
        "— Wispr Flow поднял $80M, вырос в 100x за год, 270 компаний из Fortune 500\n"
        "— Retention Wispr Flow на 6-й месяц — 80%\n"
        "— AquaVoice (voice-to-text для десктопа) вышел в топ-1 Hacker News"
    )
    # Direct substring (current behavior, preserved)
    check("match_exact_substring", _matches_query("Wispr Flow", digest_text))
    check("match_exact_lowercase", _matches_query("wispr flow", digest_text))
    check("match_exact_uppercase", _matches_query("WISPR FLOW", digest_text))

    # The actual real-world failure: typo'd query that should still match
    check("match_typo_extra_h", _matches_query("whispr flow", digest_text))
    check("match_typo_single_token", _matches_query("whispr", digest_text))

    # Other typos and partial queries
    check("match_partial_word", _matches_query("aquavoice", digest_text))
    check("match_substring_in_word", _matches_query("retention", digest_text))

    # Negative cases — must not over-match
    check("no_match_unrelated", not _matches_query("blockchain mining", digest_text))
    check("no_match_empty_query", not _matches_query("", digest_text))
    check("no_match_empty_content", not _matches_query("anything", ""))
    # Short tokens are dropped (would otherwise fuzzy-match too much)
    check("short_token_dropped", not _matches_query("xy", digest_text))
except Exception as e:
    FAIL.append("matches_query")
    print(f"  FAIL  matches_query: {e}")
    traceback.print_exc()


print("\n[10] _find_safe_cut: chat compaction boundary logic")
try:
    from agent import _find_safe_cut
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    # Case 1: short history — no cut
    msgs_short = []
    for _ in range(5):
        msgs_short.extend([HumanMessage(content="hi"), AIMessage(content="hello")])
    check("compact_short_no_cut", _find_safe_cut(msgs_short, target_keep=10, slack=5) == -1)

    # Case 2: long history with regular HumanMessage boundaries — must find one
    msgs_long = []
    for _ in range(20):
        msgs_long.append(HumanMessage(content="q"))
        msgs_long.append(AIMessage(content="a"))
    cut = _find_safe_cut(msgs_long, target_keep=10, slack=5)
    check("compact_returns_valid_cut", 0 < cut < len(msgs_long))
    check("compact_cut_at_human_boundary", isinstance(msgs_long[cut], HumanMessage))
    check("compact_tail_size_within_window",
          abs(len(msgs_long) - cut - 10) <= 5)

    # Case 3: tool-call pairs — tail must start on HumanMessage, never orphan ToolMessage
    msgs_tools = []
    for _ in range(8):
        msgs_tools.extend([
            HumanMessage(content="search for X"),
            AIMessage(content="", tool_calls=[{"id": "t1", "name": "search", "args": {}}]),
            ToolMessage(content="result", tool_call_id="t1"),
            AIMessage(content="found"),
        ])
    cut_tools = _find_safe_cut(msgs_tools, target_keep=10, slack=5)
    if cut_tools > 0:
        check("compact_tail_starts_on_human", isinstance(msgs_tools[cut_tools], HumanMessage))
    else:
        check("compact_tools_returns_neg1_safely", cut_tools == -1)

    # Case 4: no HumanMessage anywhere in cut window → -1
    msgs_no_boundary = [SystemMessage(content="sys")] + [
        AIMessage(content=f"a{i}") for i in range(40)
    ]
    check("compact_no_boundary_returns_neg1",
          _find_safe_cut(msgs_no_boundary, target_keep=10, slack=5) == -1)

    # Case 5: exactly target_keep messages — no cut needed
    msgs_exact = [HumanMessage(content="x"), AIMessage(content="y")] * 5
    check("compact_exact_target_no_cut",
          _find_safe_cut(msgs_exact, target_keep=10, slack=5) == -1)
except Exception as e:
    FAIL.append("compact_logic")
    print(f"  FAIL  compact_logic: {e}")
    traceback.print_exc()


print("\n" + "=" * 50)
print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:")
    for item in FAIL:
        print(f"  - {item}")
    sys.exit(1)
print("All integration checks passed!")
