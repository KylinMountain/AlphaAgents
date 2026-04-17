"""Tests for alpha_agents.evolution.lessons."""
from unittest.mock import patch


def test_extract_daily_lessons_parses_tag():
    report = """
复盘报告正文 blah blah...

<!-- LESSONS: [
  {"type":"success","theme":"CPO","content":"协创数据+9.4%命中","tags":"CPO,机构买入"},
  {"type":"failure","theme":"数据中心","content":"东方国信破5日线才减仓","tags":"止损时机"}
] -->
"""
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 2
    assert m.call_count == 2
    call1 = m.call_args_list[0].kwargs
    assert call1["lesson_type"] == "success"
    assert call1["theme"] == "CPO"
    assert "协创数据" in call1["content"]


def test_extract_daily_lessons_no_tag_returns_zero():
    report = "报告里完全没有 LESSONS 标签"
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 0
    assert m.call_count == 0


def test_extract_daily_lessons_malformed_json_returns_zero():
    report = "<!-- LESSONS: [this is not valid json] -->"
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 0
    assert m.call_count == 0


def test_extract_daily_lessons_skips_empty_content():
    report = """<!-- LESSONS: [
        {"type":"success","content":""},
        {"type":"success","theme":"","content":"真经验"}
    ] -->"""
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 1  # empty content row skipped
