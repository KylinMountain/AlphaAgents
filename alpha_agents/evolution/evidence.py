"""The Evidence Analyzer: turn a set of closed trades into a cited candidate.

This is the LEARN half of the loop, and until now it did not exist as a
component. The logic that splits observations into *supporting* and
*opposing* evidence lived as roughly sixty lines inside
``scripts/walk_forward.py`` — a script, outside the package, outside the
layer rules, and unreachable from anything that is not that one runner. The
diagram the operator works from has an **Evidence Analyzer** box between the
Episode and the Candidate, and the code had no such thing.

What it does, in one sentence: given trades that have already happened, it
states one falsifiable proposition, decides which trades support it and
which oppose it, and writes both lists down — including when one is empty.

Three rules it holds itself to, each of which has already been broken once
in this repository:

**Utility comes from market data only.** No model is asked what it thinks of
its own output. The proposition is a comparison of medians, the feature is
re-read from the price corpus, and the verdict per trade is arithmetic. The
GOLDEN_PRINCIPLES rule is enforced by construction here rather than promised.

**Compare median to median.** ``AGENTS.md`` records that a group median
against a market mean manufactured a fake edge in this repository once
already. Every summary number below is a median, and the per-trade test
compares each trade's return against the *window's median return* rather
than against zero. The first version tested against zero, and on a window
where every trade lost money the test collapsed into the grouping variable
itself — the support count *was* the group size, an arithmetic identity that
cannot disagree with the medians printed beside it.

**An empty list is a finding, not an absence.** ``save_candidate`` refuses a
citation dict with a missing key, so "we searched and found no opposing
evidence" is tellable from "nobody looked". That is why both lists are always
written, and why the analyzer returns a structured result rather than a
candidate id alone: a caller that cannot see the split cannot report it.

The analyzer stops at ``observation``. It writes a candidate and never
advances its status — promotion is an audited human act, and
``learning_candidates``' own design note says the pipeline must not drive
``status``. See ``docs/TRADER_CORE_DESIGN.md`` §8.2.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Below this many usable trades the analyzer refuses to state anything.
#:
#: Not the promotion floor (20 paired samples, ``GOLDEN_PRINCIPLES`` §7). This
#: is the floor for *recording an observation at all*, and the write lands in
#: the ``observation`` state whose whole purpose is to quarantine evidence
#: this thin. Raising it to the promotion floor would mean a window this size
#: records nothing — which is how "we learned nothing" gets mistaken for
#: "nothing happened". Those are different facts and this constant keeps them
#: apart.
MIN_TRADES = 4

#: The proposition every observation states, as the analyzer's own hypothesis.
#:
#: It is the decider's assumption inverted, which is what makes it worth
#: stating: the T+1 decider *ranks* on the previous session's change, and this
#: proposition is that ranking is backwards. A claim that agreed with the
#: ranking would be unfalsifiable by the ranking's own results.
PROPOSITION = "T-1 涨得更多的候选，实际收益更差"

#: The behaviour change the proposition implies, if it holds on a larger
#: sample. Stated as a delta rather than an instruction: this is a proposal,
#: and nothing here applies it.
PROPOSED_DELTA = {
    "field": "t1_change_rank",
    "direction": "down",
    "note": "若该观察在更大样本上成立，选股应向 T-1 涨幅更低的一端移动",
}


@dataclass(frozen=True)
class Trade:
    """One closed trade, as the analyzer sees it.

    ``episode_id`` is the join that makes a citation possible at all. A trade
    with no episode still counts towards ``n`` — its return is a fact — but it
    cannot be cited, so it is excluded from the split rather than silently
    cited as something it is not.
    """

    position_id: int
    code: str
    return_pct: float
    close_date: str
    t1_change: float | None
    episode_id: int | None = None
    close_reason: str | None = None


@dataclass(frozen=True)
class EvidenceBundle:
    """What was searched, by what rule, and what was left out.

    Tech-debt D25's requirement, and the reason a guard on the citation list
    alone is not enough: ``supporting = [three ids picked by hand],
    opposing = []`` is schema-legal and proves nothing about whether a
    counter-example search ever ran. The bundle makes the search *declared*
    and *replayable*:

    * ``search_scope`` names the population that was searched, in words a
      reader can check against the query;
    * ``matching_rule`` is the rule that decided each trade's side, named so
      ``replay_bundle`` can re-apply it;
    * ``eligible`` / ``excluded`` (with reasons) say what was judged and what
      was dropped — so a thin result is visibly thin rather than silently
      small;
    * ``cutoff`` is the date the evidence ends, which is what a forward-only
      evaluator compares an evaluation window against.

    An empty ``opposing`` bucket under a bundle means "searched the declared
    set and found none". Without one it means nothing at all.
    """

    search_scope: str
    matching_rule: str
    eligible: int
    excluded: int
    excluded_reasons: dict
    cutoff: str

    def as_dict(self) -> dict:
        return {
            "search_scope": self.search_scope,
            "matching_rule": self.matching_rule,
            "eligible": self.eligible,
            "excluded": self.excluded,
            "excluded_reasons": dict(self.excluded_reasons),
            "cutoff": self.cutoff,
        }


#: The name ``replay_bundle`` re-applies. Kept as a constant because a rule
#: the bundle names and a rule the replayer knows are the same string or the
#: check is theatre.
RULE_T1_VS_MEDIAN = "t1_change_above_cut XOR return_above_window_median"


@dataclass
class Evidence:
    """The analyzer's verdict: a claim, its split, and the numbers behind it.

    ``supporting`` and ``opposing`` are episode ids, sorted. Both are always
    present — an empty list means the search ran and found nothing, which is
    a different fact from a list that was never built.
    """

    claim: str
    n: int
    cut: float
    median_return: float
    high_median: float
    low_median: float
    supporting: list[int] = field(default_factory=list)
    opposing: list[int] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    bundle: EvidenceBundle | None = None

    @property
    def contrast(self) -> float:
        """High-T-1 median minus low-T-1 median.

        Negative supports the proposition. Reported as a number rather than a
        sentence so a caller can sort or threshold on it without parsing the
        claim.
        """
        return self.high_median - self.low_median

    def payload(self) -> dict:
        """The evidence, as the candidate's ``payload``.

        Every per-trade verdict is carried, so the two counts in the claim can
        be re-derived from the record alone instead of taken on trust. A
        reader who doubts "3 支持 / 1 反对" can check it here without knowing
        the rule that produced it. The bundle is carried so the *search* can
        be replayed, which is the stronger claim.
        """
        out = {
            "n": self.n,
            "cut_pct": round(self.cut, 4),
            "median_return_pct": round(self.median_return, 4),
            "high_median_return_pct": round(self.high_median, 4),
            "low_median_return_pct": round(self.low_median, 4),
            "contrast_pct": round(self.contrast, 4),
            "trades": self.trades,
        }
        if self.bundle is not None:
            out["evidence_bundle"] = self.bundle.as_dict()
        return out


def _median(values: list[float]) -> float:
    """The middle value; the mean of the two middles when the count is even.

    A median and not a mean, for the reason ``AGENTS.md`` gives: A-share
    cross-sections are right-skewed, and a mean over a handful of trades is
    one limit-up away from any number at all. ``statistics.median`` does the
    even-count averaging; the wrapper exists so the rule is stated once and
    every summary number in this module goes through it.
    """
    return float(statistics.median(values))


def analyse(trades: list[Trade]) -> Evidence | None:
    """State the proposition over ``trades``, or ``None`` if it cannot be stated.

    Returns ``None`` — never a hedged or empty claim — in three cases, each
    of which would otherwise produce a sentence that looks like evidence:

    * fewer than :data:`MIN_TRADES` usable trades (too thin to state);
    * every trade on one side of the cut (no contrast, and a claim with no
      contrast cannot be wrong, which is not a virtue);
    * the trade list is empty.

    The caller decides what to do with ``None``. The analyzer does not invent
    a weaker proposition to avoid returning it.
    """
    scored = [t for t in trades
              if t.t1_change is not None and t.episode_id is not None]
    if len(scored) < MIN_TRADES:
        return None

    cut = _median([t.t1_change for t in scored])
    high = [t for t in scored if t.t1_change > cut]
    low = [t for t in scored if t.t1_change <= cut]
    if not high or not low:
        return None

    median_return = _median([t.return_pct for t in scored])

    def _supports(t: Trade) -> bool:
        """True when this trade's outcome is the opposite of its ranking.

        A name up more than typical on T-1 that returned less than the
        window's typical result supports the proposition, and so does a name
        up less that returned more. The two mirror images are the opposition.

        Compared against the window's median return, **not** zero: on a
        window where every trade lost money, ``return_pct > 0`` is False for
        all of them and the test collapses into ``t1 > cut``, which makes the
        support count an identity rather than evidence.
        """
        return (t.t1_change > cut) != (t.return_pct > median_return)

    supporting = sorted(t.episode_id for t in scored if _supports(t))
    opposing = sorted(t.episode_id for t in scored if not _supports(t))

    high_median = _median([t.return_pct for t in high])
    low_median = _median([t.return_pct for t in low])

    # What was searched and what was left out. The excluded counts are the
    # half that makes a thin result visible: a candidate citing two episodes
    # out of forty closed trades is a different claim from one citing two out
    # of two, and without this the two look identical afterwards.
    excluded_reasons: dict[str, int] = {}
    for t in trades:
        if t.t1_change is None:
            excluded_reasons["no T-1 bar in the corpus"] = \
                excluded_reasons.get("no T-1 bar in the corpus", 0) + 1
        elif t.episode_id is None:
            excluded_reasons["closed position with no episode to cite"] = \
                excluded_reasons.get("closed position with no episode to cite", 0) + 1
    cutoff = max((t.close_date for t in scored), default="")

    claim = (
        f"本窗口已平仓 {len(scored)} 笔中，T-1 涨幅高于中位数（{cut:+.2f}%）的 "
        f"{len(high)} 笔中位收益 {high_median:+.2f}%，低于或等于的 {len(low)} 笔"
        f"中位收益 {low_median:+.2f}%（全窗口中位收益 {median_return:+.2f}%）"
        f"——「{PROPOSITION}」逐笔看有 {len(supporting)} 笔支持、"
        f"{len(opposing)} 笔反对（n={len(scored)}，远低于 n≥50，仅为观察）")

    bundle = EvidenceBundle(
        search_scope=(
            f"every position closed on or before {cutoff} whose return is "
            f"recorded ({len(trades)} row(s) read, {len(scored)} citable)"),
        matching_rule=RULE_T1_VS_MEDIAN,
        eligible=len(scored),
        excluded=len(trades) - len(scored),
        excluded_reasons=excluded_reasons,
        cutoff=cutoff,
    )

    return Evidence(
        claim=claim,
        n=len(scored),
        cut=cut,
        median_return=median_return,
        high_median=high_median,
        low_median=low_median,
        supporting=supporting,
        opposing=opposing,
        trades=[
            {"episode_id": t.episode_id, "code": t.code,
             "t1_change_pct": round(t.t1_change, 4),
             "return_pct": round(t.return_pct, 4),
             "supports": _supports(t),
             "close_reason": t.close_reason}
            for t in scored
        ],
        bundle=bundle,
    )


def replay_bundle(bundle: dict, trades: list[Trade]) -> dict:
    """Re-apply the bundle's declared search and check its counts.

    D25: "an empty opposing bucket means *searched the declared set and found
    none*, and the evaluator replays the declared search to check the counts."
    A declared scope that the data does not support is worse than no scope,
    because it reads as diligence.

    Returns ``{"ok": bool, "problems": [...]}`` rather than raising: the
    caller is usually an evaluator deciding whether to trust a candidate, and
    it needs the reasons, not an exception.
    """
    problems: list[str] = []
    rule = bundle.get("matching_rule")
    if rule != RULE_T1_VS_MEDIAN:
        problems.append(
            f"the bundle names matching_rule {rule!r}, which this replayer "
            f"does not know; it can only replay {RULE_T1_VS_MEDIAN!r}")
        return {"ok": False, "problems": problems}

    fresh = analyse(trades)
    if fresh is None:
        problems.append(
            "replaying the declared search over the given trades yields no "
            "claim at all, so the bundle's counts cannot be reproduced")
        return {"ok": False, "problems": problems}

    if bundle.get("eligible") != len(fresh.trades):
        problems.append(
            f"bundle declares eligible={bundle.get('eligible')}, replay finds "
            f"{len(fresh.trades)}")
    declared_scope = str(bundle.get("search_scope") or "")
    if declared_scope and fresh.bundle.cutoff not in declared_scope:
        problems.append(
            f"the bundle's search_scope does not name its own cutoff "
            f"{fresh.bundle.cutoff!r}, so the scope cannot be reproduced")
    if bundle.get("cutoff") != fresh.bundle.cutoff:
        problems.append(
            f"bundle declares cutoff={bundle.get('cutoff')!r}, replay finds "
            f"{fresh.bundle.cutoff!r}")
    return {"ok": not problems, "problems": problems, "replayed": fresh}


def context_for(evidence: Evidence, *, trader: str, entry_zone: tuple,
                window_start: str) -> str:
    """The context the observation applies in, stated from the run's own facts.

    A candidate without a context cannot be checked later: "this held" is
    meaningless without "where". The window start is carried because the
    claim is about a window, not about the market in general.
    """
    return (f"{trader} 交易员，入口区间 {entry_zone[0]:.3f}–{entry_zone[1]:.3f} "
            f"× T-1 收盘，按 T-1 涨幅排序选股，窗口自 {window_start} 起")


def save_observation(evidence: Evidence, *, source: str, source_date: str,
                     applicable_context: str, operation: str = "create",
                     target_id: int | None = None) -> int:
    """Write the evidence as a ``learning_candidate``. Returns its id.

    The analyzer's only write. It goes through ``save_candidate`` — which
    refuses a citation dict with a missing key — so the empty-list-is-a-finding
    rule is enforced at the boundary rather than by this function remembering
    to state both lists.

    The status is whatever ``save_candidate`` defaults to, which is
    ``observation``. Nothing here advances it: see the module docstring.
    """
    from alpha_agents.data import learning_candidates as LC

    return LC.save_candidate(
        entity_type="principle",
        operation=operation,
        target_id=target_id,
        source=source,
        source_date=source_date,
        payload={"as_of": source_date, **evidence.payload()},
        claim=evidence.claim,
        applicable_context=applicable_context,
        proposed_behavior_delta=dict(PROPOSED_DELTA),
        # Both keys, always. An empty list is the statement "searched, found
        # none" and is the reason save_candidate refuses a missing key.
        evidence_episode_ids={
            "supporting": list(evidence.supporting),
            "opposing": list(evidence.opposing),
        },
    )


def analyse_and_save(trades: list[Trade], *, source: str, source_date: str,
                     trader: str, entry_zone: tuple, window_start: str,
                     operation: str = "create",
                     target_id: int | None = None) -> dict | None:
    """``analyse`` then ``save_observation``. ``None`` when too thin to state.

    The convenience the runner calls. Returns the same facts the candidate
    carries, so the caller can log them without re-deriving anything.
    """
    evidence = analyse(trades)
    if evidence is None:
        return None
    candidate_id = save_observation(
        evidence, source=source, source_date=source_date,
        applicable_context=context_for(evidence, trader=trader,
                                       entry_zone=entry_zone,
                                       window_start=window_start),
        operation=operation, target_id=target_id)
    return {
        "candidate_id": candidate_id,
        "n": evidence.n,
        "supporting": len(evidence.supporting),
        "opposing": len(evidence.opposing),
        "high_median": round(evidence.high_median, 4),
        "low_median": round(evidence.low_median, 4),
        "contrast": round(evidence.contrast, 4),
    }
