"""Production evidence: the analyser's input, read from the live book.

The defect this closes was structural. `evolution.evidence` could state a
proposition and `evolution.variant` could build from one, but the only caller
of either was the replay runner — so in production nothing produced a
candidate that could pass D25, because every production proposer writes empty
citation buckets and the one thing that fills them lived in a script.

These tests pin the seam: the corpus read, the per-code T-1 lookup, and the
honest handling of a trade that cannot be cited.
"""

from __future__ import annotations

import pytest

from alpha_agents.data import memory_store
from alpha_agents.evolution import evidence as EV
from alpha_agents.evolution import evidence_source as ES


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


class TestPreviousSession:
    def test_it_returns_the_session_before(self):
        days = ["2026-01-05", "2026-01-06", "2026-01-07"]
        assert ES.previous_session("2026-01-07", days) == "2026-01-06"

    def test_the_first_session_has_no_previous(self):
        """None rather than the closest earlier bar: a T-1 feature that
        silently means "the nearest bar I could find" is a different feature,
        and the caller drops the trade rather than measuring the wrong day."""
        assert ES.previous_session("2026-01-05", ["2026-01-05"]) is None

    def test_an_unknown_day_is_none(self):
        assert ES.previous_session("2099-01-01", ["2026-01-05"]) is None


class TestItReadsTheLiveBook:
    def _close(self, conn, *, position_id, code, order_date, close_date,
               return_pct, episode=True):
        conn.execute(
            "INSERT INTO virtual_portfolio (id, code, trader_id, status, "
            " order_date, open_price, shares, close_date, return_pct) "
            "VALUES (?,?,?,'closed',?,10.0,100,?,?)",
            (position_id, code, "default", order_date, close_date, return_pct))
        if episode:
            conn.execute(
                "INSERT INTO episodes (trader_id, code, status, position_id, "
                " opened_at) VALUES ('default', ?, 'closed', ?, ?)",
                (code, position_id, order_date))
        conn.commit()

    def test_it_collects_closed_positions_with_their_episode(
            self, store, monkeypatch):
        monkeypatch.setattr(ES, "_sessions",
                            lambda: ["2026-01-05", "2026-01-06"])
        monkeypatch.setattr("alpha_agents.data.market_history.get_klines_for_date",
                            lambda day: [{"code": "600000", "change_pct": 2.5}])
        self._close(store, position_id=1, code="600000",
                    order_date="2026-01-06", close_date="2026-01-06",
                    return_pct=3.0)
        got = ES.collect_closed_trades(as_of="2026-01-06")
        assert len(got) == 1
        assert got[0].episode_id is not None
        assert got[0].t1_change == 2.5

    def test_a_position_with_no_episode_is_kept_but_not_citable(
            self, store, monkeypatch):
        """Its return is a fact and it counts towards n; it just cannot be
        cited, so the analyser excludes it from the split rather than citing
        something it is not."""
        monkeypatch.setattr(ES, "_sessions", lambda: ["2026-01-05"])
        monkeypatch.setattr("alpha_agents.data.market_history.get_klines_for_date",
                            lambda day: [{"code": "600000", "change_pct": 1.0}])
        self._close(store, position_id=2, code="600000",
                    order_date="2026-01-05", close_date="2026-01-05",
                    return_pct=-1.0, episode=False)
        got = ES.collect_closed_trades(as_of="2026-01-05")
        assert len(got) == 1 and got[0].episode_id is None

    def test_a_position_closed_after_the_cutoff_is_excluded(
            self, store, monkeypatch):
        monkeypatch.setattr(ES, "_sessions", lambda: ["2026-01-05"])
        monkeypatch.setattr("alpha_agents.data.market_history.get_klines_for_date",
                            lambda day: [])
        self._close(store, position_id=3, code="600000",
                    order_date="2026-01-05", close_date="2026-02-01",
                    return_pct=1.0)
        assert ES.collect_closed_trades(as_of="2026-01-31") == []

    def test_a_missing_t1_bar_leaves_the_feature_none(self, store, monkeypatch):
        """The trade is still returned; the analyser drops it for the split
        and the bundle reports why."""
        monkeypatch.setattr(ES, "_sessions", lambda: ["2026-01-05", "2026-01-06"])
        monkeypatch.setattr("alpha_agents.data.market_history.get_klines_for_date",
                            lambda day: [])
        self._close(store, position_id=4, code="600000",
                    order_date="2026-01-06", close_date="2026-01-06",
                    return_pct=1.0)
        got = ES.collect_closed_trades(as_of="2026-01-06")
        assert got[0].t1_change is None


class TestItProducesAValidCandidate:
    def _seed(self, store, monkeypatch):
        days = [f"2026-01-{d:02d}" for d in range(1, 8)]
        monkeypatch.setattr(ES, "_sessions", lambda: days)
        changes = {"600001": 5.0, "600002": 4.0, "600003": 1.0, "600004": 0.5}
        monkeypatch.setattr(
            "alpha_agents.data.market_history.get_klines_for_date",
            lambda day: [{"code": c, "change_pct": v}
                         for c, v in changes.items()])
        for i, (code, ret) in enumerate(
                [("600001", -10.0), ("600002", -8.0),
                 ("600003", 6.0), ("600004", 7.0)], start=1):
            store.execute(
                "INSERT INTO virtual_portfolio (id, code, trader_id, status, "
                " order_date, open_price, shares, close_date, return_pct) "
                "VALUES (?,?,?,'closed','2026-01-05',10.0,100,'2026-01-05',?)",
                (i, code, "default", ret))
            store.execute(
                "INSERT INTO episodes (trader_id, code, status, position_id, "
                " opened_at) VALUES ('default', ?, 'closed', ?, '2026-01-05')",
                (code, i))
        store.commit()

    def test_it_writes_an_observation_with_a_replayable_bundle(
            self, store, monkeypatch):
        from alpha_agents.data import learning_candidates as LC
        import json
        self._seed(store, monkeypatch)
        result = ES.observe(as_of="2026-01-07")
        assert result is not None and result["n"] == 4
        row = LC.get_candidate(result["candidate_id"])
        assert row["status"] == LC.OBSERVATION, (
            "the pipeline must not advance a candidate; promotion is human")
        bundle = json.loads(row["payload_json"])["evidence_bundle"]
        assert bundle["matching_rule"] == EV.RULE_T1_VS_MEDIAN
        assert bundle["eligible"] == 4

    def test_too_thin_a_book_returns_none_not_a_weaker_claim(
            self, store, monkeypatch):
        monkeypatch.setattr(ES, "_sessions", lambda: ["2026-01-05"])
        monkeypatch.setattr("alpha_agents.data.market_history.get_klines_for_date",
                            lambda day: [{"code": "600001", "change_pct": 1.0}])
        store.execute(
            "INSERT INTO virtual_portfolio (id, code, trader_id, status, "
            " order_date, open_price, shares, close_date, return_pct) "
            "VALUES (1,'600001','default','closed','2026-01-05',10.0,100,"
            " '2026-01-05',1.0)")
        store.execute(
            "INSERT INTO episodes (trader_id, code, status, position_id, "
            " opened_at) VALUES ('default','600001','closed',1,'2026-01-05')")
        store.commit()
        assert ES.observe(as_of="2026-01-07") is None

    def test_an_empty_book_returns_none(self, store, monkeypatch):
        monkeypatch.setattr(ES, "_sessions", lambda: ["2026-01-05"])
        assert ES.observe(as_of="2026-01-07") is None


class TestTheWindowStartComesFromTheData:
    def test_it_is_the_earliest_close(self):
        trades = [
            EV.Trade(1, "600000", 1.0, "2026-01-09", 1.0, episode_id=1),
            EV.Trade(2, "600001", 1.0, "2026-01-05", 1.0, episode_id=2),
        ]
        assert ES.eligible_window_start(trades) == "2026-01-05"

    def test_an_empty_list_does_not_invent_a_window(self):
        got = ES.eligible_window_start([])
        assert got and len(got) == 10
