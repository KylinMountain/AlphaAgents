"""The cash side separates 可用 from 可取, and buying power is the 可用 one.

A-share settlement, and the rule this repository follows since 2026-09-16:

* Selling on T makes the proceeds **usable immediately** — spendable on a
  further purchase the same day, repeatedly, which is what makes 换仓 possible.
* They become **withdrawable** on T+1 (bank transfer only).

Sources checked 2026-09-16: 中国证监会河南证监局 investor-protection case,
<https://www.csrc.gov.cn/henan/c104267/c2053710/content.shtml> — *"投资者卖出
股票成交后T+1日方可取出资金"*, where the dispute was exactly a client reading
**可取** as **可用**; and broker reference material summarising the 沪深 rules as
*"T日卖出资金T日可用，T+1日可提现"*.

**The defect these tests used to pin.** Until 2026-09-16
``portfolio.get_available_capital`` subtracted
``settlement.unreleased_pending_total``, so sale proceeds were withheld from
buying power for a day — the withdrawal rule applied to spending. On a paper
trader that never transfers cash out, that forbade selling one name and buying
another the same day and shifted turnover, exposure, the cash curve and
drawdown. Tech-debt D19 records the citations; this module is the contract that
replaced it.
"""

import pytest

from alpha_agents.data import memory_store, portfolio, reservations, settlement

EXIT_DAY = "2026-09-16"      # a Wednesday
PROCEEDS = 123_456.78


def _record(exit_id: int, code: str = "600000") -> None:
    settlement.record_pending(
        memory_store._get_conn(), exit_id=exit_id,
        trader_id=portfolio.DEFAULT_TRADER, code=code,
        net_amount=PROCEEDS, exit_date=EXIT_DAY)


class TestSaleProceedsAreSpendableTheSameDay:
    def test_recording_the_sale_does_not_reduce_available_capital(self):
        """The corrected contract, stated as a difference.

        Only ``record_pending`` runs here — no reservation, no position — so
        buying power must not move at all. Under the old rule it dropped by
        exactly the proceeds, which is the defect this replaced.
        """
        trader = portfolio.DEFAULT_TRADER
        before = portfolio.get_available_capital(trader)

        _record(9_101)

        assert portfolio.get_available_capital(trader) == pytest.approx(before), (
            "sale proceeds must be spendable on the day of the sale")

    def test_the_cash_is_still_visible_as_not_yet_withdrawable(self):
        """The other half of the split: spendable does not mean withdrawable.

        Without this assertion the test above would also pass if the pending
        row had simply not been written, so the fixture is proven first.
        """
        trader = portfolio.DEFAULT_TRADER
        _record(9_102, code="600001")

        held = settlement.unreleased_pending_total(memory_store._get_conn(),
                                                  trader)
        assert held >= PROCEEDS, "the sale must have produced an in-transit row"

    def test_nothing_is_withdrawable_on_the_exit_day_itself(self):
        _record(9_103, code="600002")
        released = settlement.release_due_settlements(memory_store._get_conn(),
                                                     EXIT_DAY)
        assert released == 0

    def test_the_settle_date_is_at_least_the_next_day(self):
        """Where the withdrawal rule legitimately enters: the release date."""
        assert settlement.next_settle_date(EXIT_DAY) > EXIT_DAY


class TestTheDocumentedIdentityHolds:
    def test_total_is_available_plus_invested_plus_reservations(self):
        """``get_total_capital``'s docstring promises exactly this.

        Cash in transit is deliberately absent from the identity now — it is
        already part of ``available`` — so a future change that re-subtracts it
        breaks this rather than quietly rewriting the decomposition.
        """
        conn = memory_store._get_conn()
        trader = portfolio.DEFAULT_TRADER
        _record(9_104, code="600003")

        total = portfolio.get_total_capital(trader)
        rebuilt = (portfolio.get_available_capital(trader)
                   + portfolio.get_invested_capital(trader)
                   + reservations.unconsumed_total(conn, trader))
        assert rebuilt == pytest.approx(total)
