"""Sector ranking tool — industry and concept fund flow rankings.

Two ranking dimensions:
- Industry (行业): 90 sectors — broad classification (半导体, 银行, 白酒...)
- Concept (概念): 387 themes — hot topics (华为昇腾, AI算力, 低空经济...)

Both from 同花顺 via data.10jqka.com.cn (reliable, no push2 issues).
"""

import json
import logging
import re

from alpha_agents.data.market_data import get_industry_fund_flow, get_concept_fund_flow

logger = logging.getLogger(__name__)


def theme_cross_section() -> list[dict]:
    """Every board the system can see today — concepts **and** industries.

    Both, because the tracked themes come from both. `石油加工贸易` and
    `港口航运` are industry names, and they sat unscored for a week (frozen at
    their creation strength, never retiring) because the only frame anything
    read was the concept one — 287 concepts against a theme that lives in the
    90 industries. A percentile needs the frame the theme actually belongs to,
    and both frames report the same two numbers in the same units (涨跌幅 in %,
    净额 in 亿元), so they can share one ranking.

    The ranking helpers below return the top and bottom N, which is the wrong
    sample to take a percentile from: ranking inside a pre-filtered list makes
    "40th percentile" mean "40th of the leaders", and a theme at zero flow
    would look mid-pack no matter how many quiet boards sit around it.

    Returns ``[]`` only when *neither* frame is available. A caller must not
    read that as "every theme is at percentile zero" — see
    ``theme_manager.refresh_theme_scores``, and note the difference between an
    empty board and a board that simply does not name a given theme.
    """
    out: list[dict] = []
    for scope, fetch in (("concept", get_concept_fund_flow),
                         ("industry", get_industry_fund_flow)):
        try:
            df = fetch()
        except Exception as e:
            logger.warning("%s cross-section unavailable: %s", scope, e)
            continue
        if df is None or df.empty:
            continue
        name_col = "行业" if "行业" in df.columns else "名称"
        for _, row in df.iterrows():
            name = str(row.get(name_col, "") or "").strip()
            if not name:
                continue
            out.append({
                "concept": name,
                "scope": scope,
                "change_pct": float(row.get("行业-涨跌幅", 0) or 0),
                "net_flow_yi": float(row.get("净额", 0) or 0),
            })
    return out


# Suffixes boards attach that themes drop, and vice versa.
_BOARD_SUFFIXES = ("概念", "板块", "指数", "行业", "主题")
_PAREN = re.compile(r"[（(]([^）)]*)[）)]")


def _board_keys(name: str) -> list[str]:
    """Every spelling of ``name`` worth comparing a board name against.

    Boards and themes name the same thing differently: theme `小金属概念` is
    board `小金属`, and theme `共封装光学(CPO)` writes its English name in
    brackets. Comparing exactly and silently finding nothing is the worst
    outcome available — downstream it is indistinguishable from "no data".

    Latin fragments are deliberately **not** split out as keys. `5G` would then
    match `F5G概念`, and a false match is worse than a missing one: it would
    score a theme on another board's flow.
    """
    if not name:
        return []
    candidates = [name]
    candidates += [p.strip() for p in _PAREN.findall(name)]
    candidates.append(_PAREN.sub("", name).strip())
    out: list[str] = []
    for cand in candidates:
        bases = [cand]
        for suffix in _BOARD_SUFFIXES:
            if cand.endswith(suffix) and len(cand) > len(suffix):
                bases.append(cand[: -len(suffix)])
        for base in bases:
            if base and base not in out:
                out.append(base)
    return out


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def _latin_tokens(text: str) -> set[str]:
    """Latin/digit runs in ``text``, lower-cased — `PCB概念` → {"pcb"}.

    Used to gate the containment pass: when two names both carry a Latin
    fragment, those fragments have to agree. Without it `5G概念` contains
    `F5G概念` and would be scored on the other board's flow.
    """
    return {t.lower() for t in re.findall(r"[A-Za-z0-9]+", text or "")}


def board_match(name: str, rows: list[dict], key: str = "concept") -> dict | None:
    """The board row for ``name``, or None when the board cannot name it.

    Exact spellings first, then one containment pass in both directions.
    Containment is restricted to names carrying CJK — it is what maps
    `小金属概念` onto `小金属`, and it is also what would map `5G` onto
    `F5G概念`, so names without Chinese characters only ever match exactly,
    and names that do carry a Latin fragment only match boards whose Latin
    fragments are the same.

    ``None`` means "this board cannot name the theme", which is a different
    fact from "the theme is weak" and has its own handling at every caller.
    """
    if not name or not rows:
        return None
    index = {r.get(key, ""): r for r in rows}
    keys = _board_keys(name)
    for k in keys:
        if k in index:
            return index[k]
    if not _has_cjk(name):
        return None
    wanted = _latin_tokens(name)
    for k in keys:
        if len(k) < 2:
            continue
        for board, row in index.items():
            if not k or not board or not (k in board or board in k):
                continue
            if wanted and _latin_tokens(board) != wanted:
                continue
            return row
    return None


def get_sector_ranking_fn(top_n: int = 20) -> str:
    """Get industry sector ranking by fund flow — shows sector rotation direction.

    Returns top gaining and losing industries by net fund flow.
    Use this to detect which sectors money is flowing INTO and OUT OF.

    Args:
        top_n: Number of top/bottom sectors to return. Default 20.
    """
    try:
        df = get_industry_fund_flow()

        if df is None or df.empty:
            return json.dumps({"error": "no sector data", "gainers": [], "losers": []}, ensure_ascii=False)

        gainers = []
        losers = []
        for _, row in df.iterrows():
            name = str(row.get("行业", ""))
            change_pct = float(row.get("行业-涨跌幅", 0) or 0)
            net_flow = float(row.get("净额", 0) or 0)
            leader = str(row.get("领涨股", ""))
            leader_change = float(row.get("领涨股-涨跌幅", 0) or 0)

            entry = {
                "sector": name,
                "change_pct": change_pct,
                "net_flow_yi": round(net_flow, 2),
                "leader": leader,
                "leader_change_pct": leader_change,
                "company_count": int(row.get("公司家数", 0) or 0),
            }

            if net_flow > 0:
                gainers.append(entry)
            else:
                losers.append(entry)

        gainers.sort(key=lambda x: x["net_flow_yi"], reverse=True)
        losers.sort(key=lambda x: x["net_flow_yi"])

        return json.dumps({
            "total_sectors": len(gainers) + len(losers),
            "inflow_sectors": len(gainers),
            "outflow_sectors": len(losers),
            "gainers": gainers[:top_n],
            "losers": losers[:top_n],
            "rotation_signal": "资金集中流入少数行业" if len(gainers) < len(losers) else "普涨格局",
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_sector_ranking failed: %s", e)
        return json.dumps({"error": str(e), "gainers": [], "losers": []}, ensure_ascii=False)


def get_concept_ranking_fn(top_n: int = 20) -> str:
    """Get concept board ranking by fund flow — shows hot theme rotation.

    Returns top gaining and losing concept themes by net fund flow.
    Concept names match the DB concept table (同花顺 concepts).
    Use this to discover new investment themes and track existing ones.

    Args:
        top_n: Number of top/bottom concepts to return. Default 20.
    """
    try:
        df = get_concept_fund_flow()

        if df is None or df.empty:
            return json.dumps({"error": "no concept data", "gainers": [], "losers": []}, ensure_ascii=False)

        name_col = "行业" if "行业" in df.columns else "名称"

        gainers = []
        losers = []
        for _, row in df.iterrows():
            name = str(row.get(name_col, ""))
            change_pct = float(row.get("行业-涨跌幅", 0) or 0)
            net_flow = float(row.get("净额", 0) or 0)
            leader = str(row.get("领涨股", ""))
            leader_change = float(row.get("领涨股-涨跌幅", 0) or 0)

            entry = {
                "concept": name,
                "change_pct": change_pct,
                "net_flow_yi": round(net_flow, 2),
                "leader": leader,
                "leader_change_pct": leader_change,
                "company_count": int(row.get("公司家数", 0) or 0),
            }

            if net_flow > 0:
                gainers.append(entry)
            else:
                losers.append(entry)

        gainers.sort(key=lambda x: x["net_flow_yi"], reverse=True)
        losers.sort(key=lambda x: x["net_flow_yi"])

        return json.dumps({
            "total_concepts": len(gainers) + len(losers),
            "inflow_concepts": len(gainers),
            "outflow_concepts": len(losers),
            "gainers": gainers[:top_n],
            "losers": losers[:top_n],
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_concept_ranking failed: %s", e)
        return json.dumps({"error": str(e), "gainers": [], "losers": []}, ensure_ascii=False)
