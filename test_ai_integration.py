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

from ai import (
    DigestResult,
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
    recent_digests = [{"date": "2026-03-30", "digest": html_digest_text, "is_error": False}]
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
except Exception as e:
    FAIL.append("dockerfile_check")
    print(f"  FAIL  dockerfile_check: {e}")
    traceback.print_exc()


print("\n" + "=" * 50)
print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:")
    for item in FAIL:
        print(f"  - {item}")
    sys.exit(1)
print("All integration checks passed!")
