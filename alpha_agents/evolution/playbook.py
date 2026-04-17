"""L3 Playbook — match candidates, adjust score by weight, track stats."""

from __future__ import annotations

import json
import logging

from alpha_agents.data.memory_store import get_active_playbooks

logger = logging.getLogger(__name__)

_MIN_ACTIVE_FOR_MATCHING = 2  # regime-change fallback threshold


def _check_condition(field_value, op: str, target) -> bool:
    """Evaluate one condition. Return False on any type mismatch — never raise."""
    try:
        if op == "==":
            return field_value == target
        if op == "in":
            return field_value in target
        if op == "contains":
            return isinstance(field_value, str) and str(target) in field_value
        if op == ">=":
            return float(field_value) >= float(target)
        if op == "<=":
            return float(field_value) <= float(target)
        if op == ">":
            return float(field_value) > float(target)
        if op == "<":
            return float(field_value) < float(target)
    except (TypeError, ValueError):
        return False
    logger.debug("Unknown playbook op: %s", op)
    return False


def _matches_all(candidate: dict, conditions: list[dict]) -> bool:
    """AND semantics across all conditions."""
    for cond in conditions:
        field = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")
        if field is None or op is None:
            return False
        actual = candidate.get(field)
        if not _check_condition(actual, op, value):
            return False
    return True


def match_playbook(candidate: dict) -> dict | None:
    """Return the first matching playbook (by weight DESC) or None.

    Regime-change fallback: if fewer than 2 active playbooks exist, skip
    matching entirely (return None). Prevents ranking on a broken model
    during market regime transitions.
    """
    playbooks = get_active_playbooks()
    if len(playbooks) < _MIN_ACTIVE_FOR_MATCHING:
        return None
    for pb in playbooks:
        try:
            pattern = json.loads(pb.get("pattern_json", "{}"))
        except json.JSONDecodeError:
            continue
        conditions = pattern.get("conditions", []) or []
        if not conditions:
            continue
        if _matches_all(candidate, conditions):
            return pb
    return None
