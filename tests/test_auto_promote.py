"""The market-data verdict may move the pointer; nothing else changes.

The promotion chain was already gated on market data — holdout_gate runs a
forward-only paired comparison and abstains below MIN_VALIDATION_SAMPLES.
What it could not do was act: scripts/policy.py printed "this is promotable
evidence. The pointer did not move: approve is a separate act by a person",
and no pipeline called it.

Design §11 writes the human gate as the *initial* boundary and keeps one
durable clause — "no LLM may approve its own candidate". These tests hold
that clause (the approver is a statistical rule, named as such), and hold
that every precondition in policy_registry still refuses what it refused
before.
"""

from types import SimpleNamespace

from alpha_agents.evolution import auto_promote


class _Registry:
    POLICY_KEY_DEFAULT = "trader"

    def __init__(self, versions, pointer_id=1, refuse=None):
        self._versions = versions
        self._pointer = ({"version_id": pointer_id, "version_seq": 3}
                         if pointer_id is not None else None)
        self._refuse = refuse
        self.approved = []
        self.promoted = []

    def active(self, key):
        return self._pointer

    def versions_for(self, key):
        return self._versions

    def approve(self, **kw):
        if self._refuse == "approve":
            raise RuntimeError("live configuration no longer hashes to it")
        self.approved.append(kw)
        return 1

    def promote(self, **kw):
        if self._refuse == "promote":
            raise RuntimeError("the pointer moved since this was prepared")
        self.promoted.append(kw)
        return 1


def _gate(outcome, **extra):
    payload = {"outcome": outcome, "n": 24, "validation_days": 12,
               "reason": "challenger did not degrade", **extra}
    return SimpleNamespace(run_gate=lambda *a, **k: payload)


def _run(registry, gate, monkeypatch):
    monkeypatch.setattr(
        "alpha_agents.data.policy_registry", registry, raising=False)
    monkeypatch.setattr(
        "alpha_agents.evolution.holdout_gate", gate, raising=False)
    monkeypatch.setattr(
        "alpha_agents.evolution.policy_sources",
        SimpleNamespace(collect=lambda **k: {"src": 1}), raising=False)
    return auto_promote.run()


def test_a_promote_verdict_moves_the_pointer(monkeypatch):
    reg = _Registry([{"id": 2}], pointer_id=1)
    out = _run(reg, _gate("promote"), monkeypatch)
    assert out["promoted"] == 2
    assert len(reg.approved) == 1 and len(reg.promoted) == 1


def test_the_approver_is_a_rule_and_says_so(monkeypatch):
    """§11's surviving clause: no LLM may approve its own candidate.

    A reader of policy_approvals must be able to tell a rule did this, so the
    name is neither a person's nor a model's.
    """
    reg = _Registry([{"id": 2}], pointer_id=1)
    _run(reg, _gate("promote"), monkeypatch)
    assert reg.approved[0]["approved_by"] == "holdout_gate"
    assert reg.promoted[0]["actor"] == "holdout_gate"


def test_the_numbers_travel_with_the_act(monkeypatch):
    """An approval reading "the gate said so" cannot be audited later."""
    reg = _Registry([{"id": 2}], pointer_id=1)
    _run(reg, _gate("promote"), monkeypatch)
    reason = reg.approved[0]["reason"]
    assert "n=24" in reason and "12 validation day(s)" in reason


def test_an_abstention_moves_nothing(monkeypatch):
    reg = _Registry([{"id": 2}], pointer_id=1)
    out = _run(reg, _gate("insufficient", n=4), monkeypatch)
    assert out["promoted"] is None
    assert reg.approved == [] and reg.promoted == []
    assert out["examined"][0]["outcome"] == "insufficient"


def test_a_gate_that_raises_is_a_refusal_not_a_crash(monkeypatch):
    """run_gate raises on a window that could not hold forward evidence."""
    def boom(*a, **k):
        raise ValueError("validation window cannot contain forward evidence")
    reg = _Registry([{"id": 2}], pointer_id=1)
    out = _run(reg, SimpleNamespace(run_gate=boom), monkeypatch)
    assert out["promoted"] is None
    assert out["examined"][0]["outcome"] == "gate_refused"
    assert "forward evidence" in out["examined"][0]["reason"]


def test_registry_preconditions_still_refuse(monkeypatch):
    """Adding a caller must not weaken what promote() already checked."""
    reg = _Registry([{"id": 2}], pointer_id=1, refuse="promote")
    out = _run(reg, _gate("promote"), monkeypatch)
    assert out["promoted"] is None
    assert out["examined"][0]["outcome"] == "refused_by_registry"
    assert "pointer moved" in out["examined"][0]["reason"]


def test_nothing_in_force_means_nothing_to_improve_on(monkeypatch):
    reg = _Registry([{"id": 2}], pointer_id=None)
    out = _run(reg, _gate("promote"), monkeypatch)
    assert out["promoted"] is None
    assert "no active policy" in out["skipped"]


def test_the_version_already_in_force_is_skipped(monkeypatch):
    reg = _Registry([{"id": 1}], pointer_id=1)
    out = _run(reg, _gate("promote"), monkeypatch)
    assert out["promoted"] is None and out["examined"] == []
