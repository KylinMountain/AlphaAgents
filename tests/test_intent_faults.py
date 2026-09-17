"""D32: a system fault must not be readable as a policy verdict.

``intents.reject_reason`` carries two populations that look alike in the
column and mean opposite things:

* the business rules **declined** a well-formed intent — prose, and a normal
  outcome;
* the write path **raised** — an outage, and nothing was decided at all.

D26 is the worked example. 68 closes failed on one missing column, and the
改账审计 panel printed all 68 under 拒绝原因 as ordinary 已拒绝 rows. A
rejection is a *normal* verdict, so nothing downstream asked why all 68 said
the same thing — which is the whole reason the outage stayed invisible for
as long as it did.

These tests pin both directions. A fix that relabelled every rejection a
fault would satisfy the first test alone.
"""

from __future__ import annotations

import pytest

from alpha_agents.data import intent as I
from alpha_agents.data import memory_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A memory book of this test's own.

    Same shape as ``test_intent.py``'s fixture: the module holds one
    connection per thread, so pointing the path at ``tmp_path`` and clearing
    the cached handle is what keeps this from writing into the live book.
    """
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


class TestTheWriterMarksAFaultAsAFault:
    def test_a_raised_dispatch_is_prefixed(self, store, monkeypatch):
        """The marker is written by the writer, which is the only place that
        knows which of the two populations this row belongs to.

        The intent has to be well-formed, or the business rules refuse it
        before dispatch and the test would prove nothing about a raised
        write.
        """
        def _boom(intent):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(I, "_dispatch", _boom)
        with pytest.raises(RuntimeError):
            I.submit_intent(I.TradeIntent(
                action=I.CANCEL, trader_id="slow",
                position_id=999_999, reason="x"))

        row = I.history(limit=1)[0]
        assert row["status"] == I.REJECTED
        assert row["reject_reason"].startswith(I.FAULT_PREFIX), (
            "an outage was stored without the marker, so it is now "
            "indistinguishable from a policy refusal")

    def test_a_policy_refusal_is_not_prefixed(self, store):
        """The other half. A malformed intent is refused by the rules, and
        that must stay a plain refusal."""
        r = I.submit_intent(I.TradeIntent(
            action=I.OPEN, trader_id="slow", code="600460"))
        assert r.accepted is False
        assert not (r.reject_reason or "").startswith(I.FAULT_PREFIX)


class TestTheReaderSeparatesThem:
    def test_the_marker_is_recognised(self):
        assert I.is_fault(f"{I.FAULT_PREFIX}OperationalError: no such column")
        assert I.is_fault("OperationalError: no such column: command_id")
        assert I.is_fault("sqlite3.IntegrityError: UNIQUE constraint failed")

    def test_a_verdict_is_not_a_fault(self):
        for reason in (
            "refused by the order path (duplicate, theme too weak, "
            "no capital, or a rejected link)",
            "资金不足",
            "价格已涨走(7.51)",
            "挂单到期未到价（挂5天，期限5天）",
            "",
            None,
        ):
            assert not I.is_fault(reason), f"{reason!r} was read as a fault"

    def test_the_legacy_shape_is_classified_without_the_marker(self):
        """D26's 68 rows predate the marker and can never carry it. They are
        still the reason this distinction exists, so they must classify —
        by the exact shape the writer used at the time."""
        assert I.is_fault("OperationalError: no such column: command_id")

    def test_a_colon_in_a_policy_reason_is_not_enough(self):
        """The string-sniffing rule the debt entry warned about: it is
        correct only while policy prose does not look like an exception.
        Scoping the legacy rule to real exception-class names is what keeps
        this from being a rule that is quietly wrong."""
        assert not I.is_fault("refused: theme too weak")
        assert not I.is_fault("到期未到价: 5天")
