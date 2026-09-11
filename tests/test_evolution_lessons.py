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


def test_consolidate_principles_quarantines_a_new_proposal():
    """A proposed principle is stored as a candidate, never written live.

    The model may describe a pattern; that description is a hypothesis. It
    reaches decision prompts only through an approved policy snapshot, which
    phase one does not have — so `created` stays zero by construction.
    """
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
         patch("alpha_agents.evolution.lessons.save_candidate",
               return_value=11) as m_save:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")

    assert result["candidates"] == 1
    assert result["created"] == 0          # nothing entered active knowledge
    assert m_save.call_count == 1
    kwargs = m_save.call_args.kwargs
    assert kwargs["entity_type"] == "principle"
    assert kwargs["operation"] == "create"
    assert kwargs["target_id"] is None
    assert kwargs["source_date"] == "2026-04-17"


def test_consolidate_principles_quarantines_a_reinforcement():
    """Reinforcing cites an existing rule; it does not edit it."""
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
         patch("alpha_agents.evolution.lessons.save_candidate",
               return_value=12) as m_save:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")

    assert result["candidates"] == 1
    assert result["reinforced"] == 0       # the live rule is untouched
    assert m_save.call_args.kwargs["operation"] == "reinforce"
    assert m_save.call_args.kwargs["target_id"] == 7


def test_consolidate_principles_ignores_llm_weaken_requests():
    """G3: a model may propose principles; only market data retires them.

    Asking the writer to judge its own memories is the Echo Gap — models
    accept their own wrong memories 31-54% of the time, and a stronger
    judge model provably does not fix it. Retirement moved to
    principle_scoring, which reads graded predictions.

    Phase one goes further than the original rule: even a market-derived
    retirement is a behavior change, so it needs a candidate version too.
    A `weaken` op is dropped outright — it is neither applied nor queued.
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
         patch("alpha_agents.evolution.lessons.save_candidate") as m_save:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")

    assert result["weakened"] == 0
    assert result["candidates"] == 0
    m_save.assert_not_called()


def test_consolidate_principles_survives_a_row_without_an_id():
    """A malformed lesson row must not abort the whole day's consolidation."""
    lessons = [{"date": "2026-04-17", "lesson_type": "failure",
                "content": "没有 id 的行", "relevance_tags": ""}]
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value={"operations": []}):
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")

    assert result["candidates"] == 0
    assert result["failed"] == 0
