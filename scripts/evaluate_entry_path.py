"""Evaluate the entry path on history — scaffolding, first reading only.

What this is for
----------------
``TRADER_CORE_IMPLEMENTATION`` §7 records the entry side as never evaluated
(quality grade **F**, tech-debt D6): the exit side has a forward-tested rule,
the entry side has none. The reason was that the news and flow inputs had no
history — the pipeline only ever read today's page and today's snapshot.

That changed. Three inputs now reach back years:

* ``daily_kline`` — 2020-01 onward (prices).
* CLS telegraph history — walked backwards by ``backfill_cls_history.py``.
* East-money board flow — ``moneyflow_concept_dc`` in the sibling
  ``alphaquant`` checkout: 2024-01 onward, carrying ``net_amount`` (元) and
  ``pct_change`` (%), which are exactly the two numbers ``theme_score`` reads.

So the gate itself can be run over history. This script does that and reports
one thing: **does the gate's score separate boards by their forward return?**

What it deliberately does not do
--------------------------------
* It does **not** reimplement the score. It builds the cross-section in the
  production row shape and calls ``pipeline.theme_manager.theme_score`` — the
  same function the trading path calls, with the frozen version's parameters.
  If that function's contract changes, this script breaks loudly instead of
  drifting quietly.
* It does **not** replay the LLM's pick generation. That is not reproducible
  (the prompt and the accumulated memory are gone), so this measures the
  price-and-flow judgement *around* the picks, not the picks themselves.
* It is **development evidence**. §12 says so explicitly: historical replay may
  inform development, it never authorises a promotion.

Two measurement rules this file takes from the repository's evidence section
--------------------------------------------------------------------------
**Measure against the cross-section, not against zero.** In a rising half-year
every board has a positive forward return and the report says "the gate works"
about the calendar. The primary metric here is therefore the board's forward
return **minus that date's cross-sectional median** — the same discipline as
"中位数只与中位数比".

**Average per date, never pool.** A date with 550 boards must not outvote one
with 300, or the answer becomes a statement about board counts. Both readings
below compute a per-date statistic and then average the dates, and both report
how many dates each figure rests on.

The one honest limitation to keep in view
-----------------------------------------
``theme_score`` mixes three terms, and the third is
``w_confirm * clamp(strength / 10)``. ``strength`` counts how many sessions a
theme line has been confirmed — a live counter with no history. With it forced
to 0 the confirm term contributes zero for every board, so the **ranking** is
unaffected (that term is constant across boards either way) while the **level**
is understated by up to ``w_confirm`` (0.20 by default). The frozen
``admit_score`` / ``cancel_score`` thresholds therefore do not mean here what
they mean live, and the band block is reported as a shape rather than a verdict.

Usage
-----
    .venv/bin/python scripts/evaluate_entry_path.py --start 2025-01-01
    .venv/bin/python scripts/evaluate_entry_path.py --metric raw   # for contrast
    .venv/bin/python scripts/evaluate_entry_path.py --dates-step 5   # cost control
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_agents.data import policy_registry  # noqa: E402
from alpha_agents.data import scoring  # noqa: E402
from alpha_agents.pipeline import theme_manager  # noqa: E402

logger = logging.getLogger("entry_path")

#: The sibling research checkout that already syncs East-money board flow.
DEFAULT_TUSHARE_DB = Path.home() / "Projects" / "alphaquant" / "data" / "tushare.db"

#: `theme_cross_section` feeds concepts and industries into one ranking, and a
#: percentile is taken over whatever frame it is given. 地域 boards are in the
#: table but not in that frame, so including them would shift every percentile.
_KEPT_CONTENT = {"概念": "concept", "行业": "industry"}

#: ``--metric`` names the *reading*; this says which panel column it selects.
#: Kept at module scope so a test can assert every choice maps to a column the
#: pipeline actually produces — the first version of this passed the flag
#: straight to pandas and crashed with `KeyError: ['raw']`.
_METRIC_COLUMNS = {"excess": "excess", "raw": "fwd"}

_YUAN_PER_YI = 1e8


def load_board_flow(db: Path, start: str, end: str) -> pd.DataFrame:
    """Board-day flow, in the units ``theme_score`` expects.

    ``net_amount`` is yuan and ``pct_change`` is already a percent; the
    production frame reports 净额 in 亿元, so the conversion happens here
    rather than being left as a silent unit mismatch.
    """
    if not db.exists():
        raise FileNotFoundError(
            f"Board-flow database not found at {db}. It is an external input, "
            "not part of this repository — point --tushare-db at the alphaquant "
            "checkout that syncs it, or skip this evaluation."
        )
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        frame = pd.read_sql_query(
            "SELECT ts_code, name, trade_date, content_type, pct_change, "
            "       net_amount, close "
            "FROM moneyflow_concept_dc "
            "WHERE trade_date BETWEEN ? AND ? AND net_amount IS NOT NULL "
            "  AND close IS NOT NULL",
            con, params=(start.replace("-", ""), end.replace("-", "")),
        )
    finally:
        con.close()
    frame["scope"] = frame["content_type"].map(_KEPT_CONTENT)
    frame = frame[frame["scope"].notna()].copy()
    frame["net_flow_yi"] = frame["net_amount"].astype(float) / _YUAN_PER_YI
    frame["change_pct"] = frame["pct_change"].astype(float)
    frame["date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
    frame = frame.rename(columns={"name": "board"})
    frame = frame.sort_values(["ts_code", "date"]).reset_index(drop=True)
    logger.info("loaded %d board-day rows over %d board(s), %s → %s",
                len(frame), frame["ts_code"].nunique(),
                frame["date"].min().date(), frame["date"].max().date())
    return frame


def cross_section_for(day: pd.DataFrame) -> list[dict]:
    """One day's boards in the production row shape.

    Keys and units mirror ``tools.sector_ranking.theme_cross_section`` —
    ``concept`` / ``scope`` / ``change_pct`` / ``net_flow_yi`` — because
    ``theme_score`` matches on that shape. The test in
    ``tests/test_entry_path_panel.py`` feeds this output straight into the
    production function, so a rename on either side fails rather than
    silently scoring nothing.
    """
    return [{
        "concept": str(row.board),
        "scope": row.scope,
        "change_pct": float(row.change_pct),
        "net_flow_yi": float(row.net_flow_yi),
    } for row in day.itertuples()]


def add_forward_returns(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Board forward return over ``horizon`` **trading days**, plus its excess.

    The shift runs inside each board's own series, so a halted board does not
    borrow the next board's rows. Rows without a full window keep ``NaN`` and
    are dropped downstream rather than filled — an absent future is not a zero.

    ``excess`` is the forward return minus that date's cross-sectional median
    over the boards in the panel. Without it a rising market reports itself as
    a working gate.
    """
    out = frame.copy()
    out["fwd"] = out.groupby("ts_code")["close"].transform(
        lambda s: s.shift(-horizon) / s - 1.0)
    out["excess"] = (out["fwd"]
                     - out.groupby("date")["fwd"].transform("median"))
    return out


def score_day(day: pd.DataFrame, gate: dict) -> pd.DataFrame:
    """Score every board on one date using the production scorer."""
    rows = cross_section_for(day)
    scored = []
    for row in day.itertuples():
        got = theme_manager.theme_score(rows, str(row.board), 0, gate=gate)
        if got is None:  # the board frame cannot name this board
            continue
        scored.append({
            "ts_code": row.ts_code, "board": str(row.board), "scope": row.scope,
            "date": row.date, "fwd": row.fwd, "excess": row.excess,
            "score": got["score"], "flow_pct": got["flow_pct"],
            "rel_pct": got["rel_pct"],
        })
    return pd.DataFrame(scored)


def build_panel(frame: pd.DataFrame, gate: dict, dates_step: int) -> pd.DataFrame:
    dates = sorted(frame["date"].unique())[::dates_step]
    by_date = dict(tuple(frame.groupby("date")))
    parts = []
    for i, date in enumerate(dates, 1):
        part = score_day(by_date[date], gate)
        if not part.empty:
            parts.append(part)
        if i % 20 == 0:
            logger.info("scored %d/%d date(s)", i, len(dates))
    panel = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if not panel.empty:
        logger.info("panel: %d board-day row(s) over %d date(s)",
                    len(panel), panel["date"].nunique())
    return panel


def _per_date_median(panel: pd.DataFrame, mask, value_col: str) -> pd.Series:
    """Median of ``value_col`` per date, so dates weigh equally."""
    return panel.loc[mask].groupby("date")[value_col].median()


def quantile_separation(panel: pd.DataFrame, value_col: str = "excess",
                        buckets: int = 5) -> pd.DataFrame:
    """Median ``value_col`` per score bucket, per date, then averaged.

    Quintiles are cut inside each date: a board's score is only meaningful
    against the frame it was scored in, and that frame changes daily.
    """
    usable = panel.dropna(subset=[value_col]).copy()
    if usable.empty:
        return pd.DataFrame()
    usable = usable[usable.groupby("date")["score"].transform("nunique")
                    >= buckets]
    if usable.empty:
        # No date had enough boards to cut into quintiles. Returning an
        # all-NaN frame instead would push the "is there anything here"
        # decision onto every caller, and one of them would get it wrong.
        return pd.DataFrame()
    usable["bucket"] = usable.groupby("date")["score"].transform(
        lambda s: pd.qcut(s.rank(method="first"), buckets, labels=False))
    usable = usable.dropna(subset=["bucket"])
    per_date = usable.groupby(["date", "bucket"])[value_col].median().unstack("bucket")
    return pd.DataFrame({
        "bucket": range(buckets),
        "median": [float(per_date[b].mean()) if b in per_date else float("nan")
                   for b in range(buckets)],
        "n_dates": [int(per_date[b].notna().sum()) if b in per_date else 0
                    for b in range(buckets)],
    })


def band_comparison(panel: pd.DataFrame, gate: dict,
                    value_col: str = "excess") -> dict:
    """Admitted / in-band / rejected board-days, per date then averaged.

    Read the *direction*, not the magnitude: the confirm term is missing here
    (see the module docstring), so both sides are shifted and the counts below
    are not what the live gate would produce. Counts are reported because a
    per-date median over three dates is not evidence, whatever it equals.
    """
    usable = panel.dropna(subset=[value_col])
    if usable.empty:
        return {}
    score = usable["score"]
    groups = (("above_admit", score >= gate["admit_score"]),
              ("band", (score >= gate["cancel_score"])
               & (score < gate["admit_score"])),
              ("below_cancel", score < gate["cancel_score"]))
    out = {"n_total": int(len(usable)),
           "n_dates": int(usable["date"].nunique())}
    for label, mask in groups:
        per_date = _per_date_median(usable, mask, value_col)
        out[f"median_{label}"] = (float(per_date.mean())
                                  if per_date.notna().any() else float("nan"))
        out[f"n_{label}"] = int(mask.sum())
        out[f"dates_{label}"] = int(per_date.notna().sum())
    if out["dates_above_admit"] and out["dates_below_cancel"]:
        out["gap_above_minus_below"] = (out["median_above_admit"]
                                        - out["median_below_cancel"])
    return out


def report(panel: pd.DataFrame, gate: dict, horizon: int, seps: pd.DataFrame,
           value_col: str) -> None:
    label = ("excess vs that date's board median" if value_col == "excess"
             else "raw forward return")
    print()
    print("=" * 70)
    print(f"entry-path evaluation — horizon {horizon} trading day(s), "
          f"metric: {label}")
    print("=" * 70)
    if panel.empty:
        print("panel is empty — nothing to report.")
        return
    print(f"panel: {len(panel)} board-day row(s) over "
          f"{panel['date'].nunique()} date(s), "
          f"{panel['ts_code'].nunique()} board(s)")
    print(f"window: {panel['date'].min().date()} → {panel['date'].max().date()}")
    print()
    if not seps.empty:
        print("score quintile → median outcome (per date, then averaged)")
        for row in seps.itertuples():
            print(f"  Q{row.bucket + 1}  {row.median:+.4%}   ({row.n_dates} date(s))")
        if seps["median"].notna().all():
            print(f"  Q5 − Q1 = {seps['median'].iloc[-1] - seps['median'].iloc[0]:+.4%}")
            slope = seps["median"].iloc[-1] - seps["median"].iloc[0]
            print(f"  reading: {'higher score → better outcome' if slope > 0 else 'higher score → WORSE outcome (inverted)'}")
    print()
    band = band_comparison(panel, gate, value_col)
    if band:
        print("frozen-threshold band (levels shifted — see caveat 1)")
        for key, name in (("above_admit", f"score >= {gate['admit_score']}"),
                          ("band", f"{gate['cancel_score']} <= score < {gate['admit_score']}"),
                          ("below_cancel", f"score < {gate['cancel_score']}")):
            print(f"  {name:24s} n={band[f'n_{key}']:<6} "
                  f"dates={band[f'dates_{key}']:<4} "
                  f"median {band[f'median_{key}']:+.4%}")
        if "gap_above_minus_below" in band:
            print(f"  above − below = {band['gap_above_minus_below']:+.4%}")
    print()
    print("caveats — all three matter when reading the numbers above")
    print("  1. `strength` has no history, so the confirm term is 0: the ranking")
    print("     is faithful, the absolute levels are not. Treat the band block as")
    print("     a shape, not as a count of what the live gate would admit.")
    print("  2. This is the price-and-flow judgement around the picks, not a")
    print("     replay of the LLM that produces them — that is not reproducible.")
    print("  3. Development evidence only. Promotion evidence is forward, and")
    print("     `paired_count` requires every sample to postdate the freeze.")
    print()


def gate_parameters(version: int) -> dict:
    """The frozen version's theme-gate parameters, or the code defaults."""
    gate = dict(scoring.DEFAULT_DECISION_PARAMS["theme_gate"])
    stored = (policy_registry.sources_of(version) or {}).get(
        policy_registry.SOURCE_DECISION)
    if isinstance(stored, dict) and isinstance(stored.get("theme_gate"), dict):
        logger.info("gate parameters taken from frozen version #%d", version)
        return dict(stored["theme_gate"])
    logger.warning("version #%d declares no theme_gate; using code defaults",
                   version)
    return gate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tushare-db", type=Path, default=DEFAULT_TUSHARE_DB)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default=None, help="default: today.")
    parser.add_argument("--horizon", type=int, default=5,
                        help="forward return in trading days.")
    parser.add_argument("--version", type=int, default=2,
                        help="frozen policy version whose decision parameters to "
                             "use (default: the one frozen on 2026-09-15).")
    parser.add_argument("--dates-step", type=int, default=1,
                        help="score every Nth trading date (cost control).")
    parser.add_argument("--metric", choices=("excess", "raw"), default="excess",
                        help="excess (default) subtracts that date's board "
                             "median; raw reports the unadjusted return.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    gate = gate_parameters(args.version)
    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    frame = load_board_flow(args.tushare_db, args.start, end)
    if frame.empty:
        logger.error("no board flow in %s → %s", args.start, end)
        return 1

    frame = add_forward_returns(frame, args.horizon)
    panel = build_panel(frame, gate, args.dates_step)
    # The flag names the *reading*; the column it selects is fwd or excess.
    value_col = _METRIC_COLUMNS[args.metric]
    seps = (quantile_separation(panel, value_col) if not panel.empty
            else pd.DataFrame())
    report(panel, gate, args.horizon, seps, value_col)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
