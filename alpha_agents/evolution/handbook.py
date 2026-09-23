"""The trader's own handbook: its reviews distilled into rules it rewrites itself.

``data/traders/<trader>/MEMORY.md`` is what every decision of that trader
loads, verbatim. A person can open it, read it, edit it; an edit is part of
what the trader reads next and what it rewrites from.

How it is written:

* After a day with new trade reviews, the trader reads its current handbook
  and every review it has written, and rewrites the handbook — at most
  :data:`MAX_RULES` rules, each citing the reviews it comes from, keeping the
  id of a rule it keeps, dropping one the reviews no longer support.
* The words are the trader's. The **evidence under each rule is the code's**
  (GOLDEN_PRINCIPLES #2): how the cited trades went, and — once the rule
  exists — how the trades that followed it went against the ones that broke
  it. Each later trade review states which rules it followed and which it
  broke; the returns are from ``daily_kline``. A rule is graded by the market,
  not by the model that wrote it.

Every rewrite is kept as ``memory_history/<date>.md`` (and ``.json``). A
replay's morning of D loads the latest version dated **before** D, so it
never reads a rule distilled from D's own result. A replay writes to its own
sandbox, because ``DATA_DIR`` is the sandbox there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
from pathlib import Path

from alpha_agents import config

logger = logging.getLogger(__name__)

MAX_RULES = 10
_TIMEOUT = 180


def _dir(trader_id: str) -> Path:
    return config.DATA_DIR / "traders" / trader_id


def path(trader_id: str) -> Path:
    return _dir(trader_id) / "MEMORY.md"


def _history(trader_id: str) -> Path:
    return _dir(trader_id) / "memory_history"


def _version_before(trader_id: str, before: str | None, suffix: str) -> Path | None:
    hist = _history(trader_id)
    if not hist.is_dir():
        return None
    dated = sorted(p for p in hist.glob(f"*{suffix}")
                   if before is None or p.stem < before)
    return dated[-1] if dated else None


def load(trader_id: str, *, before: str | None = None) -> str:
    """The handbook as a decision reads it.

    Live (``before=None``) that is ``MEMORY.md`` itself, including any edit a
    person made. A replay passes its session date and gets the version
    written before it.
    """
    try:
        if before is None:
            p = path(trader_id)
            return p.read_text(encoding="utf-8").strip() if p.is_file() else ""
        p = _version_before(trader_id, before, ".md")
        return p.read_text(encoding="utf-8").strip() if p else ""
    except OSError as e:
        logger.warning("Handbook for %s unreadable: %s", trader_id, e)
        return ""


def _rules(trader_id: str, before: str | None = None) -> list[dict]:
    p = _version_before(trader_id, before, ".json")
    if p is None:
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("rules", [])
    except (OSError, json.JSONDecodeError):
        return []


def _med(xs: list[float]) -> str:
    return f"{statistics.median(xs):+.1f}%" if xs else "—"


def evidence(rule: dict, reviews: list[tuple[dict, dict]]) -> str:
    """What the market says about one rule, from the trader's own trades."""
    cited = [f for f, _ in reviews if f["position_id"] in set(rule.get("from") or [])]
    line = (f"来源 {len(cited)} 笔复盘：中位到手 {_med([f['return_pct'] for f in cited])}，"
            f"中位吐回 {statistics.median([f['giveback_pp'] for f in cited]):.1f} 个点"
            if cited else "来源：未引用可核对的复盘")
    since = rule.get("since") or ""
    after = [(f, w) for f, w in reviews if f["close_date"] > since] if since else []
    kept = [f["return_pct"] for f, w in after if rule["id"] in (w.get("followed") or [])]
    broke = [f["return_pct"] for f, w in after if rule["id"] in (w.get("broke") or [])]
    if kept or broke:
        line += (f"；写下后遵守 {len(kept)} 笔（中位 {_med(kept)}），"
                 f"违反 {len(broke)} 笔（中位 {_med(broke)}）")
    elif since:
        line += "；写下后还没有交易检验过"
    return line


def render(trader_id: str, rules: list[dict], reviews, as_of: str) -> str:
    lines = [f"# 我的交易守则（{trader_id}）",
             "",
             f"最后改写：{as_of}。守则是我从自己的逐笔复盘里总结的；"
             "每条下面的「证据」由系统按日线计算，不是我写的。",
             ""]
    for r in rules:
        lines.append(f"## {r['id']}　{r['text']}")
        lines.append(f"> 证据：{evidence(r, reviews)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_INSTRUCTIONS = f"""你是这个账户的交易员。下面是你现在的交易守则（可能为空），\
以及你对自己每一笔已平仓交易写过的复盘。

请重写你的守则：
- 最多 {MAX_RULES} 条。每条都要是你下次交易时**能照着执行**的一句话，带具体的数（比例、ATR 倍数、天数）。
- 每条必须写明来自哪几笔复盘（用复盘前面的编号 #数字），至少 1 笔。
- 保留仍然成立的守则，并**沿用它原来的编号**（R1、R2…）；复盘不再支持的就删掉；新守则用新编号。
- 守则之间不能互相矛盾。宁少勿滥：说不清楚的不要写。
- 目标只有一个：盈利。守则是为这个服务的。

只输出一个 JSON 对象：
{{"rules": [{{"id": "R1", "text": "...", "from": [12, 15]}}], "removed": [{{"id": "R3", "why": "..."}}]}}"""


def _parse(text: str, known_ids: set[str]) -> list[dict] | None:
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
    items = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return None
    out, seen = [], set()
    # A fresh id must clear every id already in play — the old handbook's and
    # the ones this reply uses — or a duplicate is renumbered onto itself.
    used = set(known_ids) | {str(i.get("id") or "").strip().upper()
                             for i in items if isinstance(i, dict)}
    next_n = 1 + max([int(i[1:]) for i in used
                      if i.startswith("R") and i[1:].isdigit()] or [0])
    for item in items[:MAX_RULES]:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        rid = str(item.get("id") or "").strip().upper()
        if not (rid.startswith("R") and rid[1:].isdigit()) or rid in seen:
            rid = f"R{next_n}"
            next_n += 1
        seen.add(rid)
        cites = []
        for c in item.get("from") or []:
            try:
                cites.append(int(str(c).lstrip("#")))
            except ValueError:
                continue
        out.append({"id": rid, "text": str(item["text"]).strip()[:300],
                    "from": cites})
    return out


def _review_listing(reviews) -> str:
    from alpha_agents.evolution.trade_review import facts_line
    lines = []
    for f, w in reviews:
        lines.append(f"#{f['position_id']} {facts_line(f)}")
        for k, label in (("verdict", "判断"), ("wrong", "做错"),
                         ("right", "做对"), ("next_time", "下次")):
            if w.get(k):
                lines.append(f"  {label}：{w[k]}")
    return "\n".join(lines)


async def consolidate(conn: sqlite3.Connection, trader_id: str, *, as_of: str,
                      model, trader=None) -> bool:
    """Rewrite the handbook from every review up to ``as_of``. Never raises.

    Returns whether a new version was written. Nothing is written without a
    model, or when the reply is unreadable — the previous handbook stands.
    """
    from alpha_agents.evolution.trade_review import reviews_for

    if model is None:
        return False
    reviews = reviews_for(conn, trader_id, up_to=as_of)
    if not reviews:
        return False
    current = load(trader_id)
    previous = {r["id"]: r for r in _rules(trader_id)}
    from agents import Agent, Runner
    message = (f"## 现在的守则\n\n{current or '（还没有）'}\n\n"
               f"## 你的逐笔复盘（{len(reviews)} 笔，编号是交易编号）\n\n"
               f"{_review_listing(reviews)}")
    try:
        agent = Agent(name="handbook", instructions=_INSTRUCTIONS,
                      model=model, tools=[])
        result = await asyncio.wait_for(Runner.run(agent, message, max_turns=2),
                                        timeout=_TIMEOUT)
    except Exception as e:                            # noqa: BLE001
        logger.warning("Handbook rewrite for %s failed (%s) — keeping the old one",
                       trader_id, e)
        return False
    rules = _parse(result.final_output or "", set(previous))
    if not rules:
        logger.warning("Handbook rewrite for %s unreadable — keeping the old one",
                       trader_id)
        return False
    for r in rules:
        # A kept rule keeps the date it was first written: that is where its
        # forward evidence starts.
        r["since"] = previous.get(r["id"], {}).get("since") or as_of
    text = render(trader_id, rules, reviews, as_of)
    hist = _history(trader_id)
    hist.mkdir(parents=True, exist_ok=True)
    (hist / f"{as_of}.md").write_text(text, encoding="utf-8")
    (hist / f"{as_of}.json").write_text(
        json.dumps({"as_of": as_of, "rules": rules}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    path(trader_id).write_text(text, encoding="utf-8")
    logger.info("Handbook [%s] rewritten as of %s: %d rules", trader_id, as_of,
                len(rules))
    return True


def rule_ids(trader_id: str, before: str | None = None) -> list[str]:
    return [r["id"] for r in _rules(trader_id, before)]
