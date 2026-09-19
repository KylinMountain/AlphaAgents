"""U3 — the gate's window comes from the policy version, not from the caller.

D7 recorded that ``gate_decisions`` had rows with ``validation_days = 0`` and
that the gate "looked like governance for four days". The cause was a call
site: ``run_gate("daily_playbook", today, today)`` — a validation window whose
two ends were the same day, written by hand. It ran, it returned a verdict, and
the verdict was always ``insufficient``, because a window that contains no days
contains no evidence.

The fix is not "remember to pass the right date". It is that the window is now
**derived from the frozen version** and a window that cannot hold evidence is
**refused rather than abstained on**. A refusal is loud: it raises. An
abstention is quiet: it writes a row that looks like a verdict about evidence
that was never collected.

The tests are in nine groups:

1. The old bug is unrepresentable — no ``created_date`` parameter exists, and
   asking on or before the freeze day raises.
2. Malformed questions are refused, and a refusal writes nothing at all.
3. The window really is forward: rows at or before the freeze are not evidence.
4. The comparison is filtered to the run's report type and paired on
   (date, code), so ``n`` cannot be inflated by unpaired rows.
5. ``validation_days`` is a real column and counts paired *days*, not the
   length of the window — D7's own "recognise it is fixed" criterion.
6. A real window with too little in it still abstains, and that abstention is
   recorded with ``outcome='insufficient'``.
7. The lookback never truncates the forward window of an old version.
8. A verdict's evidence scope comes from the registered producer that emitted
   the forecasts, not from a free-text label on the run. It used to come from
   the label, so naming a run anything but ``constant_0.5`` turned no-skill
   baseline evidence into "candidate_policy" evidence — the promotion guard
   satisfied by a word.
9. The build ships no candidate producer, so nothing promotable is reachable;
   and a version shadowed by two producers is refused rather than resolved
   silently.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest

from alpha_agents.data import memory_store, policy_registry as PR
from alpha_agents.data import scoring
from alpha_agents.evolution import holdout_gate as HG
from alpha_agents.evolution import shadow as SH

FROZEN_AT = "2026-06-01"
ASKED_ON = "2026-06-30"
REPORT = "morning"

_SOURCES = {
    "prompts": {"morning_scan.md": "aaa"},
    "model": {"agent_model": "qwen-plus"},
    "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
    "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
    "knowledge": {"snapshot_id": None},
    # The pointer-controlled source. Nothing is installed in these fixtures, so
    # an honest freeze records the code default block.
    "decision": dict(scoring.DEFAULT_DECISION_PARAMS),
}


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
def frozen(store) -> int:
    """A policy version frozen on a fixed day, with a shadow run measuring it."""
    version = PR.freeze(sources=_SOURCES, created_by="kylin",
                        reason="frozen for the forward window",
                        frozen_at=FROZEN_AT)
    SH.open_run(policy_version_id=version, reason="measure the baseline",
                report_type=REPORT, opened_at=FROZEN_AT)
    return version


def _dump(conn) -> dict:
    """Every user table's contents, for asserting that a call wrote nothing."""
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        if name.startswith("sqlite_"):
            continue
        rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        out[name] = sorted(tuple(row) for row in rows)
    return out


def _nonempty(conn) -> dict:
    """Tables that hold at least one row — the write of *information*.

    ``CREATE TABLE IF NOT EXISTS`` is run by every entry point, so a refusal
    legitimately materialises empty containers. That is not a write: the claim
    being tested is that no *record* was made.
    """
    return {name: rows for name, rows in _dump(conn).items() if rows}


def _champion(store, date: str, code: str, brier: float,
              *, report_type: str = REPORT) -> int:
    """A champion prediction, graded, written through the real save path."""
    pred_id = memory_store.save_prediction(
        date, report_type, code, "X", "看多", "high", "t", 1.0, "reason",
        prob=0.6, trader_id="default", horizon_days=5)
    store.execute(
        "UPDATE predictions SET brier = ?, scored_at = ? WHERE id = ?",
        (brier, f"{date} 10:00:00", pred_id))
    store.commit()
    return pred_id


def _grade_challenger(monkeypatch, brier: float, *, as_of: str = ASKED_ON):
    """Make the challenger's forecasts gradable, deterministically.

    Market data is not available in a unit test, and the gate's job is to
    compare two graded series — so the grading step is stubbed and everything
    after it is real.

    The *window* is stubbed alongside it, and that is a separate fact rather
    than part of the grading: ``score_due`` asks the market's own calendar
    whether the forecast window has closed before it will grade anything, and
    a sandbox with no ``daily_kline`` answers "not yet" to every row. Every
    scenario here is dated after the freeze and means to be graded, so the
    window is declared shut. The predicate's real behaviour is pinned in
    ``test_scoring.py``.
    """
    monkeypatch.setattr(scoring, "evidence_window_closed",
                        lambda entry_date, horizon=5: True)
    monkeypatch.setattr(scoring, "score_prediction",
                        lambda code, entry_date, prob, horizon=None: {
                            "brier": brier, "log_score": 0.69,
                            "excess_return": 0.01, "residual_alpha": 0.005,
                            "outcome": True,
                            "scored_at": f"{as_of} 10:00:00"})
    return as_of


def _run(version, **over):
    kwargs = dict(report_type=REPORT, today=ASKED_ON)
    kwargs.update(over)
    return HG.run_gate(version, **kwargs)


# ── 1. The old bug is unrepresentable ──────────────────────────────────


class TestTheWindowIsNotACallerParameter:
    """``run_gate(..., today, today)`` must not be expressible any more."""

    def test_there_is_no_created_date_parameter(self):
        params = set(inspect.signature(HG.run_gate).parameters)
        assert "created_date" not in params
        assert "validation_start" not in params
        assert "validation_end" not in params

    def test_today_is_when_the_question_is_asked_not_the_window(self):
        """The remaining date parameter cannot shorten the window."""
        params = set(inspect.signature(HG.run_gate).parameters)
        assert params == {"policy_version_id", "report_type", "today"}

    def test_asking_on_the_freeze_day_is_refused(self, store, frozen):
        with pytest.raises(HG.GateError) as exc:
            _run(frozen, today=FROZEN_AT)
        assert "Validation is forward" in str(exc.value)

    def test_asking_before_the_freeze_is_refused(self, store, frozen):
        with pytest.raises(HG.GateError):
            _run(frozen, today="2026-05-01")

    def test_a_refused_window_is_not_recorded_as_a_verdict(self, store, frozen):
        """The whole point: no verdict-shaped row for a window with no days."""
        before = _nonempty(store)
        with pytest.raises(HG.GateError):
            _run(frozen, today=FROZEN_AT)
        assert _nonempty(store) == before

    def test_a_one_day_window_is_accepted_not_refused(self, store, frozen):
        """One day is thin, but it is a question that can be answered.

        The refusal is for windows that *cannot* hold evidence, not for ones
        that hold too little — that distinction is what keeps "insufficient"
        meaning something.
        """
        got = _run(frozen, today="2026-06-02")
        assert got["outcome"] == "insufficient"
        assert got["n"] == 0
        assert got["abstained"] is True


# ── 2. Malformed questions are refused ─────────────────────────────────


class TestMalformedQuestionsAreRefused:
    def test_an_unknown_version_is_refused(self, store):
        with pytest.raises(HG.GateError) as exc:
            _run(4242)
        assert "No policy version" in str(exc.value)

    def test_a_version_with_no_shadow_run_is_refused(self, store):
        version = PR.freeze(sources=_SOURCES, created_by="kylin",
                            reason="no run measures this", frozen_at=FROZEN_AT)
        with pytest.raises(HG.GateError) as exc:
            _run(version)
        assert "Nothing on the challenger side" in str(exc.value) or \
               "no shadow run" in str(exc.value).lower() or \
               "shadow run" in str(exc.value)

    def test_a_run_of_another_report_type_does_not_qualify(self, store):
        version = PR.freeze(sources=_SOURCES, created_by="kylin",
                            reason="intraday only", frozen_at=FROZEN_AT)
        SH.open_run(policy_version_id=version, reason="intraday",
                    report_type="intraday_signal", opened_at=FROZEN_AT)
        with pytest.raises(HG.GateError):
            _run(version, report_type=REPORT)

    def test_a_refusal_writes_nothing(self, store):
        before = _nonempty(store)
        with pytest.raises(HG.GateError):
            _run(4242)
        assert _nonempty(store) == before


# ── 3. The window really is forward ────────────────────────────────────


class TestTheWindowIsForward:
    def test_rows_on_or_before_the_freeze_are_not_evidence(self, store, frozen,
                                                           monkeypatch):
        """Both sides have graded rows before the freeze. None may count."""
        for day in ("2026-05-20", "2026-05-28", FROZEN_AT):
            _champion(store, day, "A", 0.40)
        run_id = SH.runs()[0]["id"]
        for day in ("2026-05-20", "2026-05-28", FROZEN_AT):
            SH.emit_for_date(run_id, day, panel=["A"])
        _grade_challenger(monkeypatch, 0.05, as_of="2026-06-10")
        SH.score_due(as_of="2026-06-10")

        got = _run(frozen)
        assert got["n"] == 0
        assert got["outcome"] == "insufficient"

    def test_a_day_after_the_freeze_is_evidence(self, store, frozen,
                                                monkeypatch):
        _champion(store, "2026-06-02", "A", 0.40)
        run_id = SH.runs()[0]["id"]
        SH.emit_for_date(run_id, "2026-06-02", panel=["A"])
        _grade_challenger(monkeypatch, 0.05, as_of="2026-06-10")
        SH.score_due(as_of="2026-06-10")

        assert _run(frozen)["n"] == 1


# ── 4. The comparison is filtered and paired ───────────────────────────


class TestTheComparisonIsPairedAndFiltered:
    def test_champion_rows_of_another_report_type_are_not_compared(
            self, store, frozen, monkeypatch):
        """A post-market prediction is not a forecast of the morning run."""
        for i in range(30):
            day = (datetime.strptime(FROZEN_AT, "%Y-%m-%d")
                   + timedelta(days=i + 1)).strftime("%Y-%m-%d")
            _champion(store, day, f"C{i}", 0.10, report_type="post_market")
        run_id = SH.runs()[0]["id"]
        for i in range(30):
            day = (datetime.strptime(FROZEN_AT, "%Y-%m-%d")
                   + timedelta(days=i + 1)).strftime("%Y-%m-%d")
            SH.emit_for_date(run_id, day, panel=[f"C{i}"])
        _grade_challenger(monkeypatch, 0.05)
        SH.score_due(as_of=ASKED_ON)

        assert _run(frozen)["n"] == 0

    def test_n_counts_pairs_not_forecasts(self, store, frozen, monkeypatch):
        """Forty champion rows, five shared codes: n is five."""
        for i in range(40):
            _champion(store, "2026-06-10", f"C{i}", 0.30)
        run_id = SH.runs()[0]["id"]
        SH.emit_for_date(run_id, "2026-06-10", panel=[f"C{i}" for i in range(5)])
        _grade_challenger(monkeypatch, 0.10)
        SH.score_due(as_of=ASKED_ON)

        got = _run(frozen)
        assert got["n"] == 5
        assert got["abstained"] is True

    def test_the_progress_meter_agrees_with_the_verdict(self, store, frozen,
                                                        monkeypatch):
        """``coverage().paired`` and the gate's ``n`` must be the same number.

        They are computed by different code — one SQL, one Python set
        intersection. If they can disagree, an experiment reads "enough
        evidence" right up until the verdict comes back ``insufficient``.
        """
        run_id = SH.runs()[0]["id"]
        base = datetime.strptime(FROZEN_AT, "%Y-%m-%d")
        for d in range(25):
            day = (base + timedelta(days=d + 1)).strftime("%Y-%m-%d")
            _champion(store, day, f"C{d}", 0.30)
            SH.emit_for_date(run_id, day, panel=[f"C{d}"])
        _grade_challenger(monkeypatch, 0.10)
        SH.score_due(as_of=ASKED_ON)

        assert SH.coverage(run_id)["runs"][0]["paired"] == _run(frozen)["n"]


# ── 5. validation_days is a real column ────────────────────────────────


class TestValidationDaysIsReal:
    def _scenario(self, store, monkeypatch, days: int, per_day: int,
                  *, champ=0.30, chall=0.10):
        run_id = SH.runs()[0]["id"]
        base = datetime.strptime(FROZEN_AT, "%Y-%m-%d")
        for d in range(days):
            day = (base + timedelta(days=d + 1)).strftime("%Y-%m-%d")
            codes = [f"D{d}C{i}" for i in range(per_day)]
            for code in codes:
                _champion(store, day, code, champ)
            SH.emit_for_date(run_id, day, panel=codes)
        _grade_challenger(monkeypatch, chall)
        SH.score_due(as_of=ASKED_ON)
        return run_id

    def test_it_counts_paired_days_not_window_length(self, store, frozen,
                                                     monkeypatch):
        """Six pairs over three days is three days of evidence, not 29."""
        self._scenario(store, monkeypatch, days=3, per_day=2)
        got = _run(frozen)
        assert got["n"] == 6
        assert got["validation_days"] == 3

    def test_it_is_stored_in_a_column(self, store, frozen, monkeypatch):
        self._scenario(store, monkeypatch, days=3, per_day=2)
        _run(frozen)
        row = store.execute(
            "SELECT validation_days, outcome FROM gate_decisions").fetchone()
        assert row["validation_days"] == 3
        assert row["outcome"] == "insufficient"

    def test_the_d7_criterion_is_checkable_by_sql(self, store, frozen,
                                                  monkeypatch):
        """D7's own acceptance test: a row whose validation_days is not 0."""
        self._scenario(store, monkeypatch, days=25, per_day=1)
        _run(frozen)
        row = store.execute(
            "SELECT COUNT(*) n FROM gate_decisions WHERE validation_days > 0"
        ).fetchone()
        assert row["n"] == 1

    def test_a_zero_day_window_would_have_been_zero_here(self, store, frozen,
                                                         monkeypatch):
        """The old shape, as a number: every pair pre-freeze → 0 days."""
        for day in ("2026-05-20", "2026-05-21"):
            _champion(store, day, "A", 0.30)
        run_id = SH.runs()[0]["id"]
        for day in ("2026-05-20", "2026-05-21"):
            SH.emit_for_date(run_id, day, panel=["A"])
        _grade_challenger(monkeypatch, 0.10)
        SH.score_due(as_of=ASKED_ON)

        got = _run(frozen)
        assert got["validation_days"] == 0
        assert HG.eligible_decisions(frozen) == []


# ── 6. A real verdict is recorded ──────────────────────────────────────


class TestAVerdictIsRecorded:
    def _scenario(self, store, monkeypatch, *, champ, chall, days=25):
        run_id = SH.runs()[0]["id"]
        base = datetime.strptime(FROZEN_AT, "%Y-%m-%d")
        for d in range(days):
            day = (base + timedelta(days=d + 1)).strftime("%Y-%m-%d")
            _champion(store, day, f"C{d}", champ)
            SH.emit_for_date(run_id, day, panel=[f"C{d}"])
        _grade_challenger(monkeypatch, chall)
        SH.score_due(as_of=ASKED_ON)

    def test_an_improvement_is_recorded_as_promote(self, store, frozen,
                                                   monkeypatch):
        self._scenario(store, monkeypatch, champ=0.30, chall=0.10)
        got = _run(frozen)
        assert got["outcome"] == "promote"
        row = HG.get_gate_decisions(policy_version_id=frozen)[0]
        assert row["outcome"] == "promote" and row["promoted"] == 1
        assert row["policy_version_id"] == frozen

    def test_a_degradation_is_recorded_as_reject(self, store, frozen,
                                                 monkeypatch):
        self._scenario(store, monkeypatch, champ=0.10, chall=0.30)
        got = _run(frozen)
        assert got["outcome"] == "reject"
        row = HG.get_gate_decisions(policy_version_id=frozen)[0]
        assert row["outcome"] == "reject" and row["promoted"] == 0
        assert row["abstained"] == 0

    def test_only_a_promote_verdict_is_eligible(self, store, frozen,
                                                monkeypatch):
        self._scenario(store, monkeypatch, champ=0.10, chall=0.30)
        _run(frozen)
        assert HG.eligible_decisions(frozen) == []

    def test_the_verdict_names_the_version_in_its_own_column(self, store,
                                                             frozen,
                                                             monkeypatch):
        self._scenario(store, monkeypatch, champ=0.30, chall=0.10)
        _run(frozen)
        row = HG.get_gate_decisions(policy_version_id=frozen)[0]
        assert row["candidate"] == f"trader#{frozen}"
        assert row["policy_version_id"] == frozen


# ── 7. A closed run is still the challenger's record ───────────────────


class TestAClosedRunIsStillEvaluable:
    def test_closing_does_not_unmeasure_what_was_measured(self, store, frozen,
                                                          monkeypatch):
        run_id = SH.runs()[0]["id"]
        base = datetime.strptime(FROZEN_AT, "%Y-%m-%d")
        for d in range(25):
            day = (base + timedelta(days=d + 1)).strftime("%Y-%m-%d")
            _champion(store, day, f"C{d}", 0.30)
            SH.emit_for_date(run_id, day, panel=[f"C{d}"])
        _grade_challenger(monkeypatch, 0.10)
        SH.score_due(as_of=ASKED_ON)
        SH.close_run(run_id, reason="promoted", closed_at=ASKED_ON)

        got = _run(frozen)
        assert got["run_id"] == run_id
        assert got["outcome"] == "promote"
        assert got["evidence_scope"] == "baseline_only"


# ── 8. The lookback does not truncate an old version's window ──────────


class TestTheLookbackDoesNotTruncateTheWindow:
    """``get_scored_predictions`` floors on "N days ago".

    A fixed floor of 180 would make a two-year-old version lose the *oldest*
    part of its forward window — evidence sitting in the database that the gate
    would report as never collected. The floor is therefore derived from the
    version's age.
    """

    def _old_version_scenario(self, store, monkeypatch):
        """One graded pair ~600 days ago, for a version frozen ~800 days ago."""
        old_freeze = (datetime.now() - timedelta(days=800)).strftime("%Y-%m-%d")
        version = PR.freeze(sources=_SOURCES, created_by="kylin",
                            reason="old", frozen_at=old_freeze)
        SH.open_run(policy_version_id=version, reason="measure",
                    report_type=REPORT, opened_at=old_freeze)

        day = (datetime.now() - timedelta(days=600)).strftime("%Y-%m-%d")
        graded_on = (datetime.now() - timedelta(days=590)).strftime("%Y-%m-%d")
        _champion(store, day, "A", 0.40)
        SH.emit_for_date(SH.runs()[0]["id"], day, panel=["A"])
        # The challenger's horizon has to elapse before it can be graded.
        _grade_challenger(monkeypatch, 0.10, as_of=graded_on)
        SH.score_due(as_of=graded_on)
        return version, day

    def test_an_old_version_still_sees_its_early_forward_days(
            self, store, monkeypatch):
        version, _day = self._old_version_scenario(store, monkeypatch)
        got = _run(version, today=datetime.now().strftime("%Y-%m-%d"))
        assert got["n"] == 1

    def test_a_fixed_lookback_would_have_dropped_that_day(self, store,
                                                          monkeypatch):
        """The control. Without the derived floor the same pair is invisible —
        which is what makes the test above about the mechanism and not luck."""
        version, _day = self._old_version_scenario(store, monkeypatch)
        monkeypatch.setattr(HG, "_lookback_days", lambda a, b: 180)
        got = _run(version, today=datetime.now().strftime("%Y-%m-%d"))
        assert got["n"] == 0

    def test_the_lookback_is_at_least_the_floor(self):
        assert HG._lookback_days("2026-06-01", "2026-06-02") == \
            HG.GATE_LOOKBACK_DAYS

    def test_the_lookback_grows_with_the_version_age(self):
        old = (datetime.now() - timedelta(days=900)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        assert HG._lookback_days(old, today) > HG.GATE_LOOKBACK_DAYS


# ── 8. The evidence scope belongs to the producer, not to a label ──────


CANDIDATE = "test_candidate_producer"


def _sources(tag: str) -> dict:
    """A configuration distinct per tag, so two versions can both be live."""
    return {
        "prompts": {"morning_scan.md": tag},
        "model": {"agent_model": "qwen-plus"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
        "decision": dict(scoring.DEFAULT_DECISION_PARAMS),
    }


@pytest.fixture()
def bare(store) -> int:
    """A frozen version with no shadow run — a test opens the run it means."""
    return PR.freeze(sources=_sources("bare"), created_by="kylin",
                     reason="frozen for the forward window",
                     frozen_at=FROZEN_AT)


@pytest.fixture()
def candidate(monkeypatch) -> str:
    """A **temporary** candidate producer, for the duration of one test.

    Not the shipped one: this file is about what a verdict's scope is derived
    from, and a producer whose forecast is a constant makes the scope the only
    variable. The shipped candidate is measured separately, in
    :class:`TestTheShippedCandidate`.
    """
    monkeypatch.setitem(SH.PRODUCERS, CANDIDATE, SH.Producer(
        name=CANDIDATE, kind=SH.KIND_CANDIDATE,
        forecast=lambda date, code, ctx: float(
            ctx.params["confidence_priors"]["high"]),
        observed_genes=frozenset({"decision.confidence_priors"})))
    return CANDIDATE


def _emit_and_grade(store, run_id: int, monkeypatch, *, days: int = 25):
    """``days`` paired days of champion and challenger, then grade them."""
    base = datetime.strptime(FROZEN_AT, "%Y-%m-%d")
    for d in range(days):
        day = (base + timedelta(days=d + 1)).strftime("%Y-%m-%d")
        _champion(store, day, f"C{d}", 0.30)
        SH.emit_for_date(run_id, day, panel=[f"C{d}"])
    _grade_challenger(monkeypatch, 0.10)
    SH.score_due(as_of=ASKED_ON)


class TestTheScopeBelongsToTheProducer:
    def test_the_shipped_registry_holds_exactly_one_candidate(self):
        """The documentation's claim, as an assertion — and it flipped.

        The previous revision of this test pinned *no* candidate producer, and
        its docstring asked that adding one fail here first so that the code and
        the documentation were revisited together. It did fail, when
        ``remap_confidence`` was registered. What has to hold now is the count:
        two candidates would let a verdict describe whichever run the reader
        has no reason to think was chosen, and a candidate that quietly stopped
        being registered would leave ``run_gate`` unable to produce anything
        promotable while the docs said otherwise.
        """
        kinds = [p.kind for p in SH.PRODUCERS.values()]
        assert kinds.count(SH.KIND_CANDIDATE) == 1
        assert kinds.count(SH.KIND_BASELINE) == 1
        assert SH.scope_for(SH.CANDIDATE_NAME) == PR.SCOPE_CANDIDATE

    def test_a_name_with_no_emitter_cannot_be_opened(self, store, bare):
        """The bypass, as a refusal.

        ``evidence_scope`` used to come from the run's free-text label, so
        naming a run anything but ``constant_0.5`` minted "candidate_policy"
        evidence out of the no-skill baseline while the emitted probability
        stayed 0.5. The guard was satisfied by a word.
        """
        with pytest.raises(SH.ShadowError, match="No registered producer"):
            SH.open_run(policy_version_id=bare, reason="rename to promote",
                        report_type=REPORT, producer="definitely_a_candidate")
        assert SH.runs() == []

    def test_an_unregistered_producer_has_no_scope(self):
        with pytest.raises(SH.ShadowError, match="not registered"):
            SH.scope_for("nobody_emits_this")

    def test_a_kind_the_module_never_defined_is_not_a_candidate(self,
                                                                monkeypatch):
        monkeypatch.setitem(SH.PRODUCERS, "mystery", SH.Producer(
            name="mystery", kind="wishful", forecast=lambda date, code, ctx: 0.9))
        with pytest.raises(SH.ShadowError, match="does not define"):
            SH.scope_for("mystery")

    def test_the_baseline_verdict_is_baseline_only(self, store, frozen,
                                                   monkeypatch):
        _emit_and_grade(store, SH.runs()[0]["id"], monkeypatch)
        got = _run(frozen)
        assert got["evidence_scope"] == SH.BASELINE_ONLY_SCOPE
        stored = HG.get_gate_decisions(policy_version_id=frozen)[0]
        assert stored["evidence_scope"] == SH.BASELINE_ONLY_SCOPE

    def test_a_candidate_verdict_is_promotable_grade(self, store,
                                                     candidate, monkeypatch):
        incumbent = PR.freeze(
            sources=_sources("same"), created_by="kylin",
            reason="incumbent", frozen_at="2026-05-01")
        PR.install(version_id=incumbent, actor="kylin", reason="first")
        priors = {
            **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
            "high": 0.72,
        }
        decision = {
            **scoring.DEFAULT_DECISION_PARAMS,
            "confidence_priors": priors,
        }
        target = PR.freeze(
            sources=_with_decision("same", decision), parent_id=incumbent,
            created_by="kylin", reason="candidate", frozen_at=FROZEN_AT)
        run_id = SH.open_run(policy_version_id=target, reason="the candidate",
                             report_type=REPORT, producer=candidate,
                             opened_at=FROZEN_AT)
        _emit_and_grade(store, run_id, monkeypatch)
        got = _run(target)
        assert got["evidence_scope"] == PR.SCOPE_CANDIDATE
        stored = HG.get_gate_decisions(policy_version_id=target)[0]
        assert stored["evidence_scope"] == PR.SCOPE_CANDIDATE


class TestTheWholePathIsReachable:
    """A candidate can move the pointer only after a valid experiment."""

    def test_from_a_candidate_run_to_a_moved_pointer(self, store, candidate,
                                                     monkeypatch):
        incumbent = PR.freeze(
            sources=_sources("same"), created_by="kylin",
            reason="the incumbent", frozen_at="2026-05-01")
        PR.install(version_id=incumbent, actor="kylin", reason="first policy")
        priors = {
            **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
            "high": 0.72,
        }
        decision = {
            **scoring.DEFAULT_DECISION_PARAMS,
            "confidence_priors": priors,
        }
        target_sources = _with_decision("same", decision)
        target = PR.freeze(
            sources=target_sources, parent_id=incumbent,
            created_by="kylin", reason="the candidate", frozen_at=FROZEN_AT)
        assert PR.active()["version_id"] == incumbent

        run_id = SH.open_run(policy_version_id=target, reason="the candidate",
                             report_type=REPORT, producer=candidate,
                             opened_at=FROZEN_AT)
        _emit_and_grade(store, run_id, monkeypatch)

        verdict = _run(target)
        assert verdict["outcome"] == "promote"
        assert verdict["validation_days"] >= 20

        stored = HG.get_gate_decisions(policy_version_id=target)[0]
        assert stored["evidence_scope"] == PR.SCOPE_CANDIDATE
        PR.approve(version_id=target, approved_by="a-person",
                   reason="beat the champion on a paired panel",
                   gate_decision=stored, sources=target_sources, at=ASKED_ON)
        assert PR.active()["version_id"] == incumbent, "approval moved it"

        PR.promote(version_id=target, actor="a-person", reason="approved",
                   sources=target_sources, expected_seq=1)
        assert PR.active()["version_id"] == target


class TestAnAmbiguousGateQuestionIsRefused:
    """The gate refuses two challengers instead of silently picking one."""

    def test_two_open_shadows_of_one_version_are_refused(self, store,
                                                         candidate):
        incumbent = PR.freeze(
            sources=_sources("same"), created_by="kylin",
            reason="incumbent", frozen_at="2026-05-01")
        PR.install(version_id=incumbent, actor="kylin", reason="first")
        priors = {
            **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
            "high": 0.72,
        }
        target = PR.freeze(
            sources=_with_decision(
                "same",
                {**scoring.DEFAULT_DECISION_PARAMS,
                 "confidence_priors": priors}),
            parent_id=incumbent, created_by="kylin",
            reason="candidate", frozen_at=FROZEN_AT)
        SH.open_run(policy_version_id=target, reason="a baseline reference",
                    report_type=REPORT, opened_at=FROZEN_AT)
        SH.open_run(policy_version_id=target, reason="the candidate",
                    report_type=REPORT, producer=candidate, opened_at=FROZEN_AT)
        with pytest.raises(HG.GateError, match="shadow runs are open"):
            _run(target)

    def test_one_open_shadow_is_answered_not_refused(self, store, bare):
        SH.open_run(policy_version_id=bare, reason="the only one",
                    report_type=REPORT, opened_at=FROZEN_AT)
        got = _run(bare)
        assert got["abstained"]


# ── 9. The shipped candidate reads the version it is bound to ──────────


def _with_decision(tag: str, block: dict) -> dict:
    """A configuration whose decision parameters are the thing that differs."""
    return {**_sources(tag), "decision": block}


class TestTheShippedCandidate:
    """The shipped candidate must read the exact gene its contract declares."""

    def _target(self, *, high=0.72):
        incumbent = PR.freeze(
            sources=_sources("same"), created_by="kylin",
            reason="the incumbent", frozen_at="2026-05-01")
        PR.install(version_id=incumbent, actor="kylin", reason="first")
        priors = {
            **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
            "high": high,
        }
        decision = {
            **scoring.DEFAULT_DECISION_PARAMS,
            "confidence_priors": priors,
        }
        target = PR.freeze(
            sources=_with_decision("same", decision), parent_id=incumbent,
            created_by="kylin", reason="candidate", frozen_at=FROZEN_AT)
        return incumbent, target

    def test_it_maps_the_champions_signal_through_its_versions_block(
            self, store):
        incumbent, target = self._target()
        incumbent_ctx = SH.DecisionContext(
            policy_version_id=incumbent, report_type=REPORT,
            params=scoring.decision_params_of(incumbent),
            signals={"A": "high"})
        assert SH.remap_confidence(
            "2026-06-02", "A", incumbent_ctx) == pytest.approx(0.58)

        _champion(store, "2026-06-02", "A", 0.30)
        run_id = SH.open_run(
            policy_version_id=target, reason="measure it",
            report_type=REPORT, producer=SH.CANDIDATE_NAME,
            opened_at=FROZEN_AT)
        SH.emit_for_date(run_id, "2026-06-02", panel=["A"])
        assert SH.predictions_for(run_id)[0]["prob"] == pytest.approx(0.72)

    def test_it_does_not_read_the_champions_probability(self, store):
        _, target = self._target()
        run_id = SH.open_run(
            policy_version_id=target, reason="measure it",
            report_type=REPORT, producer=SH.CANDIDATE_NAME,
            opened_at=FROZEN_AT)
        _champion(store, "2026-06-02", "A", 0.30)
        SH.emit_for_date(run_id, "2026-06-02", panel=["A"])
        before = SH.predictions_for(run_id)[0]["prob"]

        store.execute("UPDATE predictions SET prob = 0.99 WHERE code = 'A'")
        store.commit()
        SH.emit_for_date(run_id, "2026-06-02", panel=["A"])
        after = SH.predictions_for(run_id)[0]["prob"]

        assert before == pytest.approx(0.72)
        assert after == before

    def test_a_code_with_no_recorded_label_falls_to_a_coin_flip(self, store):
        _, target = self._target()
        run_id = SH.open_run(
            policy_version_id=target, reason="measure it",
            report_type=REPORT, producer=SH.CANDIDATE_NAME,
            opened_at=FROZEN_AT)
        SH.emit_for_date(run_id, "2026-06-02",
                         panel=["NOBODY_FORECAST_THIS"])
        rows = SH.predictions_for(run_id)
        assert len(rows) == 1
        assert rows[0]["prob"] == pytest.approx(0.5)

    def test_the_shipped_candidate_reaches_a_promotable_verdict(
            self, store, monkeypatch):
        incumbent, target = self._target()
        run_id = SH.open_run(
            policy_version_id=target, reason="the candidate",
            report_type=REPORT, producer=SH.CANDIDATE_NAME,
            opened_at=FROZEN_AT)
        _emit_and_grade(store, run_id, monkeypatch)

        got = _run(target)
        assert got["evidence_scope"] == PR.SCOPE_CANDIDATE
        assert got["outcome"] == "promote"
        assert got["validation_days"] >= 20
        assert PR.active()["version_id"] == incumbent, "a verdict moves nothing"

