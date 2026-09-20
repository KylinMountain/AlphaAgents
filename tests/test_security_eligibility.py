"""RP-03B: every candidate entrypoint shares one exclusion vocabulary."""

import pytest

from alpha_agents.data import security_eligibility as E


@pytest.mark.parametrize(
    "facts, expected",
    [
        (E.SecurityFacts("688001"), E.BOARD),
        (E.SecurityFacts("600001", listed=False), E.NOT_LISTED),
        (E.SecurityFacts("600001", known=False), E.UNKNOWN_INSTRUMENT),
        (E.SecurityFacts("600001", is_st=True), E.ST),
        (E.SecurityFacts("600001", is_suspended=True), E.SUSPENDED),
        (E.SecurityFacts("600001", has_prior_bar=False), E.NO_PRIOR_BAR),
        (E.SecurityFacts("600001"), None),
    ],
)
def test_shared_security_eligibility_reason(facts, expected):
    assert E.reason(facts) == expected
    assert E.eligible(facts) is (expected is None)


def test_reason_order_is_stable_and_fail_closed():
    assert E.reason(E.SecurityFacts(
        "688001", listed=False, known=False, is_st=True,
        is_suspended=True, has_prior_bar=False,
    )) == E.BOARD
    assert E.reason(E.SecurityFacts(
        "600001", listed=False, known=False, is_st=True,
        is_suspended=True, has_prior_bar=False,
    )) == E.NOT_LISTED
