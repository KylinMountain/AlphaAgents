"""The challenger: a frozen baseline that forecasts and is graded.

U2 of Phase 4. ``holdout_gate`` had the decision rule and no producer, so the
challenger side of every comparison was empty — the defect D7 recorded, one
level down. This slice supplies the producer, and the property that matters
most is where its forecasts are *stored*: a separate table, because four
champion paths read ``predictions`` without a report-type filter and would
have picked a shadow row up.

The tests are in eight groups:

1. A run is bound to a frozen policy version — an unbound run could never be
   promoted, so it is refused rather than recorded.
2. The producer forecasts the champion's codes for the day, because §12
   requires a common opportunity panel and a paired test.
3. Emitting is idempotent per (run, date, code): two rows for one stock would
   be two forecasts, and the paired test would count the stock twice.
4. A closed run produces nothing, so an experiment's denominator cannot grow
   after the fact.
5. Grading comes from market data, and an absent price leaves the forecast
   unscored rather than grading it as a miss.
6. Isolation, which is the reason for the separate table: the champion's own
   readers cannot see a shadow forecast, and emitting moves nothing outside
   the two shadow tables.
7. Coverage reports the *paired* count, not the forecast count — the honest
   distance from a verdict.
8. Integrity reports structural holes rather than repairing them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from alpha_agents.data import memory_store, policy_registry as PR
from alpha_agents.data import scoring
from alpha_agents.evolution import holdout_gate
from alpha_agents.evolution import shadow as SH


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private database.

    ``MEMORY_DB_PATH`` is imported into ``memory_store`` at module scope and is
    not read from the environment — patching the module is the only way, and a
    subprocess with the env var set writes to the real ``data/memory.db``.
    """
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


@pytest.fixture()
def version(store) -> int:
    """A frozen policy version for a run to be bound to."""
    return PR.freeze(
        sources={
            "prompts": {"morning_scan.md": "aaa"},
            "model": {"agent_model": "qwen-plus"},
            "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
            "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
            "knowledge": {"snapshot_id": None},
            # The pointer-controlled source: the decision parameters the
            # trading path reads. What is in force before anything is installed
            # is the code default block, which is what a freeze records.
            "decision": dict(scoring.DEFAULT_DECISION_PARAMS),
        },
        created_by="kylin", reason="frozen for the forward window")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _champion(store, date: str, code: str, *,
              report_type: str = "intraday_signal", hit=None) -> int:
    """A champion prediction, written through the real save path."""
    pred_id = memory_store.save_prediction(
        date, report_type, code, "X", "看多", "high", "t", 1.0, "reason",
        prob=0.6, trader_id="default", horizon_days=5)
    if hit is not None:
        store.execute("UPDATE predictions SET hit = ? WHERE id = ?",
                      (hit, pred_id))
        store.commit()
    return pred_id


def _open(version: int, **over) -> int:
    kwargs = dict(policy_version_id=version, reason="measure the baseline",
                  report_type="intraday_signal")
    kwargs.update(over)
    return SH.open_run(**kwargs)


def _dump(conn) -> dict:
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        if name.startswith("sqlite_"):
            continue
        rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        out[name] = sorted(tuple(row) for row in rows)
    return out


def _fake_score(brier: float = 0.25, outcome: bool = True):
    def _score(code, entry_date, prob, horizon=None):
        return {"brier": brier, "log_score": 0.69, "excess_return": 0.01,
                "residual_alpha": 0.005, "outcome": outcome,
                "scored_at": "2026-06-01 10:00:00"}
    return _score


@pytest.fixture(autouse=True)
def window_closed(monkeypatch):
    """The market has traded every window in this module shut.

    Autouse because this file is about the shadow *record* — runs, binding,
    isolation, grading bookkeeping — and every grading test here means "the
    window is over, grade it". The sandbox has no ``daily_kline`` at all, which
    is the other branch (deferred) and has its own tests: the real predicate in
    ``test_scoring.py``, the deferred accounting in ``test_score_due_predictions.py``
    and in ``TestGrading.test_a_window_that_is_still_open_is_deferred`` below.
    """
    monkeypatch.setattr(scoring, "evidence_window_closed",
                        lambda entry_date, horizon=5: True)


# ── 1. A run is bound to a version ─────────────────────────────────────


class TestARunIsBoundToAVersion:
    def test_a_run_names_the_version_it_measures(self, store, version):
        run = SH.get_run(_open(version))
        assert run["policy_version_id"] == version
        assert run["status"] == "open"
        assert run["producer"] == SH.BASELINE_NAME

    def test_an_unfrozen_version_is_refused(self, store):
        with pytest.raises(SH.ShadowError) as exc:
            _open(4242)
        assert "could never be promoted" in str(exc.value)

    def test_a_run_gets_its_own_trader_namespace(self, store, version):
        run = SH.get_run(_open(version))
        assert run["trader_id"] == f"{SH.SHADOW_TRADER_PREFIX}{version}"
        assert run["trader_id"] != "default"

    def test_the_namespace_can_be_named_explicitly(self, store, version):
        run = SH.get_run(_open(version, trader_id="shadow-alpha"))
        assert run["trader_id"] == "shadow-alpha"

    def test_a_reason_is_required(self, store, version):
        with pytest.raises(SH.ShadowError):
            _open(version, reason="")

    def test_a_second_open_run_of_the_same_version_is_refused(
            self, store, version):
        # Two shadows of one version would be counted twice by any later
        # comparison, and the duplicate would be silent.
        first = _open(version)
        with pytest.raises(SH.ShadowError) as exc:
            _open(version)
        assert f"run #{first}" in str(exc.value)

    def test_a_different_report_type_is_a_separate_experiment(
            self, store, version):
        first = _open(version, report_type="intraday_signal")
        second = _open(version, report_type="morning")
        assert first != second

    def test_closing_lets_a_new_run_open(self, store, version):
        first = _open(version)
        SH.close_run(first, reason="enough")
        assert SH.get_run(first)["status"] == "closed"
        assert _open(version) != first

    def test_closing_twice_is_refused(self, store, version):
        run = _open(version)
        SH.close_run(run, reason="enough")
        with pytest.raises(SH.ShadowError):
            SH.close_run(run, reason="again")

    def test_closing_an_unknown_run_is_refused(self, store):
        with pytest.raises(SH.ShadowError):
            SH.close_run(999, reason="nothing")

    def test_open_run_for_finds_the_live_experiment(self, store, version):
        run = _open(version)
        assert SH.open_run_for(version, "intraday_signal")["id"] == run
        assert SH.open_run_for(version, "morning") is None

    def test_runs_can_be_filtered_by_status(self, store, version):
        run = _open(version)
        SH.close_run(run, reason="enough")
        assert SH.runs(status="open") == []
        assert [r["id"] for r in SH.runs(status="closed")] == [run]


# ── 2. The producer ────────────────────────────────────────────────────


class TestTheProducer:
    def test_it_forecasts_the_champions_codes(self, store, version):
        _champion(store, _today(), "600000")
        _champion(store, _today(), "000001")
        run = _open(version)
        ids = SH.emit_for_date(run, _today())
        assert len(ids) == 2
        assert [p["code"] for p in SH.predictions_for(run)] == ["000001", "600000"]

    def test_the_forecast_is_the_baseline_probability(self, store, version):
        _champion(store, _today(), "600000")
        run = _open(version)
        SH.emit_for_date(run, _today())
        assert SH.predictions_for(run)[0]["prob"] == SH.BASELINE_PROB

    def test_the_panel_is_the_shared_opportunity_panel(self, store, version):
        _champion(store, _today(), "600000")
        run = _open(version)
        assert SH.panel_for(_today(), "intraday_signal") == ["600000"]
        SH.emit_for_date(run, _today())
        assert SH.predictions_for(run)[0]["code"] == "600000"

    def test_an_explicit_panel_is_honoured(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600519", "000002"])
        assert [p["code"] for p in SH.predictions_for(run)] == \
            ["000002", "600519"]

    def test_a_day_with_no_champion_picks_emits_nothing(self, store, version):
        run = _open(version)
        assert SH.emit_for_date(run, _today()) == []
        assert SH.counts()["forecasts"] == 0

    def test_the_forecast_carries_a_deadline(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"], horizon_days=5)
        row = SH.predictions_for(run)[0]
        assert row["horizon_days"] == 5
        assert row["deadline"] > row["date"]

    def test_the_forecast_records_the_policy_version(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        assert SH.predictions_for(run)[0]["policy_version_id"] == version

    def test_a_missing_run_is_refused(self, store):
        with pytest.raises(SH.ShadowError):
            SH.emit_for_date(999, _today(), panel=["600000"])


# ── 3. Idempotence ─────────────────────────────────────────────────────


class TestEmittingIsIdempotent:
    def test_re_emitting_a_day_does_not_duplicate(self, store, version):
        run = _open(version)
        first = SH.emit_for_date(run, _today(), panel=["600000"])
        second = SH.emit_for_date(run, _today(), panel=["600000"])
        assert first == second
        assert SH.counts()["forecasts"] == 1

    def test_re_emitting_keeps_the_same_row(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        row_id = SH.predictions_for(run)[0]["id"]
        SH.emit_for_date(run, _today(), panel=["600000"])
        assert [p["id"] for p in SH.predictions_for(run)] == [row_id]

    def test_a_widened_panel_adds_only_the_new_code(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        SH.emit_for_date(run, _today(), panel=["600000", "000001"])
        assert SH.counts()["forecasts"] == 2


# ── 4. A closed run stops producing ────────────────────────────────────


class TestAClosedRunStopsProducing:
    def test_a_closed_run_cannot_emit(self, store, version):
        run = _open(version)
        SH.close_run(run, reason="enough")
        with pytest.raises(SH.ShadowError) as exc:
            SH.emit_for_date(run, _today(), panel=["600000"])
        assert "closed" in str(exc.value)

    def test_a_closed_run_keeps_its_forecasts(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        SH.close_run(run, reason="enough")
        assert len(SH.predictions_for(run)) == 1


# ── 5. Grading comes from market data ──────────────────────────────────


class TestGrading:
    def test_a_matured_forecast_is_graded(self, store, version, monkeypatch):
        monkeypatch.setattr(scoring, "score_prediction", _fake_score(0.25))
        run = _open(version)
        SH.emit_for_date(run, "2026-01-05", panel=["600000"], horizon_days=5)
        result = SH.score_due(as_of="2026-06-01")
        assert result["graded"] == 1
        row = SH.scored_for(run)[0]
        assert row["brier"] == 0.25
        assert row["outcome"] == 1

    def test_an_unmatured_forecast_is_left_alone(self, store, version,
                                                 monkeypatch):
        monkeypatch.setattr(scoring, "score_prediction", _fake_score())
        run = _open(version)
        SH.emit_for_date(run, "2026-01-05", panel=["600000"], horizon_days=5)
        result = SH.score_due(as_of="2026-01-06")
        assert result["graded"] == 0
        assert SH.scored_for(run) == []

    def test_a_missing_price_leaves_the_forecast_unscored(
            self, store, version, monkeypatch):
        # An absent price must not be graded as a miss: that would manufacture
        # evidence out of a data gap.
        monkeypatch.setattr(scoring, "score_prediction",
                            lambda *a, **k: None)
        run = _open(version)
        SH.emit_for_date(run, "2026-01-05", panel=["600000"], horizon_days=5)
        result = SH.score_due(as_of="2026-06-01")
        assert result == {"graded": 0, "unscorable": 1, "deferred": 0,
                          "as_of": "2026-06-01"}
        assert SH.scored_for(run) == []

    def test_a_window_that_is_still_open_is_deferred_not_unscorable(
            self, store, version, monkeypatch):
        """The other half of the split, and the reason it is a split.

        ``unscorable`` claims the window closed and the price never came — a
        data gap. A window the market has not traded shut is not that, and
        counting it as one made a live experiment read like a broken feed.
        """
        monkeypatch.setattr(scoring, "evidence_window_closed",
                            lambda entry_date, horizon=5: False)
        monkeypatch.setattr(scoring, "score_prediction",
                            lambda *a, **k: pytest.fail("not ripe, not graded"))
        run = _open(version)
        SH.emit_for_date(run, "2026-01-05", panel=["600000"], horizon_days=5)
        result = SH.score_due(as_of="2026-06-01")
        assert result == {"graded": 0, "unscorable": 0, "deferred": 1,
                          "as_of": "2026-06-01"}
        # Still waiting, not dropped: the row is re-selected next run.
        assert SH.scored_for(run) == []
        assert len(SH.predictions_for(run)) == 1

    def test_grading_can_be_scoped_to_one_run(self, store, version, monkeypatch):
        monkeypatch.setattr(scoring, "score_prediction", _fake_score())
        first = _open(version)
        SH.emit_for_date(first, "2026-01-05", panel=["600000"], horizon_days=5)
        PR_second = PR.freeze(
            sources={"prompts": {}, "model": {}, "retrieval": {}, "rules": {},
                     "knowledge": {"snapshot_id": None},
                     "decision": dict(scoring.DEFAULT_DECISION_PARAMS)},
            created_by="kylin", reason="second")
        second = _open(PR_second)
        SH.emit_for_date(second, "2026-01-05", panel=["000001"], horizon_days=5)
        SH.score_due(as_of="2026-06-01", run_id=first)
        assert len(SH.scored_for(first)) == 1
        assert SH.scored_for(second) == []

    def test_grading_is_not_repeated(self, store, version, monkeypatch):
        calls = []

        def counting(code, entry_date, prob, horizon=None):
            calls.append(code)
            return _fake_score()(code, entry_date, prob, horizon)

        monkeypatch.setattr(scoring, "score_prediction", counting)
        run = _open(version)
        SH.emit_for_date(run, "2026-01-05", panel=["600000"], horizon_days=5)
        SH.score_due(as_of="2026-06-01")
        SH.score_due(as_of="2026-06-02")
        assert calls == ["600000"]


# ── 6. Isolation ───────────────────────────────────────────────────────


class TestIsolation:
    def test_a_shadow_forecast_never_lands_in_predictions(self, store, version):
        _champion(store, _today(), "600000")
        before = store.execute("SELECT COUNT(*) n FROM predictions").fetchone()["n"]
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000", "000001"])
        after = store.execute("SELECT COUNT(*) n FROM predictions").fetchone()["n"]
        assert before == after == 1

    def test_the_intraday_reader_does_not_see_the_challenger(
            self, store, version):
        # The leak this design avoids: get_today_intraday_predictions filters
        # `report_type LIKE 'intraday%'`, which matches an
        # `intraday_signal_shadow` row. The intraday monitor would have been
        # handed the challenger's forecast as one of its own prior calls.
        _champion(store, _today(), "600000", report_type="intraday_signal")
        run = _open(version, report_type="intraday_signal")
        SH.emit_for_date(run, _today())
        assert [r["code"] for r in
                memory_store.get_today_intraday_predictions()] == ["600000"]

    def test_the_hit_rate_stats_do_not_see_the_challenger(self, store, version):
        # get_prediction_stats feeds the agents' prompts.
        _champion(store, _today(), "600000", hit=1)
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000", "000001"])
        assert memory_store.get_prediction_stats()["total"] == 1

    def test_emitting_moves_nothing_outside_the_two_shadow_tables(
            self, store, version):
        SH.init_schema(store)  # warm up: the tables exist before the dump
        before = _dump(store)
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        after = _dump(store)
        assert set(before) == set(after)
        changed = {name for name in before if before[name] != after[name]}
        assert changed == {"shadow_runs", "shadow_predictions"}, (
            f"the challenger changed {sorted(changed)}; it forecasts and is "
            "graded, and it must not trade")

    def test_a_shadow_run_writes_no_order_position_or_intent(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        for table in ("virtual_portfolio", "intents", "position_exits",
                      "episodes", "theses"):
            n = store.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
            assert n == 0, f"{table} grew: the challenger traded"


# ── 7. Coverage ────────────────────────────────────────────────────────


class TestCoverage:
    def test_a_fresh_run_reports_the_full_distance(self, store, version):
        run = _open(version)
        row = SH.coverage(run)["runs"][0]
        assert row["paired"] == 0
        assert row["needed"] == holdout_gate.MIN_VALIDATION_SAMPLES
        assert row["remaining"] == holdout_gate.MIN_VALIDATION_SAMPLES

    def test_paired_counts_only_what_both_sides_scored(
            self, store, version, monkeypatch):
        # The honest progress number: a challenger with many forecasts that
        # share no (date, code) with the champion has an n of zero.
        #
        # Dated after the version's freeze, because a pair before it is not
        # forward evidence and counting it would let the meter reach ``needed``
        # while the gate still abstains.
        monkeypatch.setattr(scoring, "score_prediction", _fake_score())
        day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        later = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        _champion(store, day, "600000")
        store.execute("UPDATE predictions SET scored_at = ?, brier = 0.3 "
                      "WHERE date = ?", (later, day))
        store.commit()
        run = _open(version)
        SH.emit_for_date(run, day, panel=["600000", "000001"],
                         horizon_days=5)
        SH.score_due(as_of=later)
        row = SH.coverage(run)["runs"][0]
        assert row["scored"] == 2          # both forecasts were graded
        assert row["paired"] == 1          # only one has a champion counterpart
        assert row["remaining"] == holdout_gate.MIN_VALIDATION_SAMPLES - 1

    def test_paired_ignores_a_pair_from_before_the_freeze(
            self, store, version, monkeypatch):
        """The meter must count the same set the verdict will.

        A pair that exists but predates the freeze can never be evidence for
        this version, so it must not advance ``paired``. Counting it would
        make ``remaining`` reach zero and then produce an ``insufficient``
        verdict — the operator left guessing which of the two numbers lied.
        """
        monkeypatch.setattr(scoring, "score_prediction", _fake_score())
        _champion(store, "2026-01-05", "600000")
        store.execute("UPDATE predictions SET scored_at = '2026-02-01', "
                      "brier = 0.3 WHERE date = '2026-01-05'")
        store.commit()
        run = _open(version)
        SH.emit_for_date(run, "2026-01-05", panel=["600000"], horizon_days=5)
        SH.score_due(as_of="2026-06-01")
        row = SH.coverage(run)["runs"][0]
        assert row["scored"] == 1
        assert row["paired"] == 0

    def test_coverage_reports_every_run(self, store, version):
        first = _open(version)
        SH.close_run(first, reason="enough")
        second = _open(version)
        assert [r["run_id"] for r in SH.coverage()["runs"]] == [first, second]

    def test_coverage_of_one_run_can_be_asked_for(self, store, version):
        first = _open(version)
        SH.close_run(first, reason="enough")
        _open(version)
        assert [r["run_id"] for r in SH.coverage(first)["runs"]] == [first]

    def test_counts_summarises_the_shadow_side(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        assert SH.counts() == {"runs": 1, "open_runs": 1, "forecasts": 1,
                               "scored": 0}


# ── 8. Integrity ───────────────────────────────────────────────────────


class TestIntegrity:
    def test_a_clean_shadow_is_integral(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        assert SH.integrity() == []

    def test_a_run_measuring_a_missing_version_is_reported(
            self, store, version):
        run = _open(version)
        store.execute("UPDATE shadow_runs SET policy_version_id = 4242 "
                      "WHERE id = ?", (run,))
        problems = SH.integrity()
        assert any("does not exist" in p for p in problems)

    def test_an_orphaned_forecast_is_reported(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        store.execute("DELETE FROM shadow_runs WHERE id = ?", (run,))
        problems = SH.integrity()
        assert any("belongs to run" in p for p in problems)

    def test_a_forecast_naming_another_version_is_reported(
            self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        store.execute("UPDATE shadow_predictions SET policy_version_id = 4242")
        problems = SH.integrity()
        assert any("its run names" in p for p in problems)

    def test_summary_reports_the_shadow_side(self, store, version):
        run = _open(version)
        SH.emit_for_date(run, _today(), panel=["600000"])
        summary = SH.summary(run)
        assert summary["counts"]["forecasts"] == 1
        assert summary["integrity"] == []
