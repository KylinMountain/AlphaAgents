"""G3 — principle utility comes from graded predictions, never from an LLM."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from alpha_agents.evolution import principle_scoring as ps


@pytest.fixture
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store as ms

    conn = sqlite3.connect(str(tmp_path / "memory.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(ms._SCHEMA)
    conn.commit()
    monkeypatch.setattr(ms, "_get_conn", lambda: conn)
    monkeypatch.setattr(ps, "_get_conn", lambda: conn)
    yield ms, conn
    conn.close()


def _grade(conn, code, date, *, hit, brier):
    """A prediction already scored against market data."""
    conn.execute(
        "INSERT INTO predictions (date, report_type, code, name, direction, "
        "confidence, prob, hit, brier, scored_at) "
        "VALUES (?, 'morning', ?, 'X', 'bullish', 'medium', 0.6, ?, ?, ?)",
        (date, code, 1 if hit else 0, brier, "2026-09-07 16:00:00"),
    )
    conn.commit()


def _principle(evidence, pid=1, status="active"):
    return {"id": pid, "evidence": json.dumps(evidence), "status": status}


class TestDecayWeight:
    def test_today_is_full_weight(self):
        now = datetime(2026, 9, 7)
        assert ps._decay_weight("2026-09-07", now) == pytest.approx(1.0)

    def test_one_half_life_is_half_weight(self):
        now = datetime(2026, 9, 7)
        old = (now - timedelta(days=ps.HALF_LIFE_DAYS)).strftime("%Y-%m-%d")
        assert ps._decay_weight(old, now) == pytest.approx(0.5, abs=0.01)

    def test_weight_decreases_monotonically(self):
        now = datetime(2026, 9, 7)
        ws = [ps._decay_weight((now - timedelta(days=d)).strftime("%Y-%m-%d"), now)
              for d in (0, 30, 90, 180, 365)]
        assert ws == sorted(ws, reverse=True)

    def test_future_and_garbage_dates_carry_no_weight(self):
        now = datetime(2026, 9, 7)
        assert ps._decay_weight("2026-12-31", now) == 0.0
        assert ps._decay_weight("不是日期", now) == 0.0
        assert ps._decay_weight(None, now) == 0.0


class TestScorePrinciple:
    def test_no_evidence_is_not_judged(self, store):
        got = ps.score_principle(_principle([]))
        assert got["graded"] == 0 and got["should_retire"] is False
        assert got["win_rate"] is None

    def test_ungraded_evidence_is_not_judged(self, store):
        # Cited predictions exist in theory but were never scored.
        got = ps.score_principle(_principle([{"code": "300308", "date": "2026-09-01"}]))
        assert got["cases"] == 1 and got["graded"] == 0
        assert got["should_retire"] is False

    def test_win_rate_comes_from_graded_predictions(self, store):
        _ms, conn = store
        today = datetime.now().strftime("%Y-%m-%d")
        ev = []
        for i in range(4):
            code = f"30030{i}"
            _grade(conn, code, today, hit=i < 3, brier=0.2)
            ev.append({"code": code, "date": today})
        got = ps.score_principle(_principle(ev))
        assert got["graded"] == 4
        assert got["win_rate"] == pytest.approx(0.75, abs=0.01)

    def test_thin_evidence_is_recorded_but_never_retires(self, store):
        _ms, conn = store
        today = datetime.now().strftime("%Y-%m-%d")
        ev = []
        for i in range(ps.MIN_CASES_TO_JUDGE - 1):
            code = f"00000{i}"
            _grade(conn, code, today, hit=False, brier=0.9)  # dreadful
            ev.append({"code": code, "date": today})
        got = ps.score_principle(_principle(ev))
        assert got["win_rate"] == pytest.approx(0.0)
        assert got["should_retire"] is False   # too few cases to judge

    def test_bad_win_rate_retires_once_there_is_enough_evidence(self, store):
        _ms, conn = store
        today = datetime.now().strftime("%Y-%m-%d")
        ev = []
        for i in range(6):
            code = f"10000{i}"
            _grade(conn, code, today, hit=False, brier=0.2)
            ev.append({"code": code, "date": today})
        got = ps.score_principle(_principle(ev))
        assert got["should_retire"] is True
        assert "胜率" in got["reason"]

    def test_bad_brier_retires_even_when_win_rate_is_fine(self, store):
        _ms, conn = store
        today = datetime.now().strftime("%Y-%m-%d")
        ev = []
        for i in range(6):
            code = f"20000{i}"
            # Wins, but the probabilities were badly calibrated.
            _grade(conn, code, today, hit=True, brier=0.45)
            ev.append({"code": code, "date": today})
        got = ps.score_principle(_principle(ev))
        assert got["should_retire"] is True
        assert "Brier" in got["reason"]

    def test_good_evidence_survives(self, store):
        _ms, conn = store
        today = datetime.now().strftime("%Y-%m-%d")
        ev = []
        for i in range(6):
            code = f"40000{i}"
            _grade(conn, code, today, hit=True, brier=0.12)
            ev.append({"code": code, "date": today})
        got = ps.score_principle(_principle(ev))
        assert got["should_retire"] is False
        assert got["win_rate"] == pytest.approx(1.0)

    def test_recent_evidence_outweighs_stale_evidence(self, store):
        """Non-stationarity: old wins should not prop up a failing rule."""
        _ms, conn = store
        now = datetime.now()
        recent = now.strftime("%Y-%m-%d")
        stale = (now - timedelta(days=ps.HALF_LIFE_DAYS * 3)).strftime("%Y-%m-%d")
        ev = []
        for i in range(3):                       # old wins, heavily decayed
            _grade(conn, f"5000{i}", stale, hit=True, brier=0.1)
            ev.append({"code": f"5000{i}", "date": stale})
        for i in range(3):                       # recent losses, full weight
            _grade(conn, f"6000{i}", recent, hit=False, brier=0.1)
            ev.append({"code": f"6000{i}", "date": recent})
        got = ps.score_principle(_principle(ev))
        assert got["win_rate"] < 0.3             # unweighted would be 0.5

    def test_malformed_evidence_does_not_raise(self, store):
        for bad in ("not json", None, "[1,2,3]", '[{"no": "code"}]'):
            got = ps.score_principle({"id": 1, "evidence": bad})
            assert got["should_retire"] is False


class TestRescoreAll:
    def test_measures_without_writing_or_retiring(self, store):
        """A contradicted rule is flagged, not silently weakened.

        `win_rate` and `status` are both rendered into the principle prompt
        (`inject_principles`), so writing either one changes what the model
        reads. The measurement is kept in the candidate store instead; the
        live row keeps its status until an approved version says otherwise.
        """
        ms, conn = store
        today = datetime.now().strftime("%Y-%m-%d")
        ev = []
        for i in range(6):
            code = f"70000{i}"
            _grade(conn, code, today, hit=False, brier=0.2)
            ev.append({"code": code, "date": today, "outcome": "miss"})

        ms.create_trading_principle(
            principle="放量滞涨即派发", pattern_description="p",
            category="exit", action_guidance="a", evidence=ev, today=today,
        )
        assert conn.execute(
            "SELECT win_rate FROM trading_principles"
        ).fetchone()["win_rate"] is None      # the LLM path never filled it

        result = ps.rescore_all_principles()
        assert result["scored"] == 1
        assert result["retired"] == 0

        # The live row is exactly as the writer left it.
        row = conn.execute(
            "SELECT win_rate, status FROM trading_principles"
        ).fetchone()
        assert row["win_rate"] is None
        assert row["status"] == "active"

        # The measurement is not lost — it is stored where it cannot act.
        obs = conn.execute("SELECT payload_json FROM learning_observations").fetchall()
        assert len(obs) == 1
        payload = json.loads(obs[0]["payload_json"])
        assert payload["win_rate"] == pytest.approx(0.0)
        assert payload["should_retire"] is True

    def test_no_principles_is_a_no_op(self, store):
        assert ps.rescore_all_principles() == {"scored": 0, "retired": 0, "total": 0}


class TestLlmCannotWeaken:
    def test_weaken_operation_is_ignored(self, store, monkeypatch):
        """The Echo Gap fix: a model may propose, only the market judges."""
        ms, conn = store
        from alpha_agents.evolution import lessons

        today = datetime.now().strftime("%Y-%m-%d")
        ms.create_trading_principle(
            principle="某条原则", pattern_description="p", category="entry",
            action_guidance="a", evidence=[{"code": "300308", "date": today}],
            today=today,
        )
        pid = conn.execute("SELECT id FROM trading_principles").fetchone()["id"]

        monkeypatch.setattr(lessons, "get_recent_daily_lessons",
                            lambda days: [{"content": "x", "date": today}])
        monkeypatch.setattr(lessons, "_call_consolidation_llm",
                            lambda lessons_, principles: {
                                "operations": [{"op": "weaken",
                                                "principle_id": pid,
                                                "reason": "我觉得不好"}]})

        counts = lessons.consolidate_principles(today)
        assert counts["weakened"] == 0
        status = conn.execute(
            "SELECT status FROM trading_principles WHERE id = ?", (pid,)
        ).fetchone()["status"]
        assert status == "active"     # untouched by the model's opinion
