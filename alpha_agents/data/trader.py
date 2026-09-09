"""A trader is a config file, and that is the point.

Evolution needs variation and selection. The selection half was already
here — holdout_gate runs a paired test between a champion and a
challenger — but the challengers were mined from trades that had already
worked (`scan_and_auto_create`), which makes them products of the past
distribution. A system that only learns from its own successes converges
on itself.

Making a trader a file changes what a mutation costs. Adding one is
writing a YAML and a prompt, not editing code — and because the mutation
*is* a file with a closed schema, the review agent can eventually write
one itself:

    "22 of 25 orders were cancelled and 13 of those died because the
     theme faded while I waited for a pullback. I want to try entering
     on strength instead, 300k for two weeks."
        → traders/breakout_v2.yaml → holdout_gate → keep or archive

Same design as the invalidation vocabulary: give the agent a restricted
grammar that is expressive enough, rather than arbitrary power.

This file briefly carried an ``entry_style`` enum — pullback / breakout /
market — on the argument that a prompt cannot express where the order
sits relative to the market. That argument was wrong, and the data says
so. 109 of 113 orders were priced at ``price * 0.97`` not because a
prompt could not say otherwise, but because they came from a scoring
function with **no agent in it at all**; the four orders a model did
write carried levels it chose itself. Naming the constant "style" dressed
an absence of judgement up as a decision, and capped what a trader could
ever become at three values in a Python dict.

So entry pricing went back to the agent, with ``get_price_levels`` to
compute against. What stays here is what genuinely is not a judgement
call the agent gets to make: how much money it was given, which prompt it
reads, and the backstops it cannot exceed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

from alpha_agents.config import PROMPTS_DIR

logger = logging.getLogger(__name__)

TRADERS_DIR = Path(__file__).parent.parent.parent / "traders"

# The id every position, thesis and prediction carried before traders
# existed. Rows keep it, and a single-trader deployment never sees the
# difference — the whole feature stays invisible until a second file
# appears in traders/.
DEFAULT_TRADER = "default"


@dataclass
class Trader:
    """One trader: its money, its style, and its own book.

    Capital, positions and every learning statistic are per-trader.
    Sharing them would defeat the comparison — if two strategies draw
    from one pot, whichever runs first starves the other and the result
    measures ordering rather than skill.

    What *is* shared is the world: market data, theme state, the flash
    index, the tools. Those are facts, not strategy, and duplicating them
    would multiply cost for nothing.
    """
    id: str
    name: str = ""
    capital: int = 1_000_000
    prompt_file: str = "morning_scan.md"
    default_size_pct: float = 0.03
    max_position_pct: float = 0.10
    default_horizon_days: int = 5
    enabled: bool = True
    note: str = ""
    extra_prompt: str = ""
    tags: list[str] = field(default_factory=list)
    # True only for the built-in default kept alive to manage a book
    # that predates traders/. It exits its positions but opens no new
    # ones — a strategy nobody configured should not keep buying.
    legacy: bool = False

    def prompt(self) -> str:
        """The base prompt plus this trader's own instructions."""
        base = (PROMPTS_DIR / self.prompt_file).read_text(encoding="utf-8")
        if not self.extra_prompt:
            return base
        return (f"{base}\n\n---\n\n## 你是谁\n\n{self.extra_prompt.strip()}\n")


_DEFAULT = Trader(id=DEFAULT_TRADER, name="默认交易员",
                  note="traders/ 为空时使用，行为与引入多交易员之前完全一致")


def _coerce(raw: dict, path: Path) -> Trader | None:
    """Build a Trader from a config dict, or refuse it loudly.

    A malformed trader is dropped rather than defaulted: a file that
    silently becomes the default would trade real capital under a name
    that promises something else, and the comparison it exists for would
    be quietly measuring the wrong thing.
    """
    tid = str(raw.get("id") or path.stem).strip()
    if not tid or tid == DEFAULT_TRADER:
        logger.error("traders/%s: id 缺失或与内置默认冲突 — 跳过", path.name)
        return None

    try:
        return Trader(
            id=tid,
            name=str(raw.get("name") or tid),
            capital=int(raw.get("capital", 1_000_000)),
            prompt_file=str(raw.get("prompt_file", "morning_scan.md")),
            default_size_pct=float(raw.get("default_size_pct", 0.03)),
            max_position_pct=float(raw.get("max_position_pct", 0.10)),
            default_horizon_days=int(raw.get("default_horizon_days", 5)),
            enabled=bool(raw.get("enabled", True)),
            note=str(raw.get("note", "")),
            extra_prompt=str(raw.get("extra_prompt", "")),
            tags=list(raw.get("tags") or []),
        )
    except (TypeError, ValueError) as e:
        logger.error("traders/%s: 字段类型错误 (%s) — 跳过", path.name, e)
        return None


def load_traders(scanning: bool = False) -> list[Trader]:
    """Every enabled trader, or the built-in default when there are none.

    An empty or missing traders/ directory is the normal single-trader
    case, not an error — the feature costs nothing until it is used.

    ``scanning=True`` asks for the traders that may open new positions,
    which excludes a legacy default kept alive only to wind down the book
    that existed before traders/ did. Managing a book and adding to one
    are different permissions, and conflating them would let a strategy
    nobody configured keep buying.
    """
    if not TRADERS_DIR.is_dir():
        return [_DEFAULT]

    configs = (sorted(TRADERS_DIR.glob("*.yaml"))
               + sorted(TRADERS_DIR.glob("*.yml")))
    if not configs:
        return [_DEFAULT]

    try:
        import yaml
    except ImportError:
        # Only worth saying when there was something to parse — the
        # directory ships with .example files that are meant to be inert.
        logger.warning("PyYAML 未安装，%d 个交易员配置被忽略", len(configs))
        return [_DEFAULT]

    out = []
    for path in configs:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.error("traders/%s 解析失败: %s — 跳过", path.name, e)
            continue
        trader = _coerce(raw, path)
        if trader and trader.enabled:
            out.append(trader)

    if not out:
        return [_DEFAULT]
    if not scanning and _default_still_holds():
        # The first config file must not orphan the book that existed
        # before it. Those positions carry real stops and live theses, and
        # dropping the default trader would leave nothing evaluating them
        # — the positions would sit open until the horizon ran out, with
        # no monitor and no exit.
        out.insert(0, replace(
            _DEFAULT, legacy=True, name="默认交易员（仅清理旧仓）",
            note="traders/ 出现配置之前开的仓位，继续按原计划管理到平仓为止，"
                 "不再开新仓"))
    logger.info("交易员: %s", ", ".join(f"{t.name}({t.capital:,})" for t in out))
    return out


def _default_still_holds() -> bool:
    """Does the built-in trader still have anything open?

    Errs toward yes on failure: managing an empty book costs one query a
    cycle, and not managing a live one costs money.
    """
    try:
        from alpha_agents.data.memory_store import _get_conn
        row = _get_conn().execute(
            "SELECT 1 FROM virtual_portfolio WHERE trader_id = ? "
            "AND status IN ('pending', 'open') LIMIT 1", (DEFAULT_TRADER,)
        ).fetchone()
        return row is not None
    except Exception as e:
        logger.debug("Default-book check failed (%s) — keeping it", e)
        return True


def get_trader(trader_id: str) -> Trader:
    """One trader by id, falling back to the default.

    Falls back rather than raising: a position whose trader's config file
    was deleted still needs to be managed, and refusing to look at it
    would strand real money.
    """
    if trader_id and trader_id != DEFAULT_TRADER:
        for t in load_traders():
            if t.id == trader_id:
                return t
        logger.warning("交易员 %r 的配置已不存在，按默认处理", trader_id)
    return _DEFAULT
