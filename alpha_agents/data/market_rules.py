"""Market microstructure rules, versioned by instrument **and** effective date.

Why this is a service and not an ``if``
---------------------------------------
A walk-forward replay starting in 2020 spans at least one rule change, so
``if code.startswith("300"): pct = 0.10`` is wrong on one side of it for every
ChiNext name — and ChiNext is in the default universe
(``config.TRADABLE_PREFIXES``). The design already requires this shape
("market and fee rules must be versioned by instrument and effective date");
tech-debt D20 is that the code had no such service at all.

What is encoded, and from where
-------------------------------
深交所投资者教育《创业板改革 | 交易特别规定 ABC（一）》
<https://www.szse.cn/investor/institute/rules/t20200807_580310.html>, read 2026-09-16:

* 施行后（**2020-08-24**，注册制首只股票上市首日）创业板股票竞价涨跌幅由
  **10% 提高到 20%**；
* 施行**前**创业板风险警示股票（ST／*ST）为 **5%**，施行**后**统一为 **20%** ——
  ChiNext risk-warned names follow the board's limit, **not** the main board's 5%；
* 创业板新股上市后**前五个交易日不设涨跌幅限制**。

Main boards are ±10%. Shanghai main-board risk-warned names were ±5% before
**2026-07-06** and are ±10% from that date under the Shanghai Stock Exchange's
2026 Trading Rules; Shenzhen main-board risk-warned names remain modelled at
±5% because the Shanghai rule must not be projected onto another exchange.

What this module refuses to decide
----------------------------------
Silence is the failure mode this file is written against, so what it cannot
determine comes back in ``unchecked`` rather than as a default:

* ``st_status`` — absent when no ``name`` is passed, because risk-warning status
  lives in the security's name, not its code.
* ``listing_day_exemption`` — absent when no ``listed_trading_days`` is passed.
  ChiNext's five-day exemption is the sourced rule; the main boards' first-day
  rules are not encoded here because no primary source was read for them.

Fees, tick size, the continuous-auction 2% price cage and the 申报数量 caps are
out of scope. The fee engine is a stated non-goal of this project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 《创业板交易特别规定》施行日 — the first registration-system listing.
CHINEXT_REFORM_DATE = "2020-08-24"
SSE_RISK_WARNING_REFORM_DATE = "2026-07-06"

SSE_MAIN_BOARD_PREFIXES = ("60",)
SZSE_MAIN_BOARD_PREFIXES = ("000", "001", "002", "003")
MAIN_BOARD_PREFIXES = SSE_MAIN_BOARD_PREFIXES + SZSE_MAIN_BOARD_PREFIXES
CHINEXT_PREFIXES = ("300", "301")

MAIN_BOARD_LIMIT = 0.10
CHINEXT_LIMIT = 0.20
RISK_WARNED_LIMIT = 0.05

#: A-share purchases are whole lots. STAR's 200-share minimum is not modelled
#: because STAR is not in the default universe.
LOT_SIZE = 100

_CHINEXT_EXEMPT_DAYS = 5

_DIGITS = re.compile(r"(\d{6})")


@dataclass(frozen=True)
class MarketRule:
    """One instrument's tradable constraints on one date.

    ``price_limit_pct`` is ``None`` only when there is **no** limit (the ChiNext
    first-days exemption). An undecidable rule is never expressed as a number
    here — it is named in ``unchecked`` instead.
    """

    price_limit_pct: float | None
    lot_size: int
    reason: str
    unchecked: tuple[str, ...] = field(default_factory=tuple)

    def limit_prices(self, prev_close: float) -> tuple[float | None, float | None]:
        """(upper, lower) bound for the day, or (None, None) when uncapped."""
        if self.price_limit_pct is None:
            return (None, None)
        return (round(prev_close * (1 + self.price_limit_pct), 2),
                round(prev_close * (1 - self.price_limit_pct), 2))


def normalise_code(code: str) -> str:
    """``sh600000`` / ``600000.SH`` / ``600000`` → ``600000``.

    The databases hold bare six-digit codes, but the archive and hand-written
    call sites both carry exchange prefixes, and a rules service that silently
    matched none of them would raise on a real code instead of a fake one.
    """
    match = _DIGITS.search(str(code or ""))
    if not match:
        raise ValueError(f"no six-digit instrument code in {code!r}")
    return match.group(1)


def normalise_date(date: str) -> str:
    """``20200824`` → ``2020-08-24``. Returns a comparable ISO string."""
    text = str(date or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise ValueError(f"unrecognised date {date!r}; want YYYY-MM-DD or YYYYMMDD")


def _board(code: str) -> str:
    for prefix in CHINEXT_PREFIXES:
        if code.startswith(prefix):
            return "chinext"
    for prefix in MAIN_BOARD_PREFIXES:
        if code.startswith(prefix):
            return "main"
    raise ValueError(
        f"{code} is outside the modelled boards "
        f"(main {MAIN_BOARD_PREFIXES}, ChiNext {CHINEXT_PREFIXES}); "
        "STAR, BSE and funds have different rules and are deliberately not "
        "guessed here — extend the table with a source instead"
    )


def _is_risk_warned(name: str | None) -> bool:
    return bool(name) and "ST" in str(name).upper()


def market_rules(code: str, date: str, *, name: str | None = None,
                 listed_trading_days: int | None = None) -> MarketRule:
    """The rule for one instrument on one date.

    ``name`` carries risk-warning status (it is in the name, not the code) and
    ``listed_trading_days`` is how many sessions the instrument has traded,
    counted so that a brand-new listing is ``1``. Both are optional; what they
    would have decided is named in ``unchecked`` when they are absent.
    """
    instrument = normalise_code(code)
    day = normalise_date(date)
    board = _board(instrument)
    unchecked: list[str] = []

    risk_warned = _is_risk_warned(name)
    if name is None:
        unchecked.append("st_status")

    if board == "chinext":
        reformed = day >= CHINEXT_REFORM_DATE
        if reformed:
            # Risk-warned ChiNext names follow the board, per the exchange's own
            # explainer: 5% before the reform, 20% after it.
            limit = CHINEXT_LIMIT
            reason = ("ChiNext after 2020-08-24 is ±20%, including "
                      "risk-warned names")
        else:
            limit = RISK_WARNED_LIMIT if risk_warned else MAIN_BOARD_LIMIT
            suffix = " (risk-warned)" if risk_warned else ""
            reason = f"ChiNext before 2020-08-24 is ±10%{suffix}"
        if listed_trading_days is not None and listed_trading_days <= _CHINEXT_EXEMPT_DAYS:
            return MarketRule(
                price_limit_pct=None, lot_size=LOT_SIZE,
                reason=(f"ChiNext listing {listed_trading_days} day(s): the "
                        f"first {_CHINEXT_EXEMPT_DAYS} sessions are uncapped"),
                unchecked=tuple(unchecked))
        if listed_trading_days is None:
            unchecked.append("listing_day_exemption")
        return MarketRule(price_limit_pct=limit, lot_size=LOT_SIZE,
                          reason=reason, unchecked=tuple(unchecked))

    if risk_warned and instrument.startswith(SSE_MAIN_BOARD_PREFIXES):
        if day >= SSE_RISK_WARNING_REFORM_DATE:
            limit = MAIN_BOARD_LIMIT
            reason = (
                "Shanghai main-board risk-warned name from 2026-07-06 is ±10%")
        else:
            limit = RISK_WARNED_LIMIT
            reason = (
                "Shanghai main-board risk-warned name before 2026-07-06 is ±5%")
    elif risk_warned:
        limit = RISK_WARNED_LIMIT
        reason = (
            "Shenzhen main-board risk-warned name remains modelled at ±5%; "
            "the Shanghai 2026 rule is not applied cross-exchange")
    else:
        limit = MAIN_BOARD_LIMIT
        reason = "main board ±10%"
    unchecked.append("listing_day_exemption")
    return MarketRule(
        price_limit_pct=limit, lot_size=LOT_SIZE,
        reason=reason, unchecked=tuple(unchecked))
