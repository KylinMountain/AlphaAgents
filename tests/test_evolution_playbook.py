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


def test_update_playbook_stats_quarantines_a_degradation():
    """A low hit rate proposes `degraded`; the live playbook is untouched.

    Counting stays in record_playbook_trade. Moving a rule to weight 0.5
    changes what the prompt retrieval rank sees, so it needs a candidate
    version rather than a daily rule engine applying it in place.
    """
    pb = {"id": 1, "name": "X", "status": "active", "weight": 1.0,
          "total_trades": 6, "wins": 2, "hit_rate": 0.33, "avg_return": -1.0,
          "pattern_json": "{}", "annotation": ""}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.save_candidate",
               return_value=31) as m_save, \
         patch("alpha_agents.data.memory_store.update_playbook_status") as m_live, \
         patch("alpha_agents.data.memory_store.set_playbook_annotation") as m_ann:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")

    assert any("candidate" in o for o in ops)
    assert m_save.call_count == 1
    kwargs = m_save.call_args.kwargs
    assert kwargs["entity_type"] == "playbook"
    assert kwargs["operation"] == "update"
    assert kwargs["target_id"] == 1
    assert kwargs["payload"]["proposal"]["status"] == "degraded"
    assert kwargs["payload"]["proposal"]["weight"] == 0.5
    m_live.assert_not_called()
    m_ann.assert_not_called()


def test_update_playbook_stats_quarantines_a_restore():
    pb = {"id": 1, "name": "Y", "status": "degraded", "weight": 0.5,
          "total_trades": 10, "wins": 3, "hit_rate": 0.3,
          "pattern_json": "{}", "annotation": "hit_rate<40%",
          "version_history": "[]", "last_updated": "2026-04-15"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook._recent_hit_rate",
               return_value=(0.8, 5)), \
         patch("alpha_agents.evolution.playbook.save_candidate",
               return_value=32) as m_save, \
         patch("alpha_agents.data.memory_store.update_playbook_status") as m_live:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")

    assert m_save.call_args.kwargs["payload"]["proposal"]["status"] == "active"
    assert m_save.call_args.kwargs["payload"]["proposal"]["weight"] == 1.0
    m_live.assert_not_called()


def test_update_playbook_stats_quarantines_a_deprecation():
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
         patch("alpha_agents.evolution.playbook.save_candidate",
               return_value=33) as m_save, \
         patch("alpha_agents.data.memory_store.update_playbook_status") as m_live:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats(today_str)

    assert m_save.call_args.kwargs["payload"]["proposal"]["status"] == "deprecated"
    assert m_save.call_args.kwargs["payload"]["proposal"]["weight"] == 0.0
    m_live.assert_not_called()


def test_update_playbook_stats_quarantines_a_boost():
    pb = {"id": 1, "name": "H", "status": "active", "weight": 1.0,
          "total_trades": 15, "wins": 12, "hit_rate": 0.80, "avg_return": 3.5,
          "pattern_json": "{}"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.save_candidate",
               return_value=34) as m_save, \
         patch("alpha_agents.data.memory_store.update_playbook_status") as m_live:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")

    proposal = m_save.call_args.kwargs["payload"]["proposal"]
    assert proposal["weight"] == 1.5
    assert proposal["status"] == "active"
    m_live.assert_not_called()


def test_update_playbook_stats_is_a_no_op_without_a_trigger():
    """A healthy playbook inside the thresholds proposes nothing."""
    pb = {"id": 1, "name": "K", "status": "active", "weight": 1.0,
          "total_trades": 8, "wins": 4, "hit_rate": 0.55,
          "pattern_json": "{}", "annotation": ""}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.save_candidate") as m_save:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")

    assert ops == []
    m_save.assert_not_called()


def test_scan_and_auto_create_ignores_signal_rows():
    """Only report_type='intraday' counts — intraday_signal (limit-up obs) excluded."""
    from alpha_agents.evolution.playbook import scan_and_auto_create
    with patch("alpha_agents.evolution.playbook._query_hit_clusters") as m:
        m.return_value = []
        result = scan_and_auto_create("2026-04-17")
    assert result == []
    m.assert_called_once()


def test_scan_and_auto_create_quarantines_a_new_pattern():
    """The cluster is described as a candidate; no playbook is created live."""
    cluster = {"vpa_verdict": "bullish", "theme": "CPO",
               "institutional_present": 1,
               "hits": 4, "total": 5, "avg_return": 4.2}
    with patch("alpha_agents.evolution.playbook._query_hit_clusters",
               return_value=[cluster]), \
         patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[]), \
         patch("alpha_agents.evolution.playbook.save_candidate",
               return_value=42) as m_save, \
         patch("alpha_agents.data.memory_store.create_playbook") as m_live:
        from alpha_agents.evolution.playbook import scan_and_auto_create
        created = scan_and_auto_create("2026-04-17")

    assert created == [42]
    assert m_save.call_count == 1
    kwargs = m_save.call_args.kwargs
    assert kwargs["entity_type"] == "playbook"
    assert kwargs["operation"] == "create"
    assert not kwargs.get("target_id")
    pattern = kwargs["payload"]["pattern_json"]
    # Conditions must reference theme + vpa_verdict + institutional (contains 机构)
    fields = {c["field"] for c in pattern["conditions"]}
    assert fields == {"theme", "vpa_verdict", "institutional"}
    # Name includes identifying bits
    assert "CPO" in kwargs["payload"]["name"]
    assert "bullish" in kwargs["payload"]["name"]
    m_live.assert_not_called()


def test_scan_and_auto_create_skips_existing_pattern():
    cluster = {"vpa_verdict": "bullish", "theme": "CPO",
               "institutional_present": 0,
               "hits": 3, "total": 4, "avg_return": 2.0}
    # Existing playbook matches the same signature (theme + vpa_verdict, no institutional)
    existing = [{"id": 1, "name": "Auto: CPO-bullish",
                 "pattern_json": '{"conditions":[{"field":"theme","op":"==","value":"CPO"},{"field":"vpa_verdict","op":"==","value":"bullish"}]}',
                 "status": "active"}]
    with patch("alpha_agents.evolution.playbook._query_hit_clusters",
               return_value=[cluster]), \
         patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=existing), \
         patch("alpha_agents.evolution.playbook.save_candidate") as m_save:
        from alpha_agents.evolution.playbook import scan_and_auto_create
        created = scan_and_auto_create("2026-04-17")
    assert created == []
    m_save.assert_not_called()
