"""Tests for alpha_agents.evolution.playbook."""
from datetime import date
from unittest.mock import patch

import pytest

from alpha_agents.data import memory_store as ms


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
    cluster = {"change_pct_band": "strong", "theme": "CPO",
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
    # One condition per dimension the cluster pins: theme + the entry-move band
    # + institutional (contains 机构). The band replaced `vpa_verdict`, which no
    # writer recorded — a constant NULL was never a dimension.
    fields = {c["field"] for c in pattern["conditions"]}
    assert fields == {"theme", "change_pct_band", "institutional"}
    # Name includes identifying bits, and the band reads in the language of the
    # candidate list rather than as a machine value.
    name = kwargs["payload"]["name"]
    assert "CPO" in name
    assert "大涨" in name
    m_live.assert_not_called()


def test_scan_and_auto_create_skips_existing_pattern():
    cluster = {"change_pct_band": "strong", "theme": "CPO",
               "institutional_present": 0,
               "hits": 3, "total": 4, "avg_return": 2.0}
    # Existing playbook matches the same signature (theme + band, no institutional)
    existing = [{"id": 1, "name": "Auto: CPO-大涨",
                 "pattern_json": '{"conditions":[{"field":"theme","op":"==","value":"CPO"},{"field":"change_pct_band","op":"==","value":"strong"}]}',
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


class TestTheEntryMoveDimension:
    """The third clustering dimension, and why it is written down.

    It replaced `vpa_verdict`, which the query grouped on and **no writer
    recorded** — a constant NULL, so the grouping had two live dimensions while
    the SQL read as if it had three. Two things keep the replacement honest:
    the boundaries exist once (`change_band`), and the value is recorded at
    decision time rather than derived again in SQL. Deriving it twice is how a
    playbook gets created for a cluster it can never match.
    """

    @pytest.mark.parametrize("pct,band", [
        (-3, "down"), (-0.1, "down"), (0, "flat"), (1.99, "flat"),
        (2, "mid"), (4.99, "mid"), (5, "strong"), (20, "strong"),
    ])
    def test_the_boundaries(self, pct, band):
        from alpha_agents.evolution.playbook import change_band
        assert change_band(pct) == band

    def test_a_pick_with_no_move_is_unknown_not_flat(self):
        """Nothing to measure is not the same as measuring zero: the matcher
        compares against this value, so a made-up "flat" would match patterns
        the decision never justified."""
        from alpha_agents.evolution.playbook import change_band
        for bad in (None, "", "abc", [], {}):
            assert change_band(bad) == "unknown"

    def test_two_bands_are_two_clusters(self, tmp_path, monkeypatch):
        """The behavioural claim, not a grep of the SQL.

        Six rows identical in theme and institutional, differing only in the
        band. Two clusters, one per band — if the band were a constant (which is
        what `vpa_verdict` had become) there would be exactly one.
        """
        from alpha_agents.evolution.playbook import _query_hit_clusters

        monkeypatch.setattr(ms, "MEMORY_DB_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(ms._local, "conn", None, raising=False)
        today = date.today().isoformat()
        for i, band in enumerate(["strong"] * 3 + ["flat"] * 3):
            pid = ms.save_prediction(
                date=today, report_type="intraday", code=f"00000{i}",
                name="A", direction="bullish", confidence="medium",
                theme_line="CPO", entry_price=10.0, reason="r",
                features={"theme": "CPO", "change_pct_band": band,
                          "institutional": ""})
            ms.update_prediction_result(pid, next_day_return=1.0, hit=1)

        clusters = _query_hit_clusters(days=365)

        assert {c["change_pct_band"] for c in clusters} == {"strong", "flat"}
        assert len(clusters) == 2, "one cluster per band, not one merged group"

    def test_a_row_without_the_band_still_forms_a_cluster(self, tmp_path,
                                                          monkeypatch):
        """Rows written before the key existed keep their evidence: the cluster
        forms, and the pattern it generates simply omits the condition it cannot
        pin rather than pinning a null."""
        from alpha_agents.evolution.playbook import (_pattern_from_cluster,
                                                     _query_hit_clusters)

        monkeypatch.setattr(ms, "MEMORY_DB_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(ms._local, "conn", None, raising=False)
        today = date.today().isoformat()
        for i in range(3):
            pid = ms.save_prediction(
                date=today, report_type="intraday", code=f"60000{i}",
                name="A", direction="bullish", confidence="medium",
                theme_line="半导体", entry_price=10.0, reason="r",
                features={"theme": "半导体", "institutional": ""})
            ms.update_prediction_result(pid, next_day_return=1.0, hit=1)

        clusters = _query_hit_clusters(days=365)

        assert len(clusters) == 1
        assert clusters[0]["change_pct_band"] is None
        fields = {c["field"] for c in _pattern_from_cluster(clusters[0])["conditions"]}
        assert fields == {"theme"}

    def test_a_decision_without_the_band_does_not_satisfy_a_band_condition(self):
        """Fail closed. Missing must not read as "any band", or every pattern
        carrying this dimension would silently widen to cover them all."""
        from alpha_agents.evolution.playbook import match_playbook

        pattern = ('{"conditions":[{"field":"change_pct_band","op":"==",'
                   '"value":"strong"}]}')
        pb = {"id": 1, "name": "Auto: CPO-大涨", "weight": 1.0,
              "status": "active", "pattern_json": pattern}
        with patch("alpha_agents.evolution.playbook.get_active_playbooks",
                   return_value=[pb, {**pb, "id": 2}]):
            assert match_playbook({"theme": "CPO"}) is None
            assert match_playbook({"theme": "CPO",
                                   "change_pct_band": "strong"}) is not None
