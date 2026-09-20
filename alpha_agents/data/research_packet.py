"""Versioned evidence passed from research stages to the trade planner.

A research packet is immutable decision input, not a recommendation. It
preserves what upstream stages actually said/read, the world they said it in,
and a content hash. Downstream code may refuse a packet; it may not silently
reconstruct missing evidence from newer data.
"""

from __future__ import annotations

import hashlib
import json

from alpha_agents.data import sector_membership


VERSION = 1


class ResearchPacketError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def content_hash(payload: dict) -> str:
    body = {key: value for key, value in payload.items()
            if key != "packet_hash"}
    return hashlib.sha256(_dump(body).encode("utf-8")).hexdigest()


def seal(payload: dict) -> dict:
    body = {key: value for key, value in payload.items()
            if key != "packet_hash"}
    return {**body, "packet_hash": content_hash(body)}


def _eligible_themes(row: dict) -> list[str]:
    explicit = [
        str(value).strip()
        for value in (row.get("eligible_themes") or [])
        if str(value).strip()
    ]
    if explicit:
        return list(dict.fromkeys(explicit))
    values = [row.get("primary_theme"), *(row.get("supporting_themes") or [])]
    return list(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()))


def _relation_map(row: dict) -> dict[str, str]:
    explicit = row.get("theme_relation_evidence_ids") or {}
    if explicit:
        return {
            str(key): str(value)
            for key, value in explicit.items()
            if str(key).strip() and str(value).strip()
        }

    out = {}
    primary = str(row.get("primary_theme") or "").strip()
    primary_ref = str(
        row.get("primary_theme_relation_evidence_id") or "").strip()
    if primary and primary_ref:
        out[primary] = primary_ref
    supporting = list(row.get("supporting_themes") or [])
    refs = list(row.get("supporting_theme_relation_evidence_ids") or [])
    for theme, ref in zip(supporting, refs):
        if str(theme).strip() and str(ref).strip():
            out[str(theme)] = str(ref)
    return out


def validate_relation_row(archive, row: dict) -> str:
    """Re-prove one stock/theme relation against the frozen PIT archive.

    Every caller (morning, T1, replay) uses this function instead of copying
    membership/hash/evidence checks. It returns the primary theme on success
    and raises :class:`ResearchPacketError` on any missing or mismatched
    provenance.
    """
    code = str(row.get("code") or "").strip()
    snapshot_id = str(row.get("membership_snapshot_id") or "").strip()
    membership_hash = str(row.get("membership_hash") or "").strip()
    primary = str(row.get("primary_theme") or "").strip()
    if not code:
        raise ResearchPacketError("relation row is missing code")
    if not snapshot_id:
        raise ResearchPacketError(f"{code} is missing membership_snapshot_id")
    if not membership_hash:
        raise ResearchPacketError(f"{code} is missing membership_hash")
    if not primary:
        raise ResearchPacketError(f"{code} is missing primary_theme")

    try:
        snapshot = sector_membership.by_id(
            archive, snapshot_id, expected_hash=membership_hash)
        expected_primary = sector_membership.relation_evidence_id(
            snapshot, sector_id=primary, code=code)
    except Exception as exc:
        raise ResearchPacketError(str(exc)) from exc

    if str(row.get("primary_theme_relation_evidence_id") or "") != expected_primary:
        raise ResearchPacketError(
            f"{code} primary relation evidence mismatch")

    supporting = [
        str(value).strip()
        for value in (row.get("supporting_themes") or [])
        if str(value).strip()
    ]
    refs = list(row.get("supporting_theme_relation_evidence_ids") or [])
    if len(refs) != len(supporting):
        raise ResearchPacketError(
            f"{code} supporting relation evidence count mismatch")
    expected = [
        sector_membership.relation_evidence_id(
            snapshot, sector_id=theme, code=code)
        for theme in supporting
    ]
    if refs != expected:
        raise ResearchPacketError(
            f"{code} supporting relation evidence mismatch")
    return primary


def apply_stock_choices(panel: list[dict], choices: list[dict]) -> list[dict]:
    """Apply the stock selector's economic thesis to frozen panel rows."""
    by_code = {str(row.get("code") or ""): row for row in panel}
    out = []
    for choice in choices:
        code = str(choice.get("code") or "").strip()
        row = by_code.get(code)
        if row is None:
            raise ResearchPacketError(f"stock choice {code!r} is outside panel")
        eligible = _eligible_themes(row)
        primary = str(choice.get("primary_theme") or "").strip()
        if not primary:
            raise ResearchPacketError(
                f"stock choice {code} is missing primary_theme")
        if primary not in eligible:
            raise ResearchPacketError(
                f"stock choice {code} primary_theme {primary!r} is not in "
                f"eligible themes {eligible}")
        relations = _relation_map(row)
        missing = [theme for theme in eligible if theme not in relations]
        if missing:
            raise ResearchPacketError(
                f"stock choice {code} has no relation evidence for {missing}")

        materialized = dict(row)
        materialized.update({
            "primary_theme": primary,
            "supporting_themes": [
                theme for theme in eligible if theme != primary],
            "primary_theme_relation_evidence_id": relations[primary],
            "supporting_theme_relation_evidence_ids": [
                relations[theme] for theme in eligible if theme != primary],
            "selection_reason": str(choice.get("reason") or "").strip(),
            "selection_counterevidence": str(
                choice.get("counterevidence") or "").strip(),
        })
        out.append(materialized)
    return out


def _trace_for_code(trace: list[dict], code: str) -> list[dict]:
    return [
        item for item in trace
        if item.get("code") in {None, "", code}
    ]


def build(*, day: str, cutoff: str, architecture: str, policy_ref: str | None,
          membership_snapshot_id: str, membership_hash: str,
          directions: list[dict], selected_panel: list[dict],
          stock_choices: list[dict], research_trace: list[dict],
          budget: dict | None, event_snapshot_refs: list[dict]) -> dict:
    by_direction = {
        str(row.get("sector_id") or ""): dict(row)
        for row in directions if str(row.get("sector_id") or "").strip()
    }
    by_choice = {
        str(row.get("code") or ""): dict(row)
        for row in stock_choices if str(row.get("code") or "").strip()
    }

    stocks = []
    for row in selected_panel:
        code = str(row.get("code") or "").strip()
        primary = str(row.get("primary_theme") or "").strip()
        choice = by_choice.get(code) or {}
        stocks.append({
            "code": code,
            "primary_theme": primary,
            "supporting_themes": list(row.get("supporting_themes") or []),
            "primary_theme_relation_evidence_id": row.get(
                "primary_theme_relation_evidence_id"),
            "supporting_theme_relation_evidence_ids": list(
                row.get("supporting_theme_relation_evidence_ids") or []),
            "selector_reason": str(choice.get("reason") or "").strip(),
            "counterevidence": str(
                choice.get("counterevidence") or "").strip(),
            "direction_research": by_direction.get(primary),
            "panel_facts": {
                key: row.get(key)
                for key in (
                    "name", "close", "change_pct", "adv20", "turnover_rate",
                    "primary_theme_peer_covered", "primary_theme_peer_total",
                    "primary_theme_peer_5d_median_pct",
                    "primary_theme_peer_relative_5d_pct",
                    "consecutive_limits", "limit_sector", "net_amount",
                )
            },
            "tool_facts": _trace_for_code(research_trace, code),
        })

    payload = {
        "version": VERSION,
        "decision": {
            "day": str(day)[:10],
            "cutoff": str(cutoff),
            "architecture": str(architecture),
            "policy_ref": policy_ref,
        },
        "world": {
            "membership_snapshot_id": str(membership_snapshot_id),
            "membership_hash": str(membership_hash),
        },
        "directions": list(directions),
        "stocks": stocks,
        "event_snapshot_refs": list(event_snapshot_refs),
        "research_budget": budget,
        "stage_status": {
            "direction": "complete",
            "stock": "complete",
            "planner": "pending",
        },
    }
    return seal(payload)


def require_valid(packet: dict, *, cutoff: str | None = None,
                  membership_snapshot_id: str | None = None,
                  membership_hash: str | None = None) -> str:
    if not isinstance(packet, dict):
        raise ResearchPacketError("research packet must be an object")
    if packet.get("version") != VERSION:
        raise ResearchPacketError(
            f"unsupported research packet version {packet.get('version')!r}")
    stored = str(packet.get("packet_hash") or "")
    computed = content_hash(packet)
    if not stored or stored != computed:
        raise ResearchPacketError("research packet hash mismatch")

    decision = packet.get("decision") or {}
    world = packet.get("world") or {}
    if not str(world.get("membership_snapshot_id") or "").strip():
        raise ResearchPacketError("research packet is missing membership snapshot")
    if not str(world.get("membership_hash") or "").strip():
        raise ResearchPacketError("research packet is missing membership hash")
    if cutoff is not None and decision.get("cutoff") != cutoff:
        raise ResearchPacketError(
            f"research packet cutoff {decision.get('cutoff')!r} != {cutoff!r}")
    if (membership_snapshot_id is not None
            and world.get("membership_snapshot_id") != membership_snapshot_id):
        raise ResearchPacketError("research packet membership snapshot mismatch")
    if (membership_hash is not None
            and world.get("membership_hash") != membership_hash):
        raise ResearchPacketError("research packet membership hash mismatch")

    direction_ids = {
        str(row.get("sector_id") or "").strip()
        for row in (packet.get("directions") or [])
        if str(row.get("sector_id") or "").strip()
    }
    for stock in packet.get("stocks") or []:
        if not str(stock.get("code") or "").strip():
            raise ResearchPacketError("research packet stock is missing code")
        if not str(stock.get("primary_theme") or "").strip():
            raise ResearchPacketError(
                f"research packet stock {stock.get('code')} has no primary theme")
        if not str(
                stock.get("primary_theme_relation_evidence_id") or "").strip():
            raise ResearchPacketError(
                f"research packet stock {stock.get('code')} has no "
                "primary relation evidence")
        if stock.get("primary_theme") not in direction_ids:
            raise ResearchPacketError(
                f"research packet stock {stock.get('code')} primary theme "
                "was not selected by the direction stage")
    return stored


def deterministic_refusals(packet: dict) -> list[dict]:
    """Return hard tool facts that explicitly forbid planning a stock."""
    require_valid(packet)
    out = []
    for stock in packet.get("stocks") or []:
        code = str(stock.get("code") or "")
        for fact in stock.get("tool_facts") or []:
            payload = fact.get("payload")
            if not isinstance(payload, dict) or payload.get("must_not_buy") is not True:
                continue
            out.append({
                "code": code,
                "why": "research_invalidation",
                "stage": "research_packet_validation",
                "detail": str(
                    payload.get("reason")
                    or f"{fact.get('tool')} returned must_not_buy=true"
                )[:500],
                "evidence_hash": fact.get("result_hash"),
            })
            break
    return out


def render(packet: dict) -> str:
    require_valid(packet)
    return json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2)
