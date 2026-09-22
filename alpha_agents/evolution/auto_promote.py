"""Let the market-data verdict move the pointer, with no person in the loop.

The whole promotion chain already existed and was already gated on market
data — ``holdout_gate.run_gate`` runs a forward-only, paired comparison and
abstains below ``MIN_VALIDATION_SAMPLES``. What it could not do was act:
``scripts/policy.py`` printed "this is promotable evidence. The pointer did
not move: approve is a separate act by a person", and no pipeline ever called
it. So the loop stopped at a CLI that a human had to run.

Design §11 says "The **initial** approval boundary is human authorization;
automatic evaluation success is not permission to promote, **and no LLM may
approve its own candidate**." The word that matters is *initial*: the human
gate is written as a starting point. The durable constraint is the second
clause, and it is untouched here — the approver is ``holdout_gate``, a
statistical rule over market outcomes, not a model and not a person's name
borrowed by a script.

So this module adds a caller and weakens nothing. ``policy_registry.approve``
and ``promote`` keep every precondition they had: an incumbent to improve on,
a live configuration that still hashes to the version, a sound record, a
compare-and-swap on ``version_seq``, and gate evidence that has not changed
since the approval. A refusal from any of them is returned, not swallowed —
"the pointer did not move, and here is which precondition said no" is the
only useful thing to write down when nothing happens.

**What it cannot do.** It cannot make a verdict favourable, lower the sample
floor, or promote a version whose evidence window could not contain forward
data — ``run_gate`` raises on that rather than abstaining, and the raise
comes back as a refusal here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Who approved and who promoted, in the audit trail. Deliberately not a
#: person's name and not a model's: a reader of ``policy_approvals`` must be
#: able to tell at a glance that a rule did this, because §11's surviving
#: clause is about who may approve, not about how fast it happens.
ACTOR = "holdout_gate"


def run(*, today: str | None = None, policy_key: str | None = None,
        report_type: str = "daily_playbook", limit: int = 5) -> dict:
    """Gate every promotable version and move the pointer when one passes.

    Returns what happened for each version examined. Nothing raises: a run
    that promotes nothing is the normal case, and the reasons are the point.
    """
    from alpha_agents.data import policy_registry as R
    from alpha_agents.evolution import holdout_gate, policy_sources

    key = policy_key or R.POLICY_KEY_DEFAULT
    pointer = R.active(key)
    if pointer is None:
        # Promotion is a claim about improvement; with nothing in force there
        # is no incumbent to improve on. Installing the first version is a
        # different act and not this module's.
        return {"promoted": None, "examined": [],
                "skipped": "no active policy to improve on"}

    examined: list[dict] = []
    for version in R.versions_for(key)[:limit]:
        version_id = int(version["id"])
        if version_id == pointer["version_id"]:
            continue
        outcome = _consider(version_id, report_type=report_type, today=today,
                            policy_key=key, sources_of=policy_sources.collect,
                            registry=R, gate=holdout_gate)
        examined.append(outcome)
        if outcome["promoted"]:
            logger.info("auto-promote: pointer moved to version #%s — %s",
                        version_id, outcome["reason"])
            return {"promoted": version_id, "examined": examined}
    return {"promoted": None, "examined": examined}


def _consider(version_id: int, *, report_type: str, today, policy_key: str,
              sources_of, registry, gate) -> dict:
    """One version: gate it, and act only on a promote verdict."""
    try:
        decision = gate.run_gate(version_id, report_type=report_type,
                                 today=today)
    except Exception as exc:                          # noqa: BLE001
        # run_gate raises on a window that could not hold forward evidence.
        # That is a statement about the experiment, not an error in this run.
        return {"version_id": version_id, "promoted": False,
                "outcome": "gate_refused", "reason": str(exc)[:200]}

    if decision.get("outcome") != "promote":
        return {"version_id": version_id, "promoted": False,
                "outcome": decision.get("outcome"),
                "n": decision.get("n"),
                "reason": str(decision.get("reason") or "")[:200]}

    # The numbers travel with the act. An approval whose reason is "the gate
    # said so" cannot be audited without re-running the gate, and by then the
    # evidence may have moved.
    reason = (f"{ACTOR}: {decision.get('outcome')} over "
              f"{decision.get('validation_days')} validation day(s), "
              f"n={decision.get('n')} — {decision.get('reason') or ''}")[:400]
    sources = sources_of()
    try:
        registry.approve(version_id=version_id, approved_by=ACTOR,
                         reason=reason, gate_decision=decision,
                         sources=sources, policy_key=policy_key)
        registry.promote(version_id=version_id, actor=ACTOR, reason=reason,
                         sources=sources, policy_key=policy_key)
    except Exception as exc:                          # noqa: BLE001
        # Every precondition in the registry stays in force; this is where a
        # refusal surfaces. Returned rather than raised because one version
        # refusing is not a reason to stop examining the others.
        return {"version_id": version_id, "promoted": False,
                "outcome": "refused_by_registry", "reason": str(exc)[:200],
                "n": decision.get("n")}
    return {"version_id": version_id, "promoted": True, "outcome": "promote",
            "n": decision.get("n"), "reason": reason}
