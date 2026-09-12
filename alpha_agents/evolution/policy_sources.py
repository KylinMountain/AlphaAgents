"""What a policy version covers, read from the live configuration.

``data.policy_registry`` owns the record: versions, the pointer, the
compare-and-swap. This module owns the other half — the *declaration* of which
parts of the running system count as behaviour, and how to read them.

The split is a layering fact, not a preference. A version's hash has to cover
the prompt files, the model identity, the retrieval budgets and the decision
rules; those live in ``tools`` and ``evolution``, which the ``data`` layer may
not import. So the collector runs in ``evolution`` and passes a plain dict
down, and the registry treats it as data.

Why reading the *live* configuration matters. A registry that hashed a copy
someone typed would answer "what did we record", which nobody doubts. This one
answers "is what is in force still what is configured" — so a prompt edited
without opening a version turns the version in force into a **failed check**
instead of an unnoticed divergence. That is the entire reason the sources are
collected rather than declared.

What is deliberately *not* a source: the provider fallback chain is included
(it changes which model answers), but running counters are not — an approval
count or a win rate moves on its own, and a hash that moves when a trade
closes cannot answer "which policy produced this decision". Cost constants
(commission, slippage) are excluded too: they change measured *results*, not
the decision the trader makes, and mixing the two would make the fingerprint
report a change where behaviour did not move.
"""

from __future__ import annotations

import hashlib
import importlib
import json

from alpha_agents import config
from alpha_agents.data import knowledge_snapshots, policy_registry
from alpha_agents.evolution import feedback, holdout_gate

# ── The declared sources ───────────────────────────────────────────────

#: Decision-rule constants. Each entry is (module, attribute). Resolved by
#: import at collect time rather than imported at module scope so that
#: reading the policy fingerprint does not drag the whole decision path into
#: memory, and so a renamed constant fails loudly here.
_RULE_SOURCES = (
    ("alpha_agents.evolution.holdout_gate", "MIN_VALIDATION_SAMPLES"),
    ("alpha_agents.evolution.holdout_gate", "BRIER_TOLERANCE"),
    ("alpha_agents.tools.exit_signals", "RSI_PERIOD"),
    ("alpha_agents.tools.exit_signals", "LOOKBACK_BARS"),
    ("alpha_agents.tools.exit_signals", "MAX_HOLDING_DAYS"),
    ("alpha_agents.tools.exit_signals", "REGIME_STRONG_PCT"),
    ("alpha_agents.tools.exit_signals", "REGIME_WEAK_PCT"),
    ("alpha_agents.tools.exit_signals", "RUNUP_THRESHOLD_PCT"),
    ("alpha_agents.tools.sector_beta", "W_BETA"),
    ("alpha_agents.tools.sector_beta", "W_POSITION"),
    ("alpha_agents.tools.sector_beta", "W_INSTITUTIONAL"),
    ("alpha_agents.tools.sector_beta", "W_LIQUIDITY"),
    ("alpha_agents.data.decision_context", "CONTEXT_VERSION"),
)

#: Retrieval policy: how much of each kind of knowledge reaches the model.
#: These decide what the decision-maker can see, so a change to one is a
#: change in behaviour even though no rule text moved.
_RETRIEVAL_SOURCES = (
    ("alpha_agents.evolution.feedback", "_COGNITION_BUDGET"),
    ("alpha_agents.evolution.feedback", "_VPA_SIGNAL_BUDGET"),
    ("alpha_agents.evolution.feedback", "_PRINCIPLES_BUDGET"),
    ("alpha_agents.evolution.feedback", "_LESSONS_BUDGET"),
    ("alpha_agents.evolution.feedback", "_PLAYBOOKS_BUDGET"),
)


def _resolve(module_name: str, attribute: str):
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise policy_registry.PolicyError(
            f"Policy source {module_name}.{attribute} no longer exists. A "
            "renamed rule constant has to be renamed in _RULE_SOURCES or "
            "_RETRIEVAL_SOURCES too — silently dropping it would leave the "
            "fingerprint claiming to cover a rule it no longer reads."
        ) from exc


def _constant_source(pairs) -> dict:
    return {f"{module.split('.')[-1]}.{attribute}": _resolve(module, attribute)
            for module, attribute in pairs}


# ── The individual sources ─────────────────────────────────────────────


def prompt_fingerprints() -> dict:
    """sha256 per prompt file, keyed by name.

    Hashed by content, and keyed by filename, so both an edited prompt and an
    added or removed one move the fingerprint. The prompts are the largest
    single determinant of what the trader does and the easiest thing to edit
    by accident, which is why they are a source at all.
    """
    directory = config.PROMPTS_DIR
    out: dict[str, str] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.md")):
        out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def model_fingerprint() -> dict:
    """Which model answers, as declared."""
    from alpha_agents import model_factory
    return model_factory.model_identity()


def knowledge_fingerprint(snapshot_id: int | None = None) -> dict:
    """The approved knowledge this version carries.

    Defaults to the newest approval on record. Note what that means for
    drift: approving new knowledge moves this source, so the version in force
    starts failing verification the moment an approval lands — which is the
    honest reading. §10 says approval does not activate knowledge, and this is
    the machine form of "it is approved and *not* in force": to put it in
    force you freeze a version that names it and promote that, with evidence.

    ``U5`` is where a specific snapshot becomes the one retrieval reads. Until
    then this is a declaration carried by the version, not an authority.
    """
    if snapshot_id is None:
        snapshots = knowledge_snapshots.all_snapshots()
        snapshot_id = snapshots[-1]["id"] if snapshots else None
    return {"snapshot_id": snapshot_id}


# ── Collecting ─────────────────────────────────────────────────────────


def collect(*, knowledge_snapshot_id: int | None = None) -> dict:
    """Read the live configuration into the declared source tuple.

    The single definition of what a policy version covers: ``freeze`` and
    ``verify`` both go through here, so a source cannot be covered by the
    write and missing from the check.
    """
    return {
        "prompts": prompt_fingerprints(),
        "model": model_fingerprint(),
        "retrieval": _constant_source(_RETRIEVAL_SOURCES),
        "rules": _constant_source(_RULE_SOURCES),
        "knowledge": knowledge_fingerprint(knowledge_snapshot_id),
    }


# ── Convenience over the registry ──────────────────────────────────────


def freeze_live(*, created_by: str, reason: str,
                policy_key: str = policy_registry.POLICY_KEY_DEFAULT,
                parent_id: int | None = None,
                knowledge_snapshot_id: int | None = None,
                frozen_at: str | None = None) -> int:
    """Freeze the configuration as it is right now."""
    return policy_registry.freeze(
        sources=collect(knowledge_snapshot_id=knowledge_snapshot_id),
        created_by=created_by, reason=reason, policy_key=policy_key,
        parent_id=parent_id, frozen_at=frozen_at)


def verify_live(version_id: int) -> bool:
    """Does the configuration still match this version?"""
    return policy_registry.verify_version(version_id, collect())


def drifted(policy_key: str = policy_registry.POLICY_KEY_DEFAULT) -> list[dict]:
    """Frozen versions the live configuration has moved away from.

    Not corruption — a configuration is expected to keep moving. It is the
    answer to "is what is in force still what is configured", which is the one
    question the registry exists to make answerable. Every version is checked,
    not only the one in force: an older version that drifted cannot be rolled
    back to either, and a rollback that silently restored something the code
    no longer does would be worse than refusing.
    """
    live = collect()
    out = []
    for version in policy_registry.versions_for(policy_key):
        if not policy_registry.verify_version(version["id"], live):
            out.append({
                "id": version["id"],
                "policy_key": version["policy_key"],
                "frozen_at": version["frozen_at"],
                "content_hash": version["content_hash"],
                "live_hash": policy_registry.content_hash_for(live),
            })
    return out


def changed_sources(version_id: int) -> dict:
    """Which declared sources differ between a version and the live config.

    ``drifted`` answers yes or no; this answers *what moved*, because a hash
    mismatch that cannot be attributed costs a long hunt through prompt files
    and constants.
    """
    version = policy_registry.get_version(version_id)
    if version is None:
        return {}
    frozen = json.loads(version["sources_json"])
    live = collect()
    return {name: {"frozen": frozen.get(name), "live": live.get(name)}
            for name in policy_registry.SOURCE_NAMES
            if frozen.get(name) != live.get(name)}


def summary() -> dict:
    """The registry's state in one dict, for a report or a CLI."""
    pointer = policy_registry.active()
    version = policy_registry.active_version()
    return {
        "counts": policy_registry.counts(),
        "active": pointer,
        "active_version": version,
        "drifted": drifted(),
        "integrity": policy_registry.integrity(),
    }
