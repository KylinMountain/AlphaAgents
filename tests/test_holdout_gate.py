"""G2 — a candidate is promoted only if forward validation says so."""

import sqlite3

import pytest

from alpha_agents.evolution.holdout_gate import (
    BRIER_TOLERANCE, MIN_VALIDATION_SAMPLES, evaluate_candidate, forward_window,
    get_gate_history, paired_brier_test, record_gate_decision,
)


def _rows(dates, brier=0.2, report_type="morning"):
    return [{"date": d, "code": f"C{i}", "brier": brier,
             "report_type": report_type}
            for i, d in enumerate(dates)]


class TestForwardWindow:
    """Validation is what happened after the candidate existed.

    The boundary lives in one function on purpose. It used to be written
    twice — once here as ``date <= created`` and once in ``run_gate`` as a
    window the caller passed by hand — and the second copy is where the
    zero-length window came from.
    """

    def test_validation_is_strictly_after_the_freeze(self):
        rows = _rows([f"2026-09-0{d}" for d in range(1, 8)])
        val = forward_window(rows, "2026-09-04")
        assert [r["date"] for r in val] == [
            "2026-09-05", "2026-09-06", "2026-09-07"]

    def test_the_freeze_day_itself_is_not_validation(self):
        """The candidate was distilled from that day; it cannot test on it."""
        assert forward_window(_rows(["2026-09-04"]), "2026-09-04") == []

    def test_a_brand_new_candidate_has_no_validation(self):
        assert forward_window(_rows(["2026-09-01", "2026-09-02"]),
                              "2026-09-02") == []

    def test_a_zero_length_window_is_empty_not_everything(self):
        """The old bug, as a property: same day in and out yields nothing."""
        rows = _rows(["2026-09-04"])
        assert forward_window(rows, "2026-09-04") == []

    def test_empty_input(self):
        assert forward_window([], "2026-09-04") == []

    def test_rows_without_dates_are_dropped(self):
        assert forward_window([{"code": "X"}], "2026-09-04") == []

    def test_a_timestamped_freeze_still_cuts_by_day(self):
        """``frozen_at`` is stored with a time; the boundary is a day."""
        rows = _rows(["2026-09-04", "2026-09-05"])
        got = forward_window(rows, "2026-09-04T09:31:00.000Z")
        assert [r["date"] for r in got] == ["2026-09-05"]


class TestPairedBrierTest:
    def test_identical_series_show_no_difference(self):
        got = paired_brier_test([0.2] * 10, [0.2] * 10)
        assert got["mean_diff"] == 0.0
        assert got["t_stat"] == 0.0

    def test_improvement_is_negative(self):
        got = paired_brier_test([0.30] * 20, [0.20] * 20)
        assert got["mean_diff"] < 0

    def test_degradation_is_positive(self):
        got = paired_brier_test([0.20] * 20, [0.30] * 20)
        assert got["mean_diff"] > 0

    def test_consistent_improvement_gives_a_strong_t(self):
        champ = [0.30 + i * 0.001 for i in range(30)]
        chall = [c - 0.05 for c in champ]
        got = paired_brier_test(champ, chall)
        assert got["t_stat"] < -5     # same shift on every pair

    def test_noisy_difference_gives_a_weak_t(self):
        champ = [0.2, 0.5, 0.1, 0.6, 0.3] * 6
        chall = [0.5, 0.2, 0.6, 0.1, 0.4] * 6
        assert abs(paired_brier_test(champ, chall)["t_stat"]) < 2

    def test_too_few_pairs(self):
        assert paired_brier_test([0.2], [0.3])["mean_diff"] is None

    def test_none_values_are_skipped(self):
        got = paired_brier_test([0.2, None, 0.3], [0.2, 0.4, 0.3])
        assert got["n"] == 2


class TestEvaluateCandidate:
    def _pair(self, n, champ_brier, chall_brier):
        champ = [{"date": f"2026-09-{i%28+1:02d}", "code": f"C{i}",
                  "brier": champ_brier} for i in range(n)]
        chall = [{"date": r["date"], "code": r["code"], "brier": chall_brier}
                 for r in champ]
        return champ, chall

    def test_abstains_below_the_sample_floor(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES - 1, 0.4, 0.1)
        got = evaluate_candidate(champ, chall)
        assert got["promote"] is False and got["abstained"] is True
        assert "维持 champion" in got["reason"]

    def test_promotes_a_clear_improvement(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES + 5, 0.30, 0.20)
        got = evaluate_candidate(champ, chall)
        assert got["promote"] is True and got["abstained"] is False

    def test_rejects_a_degradation(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES + 5, 0.20, 0.30)
        got = evaluate_candidate(champ, chall)
        assert got["promote"] is False and got["abstained"] is False
        assert "退化" in got["reason"]

    def test_a_tie_is_promoted_since_it_did_not_degrade(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES + 5, 0.25, 0.25)
        assert evaluate_candidate(champ, chall)["promote"] is True

    def test_tolerance_absorbs_rounding(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES + 5,
                                  0.25, 0.25 + BRIER_TOLERANCE / 2)
        assert evaluate_candidate(champ, chall)["promote"] is True

    def test_only_shared_predictions_are_compared(self):
        """Unpaired rows must not sneak in — the test is paired."""
        champ = [{"date": "2026-09-01", "code": f"C{i}", "brier": 0.2}
                 for i in range(40)]
        chall = [{"date": "2026-09-01", "code": f"C{i}", "brier": 0.1}
                 for i in range(5)]        # only 5 overlap
        got = evaluate_candidate(champ, chall)
        assert got["n"] == 5 and got["abstained"] is True

    def test_no_overlap_abstains(self):
        champ = [{"date": "2026-09-01", "code": "A", "brier": 0.2}]
        chall = [{"date": "2026-09-02", "code": "B", "brier": 0.1}]
        assert evaluate_candidate(champ, chall)["abstained"] is True


class TestOutcomeNamesTheVerdict:
    """The audit row is read months later by a person, not by this code.

    Two booleans (``promote``, ``abstained``) are easy to read backwards —
    ``promote=False, abstained=False`` is a *rejection*, and that is the
    reading most likely to be got wrong. ``outcome`` says it in one word.
    """

    def _pair(self, n, champ_brier, chall_brier):
        champ = [{"date": f"2026-09-{i%28+1:02d}", "code": f"C{i}",
                  "brier": champ_brier} for i in range(n)]
        chall = [{"date": r["date"], "code": r["code"], "brier": chall_brier}
                 for r in champ]
        return champ, chall

    def test_abstention_says_insufficient(self):
        champ, chall = self._pair(3, 0.3, 0.1)
        assert evaluate_candidate(champ, chall)["outcome"] == "insufficient"

    def test_improvement_says_promote(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES + 5, 0.30, 0.20)
        assert evaluate_candidate(champ, chall)["outcome"] == "promote"

    def test_degradation_says_reject_not_merely_not_promote(self):
        champ, chall = self._pair(MIN_VALIDATION_SAMPLES + 5, 0.20, 0.30)
        got = evaluate_candidate(champ, chall)
        assert got["outcome"] == "reject"
        assert got["promote"] is False and got["abstained"] is False

    def test_the_three_outcomes_are_distinct(self):
        """A reject and an abstention must not collapse into one label."""
        abstained = evaluate_candidate(*self._pair(3, 0.3, 0.1))["outcome"]
        rejected = evaluate_candidate(
            *self._pair(MIN_VALIDATION_SAMPLES + 5, 0.2, 0.3))["outcome"]
        promoted = evaluate_candidate(
            *self._pair(MIN_VALIDATION_SAMPLES + 5, 0.3, 0.2))["outcome"]
        assert len({abstained, rejected, promoted}) == 3


@pytest.fixture
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store as ms

    conn = sqlite3.connect(str(tmp_path / "memory.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(ms._SCHEMA)
    conn.commit()
    monkeypatch.setattr(ms, "_get_conn", lambda: conn)
    yield ms, conn
    conn.close()


class TestGateAudit:
    """Rejections must leave a trace, or the same candidate returns weekly."""

    def test_records_a_rejection(self, store):
        record_gate_decision("cand-A", {
            "promote": False, "abstained": False, "n": 30,
            "mean_diff": 0.04, "t_stat": 2.6, "reason": "Brier 退化",
        }, today="2026-09-07")
        history = get_gate_history()
        assert len(history) == 1
        assert history[0]["promoted"] == 0
        assert history[0]["candidate"] == "cand-A"
        assert "退化" in history[0]["reason"]

    def test_records_a_promotion(self, store):
        record_gate_decision("cand-B", {
            "promote": True, "abstained": False, "n": 30,
            "mean_diff": -0.03, "t_stat": -2.9, "reason": "通过",
        }, today="2026-09-07")
        assert get_gate_history()[0]["promoted"] == 1

    def test_records_an_abstention_distinctly(self, store):
        record_gate_decision("cand-C", {
            "promote": False, "abstained": True, "n": 3,
            "reason": "样本不足",
        }, today="2026-09-07")
        row = get_gate_history()[0]
        assert row["promoted"] == 0 and row["abstained"] == 1

    def test_history_is_newest_first(self, store):
        for name in ("first", "second", "third"):
            record_gate_decision(name, {"promote": False, "abstained": True},
                                 today="2026-09-07")
        assert [r["candidate"] for r in get_gate_history()] == [
            "third", "second", "first"]

    def test_recording_never_raises(self, monkeypatch):
        """Audit failure must not break the review."""
        from alpha_agents.data import memory_store as ms
        monkeypatch.setattr(ms, "_get_conn",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        record_gate_decision("x", {"promote": False}, today="2026-09-07")

    def test_the_version_id_lands_in_its_own_column(self, store):
        record_gate_decision("trader#7", {
            "promote": True, "abstained": False, "n": 30,
            "policy_version_id": 7, "validation_days": 6,
            "outcome": "promote", "reason": "通过",
        }, today="2026-09-07")
        row = get_gate_history()[0]
        assert row["policy_version_id"] == 7
        assert row["validation_days"] == 6
        assert row["outcome"] == "promote"

    def test_validation_days_is_queryable_not_buried_in_json(self, store):
        """D7's own "recognise it is fixed" criterion, as SQL."""
        record_gate_decision("trader#7", {
            "promote": False, "abstained": True, "policy_version_id": 7,
            "validation_days": 0, "outcome": "insufficient",
        }, today="2026-09-07")
        row = store[1].execute(
            "SELECT COUNT(*) n FROM gate_decisions "
            "WHERE validation_days IS NOT NULL").fetchone()
        assert row["n"] == 1


class TestGateDecisionsAreFoundByVersion:
    """Eligibility is keyed on the version id, not on a string we compose.

    The first version of this class claimed ``candidate LIKE '%#12'`` would
    also match ``#120``. A mutation probe showed that is false — ``LIKE`` is
    anchored at the end, so the pattern does not match a longer number. The
    claim was wrong, and the test asserting it passed under both
    implementations, which is to say it tested nothing.

    The real reason for the column is different and sharper: the label is
    prose, and joining a promotion to its evidence by parsing prose means that
    changing the label silently empties the eligibility list. An empty list
    reads as "not eligible" — the safe-looking answer, and the same shape as
    the defect this whole phase exists to fix.
    """

    def _record(self, version_id, outcome, days, *, abstained=False,
                candidate=None):
        record_gate_decision(candidate or f"trader#{version_id}", {
            "promote": outcome == "promote", "abstained": abstained,
            "policy_version_id": version_id, "validation_days": days,
            "outcome": outcome, "reason": outcome,
        }, today="2026-09-07")

    def test_the_label_format_is_not_load_bearing(self, store):
        """The id is stored, so a relabelled verdict is still findable."""
        from alpha_agents.evolution.holdout_gate import get_gate_decisions

        self._record(7, "promote", 5, candidate="trader (frozen 2026-06-01)")
        assert [r["policy_version_id"]
                for r in get_gate_decisions(policy_version_id=7)] == [7]

    def test_another_version_is_not_returned(self, store):
        from alpha_agents.evolution.holdout_gate import get_gate_decisions

        self._record(12, "promote", 5)
        self._record(120, "reject", 5)
        assert [r["policy_version_id"]
                for r in get_gate_decisions(policy_version_id=12)] == [12]

    def test_no_filter_returns_everything(self, store):
        from alpha_agents.evolution.holdout_gate import get_gate_decisions

        self._record(12, "promote", 5)
        self._record(120, "reject", 5)
        assert len(get_gate_decisions()) == 2

    def test_a_rejection_is_not_promotion_eligible(self, store):
        """A reject compares something real and says no. Inverting it is the
        exact mistake the gate exists to prevent."""
        from alpha_agents.evolution.holdout_gate import eligible_decisions

        self._record(3, "reject", 9)
        assert eligible_decisions(3) == []

    def test_an_abstention_is_not_promotion_eligible(self, store):
        from alpha_agents.evolution.holdout_gate import eligible_decisions

        self._record(3, "insufficient", 0, abstained=True)
        assert eligible_decisions(3) == []

    def test_a_promote_verdict_is_eligible(self, store):
        from alpha_agents.evolution.holdout_gate import eligible_decisions

        self._record(3, "promote", 9)
        assert len(eligible_decisions(3)) == 1

    def test_eligibility_does_not_leak_across_versions(self, store):
        from alpha_agents.evolution.holdout_gate import eligible_decisions

        self._record(3, "promote", 9)
        assert eligible_decisions(30) == []
