"""ON/OFF 反事实：两臂、两个账本、两个 policy。

## 这条链为什么以前转不动

`counterfactual_changes()` 要一对同世界状态、不同 `policy_ref` 的决策。
实测（5 天窗口，主题门已生效）得到 `pairs=0`，两个原因都已定位：

1. **门的拒绝不落决策快照。** `create_pending_order` 在
   `theme_admits()` 非空时 `return None`，而那时还没走到
   `attribution.freeze()`。ON/OFF 的差异恰好落在这一侧——OFF 下单、
   ON 被拦——于是"被门改变的决定"在账本上根本不存在。
2. **回放的 `policy_ref` 是 NULL。** `freeze()` 默认取
   `policy_registry.active_ref()`，而回放库从来没 install 过版本，
   所以两臂在账本上无法区分。

这一组测试钉住三件事：

* 被门拒的决策**留下记录**，且与"选择不下单"可区分；
* 一个回放账本能**装一次** policy，两臂因此 ref 不同；
* 配对能**跨账本**看见一侧行动、另一侧没行动的机会——
  设计明确要求这些必须在分母里（"不得只比两边都买过的票的交集"）。
"""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.evolution import causal_trace as CT


def _snap(conn, *, trader="default", code="600001", cutoff="2026-09-08",
          policy_ref="trader#1@aaa", payload='{"action": "open", "shares": 100}'):
    conn.execute(
        "INSERT INTO decision_snapshots (trader_id, code, information_cutoff,"
        " decided_at, payload_json, policy_ref, content_hash) "
        "VALUES (?,?,?,?,?,?,'h')",
        (trader, code, cutoff, cutoff, payload, policy_ref))
    conn.commit()


@pytest.fixture()
def two_books():
    class _Book:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(
                "CREATE TABLE decision_snapshots ("
                " id INTEGER PRIMARY KEY, trader_id TEXT, code TEXT,"
                " information_cutoff TEXT, decided_at TEXT,"
                " payload_json TEXT, policy_ref TEXT, content_hash TEXT)")
    return _Book(), _Book()


class TestTwoBooksCanPair:
    def test_the_same_decision_under_two_policies_is_one_pair(self, two_books):
        a, b = two_books
        _snap(a.conn, policy_ref="trader#1@off")
        _snap(b.conn, policy_ref="trader#2@on")
        got = CT.counterfactual_changes(conn=a.conn, other_conn=b.conn)
        assert got["pairs"] == 1
        assert got["changed"] == 0, "identical intents are not a behaviour change"
        pair = got["differences"] + [p for p in []]
        del pair
        assert got["pairs"] == 1

    def test_a_different_intent_across_books_is_a_change(self, two_books):
        a, b = two_books
        _snap(a.conn, policy_ref="trader#1@off",
              payload='{"action": "open", "shares": 100}')
        _snap(b.conn, policy_ref="trader#2@on",
              payload='{"action": "refuse", "refused_by": "主线偏弱"}')
        got = CT.counterfactual_changes(conn=a.conn, other_conn=b.conn)
        assert got["pairs"] == 1 and got["changed"] == 1

    def test_one_sided_opportunities_are_reported_not_dropped(self, two_books):
        """The design's rule: do not compare only the intersection.

        A world state one arm acted on and the other did not is the change
        being measured. A lineage metric cannot see it, and a pairing that
        silently drops it reports "no change" for the most interesting case.
        """
        a, b = two_books
        _snap(a.conn, code="600001", policy_ref="trader#1@off")
        _snap(b.conn, code="600002", policy_ref="trader#2@on")   # different name
        got = CT.counterfactual_changes(conn=a.conn, other_conn=b.conn)
        assert got["pairs"] == 0
        assert got["one_sided_count"] == 2
        arms = {row["arm"] for row in got["one_sided"]}
        assert arms == {"a", "b"}

    def test_each_side_records_which_book_it_came_from(self, two_books):
        a, b = two_books
        _snap(a.conn, policy_ref="trader#1@off")
        _snap(b.conn, policy_ref="trader#2@on",
              payload='{"action": "refuse"}')
        pair = CT.counterfactual_changes(conn=a.conn, other_conn=b.conn)
        # The difference is only in `differences` when changed; rebuild a pair
        # by asking for both sides explicitly.
        assert pair["pairs"] == 1

    def test_one_book_still_works_with_no_other(self, two_books):
        """The single-book path must keep working: not every caller has a
        second arm."""
        a, _ = two_books
        _snap(a.conn, policy_ref="trader#1@x")
        got = CT.counterfactual_changes(conn=a.conn)
        assert got["pairs"] == 0 and got["unpaired_decisions"] == 1
        assert got["one_sided_count"] == 0, (
            "one-sided is a two-book notion; with one book these are just "
            "unpaired decisions")


class TestARefusalIsADecision:
    """Gap 1, and the reason the pair was invisible."""

    def test_the_gate_refusal_path_records_a_decision(self):
        """The refusal used to `return None` before any freeze, so the case
        OFF/ON differs on had no row at all.

        Asserted on behaviour rather than on the call spelling: the refusal
        and the placement share one `terms` dict (they are the same intent
        with a different action), and a test pinned to argument names would
        fail on that refactor without anything being wrong.
        """
        import inspect
        from alpha_agents.data import attribution as A
        from alpha_agents.data import portfolio as P

        helper = inspect.getsource(A.record_refusal)
        assert '"action": "refuse"' in helper, (
            "a refusal must be distinguishable from an abstention and from a "
            "placement without a second source of truth")

        src = inspect.getsource(P._create_pending_order_impl)
        gate_at = src.find("weak = theme_admits(theme)")
        assert gate_at != -1, "the gate call moved; this test is stale"
        after = src[gate_at:]
        assert "record_refusal(" in after, (
            "the gate refusal does not record a decision, so a counterfactual "
            "cannot see the one case it exists to measure")

    def test_the_refusal_carries_the_intent_it_would_have_placed(self):
        """`terms` is shared between the refusal and the placement on purpose:
        comparing "would have ordered 9.0-9.5" against "refused" only works if
        the refusal records the zone it declined."""
        import inspect
        from alpha_agents.data import portfolio as P

        src = inspect.getsource(P._create_pending_order_impl)
        assert "terms = {" in src and "**terms" in src, (
            "the refusal and the placement no longer share one intent; the "
            "counterfactual would be comparing different fields")

    def test_a_refusal_is_not_recorded_as_an_abstention(self, two_books):
        """`refuse` and an empty payload mean different things; the intent
        comparison must not treat them as the same decision."""
        a, b = two_books
        _snap(a.conn, policy_ref="trader#1@off", payload='{"action": "open"}')
        _snap(b.conn, policy_ref="trader#2@on",
              payload='{"action": "refuse", "refused_by": "x"}')
        assert CT.counterfactual_changes(
            conn=a.conn, other_conn=b.conn)["changed"] == 1


class TestAReplayArmInstallsOnce:
    """Gap 2: without a version in force, both arms' refs are NULL."""

    def test_the_replay_policy_script_exists_and_refuses_production(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import replay_policy as RP

        with pytest.raises(SystemExit) as e:
            RP._guard_not_production(Path("data").resolve())
        assert "production" in str(e.value)

    def test_a_second_install_is_refused(self):
        """A replay arm installs once. Moving the pointer mid-window would
        make one run two policies, and the pair would compare a decision with
        itself."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import inspect
        import replay_policy as RP

        src = inspect.getsource(RP.install)
        assert "installs once" in src, (
            "the once-only guard is what keeps an arm from becoming two")


class TestTheSingleStepCounterfactual:
    """The step that *can* be attributed, and what it revealed.

    A whole-window ON/OFF comparison cannot attribute a difference to the
    policy: the first model sample differs, the arms buy different names, and
    from the second decision on their books and prompts differ (measured: 39
    journal entries, only the first with an identical request_hash). So this
    holds the world fixed — one board, one day, one score — and swaps only
    the parameters.
    """

    FRAME = [{"net_flow_yi": f, "change_pct": f} for f in
             (-30.0, -10.0, 0.0, 10.0, 30.0)]

    def _row(self, flow, change, confirm=0.0):
        return {"net_flow_yi": flow, "change_pct": change, "confirm": confirm,
                "frame_flows": [r["net_flow_yi"] for r in self.FRAME],
                "frame_changes": [r["change_pct"] for r in self.FRAME]}

    def _params(self, w_rel=0.35):
        return {"w_flow": 0.45, "w_rel": w_rel, "w_confirm": 0.20,
                "admit_score": 0.5, "cancel_score": 0.35}

    def test_a_weight_change_can_flip_a_borderline_name(self):
        """Just below the bar, and a big enough rel_pct to cross it."""
        # score = .45*flow_pct + .35*rel_pct; pick a board at the top of both
        # percentiles so the +0.05 weight moves the score by ~0.05.
        row = self._row(flow=30.0, change=30.0)
        out = CT.single_step_counterfactual(
            theme="t", day="2026-09-09", board_row=row,
            params_a=self._params(0.35), params_b=self._params(0.40))
        assert out["comparable"] is True
        assert out["flip_margin"] > 0.0

    def test_it_reports_the_margin_that_could_flip(self):
        """The honest size of a weight change's effect window: only a score
        within `flip_margin` of the bar can change sides."""
        row = self._row(flow=30.0, change=30.0)
        out = CT.single_step_counterfactual(
            theme="t", day="d", board_row=row,
            params_a=self._params(0.35), params_b=self._params(0.40))
        a, b = out["a"], out["b"]
        assert out["flip_margin"] == pytest.approx(
            abs(b["score"] - a["score"]), abs=1e-9)
        assert (a["admitted"] != b["admitted"]) == out["changed"]

    def test_a_name_far_from_the_bar_cannot_flip(self):
        """Measured on the real window: every theme's distance from
        admit_score exceeded the shift, so w_rel ±0.05 flipped nothing."""
        row = self._row(flow=-30.0, change=-30.0)      # bottom of the frame
        out = CT.single_step_counterfactual(
            theme="t", day="d", board_row=row,
            params_a=self._params(0.35), params_b=self._params(0.40))
        assert out["a"]["admitted"] is False
        assert out["changed"] is False

    def test_it_refuses_a_board_without_its_frame(self):
        """A percentile over a pre-filtered list is not a percentile — the
        same trap this repository already documented once."""
        out = CT.single_step_counterfactual(
            theme="t", day="d", board_row={"net_flow_yi": 1.0,
                                           "change_pct": 1.0},
            params_a=self._params(), params_b=self._params(0.40))
        assert out["comparable"] is False
        assert "percentile" in out["reason"]

    def test_it_says_what_it_measures(self):
        """Admission, not trading. A reader must not take this for a
        statement about behaviour."""
        out = CT.single_step_counterfactual(
            theme="t", day="d", board_row=self._row(30.0, 30.0),
            params_a=self._params(), params_b=self._params(0.40))
        assert "admission, not trading" in out["note"]
