import json
from pathlib import Path

from alpha_agents.config import DB_PATH
from alpha_agents.data import security_eligibility as E
from alpha_agents.data.db import get_connection

DEFAULT_MIN_MARKET_CAP = 1_000_000_000  # 10亿


def filter_stocks_fn(
    stock_codes: list[str],
    db_path: Path = DB_PATH,
    min_market_cap: float = DEFAULT_MIN_MARKET_CAP,
) -> str:
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" for _ in stock_codes)
        rows = conn.execute(
            f"SELECT code, name, market_cap, industry, is_st, is_suspended "
            f"FROM stocks WHERE code IN ({placeholders})",
            stock_codes,
        ).fetchall()

        kept = []
        removed = []
        by_code = {str(r["code"]): r for r in rows}
        labels = {
            E.BOARD: "非可交易板块",
            E.NOT_LISTED: "未上市",
            E.UNKNOWN_INSTRUMENT: "证券信息不存在",
            E.ST: "ST",
            E.SUSPENDED: "停牌",
            E.NO_PRIOR_BAR: "无前一交易日行情",
        }

        # Iterate the requested codes, not only query hits. A missing security
        # is evidence and must not disappear from the result silently.
        seen = set()
        for raw_code in stock_codes:
            code = str(raw_code or "").strip()
            if code in seen:
                continue
            seen.add(code)
            r = by_code.get(code)
            exclusion = E.reason(E.SecurityFacts(
                code=code,
                listed=True,
                known=r is not None,
                is_st=bool(r and r["is_st"]),
                is_suspended=bool(r and r["is_suspended"]),
                # Morning's current-universe filter does not own a historical
                # decision clock; the T1 adapter supplies this fact itself.
                has_prior_bar=True,
            ))
            reasons = [labels[exclusion]] if exclusion else []
            if (r is not None and r["market_cap"] is not None
                    and r["market_cap"] < min_market_cap):
                reasons.append(f"市值不足{min_market_cap/1e8:.0f}亿")

            stock = {
                "code": code,
                "name": r["name"] if r is not None else "",
                "market_cap": r["market_cap"] if r is not None else None,
                "industry": r["industry"] if r is not None else None,
            }
            if reasons:
                removed.append({
                    **stock, "reasons": reasons,
                    "eligibility_reason": exclusion,
                })
            else:
                kept.append(stock)

        return json.dumps({"stocks": kept, "removed": removed}, ensure_ascii=False)
    finally:
        conn.close()
