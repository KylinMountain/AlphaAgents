"""The invalidation vocabulary is what makes the thesis design work.

If a condition can be written but not evaluated, the design collapses
back into asking the model every cycle while pretending it does not.
So the tests that matter here are the ones about refusal: an unknown
kind, a non-numeric value, an always-true threshold, a narrative with no
text — each has to be dropped loudly rather than stored and never checked.

The second property is that a missing data field never fires a condition.
A failed sector fetch must not close a position; "I could not check" and
"the thesis broke" are different answers.
"""

import pytest

from alpha_agents.data import thesis as T
from alpha_agents.data.thesis import Condition, MarketView


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def mv(**kw):
    base = dict(price=18.56, current_return_pct=0.9)
    base.update(kw)
    return MarketView(**base)


class TestValidation:
    def test_a_known_condition_survives(self):
        c = T.validate_condition({"kind": "price_below", "value": 17.5,
                                  "note": "跌破平台"})
        assert c and c.kind == "price_below" and c.value == 17.5

    def test_an_invented_kind_is_dropped(self, caplog):
        with caplog.at_level("WARNING"):
            assert T.validate_condition(
                {"kind": "rsi_oversold", "value": 30}) is None
        assert any("Unknown invalidation kind" in r.getMessage()
                   for r in caplog.records)

    def test_a_non_numeric_value_is_dropped(self, caplog):
        with caplog.at_level("WARNING"):
            assert T.validate_condition(
                {"kind": "price_below", "value": "跌破支撑"}) is None

    def test_an_always_true_threshold_is_dropped(self, caplog):
        """strength < 11 is true for every theme that can exist."""
        with caplog.at_level("WARNING"):
            assert T.validate_condition(
                {"kind": "theme_strength_below", "value": 11}) is None
        assert any("outside" in r.getMessage() for r in caplog.records)

    def test_narrative_needs_its_text(self, caplog):
        with caplog.at_level("WARNING"):
            assert T.validate_condition({"kind": "narrative"}) is None
        assert T.validate_condition(
            {"kind": "narrative", "note": "若关税豁免未续期"}) is not None

    def test_garbage_input_does_not_raise(self):
        assert T.validate_condition("price_below") is None
        assert T.validate_condition(None) is None


class TestEvaluation:
    def test_price_below_fires(self):
        c = [Condition("price_below", 19.0)]
        assert T.evaluate(c, mv(price=18.56)) is not None

    def test_price_below_does_not_fire_above(self):
        assert T.evaluate([Condition("price_below", 18.0)], mv()) is None

    def test_drawdown_needs_a_peak(self):
        """A position that never went up cannot draw down from a peak."""
        c = [Condition("drawdown_from_peak", 5.0)]
        assert T.evaluate(c, mv(peak_return_pct=0, current_return_pct=-6)) is None
        assert T.evaluate(c, mv(peak_return_pct=8, current_return_pct=2)) is not None

    def test_loss_exceeds_reads_absolute(self):
        c = [Condition("loss_exceeds", 5.0)]
        assert T.evaluate(c, mv(current_return_pct=-5.2)) is not None
        assert T.evaluate(c, mv(current_return_pct=-4.8)) is None

    def test_theme_daily_score_catches_the_first_weak_day(self):
        c = [Condition("theme_daily_score_below", 0)]
        assert T.evaluate(c, mv(theme_daily_score=-1)) is not None
        assert T.evaluate(c, mv(theme_daily_score=2)) is None

    def test_flow_condition_reads_outflow(self):
        c = [Condition("theme_flow_negative", 10)]
        assert T.evaluate(c, mv(theme_net_flow_yi=-12.3)) is not None
        assert T.evaluate(c, mv(theme_net_flow_yi=54.3)) is None

    def test_no_progress_needs_both_time_and_flatness(self):
        c = [Condition("no_progress_by_day", 3)]
        assert T.evaluate(c, mv(holding_days=4, current_return_pct=0.4)) is not None
        assert T.evaluate(c, mv(holding_days=4, current_return_pct=3.0)) is None
        assert T.evaluate(c, mv(holding_days=1, current_return_pct=0.4)) is None

    def test_the_first_fired_condition_is_returned(self):
        conds = [Condition("price_below", 10.0),      # not fired
                 Condition("loss_exceeds", 0.5)]       # fired
        got = T.evaluate(conds, mv(current_return_pct=-2))
        assert got.kind == "loss_exceeds"


class TestMissingDataNeverFires:
    """A failed fetch must not be read as a broken thesis."""

    @pytest.mark.parametrize("kind,value,attr", [
        ("theme_strength_below", 4, "theme_strength"),
        ("theme_daily_score_below", 0, "theme_daily_score"),
        ("theme_flow_negative", 10, "theme_net_flow_yi"),
        ("theme_rank_worse_than", 20, "theme_rank"),
        ("breadth_below", 1.0, "breadth_ratio"),
    ])
    def test_none_field_holds(self, kind, value, attr):
        assert T.evaluate([Condition(kind, value)], mv(**{attr: None})) is None

    def test_but_it_fires_when_the_data_is_there(self):
        assert T.evaluate([Condition("theme_strength_below", 4)],
                          mv(theme_strength=2)) is not None


class TestNarrative:
    def test_never_fires_mechanically(self):
        c = [Condition("narrative", None, "若关税豁免未续期")]
        assert T.evaluate(c, mv(current_return_pct=-30)) is None

    def test_but_flags_the_thesis_for_review(self):
        assert T.needs_narrative_review(
            [Condition("narrative", None, "x")])
        assert not T.needs_narrative_review([Condition("price_below", 1)])


class TestPersistence:
    def make(self, **kw):
        base = dict(code="000962", name="东方钽业", theme="小金属概念",
                    claim="小金属资金持续流入，高beta标的跟涨", horizon_days=5,
                    prob=0.62, conviction=0.7,
                    conditions=[Condition("theme_daily_score_below", 0),
                                Condition("drawdown_from_peak", 5)])
        base.update(kw)
        return T.Thesis(**base)

    def test_round_trip_keeps_the_conditions(self, store):
        tid = T.create(self.make())
        got = T.get_active()[0]
        assert got.id == tid
        assert got.prob == 0.62
        assert [c.kind for c in got.conditions] == [
            "theme_daily_score_below", "drawdown_from_peak"]

    def test_closing_records_which_condition_fired(self, store):
        tid = T.create(self.make())
        T.close(tid, T.INVALIDATED, close_kind="drawdown_from_peak",
                close_note="峰值+8%回撤到+2%")
        assert T.get_active() == []
        closed = T.get_closed()
        assert closed[0].status == T.INVALIDATED
        assert closed[0].close_kind == "drawdown_from_peak"

    def test_a_non_closing_status_is_refused(self, store):
        tid = T.create(self.make())
        with pytest.raises(ValueError):
            T.close(tid, "active")

    def test_checkpoints_accumulate(self, store):
        tid = T.create(self.make())
        T.add_checkpoint(tid, "主线仍在流入", "hold")
        T.add_checkpoint(tid, "资金转出", "sell")
        got = T.get_closed(days=1) or T.get_active()
        assert len(got[0].checkpoints) == 2
        assert got[0].checkpoints[-1]["verdict"] == "sell"

    def test_position_link(self, store):
        tid = T.create(self.make())
        T.attach_position(tid, 42)
        assert T.get_by_position(42).id == tid


class TestPromptVocabulary:
    def test_lists_every_mechanical_kind(self):
        text = T.prompt_vocabulary()
        for kind in T.VALID_KINDS:
            assert kind in text

    def test_generated_from_the_evaluator(self):
        """Prompt and evaluator read the same table, so they cannot drift."""
        listed = {line.split('"')[1] for line in T.prompt_vocabulary().splitlines()
                  if '"' in line}
        assert listed == set(T.VALID_KINDS)


class TestFromRecommendation:
    """One constructor, two producers. The morning agent writes its own
    conditions; the intraday picks are code-generated, so theirs are
    derived from the signals that selected the stock — the entry reasons
    inverted rather than thresholds a model guessed at."""

    def test_agent_written_conditions_are_kept(self, store):
        rec = {"name": "东方钽业", "theme": "小金属概念", "prob": 0.7,
               "horizon_days": 5, "claim": "补涨",
               "invalidations": [{"kind": "price_below", "value": 49.5}]}
        T.from_recommendation(rec, "000962", created_by="morning")
        th = T.get_active()[0]
        assert [c.kind for c in th.conditions] == ["price_below"]
        assert th.conditions[0].value == 49.5
        assert th.created_by == "morning"

    def test_a_code_pick_derives_from_its_own_signals(self, store):
        rec = {"name": "锡业股份", "theme": "金属铜", "reason": "高beta+机构买入",
               "stop_loss": 32.5, "confidence": "high"}
        T.from_recommendation(rec, "000960", created_by="intraday")
        th = T.get_active()[0]
        kinds = [c.kind for c in th.conditions]
        assert "price_below" in kinds, "止损是选股时算出来的真实价位"
        assert "theme_daily_score_below" in kinds, "买入依据是主线在流入"
        assert "no_progress_by_day" in kinds
        assert th.conditions[0].value == 32.5

    def test_a_themeless_pick_gets_no_theme_condition(self, store):
        """A stock picked on its own merits must not exit on a theme it
        was never in."""
        rec = {"name": "X", "theme": "", "stop_loss": 10.0}
        T.from_recommendation(rec, "000001", created_by="intraday")
        kinds = [c.kind for c in T.get_active()[0].conditions]
        assert "theme_daily_score_below" not in kinds

    def test_no_stop_loses_the_price_condition_not_the_thesis(self, store):
        rec = {"name": "X", "theme": "金属铜"}
        assert T.from_recommendation(rec, "000001", created_by="intraday")
        kinds = [c.kind for c in T.get_active()[0].conditions]
        assert "price_below" not in kinds
        assert kinds, "其余条件仍在，仓位不该没有任何防守"

    def test_an_invalid_agent_condition_falls_back_to_derived(self, store):
        """A dropped condition must not leave the position unguarded."""
        rec = {"name": "X", "theme": "金属铜", "stop_loss": 10.0,
               "invalidations": [{"kind": "rsi_oversold", "value": 30}]}
        T.from_recommendation(rec, "000001", created_by="morning")
        assert T.get_active()[0].conditions

    def test_probability_is_clamped(self, store):
        T.from_recommendation({"prob": 1.5, "name": "X"}, "000001", "morning")
        assert T.get_active()[0].prob == 0.95

    def test_a_missing_probability_falls_back_to_the_label(self, store):
        T.from_recommendation({"confidence": "high", "name": "X"},
                              "000001", "morning")
        assert 0 < T.get_active()[0].prob < 1

    def test_conviction_follows_probability(self, store):
        T.from_recommendation({"prob": 0.8, "name": "X"}, "000001", "morning")
        assert T.get_active()[0].conviction == pytest.approx(0.6)

    def test_a_coin_flip_earns_no_extra_size(self, store):
        T.from_recommendation({"prob": 0.5, "name": "X"}, "000001", "morning")
        assert T.get_active()[0].conviction == 0.0

    def test_a_zero_conviction_survives_the_round_trip(self, store):
        """`or 0.5` read 0.0 as missing, so an idea the agent had no
        conviction in came back as a medium one and sized near the top of
        the range instead of the floor."""
        T.from_recommendation({"prob": 0.4, "name": "X"}, "000001", "morning")
        assert T.get_active()[0].conviction == 0.0
