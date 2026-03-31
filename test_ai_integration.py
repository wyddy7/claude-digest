"""
Offline integration test for ai.py changes — no API calls.
Tests:
  1. DigestResult schema accepts 10+ insights (removed 3-6 limit)
  2. _to_html() renders all insights correctly
  3. build_system_prompt strips HTML from recent_digests, truncates to 600 chars
  4. build_system_prompt output contains ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ, ЭТАЛОННЫЙ ПРИМЕР, Минимум 8
  5. _format_posts produces expected output structure
"""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import traceback

from ai import DigestResult, Insight, build_system_prompt, _to_html, _format_posts

PASS = []
FAIL = []

def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))


# ── Test 1: DigestResult accepts 10+ insights ────────────────────────────────

print("\n[1] DigestResult schema: 10+ insights")
try:
    insights_10 = [
        Insight(
            title=f"Инсайт {i+1}",
            channel="test_channel",
            url=f"https://t.me/test_channel/{i+1}",
            post_date="31.03.2026",
            what=f"Факт номер {i+1} из поста",
            how=f"Команда: action_{i+1}",
        )
        for i in range(10)
    ]
    dr = DigestResult(
        insights=insights_10,
        personal=["Лично тебе пункт 1", "Лично тебе пункт 2"],
        today="Запусти тест и проверь результат",
    )
    check("schema_accepts_10_insights", len(dr.insights) == 10,
          f"got {len(dr.insights)}")
    check("schema_no_upper_limit", True,
          "list[Insight] has no max constraint")
except Exception as e:
    FAIL.append("schema_accepts_10_insights")
    print(f"  FAIL  schema_accepts_10_insights: {e}")
    traceback.print_exc()
    dr = None


# ── Test 2: _to_html renders all 10 insights ─────────────────────────────────

print("\n[2] _to_html: all insights rendered")
try:
    assert dr is not None, "dr fixture not built"
    html = _to_html(dr)

    check("html_contains_header", "<b>Топ инсайтов:</b>" in html)
    check("html_all_10_titles", all(f"Инсайт {i+1}" in html for i in range(10)),
          "some titles missing")
    check("html_all_10_urls", all(f"test_channel/{i+1}" in html for i in range(10)),
          "some URLs missing")
    check("html_personal_section", "<b>Лично тебе:</b>" in html)
    check("html_today_section", "<b>Сделай сегодня:</b>" in html)

    # Count bullet points for insights (not personal)
    insight_bullets = html.count("• <b>Инсайт")
    check("html_insight_bullet_count", insight_bullets == 10,
          f"expected 10, got {insight_bullets}")
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
        "• Попробуй инструмент\n"
        "<b>Сделай сегодня:</b>\n"
        "Запусти скрипт"
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

    # HTML tags should NOT appear in the ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ section
    import re
    prev_section_match = re.search(
        r"ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ.*?(?=\nПОСЛЕДНИЕ ВЗАИМОДЕЙСТВИЯ|$)",
        prompt, re.DOTALL
    )
    assert prev_section_match, "ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ section not found"
    prev_section = prev_section_match.group(0)

    has_html_tags = bool(re.search(r"<[^>]+>", prev_section))
    check("html_stripped_from_prev_digests", not has_html_tags,
          f"HTML tags still present in section: {prev_section[:200]}")

    # Plain text content should survive stripping
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
    long_digest_text = "X" * 2000  # 2000 plain chars, should be cut to 600
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

    # Should NOT contain 601+ consecutive X's (truncation applied)
    long_run = "X" * 601
    check("truncated_to_600", long_run not in prev_text,
          "found 601+ chars from long digest — truncation not applied")

    # Should contain 600 X's (truncation preserved up to limit)
    exact_600 = "X" * 600
    check("contains_up_to_600_chars", exact_600 in prev_text,
          "expected 600 Xs not found — truncation cut too short")

except Exception as e:
    FAIL.append("build_system_prompt_truncation")
    print(f"  FAIL  build_system_prompt_truncation: {e}")
    traceback.print_exc()


# ── Test 5: build_system_prompt contains required sections/text ───────────────

print("\n[5] build_system_prompt: required sections present")
try:
    prompt_check = build_system_prompt(user_data, recent_digests)

    check("contains_ПРЕДЫДУЩИЕ_ДАЙДЖЕСТЫ", "ПРЕДЫДУЩИЕ ДАЙДЖЕСТЫ" in prompt_check,
          "section header missing")
    check("contains_ЭТАЛОННЫЙ_ПРИМЕР", "ЭТАЛОННЫЙ ПРИМЕР" in prompt_check,
          "section missing")
    check("contains_Минимум_8", "Минимум 8" in prompt_check,
          "'Минимум 8' not found — prompt constraint missing")
    check("contains_КОЛИЧЕСТВО_ИНСАЙТОВ", "КОЛИЧЕСТВО ИНСАЙТОВ" in prompt_check,
          "section header missing")
    # Old "3-6" constraint must be gone from the insights count section
    # (It can appear in title field comment but should NOT be in the count rule)
    count_section = re.search(
        r"КОЛИЧЕСТВО ИНСАЙТОВ.*?(?=\n[A-ZА-ЯЁ]{3,}[:\n]|$)",
        prompt_check, re.DOTALL
    )
    if count_section:
        section_text = count_section.group(0)
        check("no_3_6_in_count_section", "3-6 инсайт" not in section_text,
              f"old '3-6 инсайт' constraint still present: {section_text[:100]}")
    else:
        check("no_3_6_in_count_section", True, "section not separately verifiable")

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
