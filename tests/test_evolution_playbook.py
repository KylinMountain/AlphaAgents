"""Tests for alpha_agents.evolution.playbook."""
from unittest.mock import patch


SAMPLE_PLAYBOOK = {
    "id": 1,
    "name": "CPO+机构买入",
    "pattern_json": (
        '{"conditions":[{"field":"vpa_verdict","op":"==","value":"bullish"},'
        '{"field":"theme","op":"==","value":"CPO"},'
        '{"field":"institutional","op":"contains","value":"机构"}]}'
    ),
    "weight": 1.5, "status": "active",
    "total_trades": 10, "wins": 8, "hit_rate": 0.8,
}


def test_match_playbook_all_conditions_pass():
    candidate = {"vpa_verdict": "bullish", "theme": "CPO",
                 "institutional": "机构买入1.42亿", "score": 62}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[SAMPLE_PLAYBOOK, {**SAMPLE_PLAYBOOK, "id": 2, "name": "B"}]):
        from alpha_agents.evolution.playbook import match_playbook
        pb = match_playbook(candidate)
    assert pb is not None
    assert pb["name"] == "CPO+机构买入"


def test_match_playbook_condition_fails_returns_none():
    candidate = {"vpa_verdict": "bullish", "theme": "数据中心",
                 "institutional": "机构买入", "score": 62}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[SAMPLE_PLAYBOOK, {**SAMPLE_PLAYBOOK, "id": 2, "name": "B"}]):
        from alpha_agents.evolution.playbook import match_playbook
        assert match_playbook(candidate) is None


def test_match_playbook_regime_fallback_skips_when_too_few_active():
    """If fewer than 2 active playbooks, skip matching entirely."""
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[SAMPLE_PLAYBOOK]):  # only 1 active
        from alpha_agents.evolution.playbook import match_playbook
        candidate = {"vpa_verdict": "bullish", "theme": "CPO",
                     "institutional": "机构", "score": 62}
        assert match_playbook(candidate) is None


def test_match_playbook_first_match_wins():
    """get_active_playbooks returns DB-ordered (weight DESC) — first match wins."""
    pb_a = {**SAMPLE_PLAYBOOK, "id": 1, "name": "A", "weight": 1.5}
    pb_b = {**SAMPLE_PLAYBOOK, "id": 2, "name": "B", "weight": 1.0}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[pb_a, pb_b]):
        from alpha_agents.evolution.playbook import match_playbook
        candidate = {"vpa_verdict": "bullish", "theme": "CPO",
                     "institutional": "机构", "score": 62}
        pb = match_playbook(candidate)
        assert pb["name"] == "A"


def test_match_playbook_operators():
    """Support ==, in, contains, >=, <=, >, < operators."""
    pb_a = {"id": 1, "name": "T", "weight": 1.0, "status": "active",
            "pattern_json": '{"conditions":['
            '{"field":"score","op":">=","value":60},'
            '{"field":"vpa_verdict","op":"in","value":["bullish","neutral"]},'
            '{"field":"change_pct","op":"<=","value":5.0}'
            ']}'}
    pb_b = {**pb_a, "id": 2, "name": "T2"}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[pb_a, pb_b]):
        from alpha_agents.evolution.playbook import match_playbook
        assert match_playbook({"score": 60, "vpa_verdict": "neutral",
                                "change_pct": 3.0}) is not None
        assert match_playbook({"score": 59, "vpa_verdict": "bullish",
                                "change_pct": 1.0}) is None
        assert match_playbook({"score": 70, "vpa_verdict": "bearish",
                                "change_pct": 1.0}) is None


def test_update_playbook_stats_degrades_low_hit_rate():
    pb = {"id": 1, "name": "X", "status": "active", "weight": 1.0,
          "total_trades": 6, "wins": 2, "hit_rate": 0.33, "avg_return": -1.0,
          "pattern_json": "{}", "annotation": ""}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd, \
         patch("alpha_agents.evolution.playbook.set_playbook_annotation") as m_ann, \
         patch("alpha_agents.evolution.playbook.annotate_degraded",
               return_value="主线资金退潮"):
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    assert any("degraded" in o.lower() for o in ops)
    m_upd.assert_called_once()
    assert m_upd.call_args.kwargs["status"] == "degraded"
    assert m_upd.call_args.kwargs["weight"] == 0.5
    m_ann.assert_called_once()


def test_update_playbook_stats_restores_degraded_on_recovery():
    pb = {"id": 1, "name": "Y", "status": "degraded", "weight": 0.5,
          "total_trades": 10, "wins": 3, "hit_rate": 0.3,
          "pattern_json": "{}", "annotation": "hit_rate<40%",
          "version_history": "[]", "last_updated": "2026-04-15"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook._recent_hit_rate",
               return_value=(0.8, 5)), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    assert any("active" in o.lower() for o in ops)
    assert m_upd.call_args.kwargs["status"] == "active"
    assert m_upd.call_args.kwargs["weight"] == 1.0


def test_update_playbook_stats_deprecates_after_14_days():
    from datetime import datetime, timedelta
    fifteen_ago = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    pb = {"id": 1, "name": "Z", "status": "degraded", "weight": 0.5,
          "total_trades": 20, "wins": 6, "hit_rate": 0.3,
          "pattern_json": "{}", "annotation": "",
          "last_updated": fifteen_ago,
          "version_history": "[]"}
    today_str = datetime.now().strftime("%Y-%m-%d")
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook._recent_hit_rate",
               return_value=(0.3, 5)), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats(today_str)
    assert any("deprecated" in o.lower() for o in ops)
    assert m_upd.call_args.kwargs["status"] == "deprecated"
    assert m_upd.call_args.kwargs["weight"] == 0.0


def test_update_playbook_stats_boosts_high_performer():
    pb = {"id": 1, "name": "H", "status": "active", "weight": 1.0,
          "total_trades": 15, "wins": 12, "hit_rate": 0.80, "avg_return": 3.5,
          "pattern_json": "{}"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    assert any("boost" in o.lower() or "1.5" in o for o in ops)
    assert m_upd.call_args.kwargs["weight"] == 1.5
    assert m_upd.call_args.kwargs["status"] == "active"
