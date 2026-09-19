"""Point-in-time sector snapshots for sector-first opportunity selection.

This module is policy-light. It computes facts that a later selector may use,
but it does not emit "buy this sector" or a synthetic alpha score.

The caller must provide an explicit membership snapshot. Current-only concept
membership therefore cannot enter strict historical replay by accident.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import statistics


FORMULA_VERSION = "sector_snapshot_v0"


class SectorSnapshotError(ValueError):
    """A sector world cannot be reconstructed honestly."""


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 6)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


@dataclass(frozen=True)
class MembershipSnapshot:
    snapshot_id: str
    available_at: str
    source: str
    sector_type: str
    members: dict[str, tuple[str, ...]]
    point_in_time: bool = False

    @property
    def content_hash(self) -> str:
        return _hash({
            "snapshot_id": self.snapshot_id,
            "available_at": self.available_at,
            "source": self.source,
            "sector_type": self.sector_type,
            "members": {
                key: list(value) for key, value in sorted(self.members.items())
            },
            "point_in_time": self.point_in_time,
        })

    def validate(self, decision_at: str, *, strict_pit: bool = True) -> None:
        if self.available_at > decision_at:
            raise SectorSnapshotError(
                "membership snapshot was not available at the decision cutoff")
        if strict_pit and not self.point_in_time:
            raise SectorSnapshotError(
                "strict sector replay requires point-in-time membership")


@dataclass(frozen=True)
class SectorSnapshot:
    sector_id: str
    sector_type: str
    as_of_session: str
    decision_at: str
    membership_snapshot_id: str
    membership_hash: str
    formula_version: str
    member_count: int
    coverage: dict
    returns_pct: dict
    market_returns_pct: dict
    relative_returns_pct: dict
    advancers_pct: float | None
    outperform_market_pct: float | None
    breadth_improvement_5d_pp: float | None
    ex_top1_5d_median_pct: float | None
    ex_top3_5d_median_pct: float | None
    fund_flow: dict
    input_hash: str

    def compact(self) -> dict:
        return asdict(self)


def _session_index(sessions: list[str], as_of_session: str) -> int:
    try:
        return sessions.index(as_of_session)
    except ValueError as exc:
        raise SectorSnapshotError(
            f"as_of_session {as_of_session!r} is absent from the calendar"
        ) from exc


def _window_return(bars_by_day: dict[str, dict[str, dict]], sessions: list[str],
                   index: int, code: str, horizon: int) -> float | None:
    if index - horizon < 0:
        return None
    now = bars_by_day.get(sessions[index], {}).get(code) or {}
    before = bars_by_day.get(sessions[index - horizon], {}).get(code) or {}
    close = now.get("close")
    prior = before.get("close")
    if close is None or prior in {None, 0}:
        return None
    return (float(close) / float(prior) - 1.0) * 100.0


def _day_change(bars_by_day: dict[str, dict[str, dict]], day: str,
                code: str) -> float | None:
    row = bars_by_day.get(day, {}).get(code) or {}
    value = row.get("change_pct")
    return float(value) if value is not None else None


def _advancer_share(bars_by_day: dict[str, dict[str, dict]], day: str,
                    codes: tuple[str, ...]) -> float | None:
    values = [
        value for code in codes
        if (value := _day_change(bars_by_day, day, code)) is not None
    ]
    if not values:
        return None
    return round(100.0 * sum(value > 0 for value in values) / len(values), 6)


def _market_returns(*, bars_by_day: dict[str, dict[str, dict]],
                    sessions: list[str], index: int,
                    market_codes: set[str]) -> dict[int, float | None]:
    out = {}
    for horizon in (1, 5, 20):
        values = [
            value for code in sorted(market_codes)
            if (value := _window_return(
                bars_by_day, sessions, index, code, horizon)) is not None
        ]
        out[horizon] = _median(values)
    return out


def _fund_flow_summary(codes: tuple[str, ...],
                       fund_flow_by_code: dict[str, dict] | None) -> dict:
    if fund_flow_by_code is None:
        return {
            "available": False,
            "covered": 0,
            "member_count": len(codes),
            "net_amount_sum": None,
            "net_amount_rate_median": None,
        }

    rows = [
        fund_flow_by_code.get(code) for code in codes
        if fund_flow_by_code.get(code) is not None
    ]
    amounts = [
        float(row["net_amount"]) for row in rows
        if row.get("net_amount") is not None
    ]
    rates = [
        float(row["net_amount_rate"]) for row in rows
        if row.get("net_amount_rate") is not None
    ]
    return {
        "available": bool(rows),
        "covered": len(rows),
        "member_count": len(codes),
        "net_amount_sum": round(sum(amounts), 6) if amounts else None,
        "net_amount_rate_median": _median(rates),
    }


def build_sector_snapshots(*, membership: MembershipSnapshot,
                           decision_at: str, as_of_session: str,
                           sessions: list[str],
                           bars_by_day: dict[str, dict[str, dict]],
                           market_codes: set[str] | None = None,
                           fund_flow_by_code: dict[str, dict] | None = None,
                           strict_pit: bool = True) -> list[SectorSnapshot]:
    """Build one deterministic sector state table at a fixed cutoff."""
    membership.validate(decision_at, strict_pit=strict_pit)
    if not sessions or sessions != sorted(sessions):
        raise SectorSnapshotError("sessions must be non-empty and sorted")
    index = _session_index(sessions, as_of_session)
    if as_of_session > decision_at[:10]:
        raise SectorSnapshotError(
            "sector facts cannot come from a session after the decision date")

    if market_codes is None:
        market_codes = set(bars_by_day.get(as_of_session, {}))
    market = _market_returns(
        bars_by_day=bars_by_day, sessions=sessions, index=index,
        market_codes=set(market_codes))

    current_day = sessions[index]
    old_day = sessions[index - 5] if index >= 5 else None
    out = []

    for sector_id in sorted(membership.members):
        codes = tuple(sorted(set(membership.members[sector_id])))
        if not codes:
            continue

        by_horizon: dict[int, list[float]] = {}
        covered: dict[str, int] = {}
        medians: dict[str, float | None] = {}
        relatives: dict[str, float | None] = {}

        for horizon in (1, 5, 20):
            values = [
                value for code in codes
                if (value := _window_return(
                    bars_by_day, sessions, index, code, horizon)) is not None
            ]
            by_horizon[horizon] = values
            covered[f"{horizon}d"] = len(values)
            medians[f"{horizon}d"] = _median(values)
            benchmark = market[horizon]
            relatives[f"{horizon}d"] = (
                round(medians[f"{horizon}d"] - benchmark, 6)
                if medians[f"{horizon}d"] is not None
                and benchmark is not None else None)

        current_breadth = _advancer_share(bars_by_day, current_day, codes)
        old_breadth = (
            _advancer_share(bars_by_day, old_day, codes) if old_day else None)
        breadth_improvement = (
            round(current_breadth - old_breadth, 6)
            if current_breadth is not None and old_breadth is not None
            else None)

        market_1d = market[1]
        current_returns = [
            value for code in codes
            if (value := _window_return(
                bars_by_day, sessions, index, code, 1)) is not None
        ]
        outperform = (
            round(
                100.0 * sum(value > market_1d for value in current_returns)
                / len(current_returns), 6)
            if current_returns and market_1d is not None else None)

        sorted_5d = sorted(by_horizon[5], reverse=True)
        ex_top1 = _median(sorted_5d[1:]) if len(sorted_5d) > 1 else None
        ex_top3 = _median(sorted_5d[3:]) if len(sorted_5d) > 3 else None
        fund_flow = _fund_flow_summary(codes, fund_flow_by_code)

        fact_payload = {
            "sector_id": sector_id,
            "sector_type": membership.sector_type,
            "as_of_session": as_of_session,
            "decision_at": decision_at,
            "membership_hash": membership.content_hash,
            "formula_version": FORMULA_VERSION,
            "coverage": covered,
            "returns_pct": medians,
            "market_returns_pct": {
                f"{h}d": market[h] for h in (1, 5, 20)},
            "relative_returns_pct": relatives,
            "advancers_pct": current_breadth,
            "outperform_market_pct": outperform,
            "breadth_improvement_5d_pp": breadth_improvement,
            "ex_top1_5d_median_pct": ex_top1,
            "ex_top3_5d_median_pct": ex_top3,
            "fund_flow": fund_flow,
        }

        out.append(SectorSnapshot(
            sector_id=sector_id,
            sector_type=membership.sector_type,
            as_of_session=as_of_session,
            decision_at=decision_at,
            membership_snapshot_id=membership.snapshot_id,
            membership_hash=membership.content_hash,
            formula_version=FORMULA_VERSION,
            member_count=len(codes),
            coverage={
                **covered,
                "member_count": len(codes),
                "5d_ratio": round(
                    covered["5d"] / len(codes), 6) if codes else None,
            },
            returns_pct=medians,
            market_returns_pct={
                f"{h}d": market[h] for h in (1, 5, 20)},
            relative_returns_pct=relatives,
            advancers_pct=current_breadth,
            outperform_market_pct=outperform,
            breadth_improvement_5d_pp=breadth_improvement,
            ex_top1_5d_median_pct=ex_top1,
            ex_top3_5d_median_pct=ex_top3,
            fund_flow=fund_flow,
            input_hash=_hash(fact_payload),
        ))

    return out


def candidate_leave_one_out_5d(*, membership: MembershipSnapshot,
                               decision_at: str, as_of_session: str,
                               sessions: list[str],
                               bars_by_day: dict[str, dict[str, dict]],
                               market_codes: set[str] | None = None,
                               strict_pit: bool = True) -> dict[str, dict[str, dict]]:
    """Peer 5-day strength for each candidate with the candidate removed.

    This prevents circular evidence: a stock cannot make its sector look strong
    and then cite that same sector strength as independent support for itself.
    Missing peer bars stay missing and are named by coverage rather than filled
    with zero.
    """
    membership.validate(decision_at, strict_pit=strict_pit)
    if not sessions or sessions != sorted(sessions):
        raise SectorSnapshotError("sessions must be non-empty and sorted")
    index = _session_index(sessions, as_of_session)
    if as_of_session > decision_at[:10]:
        raise SectorSnapshotError(
            "sector facts cannot come from a session after the decision date")
    if index < 5:
        return {
            sector_id: {
                code: {
                    "peer_covered": 0,
                    "peer_total": max(0, len(set(codes)) - 1),
                    "peer_5d_median_pct": None,
                    "peer_relative_5d_pct": None,
                }
                for code in sorted(set(codes))
            }
            for sector_id, codes in sorted(membership.members.items())
        }

    if market_codes is None:
        market_codes = set(bars_by_day.get(as_of_session, {}))
    market_5d_by_code = {
        code: _window_return(
            bars_by_day, sessions, index, code, 5)
        for code in sorted(set(market_codes))
    }

    out: dict[str, dict[str, dict]] = {}
    for sector_id, raw_codes in sorted(membership.members.items()):
        codes = tuple(sorted(set(raw_codes)))
        returns = {
            code: _window_return(
                bars_by_day, sessions, index, code, 5)
            for code in codes
        }
        per_code = {}
        for code in codes:
            peers = [
                value for peer, value in returns.items()
                if peer != code and value is not None
            ]
            median = _median(peers)
            market_peers = [
                value for peer, value in market_5d_by_code.items()
                if peer != code and value is not None
            ]
            market_median = _median(market_peers)
            per_code[code] = {
                "peer_covered": len(peers),
                "peer_total": max(0, len(codes) - 1),
                "peer_5d_median_pct": median,
                "market_peer_covered": len(market_peers),
                "market_5d_median_ex_candidate_pct": market_median,
                "peer_relative_5d_pct": (
                    round(median - market_median, 6)
                    if median is not None and market_median is not None
                    else None
                ),
            }
        out[sector_id] = per_code
    return out


def rank_sector_snapshots(snapshots: list[SectorSnapshot]) -> list[dict]:
    """Transparent v0 shortlist ranking from three observable columns."""
    required = [
        snapshot for snapshot in snapshots
        if snapshot.relative_returns_pct.get("5d") is not None
        and snapshot.advancers_pct is not None
        and snapshot.breadth_improvement_5d_pp is not None
    ]

    def _ordinal(field) -> dict[str, int]:
        ordered = sorted(
            required,
            key=lambda snap: (-float(field(snap)), snap.sector_id))
        return {snap.sector_id: idx + 1 for idx, snap in enumerate(ordered)}

    rel_rank = _ordinal(lambda snap: snap.relative_returns_pct["5d"])
    breadth_rank = _ordinal(lambda snap: snap.advancers_pct)
    improve_rank = _ordinal(lambda snap: snap.breadth_improvement_5d_pp)

    rows = []
    for snapshot in snapshots:
        if snapshot.sector_id not in rel_rank:
            rows.append({
                "sector_id": snapshot.sector_id,
                "rank": None,
                "average_rank": None,
                "reason": "missing_required_fact",
                "snapshot_hash": snapshot.input_hash,
            })
            continue
        avg = _mean([
            rel_rank[snapshot.sector_id],
            breadth_rank[snapshot.sector_id],
            improve_rank[snapshot.sector_id],
        ])
        rows.append({
            "sector_id": snapshot.sector_id,
            "rank": 0,
            "average_rank": avg,
            "relative_5d_rank": rel_rank[snapshot.sector_id],
            "breadth_rank": breadth_rank[snapshot.sector_id],
            "breadth_improvement_rank": improve_rank[snapshot.sector_id],
            "snapshot_hash": snapshot.input_hash,
        })

    eligible = sorted(
        [row for row in rows if row["average_rank"] is not None],
        key=lambda row: (row["average_rank"], row["sector_id"]))
    for idx, row in enumerate(eligible):
        row["rank"] = idx + 1
    missing = [row for row in rows if row["average_rank"] is None]
    return eligible + sorted(missing, key=lambda row: row["sector_id"])
