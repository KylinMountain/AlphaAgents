"""The deterministic T1 candidate-pool selection policy.

This is intentionally small. The LLM still decides what to buy; this policy
decides which ranking lane contributes the next name to the panel it may see.

Historically the runner alternated:
    change, turnover, change, turnover, ...

That behavior is exactly change_share=0.5. Making the share explicit turns an
implicit heuristic into one frozen, replayable policy gene without changing
the incumbent's behavior.
"""

from __future__ import annotations

from alpha_agents.data import scoring

DEFAULT_CHANGE_SHARE = 0.50


class SelectionPolicyError(ValueError):
    """A frozen selection parameter cannot be executed safely."""


def change_share(params: dict | None = None) -> float:
    if params is None:
        params = scoring.in_force_decision_params()
    block = params.get("selection_rank") or {}
    value = float(block.get("change_share", DEFAULT_CHANGE_SHARE))
    if not 0.0 <= value <= 1.0:
        raise SelectionPolicyError(
            f"selection_rank.change_share must be in [0,1], got {value}")
    return value


def in_force_params() -> dict:
    params = scoring.in_force_decision_params()
    return {
        "change_share": change_share(params),
    }


def weighted_merge(change_ranked: list, turnover_ranked: list, *,
                   total: int, params: dict | None = None) -> list:
    """Merge two ranked lanes while preserving their own order.

    At share=0.5 the sequence is byte-for-byte the old intent:
    change, turnover, change, turnover, ...

    The merge deliberately allows duplicate codes. The caller already owns
    deduplication after liquidity checks; changing that here would make a gene
    silently alter two behaviors at once.
    """
    if total <= 0:
        return []
    share = change_share(params)
    out = []
    ci = ti = 0
    change_used = turnover_used = 0

    while len(out) < total and (
            ci < len(change_ranked) or ti < len(turnover_ranked)):
        target_change = (len(out) + 1) * share
        prefer_change = change_used < target_change

        if prefer_change and ci < len(change_ranked):
            out.append(change_ranked[ci])
            ci += 1
            change_used += 1
        elif not prefer_change and ti < len(turnover_ranked):
            out.append(turnover_ranked[ti])
            ti += 1
            turnover_used += 1
        elif ci < len(change_ranked):
            out.append(change_ranked[ci])
            ci += 1
            change_used += 1
        elif ti < len(turnover_ranked):
            out.append(turnover_ranked[ti])
            ti += 1
            turnover_used += 1

    return out


def candidate_pool_rows(by_change: list[tuple], by_turnover: list[tuple],
                        *, lane_depth: int) -> list[dict]:
    """The fixed pre-panel world a ranking variant is allowed to replay.

    Persist the two ordinal ranks as well as their values. Equal values must
    replay in the same order as the live stable sort; reconstructing a tie from
    values alone can choose a different security and turn a tie-break into an
    apparent policy effect.
    """
    change_rank = {
        code: rank for rank, (_score, code, _row)
        in enumerate(by_change[:lane_depth])
    }
    turnover_rank = {
        code: rank for rank, (_score, code, _row)
        in enumerate(by_turnover[:lane_depth])
    }
    raw = {}
    for _score, code, row in [
            *by_change[:lane_depth], *by_turnover[:lane_depth]]:
        raw.setdefault(code, row)

    rows = []
    for code in dict.fromkeys([
            *[x[1] for x in by_change[:lane_depth]],
            *[x[1] for x in by_turnover[:lane_depth]]]):
        row = raw[code]
        rows.append({
            "code": code,
            "change_pct": (
                float(row["change_pct"])
                if row.get("change_pct") is not None else None),
            "turnover_rate": (
                float(row.get("turnover_rate"))
                if row.get("turnover_rate") is not None else None),
            "change_rank": change_rank.get(code),
            "turnover_rank": turnover_rank.get(code),
        })
    return rows


def rank_candidate_rows(rows: list[dict], *, limit: int,
                        params: dict | None = None) -> list[dict]:
    """Reconstruct the lane merge from a frozen candidate pool.

    Liquidity eligibility is intentionally not handled here. Live execution
    and Dream both apply the same ADV20 rule *after* this ordering.
    """
    usable = [
        row for row in rows
        if row.get("code")
        and row.get("change_pct") is not None
        and row.get("turnover_rate") is not None
    ]
    if all(row.get("change_rank") is not None for row in usable):
        by_change = sorted(usable, key=lambda row: int(row["change_rank"]))
    else:
        by_change = sorted(
            usable, key=lambda row: float(row["change_pct"]), reverse=True)
    if all(row.get("turnover_rank") is not None for row in usable):
        by_turnover = sorted(usable, key=lambda row: int(row["turnover_rank"]))
    else:
        by_turnover = sorted(
            usable, key=lambda row: float(row["turnover_rate"]), reverse=True)
    merged = weighted_merge(
        by_change, by_turnover, total=max(limit * 4, limit), params=params)
    return merged


def materialize_codes(rows: list[dict], *, limit: int,
                      eligible, params: dict | None = None) -> list[str]:
    """Apply the frozen lane mix, dedup and the caller's PIT eligibility.

    eligible(code) is where the market-world check lives (ADV20 today).
    Keeping it injected lets live, Dream and forward shadow execute the same
    ranking code against their own read-only price world.
    """
    if limit <= 0:
        return []
    ordered = rank_candidate_rows(rows, limit=limit, params=params)
    chosen = []
    seen = set()
    for row in ordered:
        code = str(row["code"])
        if code in seen:
            continue
        seen.add(code)
        if not eligible(code):
            continue
        chosen.append(code)
        if len(chosen) >= limit:
            break
    return chosen
