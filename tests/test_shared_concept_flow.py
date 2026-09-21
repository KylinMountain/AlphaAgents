"""The one concept-flow ranking, and the one anomaly rule, shared by both.

The defect these pin. The live intraday trader picked directions from concept
**fund-flow anomalies** (Cases A-E) while the replay picked them from a
price/breadth ranking. So "which concept is hot" was answered two ways, and
sector-level replay evidence described the replay rule rather than the
trader's.

Now both call `data/sector_scoring.rank_concepts_by_flow` (shaping) and
`pipeline.tasks.anomaly_scan.concept_anomaly_signals` (the Cases). These tests
pin the arithmetic and, more importantly, that a second copy has not grown
back.
"""

import inspect

from alpha_agents.data import sector_scoring
from alpha_agents.pipeline.tasks import anomaly_scan as A


def _ranking():
    return {
        "gainers": [
            {"concept": "存储芯片", "change_pct": 1.4, "net_flow_yi": 50.7,
             "leader": "气派科技"},
            {"concept": "汽车芯片", "change_pct": 0.4, "net_flow_yi": 27.4,
             "leader": "X"},
            {"concept": "平静板块", "change_pct": 0.2, "net_flow_yi": 0.5,
             "leader": "Y"},
        ],
        "losers": [
            {"concept": "国企改革", "change_pct": -1.7, "net_flow_yi": -208.6},
        ],
    }


class TestTheShapingIsOneImplementation:
    def test_it_accepts_both_the_raw_and_normalised_column_names(self):
        """The live reader passes akshare names, the replay passes db names."""
        raw = [{"行业": "X", "行业-涨跌幅": 2.0, "净额": 7.0, "领涨股": "L"}]
        shaped = sector_scoring.rank_concepts_by_flow(raw)
        assert shaped["gainers"][0]["concept"] == "X"
        assert shaped["gainers"][0]["net_flow_yi"] == 7.0
        assert shaped["gainers"][0]["change_pct"] == 2.0

        # The same row through the normalised names must produce the same
        # numbers. ``leader`` is absent from this input, so an empty string is
        # the correct answer — the point is that the financial fields match.
        normalised = [{"concept": "X", "change_pct": 2.0, "net_flow_yi": 7.0}]
        other = sector_scoring.rank_concepts_by_flow(normalised)["gainers"][0]
        for field in ("concept", "change_pct", "net_flow_yi"):
            assert other[field] == shaped["gainers"][0][field]

    def test_inflow_goes_to_gainers_and_outflow_to_losers(self):
        shaped = sector_scoring.rank_concepts_by_flow([
            {"concept": "A", "net_flow_yi": 5.0},
            {"concept": "B", "net_flow_yi": -5.0},
        ])
        assert [r["concept"] for r in shaped["gainers"]] == ["A"]
        assert [r["concept"] for r in shaped["losers"]] == ["B"]

    def test_ties_break_on_the_name_not_on_input_order(self):
        """Two concepts at the same inflow must not order by who arrived first."""
        first = sector_scoring.rank_concepts_by_flow([
            {"concept": "B", "net_flow_yi": 5.0},
            {"concept": "A", "net_flow_yi": 5.0},
        ])
        second = sector_scoring.rank_concepts_by_flow([
            {"concept": "A", "net_flow_yi": 5.0},
            {"concept": "B", "net_flow_yi": 5.0},
        ])
        assert [r["concept"] for r in first["gainers"]] == ["A", "B"]
        assert first == second

    def test_the_live_path_has_no_second_copy(self):
        from alpha_agents.tools import sector_ranking
        source = inspect.getsource(sector_ranking.get_concept_ranking_fn)
        assert "rank_concepts_by_flow" in source
        assert "gainers.sort" not in source, (
            "the shaping has been written out again instead of delegated")


class TestTheCasesAreOneImplementation:
    def test_case_a_fires_on_a_rise_with_large_inflow(self):
        got = A.concept_anomaly_signals(_ranking())
        names = [name for name, _text in got]
        assert "存储芯片" in names

    def test_case_c_fires_on_a_flat_board_with_large_inflow(self):
        got = A.concept_anomaly_signals(_ranking())
        names = [name for name, _text in got]
        assert "汽车芯片" in names

    def test_case_d_fires_on_a_fall_with_large_outflow(self):
        got = A.concept_anomaly_signals(_ranking())
        names = [name for name, _text in got]
        assert "国企改革" in names

    def test_a_quiet_board_raises_nothing(self):
        got = A.concept_anomaly_signals({
            "gainers": [{"concept": "平静", "change_pct": 0.2,
                         "net_flow_yi": 0.5}],
            "losers": [],
        })
        assert got == []

    def test_it_returns_names_not_only_text(self):
        """The replay selects directions by name.

        It used to re-parse the concept out of the rendered sentence, which
        made the message format load-bearing and was the first thing to break
        when the wording changed.
        """
        for name, text in A.concept_anomaly_signals(_ranking()):
            assert name and name in text

    def test_the_caller_does_not_reimplement_the_cases(self):
        source = inspect.getsource(A._detect_anomalies)
        assert "concept_anomaly_signals" in source
        for literal in ("flow > 3", "flow > 5", "flow < -3", "flow > 2"):
            assert literal not in source, (
                f"{literal!r} is inline again; the shared Cases are not the "
                "only definition")
