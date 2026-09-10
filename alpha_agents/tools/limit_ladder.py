"""涨停梯队：一个短线交易员开盘前看的第一张表。

回答的是所有决策之前的那个问题——**今天这个市场能不能做**。

系统一直在存这些数据：涨停池、炸板池、跌停池，带连板数、封单额、
炸板次数、首封时间，一天上万条。但从来只是把原始行丢给 agent，
指望它自己想出「炸板率」这个概念。真实交易员的经验，很大一部分
就是知道该做哪些除法——这个文件把那些除法做出来。

四个数决定今天动不动手：

**炸板率** — 今天 42%（36 炸 / 85 次封板尝试）。这是承接强度最直接
的读数：一半的封板守不住，说明买盘接不动，昨天的涨停今天大概率贴水。

**赚钱效应** — 昨天涨停的票今天平均涨跌。这是唯一真正的「跟进能不能
赚钱」的答案，其余都是推测。为负就是在告诉你：昨天进场的人今天在亏。

**梯队结构** — 4板×2、3板×3、2板×9、1板×35。断层意味着高度做不上去；
最高板是几板，决定了这个市场的想象空间。

**首封时间分布** — 9:35 前封板的占比。早封是资金确定性强，尾盘才封
是勉强凑上去的，第二天完全是两种命运。

这里不做判断，只做算术。要不要动手是 agent 的事，它错了会进校准曲线。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

_DB = DATA_DIR / "market_snapshots.db"

# 9:35 之前封住的，是开盘就有人抢；之后的一路递减到尾盘偷袭。
EARLY_SEAL_CUTOFF = "0935"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _pools(conn, date: str) -> dict[str, list[dict]]:
    """当日三个池子，同一只票在同一池只算一次。

    快照每 5 分钟写一遍，一只涨停股一天会有几十行。按 (code, pool_type)
    去重后取最后一条 —— 最后一条才是收盘时的状态，中途炸过又封回去的，
    应该算封住。
    """
    rows = conn.execute(
        "SELECT code, name, pool_type, consecutive_limits, seal_amount_yi, "
        "       break_count, first_seal_time, sector, turnover_rate, "
        "       MAX(captured_at) AS last_seen "
        "FROM limit_pool_snapshots WHERE substr(captured_at,1,10) = ? "
        "GROUP BY code, pool_type", (date,)).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["pool_type"] or "up"].append(dict(r))
    return out


def _money_effect(conn, date: str) -> dict:
    """昨日涨停股今天的平均表现 —— 赚钱效应。

    唯一直接回答「跟进能不能赚钱」的数字，其余都是推测。需要昨天的
    涨停名单和今天的行情，两个都在快照库里；缺任何一个就诚实返回
    None，而不是拿一个残缺的样本充数。
    """
    prev = conn.execute(
        "SELECT DISTINCT substr(captured_at,1,10) d FROM limit_pool_snapshots "
        "WHERE substr(captured_at,1,10) < ? ORDER BY d DESC LIMIT 1",
        (date,)).fetchone()
    if not prev:
        return {"available": False, "reason": "没有昨日涨停名单"}
    pd = prev["d"]

    codes = [r["code"] for r in conn.execute(
        "SELECT DISTINCT code FROM limit_pool_snapshots "
        "WHERE substr(captured_at,1,10)=? AND pool_type='up'", (pd,))]
    if not codes:
        return {"available": False, "reason": f"{pd} 无涨停记录"}

    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, change_pct, MAX(captured_at) FROM all_quote_snapshots "
        f"WHERE substr(captured_at,1,10)=? AND code IN ({marks}) "
        f"GROUP BY code", (date, *codes)).fetchall()
    vals = [r["change_pct"] for r in rows if r["change_pct"] is not None]
    if not vals:
        return {"available": False, "reason": f"{pd} 的涨停股今日无行情快照"}

    return {
        "available": True,
        "prev_date": pd,
        "n": len(vals),
        "avg_change_pct": round(sum(vals) / len(vals), 2),
        "positive_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        # 亏损面比平均值更能说明问题：均值可以被一两只20cm拉起来。
        "down_over_5pct": sum(1 for v in vals if v < -5),
    }


def get_limit_ladder_fn(date: str = "") -> str:
    """今天的涨停梯队、炸板率、赚钱效应与题材集中度。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    try:
        conn = _conn()
    except Exception as e:
        logger.warning("Limit ladder unavailable: %s", e)
        return json.dumps({"error": f"快照库不可用: {e}"}, ensure_ascii=False)

    try:
        pools = _pools(conn, date)
        up, broken, down = pools.get("up", []), pools.get("broken", []), pools.get("down", [])
        if not up and not broken:
            return json.dumps(
                {"date": date, "error": "当日无涨停池数据 —— 可能非交易日，"
                                        "或快照未采集。不要据此判断市场冷清。"},
                ensure_ascii=False)

        attempts = len(up) + len(broken)
        ladder = Counter(r.get("consecutive_limits") or 1 for r in up)
        early = sum(1 for r in up
                    if (r.get("first_seal_time") or "9999")[:4] <= EARLY_SEAL_CUTOFF)
        sectors = Counter(r["sector"] for r in up if r.get("sector"))

        # 连板股单独列出：梯队的高度由它们决定，而它们的封单和炸板
        # 次数是明天还能不能接力的直接证据。
        leaders = sorted(
            (r for r in up if (r.get("consecutive_limits") or 1) >= 2),
            key=lambda r: -(r.get("consecutive_limits") or 0))[:12]

        out = {
            "date": date,
            "涨停": len(up), "炸板": len(broken), "跌停": len(down),
            "炸板率": round(len(broken) / attempts * 100, 1) if attempts else None,
            "最高板": max(ladder) if ladder else 0,
            "梯队": {f"{k}板": v for k, v in sorted(ladder.items(), reverse=True)},
            "早盘封板占比": round(early / len(up) * 100, 1) if up else None,
            "赚钱效应": _money_effect(conn, date),
            "题材集中度": [{"板块": k, "涨停数": v} for k, v in sectors.most_common(6)],
            "连板股": [{
                "code": r["code"], "name": r.get("name", ""),
                "板数": r.get("consecutive_limits"),
                "封单亿": r.get("seal_amount_yi"),
                "炸板次数": r.get("break_count"),
                "首封": r.get("first_seal_time"),
                "板块": r.get("sector", ""),
            } for r in leaders],
            "读法": (
                "炸板率高=承接弱，昨日涨停今日多半贴水；梯队断层=高度做不上去；"
                "早盘封板占比低=资金犹豫。赚钱效应为负时，跟进买入的人正在亏钱。"
                "这些是事实，动不动手你自己判断。"
            ),
        }
        return json.dumps(out, ensure_ascii=False)
    except Exception as e:
        logger.warning("Limit ladder failed: %s", e)
        return json.dumps({"error": f"梯队计算失败: {e}"}, ensure_ascii=False)
    finally:
        try:
            conn.close()
        except Exception as e:
            logger.debug("Ladder conn close failed: %s", e)
