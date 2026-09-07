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


def test_consolidate_principles_no_new_lessons_noop():
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=[]):
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["created"] == 0
    assert result["reinforced"] == 0
    assert result["weakened"] == 0


def test_consolidate_principles_creates_new():
    lessons = [{"id": 1, "date": "2026-04-17", "lesson_type": "success",
                "theme": "CPO", "content": "协创数据+9.4%", "relevance_tags": "CPO,机构买入"}]
    fake_llm_response = {
        "operations": [
            {"op": "create",
             "principle": "CPO+机构买入 = 高胜率模式",
             "pattern_description": "CPO板块+龙虎榜机构买入",
             "category": "theme_timing",
             "action_guidance": "优先介入",
             "evidence": [{"code": "300857", "date": "04-17", "outcome": "+9.4%"}]},
        ]
    }
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value=fake_llm_response), \
         patch("alpha_agents.evolution.lessons.create_trading_principle",
               return_value=1) as m_create:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["created"] == 1
    m_create.assert_called_once()


def test_consolidate_principles_reinforces_existing():
    lessons = [{"id": 2, "date": "2026-04-17", "lesson_type": "success",
                "theme": "CPO", "content": "另一只CPO命中", "relevance_tags": ""}]
    existing = [{"id": 7, "principle": "CPO+机构买入 = 高胜率模式",
                 "status": "active", "evidence_count": 3}]
    fake_llm = {"operations": [
        {"op": "reinforce", "principle_id": 7,
         "new_case": {"code": "688xxx", "date": "04-17", "outcome": "+5%"}},
    ]}
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=existing), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value=fake_llm), \
         patch("alpha_agents.evolution.lessons.reinforce_trading_principle") as m_reinf:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["reinforced"] == 1
    m_reinf.assert_called_once()


def test_consolidate_principles_ignores_llm_weaken_requests():
    """G3: a model may propose principles; only market data retires them.

    Asking the writer to judge its own memories is the Echo Gap — models
    accept their own wrong memories 31-54% of the time, and a stronger
    judge model provably does not fix it. Retirement moved to
    principle_scoring, which reads graded predictions.
    """
    lessons = [{"id": 3, "date": "2026-04-17", "lesson_type": "failure",
                "theme": "数据中心", "content": "数据中心再次失败", "relevance_tags": ""}]
    existing = [{"id": 9, "principle": "数据中心 = 稳健加仓方向",
                 "status": "active", "evidence_count": 5,
                 "evidence": '[{"outcome":"-2%"},{"outcome":"-1%"},{"outcome":"-3%"}]'}]
    fake_llm = {"operations": [
        {"op": "weaken", "principle_id": 9, "reason": "近3次全亏"},
    ]}
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=existing), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value=fake_llm), \
         patch("alpha_agents.evolution.lessons.set_principle_status") as m_status:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["weakened"] == 0
    m_status.assert_not_called()
