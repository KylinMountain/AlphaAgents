"""The trailing stop must not measure its own output.

The rule reads an entry stop, computes a trailed stop from it, and writes the
result back to ``virtual_portfolio.stop_loss`` — the same column it just read.
While the formula derived its distance from that column, every cycle multiplied
the stop by ``peak / open``, so the value diverged geometrically instead of
converging. On 2026-09-16 the live book showed the end state of that:
``000510 新金路`` carried a stop of ``15,352,643.13`` on a ``16.31`` entry, and
the log recorded the per-cycle factor exactly — ``35630.83 / 31635.21 = 1.126303
= 18.37 / 16.31`` (D29).

The tests below are deliberately **pairwise**, because a rule that "does not
diverge" and a rule that "does nothing" look identical from one side:

* a healthy position trails up and then **stops moving** (the divergence case),
* the distance comes from the entry stop, not from the ratcheted one (with the
  same row, and *without* the column, showing the column is what decides it),
* a stop above the peak is repaired, and a sane stop is not reset by that repair,
* an entry stop that cannot be measured is refused rather than invented — and a
  column the query never selected *raises*, because reading "absent" as "NULL"
  is the other half of how D29 stayed hidden.
"""

import sqlite3
from unittest.mock import patch

import pytest

from alpha_agents.data.memory_store import _SCHEMA
# Through ``portfolio``, not through ``position_monitor``: the two modules
# import each other and ``portfolio`` re-exports this name, so entering the
# cycle from the rule side leaves ``portfolio`` half-initialised and the import
# fails. Same reason ``tests/test_portfolio.py`` reads it from here.
from alpha_agents.data.portfolio import check_positions
from alpha_agents.data.portfolio_book import entry_stop

TRADER = "default"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _seed(conn, *, code="000510", name="新金路", open_price, stop_loss,
          initial_stop_loss=None, peak_return_pct=0.0, shares=6100,
          open_date="2026-09-09", theme="") -> int:
    conn.execute(
        "INSERT INTO virtual_portfolio "
        "(code, name, theme, order_date, open_date, open_price, shares, "
        " stop_loss, initial_stop_loss, status, source, reason, trader_id, "
        " peak_return_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'test', 'seed', ?, ?)",
        (code, name, theme, open_date, open_date, open_price, shares,
         stop_loss, initial_stop_loss, TRADER, peak_return_pct),
    )
    conn.commit()
    return conn.execute("SELECT id FROM virtual_portfolio").fetchone()[0]


def _row(conn, position_id: int) -> dict:
    return dict(conn.execute(
        "SELECT * FROM virtual_portfolio WHERE id = ?", (position_id,)).fetchone())


@pytest.fixture()
def book():
    """An in-memory book, with every rule that reads the outside world pinned.

    The trailing rule is what is under test. The bearish-signal tightening, the
    sentiment phase and the holding-period cap are all *other* rules that can
    move the same stop, and each reaches for the network or the real corpus —
    so leaving them live would make the assertion "the stop is X" depend on
    which of them happened to succeed.
    """
    conn = _conn()
    patches = [
        patch("alpha_agents.data.position_monitor._get_conn", return_value=conn),
        patch("alpha_agents.data.portfolio_exit._get_conn", return_value=conn),
        patch("alpha_agents.data.portfolio_book._get_conn", return_value=conn),
        patch("alpha_agents.data.position_monitor._is_phase_bearish",
              return_value=(False, "")),
        patch("alpha_agents.data.position_monitor._check_bearish_signals",
              return_value=(0.0, [])),
        patch("alpha_agents.tools.exit_signals.check_holding_period",
              return_value=(False, "")),
        patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
              return_value={"phase": "升温",
                            "strategy": {"trailing_stop_pct": 5.0,
                                         "theme_exit_threshold": 3}}),
    ]
    for p in patches:
        p.start()
    try:
        yield conn
    finally:
        for p in patches:
            p.stop()


class TestItConvergesInsteadOfDiverging:
    def test_a_rising_position_trails_up_to_the_policy_distance(self, book):
        """Up 10%, policy trails 5% off the peak, the entry stop was 8% down.

        The tighter of the two distances wins for a position already up 5%+, so
        the stop lands at ``peak × (1 − 0.05)`` and not at the 8% entry
        distance. Asserted together with "it moved at all": a rule that returned
        the entry stop unchanged would satisfy the divergence test below.
        """
        position_id = _seed(book, open_price=10.0, stop_loss=9.2,
                            initial_stop_loss=9.2)

        check_positions(realtime_prices={"000510": 11.0}, today="2026-09-16")

        assert _row(book, position_id)["stop_loss"] == pytest.approx(10.45, abs=0.01)

    def test_the_stop_stops_moving_when_the_price_does(self, book):
        """Twenty cycles at one price must not move the stop twenty times.

        This is D29 itself. While the distance was read back from ``stop_loss``,
        each cycle multiplied the stop by ``peak / open`` — on the live row that
        compounded 65 times into seven figures. The pair is deliberate: the
        first assertion says the rule *did* raise the stop, the second says it
        then held still, so a no-op implementation cannot pass by doing nothing.
        """
        position_id = _seed(book, open_price=10.0, stop_loss=9.2,
                            initial_stop_loss=9.2)

        check_positions(realtime_prices={"000510": 11.0}, today="2026-09-16")
        after_one = _row(book, position_id)["stop_loss"]
        assert after_one > 9.2, "the rule must raise the stop at all"

        for _ in range(20):
            check_positions(realtime_prices={"000510": 11.0}, today="2026-09-16")

        row = _row(book, position_id)
        assert row["stop_loss"] == after_one
        assert row["status"] == "open", (
            "a stop above the price would have closed the position: the "
            "divergent value reads as 'stopped' on every cycle from the second "
            "one onward")

    def test_the_stop_never_ends_up_above_the_peak(self, book):
        """A stop at or above the peak is a market sell wearing a stop's name."""
        position_id = _seed(book, open_price=10.0, stop_loss=9.2,
                            initial_stop_loss=9.2)

        for price in (10.2, 10.6, 11.0, 11.4):
            check_positions(realtime_prices={"000510": price}, today="2026-09-16")

        row = _row(book, position_id)
        peak_price = row["open_price"] * (1 + row["peak_return_pct"] / 100)
        assert row["stop_loss"] <= peak_price

    def test_a_hostile_policy_value_cannot_put_the_stop_above_the_peak(self, book):
        """That invariant has to hold for the policy, not only for sane policies.

        ``trailing_stop_pct`` is read from the sentiment policy, so it is an
        input this rule does not own. A negative one makes ``peak × (1 − pct)``
        larger than the peak — the same impossible level the divergence landed on
        — and no amount of correct anchoring prevents it. So the clamp is
        asserted against a hostile value rather than trusted, since a clamp
        nothing can reach is a comment that looks like a guard.
        """
        position_id = _seed(book, open_price=10.0, stop_loss=9.2,
                            initial_stop_loss=9.2, peak_return_pct=20.0)

        with patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
                   return_value={"phase": "升温",
                                 "strategy": {"trailing_stop_pct": -20.0,
                                              "theme_exit_threshold": 3}}):
            check_positions(realtime_prices={"000510": 11.0}, today="2026-09-16")

        row = _row(book, position_id)
        peak_price = row["open_price"] * (1 + row["peak_return_pct"] / 100)
        assert row["stop_loss"] == pytest.approx(peak_price, abs=0.01)


class TestTheDistanceComesFromTheEntryStop:
    def test_it_trails_from_the_entry_stop_not_from_the_ratcheted_one(self, book):
        """A row whose stop has already been trailed keeps its entry distance.

        Entry 10.0, entry stop 9.2 (an 8% distance), stop already trailed to
        9.9 (a 1% distance). The price peaked at +12% and has come back to +4%,
        which is the 3–5% branch — the branch that trails at the *entry*
        distance. From the peak of 11.20 that is ``11.20 × 0.92 = 10.30``.
        Reading the ratcheted column instead would use 1% and propose
        ``11.20 × 0.99 = 11.09``, which the companion test pins.

        Both proposals sit above the stored 9.9, so the "never loosen a stop"
        guard lets either through — it is not what is being measured here. That
        guard is why the numbers are shaped this way: a proposal *below* the
        stored stop would be discarded, and the test would pass or fail for a
        reason that has nothing to do with the anchor.
        """
        position_id = _seed(book, open_price=10.0, stop_loss=9.9,
                            initial_stop_loss=9.2, peak_return_pct=12.0)

        check_positions(realtime_prices={"000510": 10.4}, today="2026-09-16")

        assert _row(book, position_id)["stop_loss"] == pytest.approx(10.30, abs=0.01)

    def test_without_the_column_the_ratcheted_stop_is_all_there_is(self, book):
        """The companion: a row that never recorded an entry stop.

        Nothing else about the row changes. It still trails, and still only
        upward — but the distance it can attest to is the ratcheted one, so the
        stop it proposes is a full percentage point tighter. That difference
        *is* the column's contribution, which is why the pair is here instead of
        one assertion that could pass for the wrong reason.
        """
        position_id = _seed(book, open_price=10.0, stop_loss=9.9,
                            initial_stop_loss=None, peak_return_pct=12.0)

        check_positions(realtime_prices={"000510": 10.4}, today="2026-09-16")

        assert _row(book, position_id)["stop_loss"] == pytest.approx(11.09, abs=0.01)


class TestACorruptedStopIsRepaired:
    #: The live value, copied from ``data/memory.db`` on 2026-09-16.
    CORRUPT = 15352643.13

    def test_a_stop_above_the_peak_is_recomputed(self, book, caplog):
        """The end state of the divergence, on the row it happened to.

        Entry 16.31, stop 15.3M — a level the price stopped being comparable to
        long ago. Left alone, the ratchet guard would *preserve* it, because the
        guard only ever raises: the position would read as 'stopped' on every
        cycle and could never be held again. Repaired, the stop falls back to
        ``HARD_STOP_PCT`` below the entry (the row predates the column, so there
        is no recorded entry stop to use) and the position is holdable again.
        """
        position_id = _seed(book, open_price=16.31, stop_loss=self.CORRUPT,
                            peak_return_pct=12.63)

        with caplog.at_level("WARNING", logger="alpha_agents.data.position_monitor"):
            check_positions(realtime_prices={"000510": 17.71}, today="2026-09-16")

        row = _row(book, position_id)
        assert row["stop_loss"] == pytest.approx(17.45, abs=0.01)
        assert row["status"] == "open"
        assert any("高于峰值" in r.message for r in caplog.records), \
            "the repair must say what it was, or the next reader will assume a bad rule"

    def test_a_sane_stop_is_not_reset_by_that_repair(self, book, caplog):
        """The companion: the repair must not fire on an ordinary row.

        Same entry price, with a stop that is merely trailed rather than
        impossible, and a price that has not moved enough to re-trail — so the
        only thing that could change the stop is the repair. It stays exactly
        where it was, and nothing is logged as corrupted.
        """
        position_id = _seed(book, open_price=16.31, stop_loss=15.5,
                            initial_stop_loss=15.0, peak_return_pct=4.0)

        with caplog.at_level("WARNING", logger="alpha_agents.data.position_monitor"):
            check_positions(realtime_prices={"000510": 16.6}, today="2026-09-16")

        assert _row(book, position_id)["stop_loss"] == pytest.approx(15.5, abs=0.01)
        assert not any("高于峰值" in r.message for r in caplog.records)


class TestAnUnmeasurableEntryStop:
    """``entry_stop`` is now the single derivation of the entry stop, so its
    refusals are part of the contract and are asserted here rather than left
    for each caller to rediscover.

    The distinction under test is between a column that is *absent* and one
    that is NULL. Collapsing them is not a style point: it is the exact shape
    of the bug that hid D29 for a day.
    """

    def test_an_unselected_column_raises_instead_of_reading_as_null(self):
        """A SELECT that forgets the column must not answer "unmeasurable".

        The code this replaces read ``pos["initial_stop_loss"] if
        "initial_stop_loss" in pos.keys() else None``. ``pos.keys()`` lists the
        *query's* columns, not the table's — so when the top-up's SELECT forgot
        ``initial_stop_loss``, the guard answered False, the caller declined,
        and the decline was indistinguishable from a policy decision: the stop
        quietly stopped tracking the average, one test away from the same
        self-reference the column was added to prevent.

        The second half matters just as much: a row that *has* the column and
        holds NULL is an ordinary pre-migration row, not an error. It must fall
        through to the stop it can still attest to.
        """
        with pytest.raises(KeyError):
            entry_stop({"stop_loss": 9.2}, 10.0, None)

        assert entry_stop(
            {"stop_loss": 9.2, "initial_stop_loss": None}, 10.0, None) == 9.2

    def test_no_floor_of_last_resort_returns_zero(self):
        """``None`` is the refusal spelled out, and a number is the floor.

        Same row, both recorded stops unusable: the entry stop was never
        written and the ratcheted one sits above cost, so there is no fraction
        to hold. ``None`` yields 0.0 — which the top-up reads as "leave the stop
        alone" — while a number yields a stop, which is what the trailing rule
        needs because it always has to name one.
        """
        pos = {"stop_loss": 12.0, "initial_stop_loss": None}
        assert entry_stop(pos, 10.0, None) == 0.0
        assert entry_stop(pos, 10.0, 0.08) == pytest.approx(9.2)
