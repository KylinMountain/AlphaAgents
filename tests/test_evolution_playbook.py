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
