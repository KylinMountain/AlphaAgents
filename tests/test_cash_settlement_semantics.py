"""The cash-side T+1 rule this repository implements is not the A-share rule.

A-share settlement separates **可用** (usable) from **可取** (withdrawable):

* Selling on T makes the proceeds **usable immediately** — they can be spent on
  a further purchase the same day, as many times as you like.
* They become **withdrawable** on T+1 (bank transfer only).

Sources checked 2026-09-16: 中国证监会河南证监局 investor-protection case,
<https://www.csrc.gov.cn/henan/c104267/c2053710/content.shtml> — *"投资者卖出
股票成交后T+1日方可取出资金"*, where the dispute was precisely that a client
read 可取 as 可用; and broker reference material summarising the 沪深 rules as
*"T日卖出资金T日可用，T+1日可提现"*.

What this repository does instead is apply the **withdrawal** rule to
**buying power**. ``portfolio.get_available_capital`` subtracts
``settlement.unreleased_pending_total``, and ``settlement.record_pending``
stamps the row with ``next_settle_date(exit_date)`` — so the proceeds of a sale
are withheld from available capital for a day. On a paper trader that never
transfers cash out, that understates buying power on **every day with a sale**:
it forbids selling one name and buying another the same day, and so distorts
turnover, exposure, the cash curve and drawdown. Any walk-forward replay built
on this kernel would reproduce the wrong rule faithfully, which is why it is
worth fixing before, not after, a long replay.

**These tests assert the defect, not the contract** — named so that reading
them cannot look like approval. When the split is corrected (usable on T,
withdrawable on T+1) they should be replaced by tests asserting the corrected
behaviour, per tech-debt D19.
"""

import pytest

from alpha_agents.data import memory_store, portfolio, settlement

EXIT_DAY = "2026-09-16"      # a Wednesday
PROCEEDS = 123_456.78


def test_sale_proceeds_are_withheld_from_available_capital():
    """The defect, demonstrated rather than described.

    A sale settles cash that the trader may not spend until tomorrow. Under the
    real rule that money is spendable today — so ``after`` should equal
    ``before`` and does not.
    """
    conn = memory_store._get_conn()
    trader = portfolio.DEFAULT_TRADER
    before = portfolio.get_available_capital(trader)

    settlement.record_pending(conn, exit_id=9_001, trader_id=trader,
                              code="600000", net_amount=PROCEEDS,
                              exit_date=EXIT_DAY)

    after = portfolio.get_available_capital(trader)
    assert after == pytest.approx(before - PROCEEDS), (
        "if this now passes with `after == before`, the cash-side rule was "
        "corrected and this test should be replaced (D19)")


def test_the_pending_row_still_holds_the_cash_on_the_exit_day():
    """The mechanism behind the defect: the row is unreleased until T+1."""
    conn = memory_store._get_conn()
    trader = portfolio.DEFAULT_TRADER
    settlement.record_pending(conn, exit_id=9_002, trader_id=trader,
                              code="600001", net_amount=PROCEEDS,
                              exit_date=EXIT_DAY)

    held = settlement.unreleased_pending_total(conn, trader)
    assert held >= PROCEEDS

    released = settlement.release_due_settlements(conn, EXIT_DAY)
    assert released == 0, (
        "nothing should release on the exit day itself — which is the defect: "
        "the cash is usable, not withdrawable, on this day")
    assert settlement.unreleased_pending_total(conn, trader) >= PROCEEDS


def test_the_settle_date_is_at_least_the_next_day():
    """``next_settle_date`` is where the withdrawal rule enters the cash path."""
    assert settlement.next_settle_date(EXIT_DAY) > EXIT_DAY
