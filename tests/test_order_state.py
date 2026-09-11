"""The order state graph: reachable, terminal where it says, and closed.

A state machine is only worth having if it cannot be talked around, so
these tests pin the graph itself rather than one caller's use of it: every
declared state is reachable, terminal states really are terminal, and the
two transitions that look like mistakes are legal on purpose.
"""

from __future__ import annotations

import pytest

from alpha_agents.data import order_state as S


class TestTheGraphIsWellFormed:
    def test_every_declared_state_is_classified(self):
        """A state that is neither terminal nor live is a state nobody owns."""
        assert S.TERMINAL | S.LIVE == S.ALL
        assert not (S.TERMINAL & S.LIVE)

    def test_every_state_can_be_reached_from_a_pending_order(self):
        """No dead states.

        This is the reason ``partially_filled`` is not in the set: with no
        simulated matching there is no path that could ever enter it, and a
        status that cannot be reached is dead code wearing the label of
        completeness.
        """
        seen, frontier = {S.PENDING}, [S.PENDING]
        while frontier:
            for nxt in S.legal_from(frontier.pop()):
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert seen == S.ALL, f"unreachable: {sorted(S.ALL - seen)}"

    def test_terminal_states_lead_nowhere(self):
        for state in S.TERMINAL:
            assert S.legal_from(state) == frozenset()
            assert S.is_terminal(state)
            assert not S.is_live(state)

    def test_only_open_is_a_position(self):
        assert S.LIVE == {S.PENDING, S.CANCEL_PENDING, S.OPEN}
        assert not S.is_terminal(S.OPEN)


class TestTheSurprisingTransitionsAreDeliberate:
    def test_a_partial_exit_leaves_the_position_open(self):
        """Selling a third of a position changes the book; it does not close it."""
        assert S.can_transition(S.OPEN, S.OPEN)

    def test_a_fill_may_win_a_race_against_a_cancellation(self):
        """The design preserves a valid racing fill instead of dropping it."""
        assert S.can_transition(S.CANCEL_PENDING, S.OPEN)

    def test_a_requested_cancellation_can_complete(self):
        assert S.can_transition(S.PENDING, S.CANCEL_PENDING)
        assert S.can_transition(S.CANCEL_PENDING, S.CANCELLED)


class TestIllegalMovesAreRefused:
    @pytest.mark.parametrize("current,target", [
        (S.CANCELLED, S.OPEN),
        (S.STOPPED, S.OPEN),
        (S.TARGET_HIT, S.OPEN),
        (S.EXPIRED, S.OPEN),
        (S.REJECTED, S.OPEN),
        (S.OPEN, S.PENDING),
        (S.OPEN, S.REJECTED),
        (S.CANCEL_PENDING, S.PENDING),
    ])
    def test_refused(self, current, target):
        assert not S.can_transition(current, target)
        with pytest.raises(S.IllegalTransition):
            S.assert_transition(current, target)

    def test_a_terminal_row_says_why_it_cannot_move(self):
        with pytest.raises(S.IllegalTransition, match="terminal"):
            S.assert_transition(S.STOPPED, S.OPEN)

    def test_an_illegal_move_names_what_would_have_been_legal(self):
        """An error that does not say the remedy costs a debugging session."""
        with pytest.raises(S.IllegalTransition) as excinfo:
            S.assert_transition(S.PENDING, S.STOPPED)
        message = str(excinfo.value)
        assert S.OPEN in message and S.REJECTED in message

    def test_an_unknown_current_state_is_not_silently_allowed(self):
        with pytest.raises(S.IllegalTransition, match="Unknown current status"):
            S.assert_transition("half_open", S.OPEN)


class TestAssertReturnsItsTarget:
    def test_so_the_check_sits_on_the_path_that_uses_the_value(self):
        assert S.assert_transition(S.PENDING, S.OPEN) == S.OPEN

    def test_validate_rejects_a_status_no_query_would_ever_match(self):
        assert S.validate(S.OPEN) == S.OPEN
        with pytest.raises(ValueError, match="Known states"):
            S.validate("open ")
