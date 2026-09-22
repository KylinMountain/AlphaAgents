"""复盘读它看见的全部，而不只是它买到的那几笔。

``evidence.analyse`` asks one question of closed trades. On the 2026-01-05
20-day replay that sample was **3**, below ``MIN_TRADES``, so nothing was
written — while 8,599 rows of the same run's own decisions sat in
``theme_opportunity_items`` (7,800), ``opportunity_items`` (799) and
``position_exits`` (10).

The tests below pin the two things that make the output usable rather than
flattering: the numbers come from ``daily_kline`` and nothing else, and a
sample below the dimension's floor is reported as such instead of being
turned into a claim.
"""

import sqlite3

import pytest

from alpha_agents.evolution import review as RV


# ── a small, fully-specified world ──────────────────────────────────────


def _hist() -> sqlite3.Connection:
    """Six sessions, four stocks, prices chosen so every expectation is
    arithmetic a reader can redo by hand."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL,"
                 " high REAL, low REAL, close REAL, change_pct REAL)")
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
            "2026-01-09", "2026-01-12", "2026-01-13"]
    #: code -> closes across the seven sessions
    series = {
        "AAA": [10, 10, 11, 12, 13, 14, 15],     # strong
        "BBB": [10, 10, 10, 10, 10, 10, 10],     # flat
        "CCC": [10, 10, 9, 8, 7, 6, 5],          # weak
        "DDD": [10, 10, 10.5, 11, 11.5, 12, 12.5],
    }
    for code, closes in series.items():
        for day, close in zip(days, closes):
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)",
                (code, day, close, close * 1.02, close * 0.98, close, 0.0))
    return conn


def _book() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE theme_opportunity_sets (id INTEGER PRIMARY KEY,
            run_id TEXT, day TEXT);
        CREATE TABLE theme_opportunity_items (id INTEGER PRIMARY KEY,
            theme_opportunity_set_id INTEGER, sector_id TEXT, status TEXT);
        CREATE TABLE opportunity_sets (id INTEGER PRIMARY KEY,
            run_id TEXT, day TEXT);
        CREATE TABLE opportunity_items (id INTEGER PRIMARY KEY,
            opportunity_set_id INTEGER, code TEXT, status TEXT,
            panel_row_json TEXT);
        CREATE TABLE virtual_portfolio (id INTEGER PRIMARY KEY, code TEXT,
            order_date TEXT, entry_high REAL, status TEXT, trader_id TEXT,
            open_date TEXT, close_date TEXT, shares INTEGER);
        CREATE TABLE position_exits (id INTEGER PRIMARY KEY, code TEXT,
            exit_date TEXT, price REAL, shares INTEGER, return_pct REAL,
            reason TEXT);
    """)
    return conn


MEMBERS = {"strong": ["AAA"], "flat": ["BBB"], "weak": ["CCC"],
           "mild": ["DDD"]}


# ── ① direction ─────────────────────────────────────────────────────────


class TestDirectionChoice:
    """The control group is the shortlist it actually saw.

    Scoring a pick against all 390 concepts would say "you missed today's
    best" every single day, which is true and useless. Against the five it
    looked at, "you took the second-weakest" is actionable.
    """

    def _world(self, selected: str):
        book, hist = _book(), _hist()
        book.execute("INSERT INTO theme_opportunity_sets VALUES (1,'r','2026-01-05')")
        for i, (theme, status) in enumerate(
                [("strong", "offered_not_researched"),
                 ("flat", "offered_not_researched"),
                 ("weak", "offered_not_researched"),
                 ("mild", "offered_not_researched")], start=1):
            book.execute("INSERT INTO theme_opportunity_items VALUES (?,1,?,?)",
                         (i, theme, "agent_selected" if theme == selected
                          else status))
        return book, hist

    def test_picking_the_best_of_four_scores_at_the_top(self):
        book, hist = self._world("strong")
        f = RV.direction_choice(book, hist, MEMBERS, run_id="r", horizon=5)
        assert f.n == 1
        assert f.detail[0]["percentile"] == pytest.approx(87.5)

    def test_picking_the_worst_scores_at_the_bottom(self):
        book, hist = self._world("weak")
        f = RV.direction_choice(book, hist, MEMBERS, run_id="r", horizon=5)
        assert f.detail[0]["percentile"] == pytest.approx(12.5)

    def test_one_day_is_below_the_floor_and_says_so(self):
        book, hist = self._world("strong")
        f = RV.direction_choice(book, hist, MEMBERS, run_id="r", horizon=5)
        assert f.too_thin is True
        assert f.value is None
        assert "下限" in f.note

    def test_a_direction_it_was_never_offered_is_not_a_rejection(self):
        """``evaluated_not_offered`` is 7,640 of the 7,800 rows. Counting it
        as a control would score the agent against directions nobody put in
        front of it."""
        book, hist = self._world("strong")
        book.execute("INSERT INTO theme_opportunity_items VALUES "
                     "(9,1,'never','evaluated_not_offered')")
        f = RV.direction_choice(book, hist, MEMBERS, run_id="r", horizon=5)
        assert f.detail[0]["offered"] == 4


# ── ② stock ─────────────────────────────────────────────────────────────


class TestStockChoice:
    """"我选中主线了，为什么没选对股票" — the peers are the same day and
    the same line, so the direction's own move cancels out."""

    def _world(self, chosen: str):
        book, hist = _book(), _hist()
        book.execute("INSERT INTO opportunity_sets VALUES (1,'r','2026-01-05')")
        for i, code in enumerate(["AAA", "BBB", "CCC", "DDD"], start=1):
            book.execute(
                "INSERT INTO opportunity_items VALUES (?,1,?,?,?)",
                (i, code, "agent_selected" if code == chosen
                 else "offered_not_researched",
                 '{"primary_theme": "one_line"}'))
        return book, hist

    def test_the_best_of_the_line_scores_at_the_top(self):
        book, hist = self._world("AAA")
        f = RV.stock_choice(book, hist, run_id="r", horizon=5)
        assert f.detail[0]["percentile"] == pytest.approx(100.0)
        assert f.detail[0]["peers"] == 3

    def test_the_worst_of_the_line_scores_at_the_bottom(self):
        book, hist = self._world("CCC")
        f = RV.stock_choice(book, hist, run_id="r", horizon=5)
        assert f.detail[0]["percentile"] == pytest.approx(0.0)

    def test_the_best_peer_is_named_so_the_note_can_cite_it(self):
        book, hist = self._world("CCC")
        f = RV.stock_choice(book, hist, run_id="r", horizon=5)
        # AAA over the five sessions from 01-05: 10 → 14, so +40%.
        assert f.detail[0]["best_peer_pct"] == pytest.approx(40.0)

    def test_a_pick_with_no_theme_is_skipped_not_scored_against_everything(self):
        book, hist = _book(), _hist()
        book.execute("INSERT INTO opportunity_sets VALUES (1,'r','2026-01-05')")
        book.execute("INSERT INTO opportunity_items VALUES "
                     "(1,1,'AAA','agent_selected','{}')")
        assert RV.stock_choice(book, hist, run_id="r", horizon=5).n == 0


# ── ③ entry ─────────────────────────────────────────────────────────────


class TestEntryPricing:
    """The one measurement that needs no P&L at all.

    Measured on the live 20-day run: median **−2.1 percentage points** — it
    asked 2.1pp below where the stock actually opened, on 17 of 17 orders.
    A cancelled order is evidence, and it is the evidence survivorship bias
    keeps out of a trade-only review.
    """

    def _order(self, entry_high: float):
        book, hist = _book(), _hist()
        book.execute(
            "INSERT INTO virtual_portfolio (id, code, order_date, entry_high,"
            " status, trader_id) VALUES (1,'AAA','2026-01-07',?, 'cancelled',"
            " 'pullback')", (entry_high,))
        return book, hist

    def test_asking_below_the_open_is_a_negative_gap(self):
        # 2026-01-06 close 10, 2026-01-07 open 11 → open is 1.10 × prev close.
        book, hist = self._order(10.0)          # ask 1.00 × prev close
        f = RV.entry_pricing(book, hist, trader_id="pullback")
        assert f.detail[0]["gap_pp"] == pytest.approx(-10.0)

    def test_asking_above_the_open_is_positive(self):
        book, hist = self._order(12.0)          # ask 1.20 × prev close
        f = RV.entry_pricing(book, hist, trader_id="pullback")
        assert f.detail[0]["gap_pp"] == pytest.approx(10.0)

    def test_an_order_with_no_prior_bar_is_dropped_not_zeroed(self):
        book, hist = _book(), _hist()
        book.execute(
            "INSERT INTO virtual_portfolio (id, code, order_date, entry_high,"
            " status, trader_id) VALUES (1,'AAA','2026-01-05',10.0,"
            " 'cancelled','pullback')")
        assert RV.entry_pricing(book, hist, trader_id="pullback").n == 0


# ── ④ exit ──────────────────────────────────────────────────────────────


class TestExitTiming:
    """The window is what happened **after** the sale.

    The holding period's own high is what the agent could see; pointing at
    it afterwards is hindsight. What the trim actually bet on is the next
    few sessions, so that is the window scored.
    """

    def _exit(self, code: str, price: float):
        book, hist = _book(), _hist()
        book.execute("INSERT INTO position_exits (id, code, exit_date, price,"
                     " shares, return_pct, reason) VALUES "
                     "(1,?, '2026-01-06', ?, 100, 0.0, 'agent减仓')",
                     (code, price))
        return book, hist

    def test_selling_at_the_forward_high_scores_at_the_top(self):
        # AAA over 01-06..01-13: low 9.8, high 15.3
        book, hist = self._exit("AAA", 15.3)
        f = RV.exit_timing(book, hist, horizon=5)
        assert f.detail[0]["position_pct"] == pytest.approx(100.0)
        assert f.detail[0]["left_on_table_pct"] == pytest.approx(0.0)

    def test_selling_at_the_forward_low_scores_at_the_bottom(self):
        book, hist = self._exit("AAA", 9.8)
        f = RV.exit_timing(book, hist, horizon=5)
        assert f.detail[0]["position_pct"] == pytest.approx(0.0)

    def test_what_was_left_on_the_table_is_reported(self):
        book, hist = self._exit("AAA", 10.0)
        assert f_left(RV.exit_timing(book, hist, horizon=5)) == pytest.approx(53.0)

    def test_an_unclosed_forward_window_is_skipped(self):
        """A 5-day window that has run 2 days is a different quantity."""
        book, hist = self._exit("AAA", 10.0)
        book.execute("UPDATE position_exits SET exit_date = '2026-01-12'")
        assert RV.exit_timing(book, hist, horizon=5).n == 0


def f_left(finding) -> float:
    return finding.detail[0]["left_on_table_pct"]


# ── ⑤ absence ───────────────────────────────────────────────────────────


class TestAbsence:
    """Doing nothing in a rising window costs money the P&L cannot show."""

    def _world(self, *, held: bool):
        book, hist = _book(), _hist()
        for i, day in enumerate(["2026-01-05", "2026-01-06", "2026-01-07"], 1):
            book.execute("INSERT INTO opportunity_sets VALUES (?,'r',?)", (i, day))
        hist.executemany(
            "INSERT INTO daily_kline (code, date, close, change_pct) VALUES (?,?,?,?)",
            [(f"F{i:04d}", "2026-01-06", 10.0, 2.0) for i in range(60)])
        if held:
            book.execute(
                "INSERT INTO virtual_portfolio (id, code, open_date, close_date,"
                " shares, status) VALUES (1,'AAA','2026-01-05','2026-01-07',100,"
                " 'closed')")
        return book, hist

    def test_a_flat_day_carries_the_markets_median(self):
        book, hist = self._world(held=False)
        f = RV.absence(book, hist, run_id="r")
        day = [d for d in f.detail if d["day"] == "2026-01-06"][0]
        assert day["market_median_pct"] == pytest.approx(2.0)

    def test_a_day_on_the_field_is_not_an_absence(self):
        book, hist = self._world(held=True)
        assert RV.absence(book, hist, run_id="r").n == 0


# ── the property the whole thing rests on ───────────────────────────────


class TestNoModelGradesThisRun:
    """``GOLDEN_PRINCIPLES`` §2. A model that reads 7,800 rows and concludes
    "my direction picks were good" has said something nothing can refute.
    The numbers come from ``daily_kline``; the model's job is to explain
    them, and its explanation is checked by the next window's numbers.
    """

    def test_the_module_imports_nothing_that_can_reach_a_model(self):
        """Asserted on the import graph, not on the text — the docstring
        talks about LLMs precisely because the module must not call one."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(RV))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {"__future__", "json", "logging", "sqlite3",
                            "statistics", "dataclasses"}, imported

    def test_review_runs_with_only_two_sqlite_handles(self):
        book, hist = _book(), _hist()
        out = RV.review(book, hist, MEMBERS, run_id="r")
        assert [f.dimension for f in out] == [
            "direction", "stock", "entry", "exit", "absence"]
        assert all(f.too_thin for f in out), "an empty book states nothing"

    def test_every_dimension_has_a_floor(self):
        assert set(RV.MIN_N) == {"direction", "stock", "entry", "exit", "absence"}
        assert all(v > 0 for v in RV.MIN_N.values())


class TestTheTextHandedToTheAgent:
    def test_a_thin_dimension_says_so_rather_than_stating_a_value(self):
        book, hist = _book(), _hist()
        text = RV.as_text(RV.review(book, hist, MEMBERS, run_id="r"))
        assert "不足以判断" in text
        assert "中位" not in text.split("【")[1].split("每一条")[0].replace(
            "中位数", "")

    def test_the_header_says_the_numbers_are_not_an_appraisal(self):
        text = RV.as_text([RV.Finding("entry", "q", 9, -2.1, "百分点", False)])
        assert "数由行情算出" in text
        assert "-2.1百分点" in text
        assert "n=9" in text


class TestPercentileRank:
    def test_two_others_is_a_coin_flip_not_a_percentile(self):
        assert RV._pct_rank(1.0, [0.0, 2.0]) is None

    def test_ties_land_in_the_middle(self):
        assert RV._pct_rank(1.0, [1.0, 1.0, 1.0]) == pytest.approx(50.0)


class TestTheWindowIsSealedAtAsOf:
    """The review's output goes into the next day's prompt, so it has to be
    point-in-time. Without the cap, day 10's review scores day 9's pick with
    day 14's bars and hands that score to day 11's agent — lookahead entering
    the decision through the feedback channel rather than the data feed.
    """

    def test_a_window_that_has_not_closed_yet_is_not_scored(self):
        hist = _hist()
        # The 5-day window from 01-06 closes on 01-13.
        assert RV._forward_pct(hist, "AAA", "2026-01-06", 5,
                               as_of="2026-01-09") is None
        assert RV._forward_pct(hist, "AAA", "2026-01-06", 5,
                               as_of="2026-01-13") == pytest.approx(50.0)

    def test_a_closed_window_reads_the_same_with_or_without_the_cap(self):
        hist = _hist()
        capped = RV._forward_pct(hist, "AAA", "2026-01-05", 5, as_of="2026-01-12")
        uncapped = RV._forward_pct(hist, "AAA", "2026-01-05", 5)
        assert capped == uncapped == pytest.approx(40.0)

    def test_an_order_placed_after_as_of_is_not_reviewed(self):
        book, hist = _book(), _hist()
        book.execute(
            "INSERT INTO virtual_portfolio (id, code, order_date, entry_high,"
            " status, trader_id) VALUES (1,'AAA','2026-01-09',10.0,'cancelled',"
            " 'pullback')")
        assert RV.entry_pricing(book, hist, trader_id="pullback",
                                as_of="2026-01-07").n == 0
        assert RV.entry_pricing(book, hist, trader_id="pullback",
                                as_of="2026-01-09").n == 1

    def test_a_future_session_is_not_an_absence_to_regret(self):
        hist = _hist()
        assert RV._market_median_pct(hist, "2026-01-13", as_of="2026-01-07") is None

    def test_review_threads_the_cap_into_every_dimension(self):
        """A dimension that quietly dropped it would leak without a symptom."""
        import inspect
        src = inspect.getsource(RV.review)
        assert src.count("as_of=as_of") == 5
