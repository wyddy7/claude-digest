"""
Offline integration test for ai.py — no API calls.
Tests:
  1. DigestResult / SourceBlock schema works correctly
  2. _to_html_digest() and _to_html_personal() render correctly
  3. build_system_prompt strips HTML from recent_digests, truncates to 600 chars
  4. build_system_prompt contains required sections/text
  5. _format_posts produces expected output structure
"""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import traceback

from ai import DigestResult, SourceBlock, build_system_prompt, _to_html_digest, _to_html_personal, _format_posts

PASS = []
FAIL = []

def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


# ── Test 1: DigestResult / SourceBlock schema ────────────────────────────────

print("\n[1] DigestResult / SourceBlock schema")
try:
    sources = [
        SourceBlock(
            channel=f"channel_{i}",
            url=f"https://t.me/channel_{i}/1",
            post_date="01.04.2026",
            bullets=[f"Факт {i}.1", f"Факт {i}.2"],
            example=f"Пример {i}" if i % 2 == 0 else "",
        )
        for i in range(6)
    ]
    dr = DigestResult(
        sources=sources,
        personal=["Лично тебе пункт 1", "Лично тебе пункт 2"],
    )
    check("schema_accepts_sources", len(dr.sources) == 6, f"got {len(dr.sources)}")
    check("schema_personal_field", len(dr.personal) == 2)
    check("sourceblock_bullets", len(dr.sources[0].bullets) == 2)
    check("sourceblock_url_validator", dr.sources[0].url.startswith("https://t.me/"))
except Exception as e:
    FAIL.append("schema")
    print(f"  FAIL  schema: {e}")
    traceback.print_exc()
    dr = None


# ── Test 2: _to_html_digest and _to_html_personal ────────────────────────────

print("\n[2] _to_html_digest / _to_html_personal rendering")
try:
    assert dr is not None, "dr fixture not built"
    html_digest = _to_html_digest(dr)
    html_personal = _to_html_personal(dr)

    check("digest_contains_channel_link", 'href="https://t.me/channel_0/1"' in html_digest)
    check("digest_contains_channel_name", "channel_0" in html_digest)
    check("digest_contains_post_date", "01.04.2026" in html_digest)
    check("digest_contains_bullet", "— Факт 0.1" in html_digest)
    check("digest_contains_example", "💡 Пример 0" in html_digest)
    check("digest_no_example_for_empty", "💡 Пример 1" not in html_digest,
          "empty example should not render")

    check("personal_not_none", html_personal is not None)
    check("personal_header", "<b>Лично тебе:</b>" in html_personal)
    check("personal_bullet", "• Лично тебе пункт 1" in html_personal)

    # _to_html_personal returns None when personal is empty
    dr_empty = DigestResult(sources=[], personal=[])
    check("personal_none_when_empty", _to_html_personal(dr_empty) is None)

except Exception as e:
    FAIL.append("_to_html")
    print(f"  FAIL  _to_html: {e}")
    traceback.print_exc()


# ── Test 3: build_system_prompt strips HTML from recent_digests ───────────────

print("\n[3] build_system_prompt: HTML stripping in recent_digests")
try:
    html_digest_text = (
        "<b>Топ инсайтов:</b>\n"
        "• <b>Пример инсайта</b> <a href='https://t.me/ch/1'>channel</a>\n"
        "  Факт о технологии.\n"
        "  <i>Конкретная команда: pip install x</i>\n"
        "<b>Лично тебе:</b>\n"
        "• Попробуй инструмент"
    )
    recent_digests = [
        {"date": "2026-03-30", "digest": html_digest_text, "is_error": False},
    ]
    user_data = {
        "description": "Тестовый пользователь",
        "current_focus": "AI-агенты",
        "interaction_history": [],
    }
    prompt = build_system_prompt(user_data, recent_digests)

    import re
    prev_section_match = re.search(
        r"ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ.*?(?=\nПОСЛЕДНИЕ ВЗАИМОДЕЙСТВИЯ|$)",
        prompt, re.DOTALL
    )
    assert prev_section_match, "ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ section not found"
    prev_section = prev_section_match.group(0)

    has_html_tags = bool(re.search(r"<[^>]+>", prev_section))
    check("html_stripped_from_prev_digests", not has_html_tags,
          f"HTML tags still present: {prev_section[:200]}")
    check("plain_text_survives_strip", "Пример инсайта" in prev_section,
          "expected plain text not found")
    check("plain_text_command_survives", "pip install x" in prev_section,
          "command text not found")

except Exception as e:
    FAIL.append("build_system_prompt_html_strip")
    print(f"  FAIL  build_system_prompt_html_strip: {e}")
    traceback.print_exc()


# ── Test 4: build_system_prompt truncates to 600 chars ───────────────────────

print("\n[4] build_system_prompt: 600-char truncation of prev digest")
try:
    long_digest_text = "X" * 2000
    recent_digests_long = [
        {"date": "2026-03-29", "digest": long_digest_text, "is_error": False},
    ]
    prompt_long = build_system_prompt(user_data, recent_digests_long)

    prev_match = re.search(
        r"ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ.*?(?=\nПОСЛЕДНИЕ ВЗАИМОДЕЙСТВИЯ|$)",
        prompt_long, re.DOTALL
    )
    assert prev_match, "ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ not found in long digest prompt"
    prev_text = prev_match.group(0)

    check("truncated_to_600", "X" * 601 not in prev_text,
          "found 601+ chars — truncation not applied")
    check("contains_up_to_600_chars", "X" * 600 in prev_text,
          "expected 600 Xs not found — truncation cut too short")

except Exception as e:
    FAIL.append("build_system_prompt_truncation")
    print(f"  FAIL  build_system_prompt_truncation: {e}")
    traceback.print_exc()


# ── Test 5: build_system_prompt contains required sections ───────────────────

print("\n[5] build_system_prompt: required sections present")
try:
    prompt_check = build_system_prompt(user_data, recent_digests)

    check("contains_ПРЕДЫДУЩИЕ_ДАЙДЖЕСТЫ", "ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ" in prompt_check)
    check("contains_ЭТАЛОННЫЙ_ПРИМЕР", "ЭТАЛОННЫЙ ПРИМЕР" in prompt_check)
    check("contains_КОЛИЧЕСТВО_ИСТОЧНИКОВ", "КОЛИЧЕСТВО ИСТОЧНИКОВ" in prompt_check)
    check("contains_4_6_sources", "4-6" in prompt_check)

except Exception as e:
    FAIL.append("build_system_prompt_sections")
    print(f"  FAIL  build_system_prompt_sections: {e}")
    traceback.print_exc()


# ── Test 6: _format_posts ─────────────────────────────────────────────────────

print("\n[6] _format_posts: structure")
try:
    posts = [
        {
            "channel": "test_ch",
            "text": "Короткий тестовый пост про AI.",
            "link": "https://t.me/test_ch/42",
            "time": "2026-03-31T10:00:00+00:00",
            "is_thread": False,
        },
        {
            "channel": "another_ch",
            "text": "Тред про модели.",
            "link": "https://t.me/another_ch/99",
            "time": "2026-03-30T08:30:00+00:00",
            "is_thread": True,
        },
    ]
    formatted = _format_posts(posts)

    check("format_posts_ПОСТ_label", "ПОСТ: test_ch" in formatted)
    check("format_posts_ТРЕД_label", "ТРЕД: another_ch" in formatted)
    check("format_posts_ДАТА_field", "ДАТА:" in formatted)
    check("format_posts_ССЫЛКА_field", "ССЫЛКА: https://t.me/test_ch/42" in formatted)
    check("format_posts_ТЕКСТ_field", "ТЕКСТ:" in formatted)
    check("format_posts_separator", "---" in formatted)
    check("format_posts_date_format", "31.03.2026" in formatted,
          "date not formatted as DD.MM.YYYY")

except Exception as e:
    FAIL.append("_format_posts")
    print(f"  FAIL  _format_posts: {e}")
    traceback.print_exc()


# ── Summary ───────────────────────────────────────────────────────────────────

print("\n" + "=" * 50)
print(f"RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All integration checks passed!")
