"""Tests for alpha_agents.evolution.metrics."""
from unittest.mock import patch


def test_compute_metrics_with_empty_db_returns_zeros():
    with patch("alpha_agents.evolution.metrics._query_intraday_buckets",
               return_value={
                   "intraday_hit_rate_7d": 0.0, "intraday_count_7d": 0,
                   "matched_hit_rate_7d": 0.0, "matched_count_7d": 0,
                   "unmatched_hit_rate_7d": 0.0, "unmatched_count_7d": 0,
               }), \
         patch("alpha_agents.evolution.metrics.get_active_principles",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_playbooks",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_recent_daily_lessons",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.upsert_evolution_metrics"):
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        m = compute_evolution_metrics("2026-04-17")
    assert m["intraday_hit_rate_7d"] == 0.0
    assert m["active_principles"] == 0
    assert m["active_playbooks"] == 0
    assert m["lessons_count_7d"] == 0


def test_compute_metrics_tallies_populated_state():
    buckets = {
        "intraday_hit_rate_7d": 0.60, "intraday_count_7d": 200,
        "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
        "unmatched_hit_rate_7d": 0.56, "unmatched_count_7d": 160,
    }
    act_princ = [{"status": "active"}] * 3
    all_princ_inc_weak = act_princ + [{"status": "weakened"}]
    pbs = [{"status": "active"}, {"status": "active"},
            {"status": "degraded"}, {"status": "deprecated"}]
    lessons = [{"date": "2026-04-16"}] * 10
    with patch("alpha_agents.evolution.metrics._query_intraday_buckets",
               return_value=buckets), \
         patch("alpha_agents.evolution.metrics.get_active_principles",
               return_value=act_princ), \
         patch("alpha_agents.evolution.metrics.get_all_principles_including_weakened",
               return_value=all_princ_inc_weak), \
         patch("alpha_agents.evolution.metrics.get_all_playbooks",
               return_value=pbs), \
         patch("alpha_agents.evolution.metrics.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.metrics.upsert_evolution_metrics"):
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        m = compute_evolution_metrics("2026-04-17")
    assert m["intraday_hit_rate_7d"] == 0.60
    assert m["active_principles"] == 3
    assert m["weakened_principles"] == 1
    assert m["active_playbooks"] == 2
    assert m["degraded_playbooks"] == 1
    assert m["lessons_count_7d"] == 10


def test_compute_metrics_persists_via_upsert():
    with patch("alpha_agents.evolution.metrics._query_intraday_buckets",
               return_value={
                   "intraday_hit_rate_7d": 0.5, "intraday_count_7d": 10,
                   "matched_hit_rate_7d": 0.0, "matched_count_7d": 0,
                   "unmatched_hit_rate_7d": 0.5, "unmatched_count_7d": 10,
               }), \
         patch("alpha_agents.evolution.metrics.get_active_principles",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_playbooks",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_recent_daily_lessons",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.upsert_evolution_metrics") as m_upsert:
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        result = compute_evolution_metrics("2026-04-17")
    m_upsert.assert_called_once()
    assert m_upsert.call_args.args[0] == "2026-04-17"
    assert m_upsert.call_args.args[1] == result


def test_format_metrics_trend_produces_readable_summary():
    from alpha_agents.evolution.metrics import format_metrics_trend
    rows = [
        {"date": "2026-04-10", "intraday_hit_rate_7d": 0.55,
         "matched_hit_rate_7d": 0.55, "matched_count_7d": 0,
         "unmatched_hit_rate_7d": 0.55, "unmatched_count_7d": 40,
         "active_principles": 0, "active_playbooks": 0, "lessons_count_7d": 0},
        {"date": "2026-04-17", "intraday_hit_rate_7d": 0.65,
         "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
         "unmatched_hit_rate_7d": 0.56, "unmatched_count_7d": 160,
         "active_principles": 5, "active_playbooks": 3, "lessons_count_7d": 18},
    ]
    text = format_metrics_trend(rows)
    assert "55" in text and "65" in text
    assert "matched" in text.lower() or "匹配" in text
