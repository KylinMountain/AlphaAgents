#!/usr/bin/env python3
"""Move the active policy, or refuse and say why. One entry point, audited.

§14 ends Phase 4 with "evidence controls future policy changes through one
audited entry point". This is that entry point: the only thing in the repo a
person runs to change which policy is in force. Every move goes through
``policy_registry.promote`` / ``rollback`` / ``install``, and all three are a
compare-and-swap on ``active_policy.version_seq`` — so a change that raced
another loses without writing rather than overwriting it.

Nine verbs, in the order a controlled change goes through them. The record
first (``freeze`` / ``install``), then the experiment (``shadow-open`` /
``shadow-emit`` / ``shadow-score`` / ``gate``), then the move
(``approve`` / ``promote``), with ``rollback`` kept for going back:

* ``freeze`` — record the configuration as it is right now. **Moves no
  pointer**, and needs no evidence: a frozen version is a name for behaviour,
  and nothing is in force until something puts it there. This is the
  ``draft → frozen`` of §11, and without it the registry had no way to get its
  first row — which is why every other verb here refuses with "no policy
  version #N" until it has been run once.
* ``install`` — put a version in force **where none is**. Refuses if the policy
  already has a pointer: from the second change onward the pointer may only
  move by promotion or rollback, both of which demand evidence, and an install
  that could be repeated would be a back door around exactly that.
* ``shadow-open`` — open an experiment measuring one frozen version. It writes
  a run row and nothing else; the challenger is graded, never traded.
* ``shadow-emit`` — write the challenger's forecasts for one date. The panel is
  the champion's own picks for that day, so this is run **after** the champion
  has forecast, or there is nothing to pair against.
* ``shadow-score`` — grade the challenger's forecasts whose window the market
  has closed. Re-runnable; it only fills in unscored rows.
* ``gate`` — ask for a verdict on a version and **record it**. §11: a
  successful automatic evaluation is not a licence to promote, so this writes
  evidence and moves nothing.
* ``approve`` — a person authorises a version, citing the gate verdict that
  justifies it. Writes one approval row. **Moves no pointer.**
* ``promote`` — move the pointer to an approved version. Refuses unless a
  person approved it *and* the live configuration still hashes to it.
* ``rollback`` — restore a version that was in force before. Needs no verdict
  (the evidence is the transition trail), and deletes nothing.

``status`` changes nothing and is the intended way to look before you leap: it
prints, among the rest, how many **paired samples** each experiment has, the
promotion floor the version in force declares, and the decision parameters in
force **as values** — because once a version is installed the code defaults stop
deciding anything, and an edit to them that has no effect must not look like an
edit that worked. A verdict asked for too early is refused rather than recorded,
so the progress meter is also how an operator knows when asking is worth it.

The unit word is load-bearing. The gate's floor counts **paired samples** — one
``(date, code)`` both sides scored — and this command used to print that number
as "paired day(s)", while the repository's own rule is stated in samples too
("n < 50 does not ship", golden principles §7). Three spellings of one
denominator is how an operator comes to believe an experiment is twenty days old
when it is twenty rows old, all of them from one morning.

"Changes nothing" means no pointer move and no record. Like every other entry
point it will create the registry's tables and the shadow tables if they do not
exist yet — that is one ``CREATE TABLE IF NOT EXISTS``, not a decision, and the
trading path triggers the same DDL on its first intent.

    uv run python scripts/policy.py status
    uv run python scripts/policy.py freeze --by kylin --reason "the seed"
    uv run python scripts/policy.py install --version 1 --by kylin --reason "nothing was in force"
    uv run python scripts/policy.py freeze --by kylin --reason "a bolder mapping" \\
        --decision-json '{"dim_step": 0.09}'
    uv run python scripts/policy.py shadow-open --version 2 --reason "measure it"
    uv run python scripts/policy.py shadow-emit --run 1 --date 2026-09-14
    uv run python scripts/policy.py shadow-score
    uv run python scripts/policy.py gate --version 2
    uv run python scripts/policy.py approve --version 2 --by kylin --reason "beat the champion"
    uv run python scripts/policy.py promote --version 2 --by kylin --reason "approved"

Exit 0 on success, 1 on refusal. A refusal prints which condition failed —
someone running this by hand needs the reason, not just the verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.config import MEMORY_DB_PATH  # noqa: E402
from alpha_agents.data import policy_registry as registry  # noqa: E402
from alpha_agents.data import scoring  # noqa: E402
from alpha_agents.evolution import holdout_gate, policy_sources, shadow  # noqa: E402


def _sources(version_id):
    """Stage the target's own pointer-controlled sources; all others stay live.

    Changing the pointer is what switches the pointer-controlled sources —
    knowledge, and the decision parameters the trading path reads. Comparing a
    candidate against the incumbent's values (or the newest unrelated approval)
    would make both promotion and rollback impossible for a change to either
    one, because the target would always hash to something other than itself.

    Everything else — prompts, model, retrieval budgets, the rule constants —
    is read from the running system, which is the point of the check: a prompt
    edited without opening a version has to fail here.
    """
    sources = policy_sources.collect_for_version(version_id)
    if sources is None:
        raise ValueError(f"No policy version #{version_id}")
    return sources


def _eligible_verdict(version_id: int, decision_id: int | None):
    """The verdict an approval would cite, or raise.

    Prefers an explicit ``--gate-decision``; otherwise takes the newest
    eligible verdict for the version. ``eligible_decisions`` already excludes
    abstentions and rejections, so "no eligible verdict" is the honest answer
    rather than "the newest row, whatever it says".
    """
    rows = holdout_gate.get_gate_decisions(policy_version_id=version_id)
    if decision_id is not None:
        for row in rows:
            if row["id"] == decision_id:
                return row
        raise ValueError(
            f"gate decision #{decision_id} is not a verdict about policy "
            f"version #{version_id}")
    eligible = holdout_gate.eligible_decisions(version_id)
    if not eligible:
        raise ValueError(
            f"policy version #{version_id} has no eligible gate verdict: "
            f"{len(rows)} verdict(s) on record, none of them a promote over "
            "at least one day of paired forward evidence. There is nothing "
            "for a person to approve — §16 forbids inventing the evidence.")
    return eligible[0]


def _staged_decision_params(raw: str | None) -> dict | None:
    """Parse ``--decision-json`` into the block a version will assert.

    None means "the parameters in force", which is what an ordinary freeze
    records. A block is what a *candidate* asserts, and it is merged over the
    parameters in force key by key: ``confidence_priors`` is replaced whole
    rather than merged into, so a version never depends on a prior it did not
    declare. Pass all of the priors you mean to change.
    """
    if raw is None:
        return None
    try:
        staged = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--decision-json is not valid JSON: {exc}") from exc
    if not isinstance(staged, dict):
        raise ValueError(
            "--decision-json must be a JSON object of decision parameters, "
            f"got {type(staged).__name__}")
    return staged


def _cmd_freeze(args) -> int:
    """Record the live configuration as a version. Moves nothing.

    The first row of the registry has to come from somewhere, and it cannot
    come from an approval or a promotion: both cite evidence about a version
    that has to exist already. This is the other direction — the configuration
    is read off the running system and named. It is deliberately allowed to
    produce a version nothing uses yet, because that is what a challenger is.
    """
    staged = _staged_decision_params(args.decision_json)
    before = {version["id"] for version in registry.versions_for(args.policy_key)}
    if args.dry_run:
        digest = registry.content_hash_for(
            policy_sources.collect(decision_params=staged))
        same = next((version["id"]
                     for version in registry.versions_for(args.policy_key)
                     if version["content_hash"] == digest), None)
        print(f"  content hash {digest[:16]}")
        print("  " + (f"already on record as version #{same} — freezing it "
                      "again would return that version unchanged."
                      if same else
                      "not on record; this would write a new version."))
        print("dry run: nothing written.")
        return 0

    version_id = policy_sources.freeze_live(
        created_by=args.by, reason=args.reason, policy_key=args.policy_key,
        parent_id=args.parent_version, decision_params=staged,
        frozen_at=args.at)
    version = registry.get_version(version_id)
    print(f"  version #{version_id} "
          + ("written." if version_id not in before
             else "was already on record; nothing new was written."))
    print(f"  frozen {version['frozen_at']} by {version['created_by']}: "
          f"{version['reason']}")
    print(f"  content hash {version['content_hash'][:16]}")
    if staged:
        print(f"  this version asserts: {json.dumps(staged, sort_keys=True)}")
    print("  the pointer did not move: nothing is in force until an install "
          "or a promotion says so.")
    return 0


def _cmd_install(args) -> int:
    """Put a version in force where none is. Refuses if one already is."""
    pointer = registry.active(args.policy_key)
    if args.dry_run:
        version = registry.get_version(args.version)
        if version is None:
            print(f"  refused: no policy version #{args.version} to install")
        elif pointer is None:
            print(f"  would put version #{args.version} "
                  f"({version['content_hash'][:12]}) in force at seq 1")
        else:
            print(f"  refused: policy {args.policy_key!r} already has version "
                  f"#{pointer['version_id']} in force at seq "
                  f"{pointer['version_seq']}. Moving it is a promotion or a "
                  "rollback, both of which require evidence.")
        print("dry run: nothing written.")
        return 0

    seq = registry.install(version_id=args.version, actor=args.by,
                           reason=args.reason, policy_key=args.policy_key,
                           at=args.at)
    print(f"policy {args.policy_key!r} now has version #{args.version} in "
          f"force at seq {seq}. This is the first version: from here the only "
          "way to move the pointer is a promotion or a rollback.")
    return 0


def _in_force_note(producer_name: str) -> str:
    """What it means to shadow the version that is in force — as one message.

    The warning this replaces fired for every producer, and it is true for only
    one of them: ``remap_confidence`` reads the version's parameters, so
    shadowing the incumbent hands the challenger the champion's own mapping and
    the experiment compares a policy with itself. A baseline producer emits one
    constant and reads none of them, so the same setup compares the champion
    against no skill — a real measurement, just not a promotable one. A warning
    that fires where it does not apply is how the one that does gets scrolled
    past.

    An unregistered name falls to the warning rather than to a refusal here:
    the refusal belongs to ``shadow.open_run``, and a second implementation of
    it is how the two come to disagree.
    """
    producer = shadow.PRODUCERS.get(producer_name)
    if producer is not None and producer.kind == shadow.KIND_BASELINE:
        return (f"  note: this is the version in force, but {producer_name!r} "
                "is a baseline producer — it emits a fixed forecast and reads "
                "none of the version's parameters, so this measures the "
                "champion against no skill rather than against itself. Its "
                "verdict carries baseline scope and cannot move the pointer.")
    return ("  WARNING: this is the version in force, so the challenger would "
            "apply the same parameters as the champion. Measure a *different* "
            "version or the experiment compares a policy with itself.")


def _cmd_shadow_open(args) -> int:
    """Open an experiment measuring one frozen version. Moves nothing.

    The dry run prints the *facts* the refusal would rest on rather than
    re-deciding it: whether the version exists, how many runs are already open
    on it, and whether it is the version in force. It says "would", and the
    refusal itself is still decided by ``shadow.open_run`` when the command is
    run for real — two implementations of one rule is how they come to
    disagree.
    """
    if args.dry_run:
        version = registry.get_version(args.version)
        if version is None:
            print(f"  refused: no policy version #{args.version} to shadow — a "
                  "run that cannot name the policy it measures could never be "
                  "promoted")
        else:
            open_now = shadow.open_runs_for(args.version, args.report_type)
            pointer = registry.active(args.policy_key)
            print(f"  would open a {args.report_type!r} run on version "
                  f"#{args.version} as producer {args.producer!r}")
            print(f"  {len(open_now)} run(s) already open on that version; a "
                  "second open shadow of one version would be refused, because "
                  "the two would be counted as one experiment")
            if pointer and pointer["version_id"] == args.version:
                print(_in_force_note(args.producer))
        print("dry run: nothing written.")
        return 0

    run_id = shadow.open_run(policy_version_id=args.version, reason=args.reason,
                             report_type=args.report_type,
                             producer=args.producer, opened_at=args.at)
    run = shadow.get_run(run_id)
    print(f"  shadow run #{run_id} opened: policy version #{run['policy_version_id']} "
          f"via producer {run['producer']!r} on {run['report_type']!r}, "
          f"shadow trader {run['trader_id']!r}")
    print("  it emits forecasts and nothing else — no order, no position, no "
          "intent. Next: shadow-emit once the champion has picks for the day.")
    return 0


def _cmd_shadow_close(args) -> int:
    """End a shadow run. Its forecasts and scores stay readable.

    The operator half of ``shadow.close_run``, which until now had no caller
    at all — the same "declared but unreachable" shape as D31. Without it an
    experiment opened on the wrong book could not be retired, and
    ``open_run`` refuses a second open run for the same
    (version, report type, producer), so the version was blocked from being
    measured correctly for as long as the mistake stood.
    """
    run = shadow.get_run(args.run)
    if run is None:
        raise ValueError(f"No shadow run #{args.run} to close")
    if run["status"] != "open":
        print(f"  run #{args.run} is already {run['status']}; nothing to do.")
        return 0
    if args.dry_run:
        coverage = shadow.coverage(args.run)["runs"]
        paired = coverage[0]["paired"] if coverage else 0
        print(f"  would close run #{args.run} "
              f"({run['report_type']!r} / {run['producer']!r}), "
              f"which has {paired} paired sample(s)")
        print("  its forecasts and scores stay readable; only the status moves.")
        print("dry run: nothing written.")
        return 0
    shadow.close_run(args.run, reason=args.reason, closed_at=args.at)
    print(f"  run #{args.run} closed. Its forecasts and scores remain "
          "readable — closing ends the experiment, it does not erase it.")
    return 0


def _cmd_shadow_emit(args) -> int:
    """Write the challenger's forecasts for one date.

    The panel is the champion's codes for that date and report type, so this is
    run *after* the champion has forecast. An empty panel is reported as such
    rather than as success: zero forecasts written looks identical to a
    successful quiet day otherwise, and the difference is the whole experiment.
    """
    run = shadow.get_run(args.run)
    if run is None:
        raise ValueError(f"No shadow run #{args.run} to emit for")

    if args.dry_run:
        panel = shadow.panel_for(args.date, run["report_type"])
        print(f"  the panel is the champion's {run['report_type']!r} codes for "
              f"{args.date}: {len(panel)} code(s)")
        if panel:
            print("    " + ", ".join(panel))
        else:
            print("  the champion has no forecast for that date and report "
                  "type, so the panel is empty and there would be nothing to "
                  "pair against. Emit after the champion's picks exist.")
        print("dry run: nothing written.")
        return 0

    ids = shadow.emit_for_date(args.run, args.date, horizon_days=args.horizon)
    if args.horizon is None:
        # Say what was actually used, per code, rather than a number this
        # command no longer chooses.
        horizons = sorted({r["horizon_days"] for r in
                           shadow.predictions_for(args.run)
                           if r["date"] == args.date})
        described = ", ".join(f"{h} 天" for h in horizons) or "无"
        print(f"  run #{args.run} wrote {len(ids)} forecast(s) for {args.date}，"
              f"horizon 继承冠军声明：{described}")
    else:
        print(f"  run #{args.run} wrote {len(ids)} forecast(s) for {args.date} "
              f"at an overridden {args.horizon}-day horizon — this differs "
              "from the champion's declaration wherever they disagree")
    if not ids:
        print("  the panel was empty: the champion has no forecast for that "
              "date and report type. This is not a failure — it is the "
              "experiment having nothing to measure that day.")
    return 0


def _cmd_shadow_score(args) -> int:
    """Grade the challenger's forecasts whose window the market has closed.

    No dry run: it cannot write anything except a score for a forecast that
    already exists, and re-running it changes nothing. The three counts are
    kept apart because they are three different facts — see
    ``shadow.score_due``.
    """
    if args.dry_run:
        print("dry run: this verb has none. It writes only scores for "
              "forecasts that already exist, is idempotent, and touches "
              "neither the policy record nor the books.")
        return 0
    result = shadow.score_due(as_of=args.at, run_id=args.run)
    print(f"  as of {result['as_of']}: {result['graded']} graded, "
          f"{result['deferred']} window still open, "
          f"{result['unscorable']} unscorable")
    print("  'window still open' means the market has not traded the horizon "
          "shut yet; those rows stay unscored and are re-checked next run.")
    return 0


def _cmd_gate(args) -> int:
    """Ask for a verdict on one version, and record what it says.

    No dry run, on purpose: there is no way to preview the answer without
    re-deciding eligibility outside the gate, and a second implementation of
    that rule is how the two come to disagree. ``status`` is the way to look
    first — it prints how many paired samples the experiment has, against what
    the gate needs, and it reports ``n`` and ``validation_days`` separately for
    the same reason this print does.
    """
    decision = holdout_gate.run_gate(args.version,
                                     report_type=args.report_type,
                                     today=args.today)
    print(f"  verdict recorded: {decision['outcome']} "
          f"over {decision['validation_days']} validation day(s), "
          f"n={decision['n']}")
    print(f"  {decision['reason']}")
    print(f"  evidence scope {decision['evidence_scope']!r} "
          f"(run #{decision['run_id']})")
    if decision["outcome"] == "promote":
        print("  this is promotable evidence. The pointer did not move: "
              "approve is a separate act by a person.")
    else:
        print("  the pointer did not move, and this verdict cannot move it.")
    return 0


def _cmd_variant_build(args) -> int:
    """Build a policy variant from a candidate. Moves nothing.

    The operator half of ``evolution.variant.build_variant``. Without a verb
    here the module would be the "declared but unreachable" shape D31 records:
    a capability with tests and no caller, which reads as finished work until
    someone tries to use it.

    It writes a *frozen version* and archives the proposed configuration. It
    does not install, approve or promote anything — the pointer moves only by
    ``promote``, and only after a person approves, which is a separate act.
    """
    from alpha_agents.evolution import variant as V

    if args.dry_run:
        try:
            candidate = _preview_variant(args)
        except V.VariantError as e:
            print(f"  refused: {e}")
            print("dry run: nothing written.")
            return 1
        print(f"  would build a variant of version #{args.parent} from "
              f"candidate #{args.candidate}")
        for change in candidate["changes"]:
            print(f"    {change['block']}.{change['param']} "
                  f"{change['step']:+.3f} ({change['field']}/{change['direction']})")
        print(f"    evidence: {candidate['supporting']} supporting, "
              f"{candidate['opposing']} opposing")
        print("dry run: nothing written.")
        return 0

    try:
        built = V.build_variant(
            args.candidate, parent_version_id=args.parent,
            built_by=args.by, reason=args.reason)
    except V.VariantError as e:
        # A refusal is an answer about the candidate, not a crash: the operator
        # needs to read why, and it exits non-zero so a script notices.
        print(f"  refused: {e}")
        return 1
    print(f"  variant built: policy version #{built.version_id} "
          f"(parent #{built.parent_version_id}) from candidate "
          f"#{built.candidate_id}")
    for change in built.changes:
        print(f"    {change['block']}.{change['param']} "
              f"{change['step']:+.3f} ({change['field']}/{change['direction']})")
    if built.path:
        print(f"    archived: {built.path}")
    print("  it is frozen, not in force. The pointer did not move: it moves "
          "only by promote, after a person approves.")
    return 0


def _preview_variant(args) -> dict:
    """What ``variant-build`` would do, without writing the version.

    Checks the same guards in the same order by asking the module, rather than
    re-implementing them here: two implementations of one rule is how they come
    to disagree.
    """
    from alpha_agents.data import learning_candidates as LC
    from alpha_agents.evolution import variant as V

    candidate = LC.get_candidate(args.candidate)
    if candidate is None:
        raise V.VariantError(f"No learning candidate #{args.candidate}")
    supporting, opposing = V.assert_evidence_is_not_empty(candidate)
    delta = json.loads(candidate["proposed_behavior_delta"])
    change = V._delta_to_change(delta)
    return {"changes": [change], "supporting": len(supporting),
            "opposing": len(opposing)}


def _declared_floor(version_id: int) -> int | None:
    """The promotion floor a frozen version asserts, or None.

    Read from the version's own ``rules`` block rather than from the constant,
    because that is the copy a promotion is re-checked against: the floor is a
    property of the version, and the code constant only decides when the gate
    stops abstaining. An operator comparing the wrong one of those two would
    conclude the boundary is 50 when the version says 20, or the reverse.
    """
    sources = registry.sources_of(version_id) or {}
    value = (sources.get("rules") or {}).get(
        "holdout_gate.MIN_VALIDATION_SAMPLES")
    return value if type(value) is int else None


def _print_promotion_floor(version_id: int) -> None:
    """Print the numbers that decide whether a promotion is even possible.

    All of them, always, and then any gap between the version's floor and the
    rule the repository declares, because those are values that can live in two
    different units and the operator's next action depends on which one binds.
    The rule is printed whether or not it is violated: printing it only alongside
    a complaint would make "the two agree" and "the rule was never consulted"
    look identical, which is the failure this whole report exists to prevent.
    """
    declared = _declared_floor(version_id)
    print("  promotion floor: "
          + (f"version #{version_id} declares {declared} paired sample(s)"
             if declared is not None
             else f"version #{version_id} declares none"))
    print(f"  gate abstains below {holdout_gate.MIN_VALIDATION_SAMPLES} paired "
          "sample(s) (holdout_gate.MIN_VALIDATION_SAMPLES, live code)")
    print(f"  repository rule: n >= {holdout_gate.GOVERNANCE_MIN_SAMPLES} "
          "paired sample(s) (GOLDEN_PRINCIPLES §7)")
    for gap in holdout_gate.promotion_floor_gap(
            declared, when=f"version #{version_id}"):
        print(f"    ! {gap}")


def _cmd_status(args) -> int:
    pointer = registry.active(args.policy_key)
    print(f"policy {args.policy_key!r}")
    if pointer is None:
        print("  nothing in force")
    else:
        version = registry.get_version(pointer["version_id"])
        print(f"  in force: version #{pointer['version_id']} at seq "
              f"{pointer['version_seq']} "
              f"(changed by {pointer['changed_by']}, {pointer['changed_at']})")
        if version is None:
            # Reported rather than raised. A pointer at a version that is not
            # on record is precisely the situation where an operator needs
            # this command to work, and crashing would hide the diagnosis
            # behind a traceback.
            print("  but that version is not on record — see integrity below")
        else:
            print(f"  frozen {version['frozen_at']} by "
                  f"{version['created_by']}: {version['reason']}")
            print(f"  content hash {version['content_hash'][:16]}")
            approval = registry.approval_for(pointer["version_id"],
                                             args.policy_key)
            print("  approved by "
                  + (f"{approval['approved_by']} on {approval['at']}"
                     if approval else "(an install — no approval needed)"))
            print("  live configuration still matches: "
                  f"{policy_sources.verify_live(pointer['version_id'])}")
            # Printed as values, not only as a hash. Once a version is in force
            # the decision parameters are owned by the version, and the code
            # defaults stop deciding anything — so "what mapping is the trader
            # actually applying" has to be answerable without reading the
            # database by hand. An edit to the defaults that has no effect is
            # otherwise indistinguishable from an edit that was forgotten.
            print("  decision parameters in force: "
                  f"{json.dumps(scoring.in_force_decision_params(), sort_keys=True)}")
            # Printed next to the parameters, and for the same reason: once a
            # version is in force the floor a promotion is re-checked against
            # belongs to the version, so "what would it take" is unanswerable
            # without reading the record.
            _print_promotion_floor(pointer["version_id"])

    versions = registry.versions_for(args.policy_key)
    print(f"  {len(versions)} version(s) on record:")
    for version in versions:
        mark = " *" if pointer and pointer["version_id"] == version["id"] else ""
        print(f"    #{version['id']} frozen {version['frozen_at']} "
              f"{version['content_hash'][:12]}{mark}")

    print(f"  {len(registry.transitions_for(args.policy_key))} transition(s):")
    for step in registry.transitions_for(args.policy_key):
        source = (f"#{step['from_version_id']}"
                  if step["from_version_id"] else "(none)")
        print(f"    seq {step['version_seq']} {step['kind']:9} {source} -> "
              f"#{step['to_version_id']} by {step['actor']}")

    problems = registry.integrity()
    print("  integrity: " + ("clean" if not problems else ""))
    for problem in problems:
        print(f"    - {problem}")

    # The progress meter for every experiment, in the same command that shows
    # the record. The gate refuses a verdict asked for too early rather than
    # recording an abstention, so "how many paired samples do I have" is the
    # number that decides when asking is worth it — and it belongs next to the
    # pointer it would move, not in a second command an operator has to know
    # about. Samples, not days: see the module docstring.
    runs = shadow.coverage()["runs"]
    if not runs:
        print("  no shadow run has been opened: nothing is under experiment")
    for run in runs:
        print(f"  shadow run #{run['run_id']} (version "
              f"#{run['policy_version_id']}, {run['status']}, "
              f"{run['report_type']}): {run['paired']}/{run['needed']} paired "
              f"sample(s) over {run['scored_days']} scored day(s), "
              f"{run['scored']} of {run['forecasts']} forecast(s) scored — "
              f"{run['remaining']} sample(s) to go")
    return 0


def _cmd_approve(args) -> int:
    verdict = _eligible_verdict(args.version, args.gate_decision)
    print(f"  citing gate decision #{verdict['id']}: {verdict['reason']}")
    print(f"    outcome={verdict['outcome']} "
          f"validation_days={verdict['validation_days']} n={verdict['n']}")
    if args.dry_run:
        print("dry run: nothing written.")
        return 0
    approval_id = registry.approve(
        version_id=args.version, approved_by=args.by, reason=args.reason,
        gate_decision=verdict, sources=_sources(args.version if args.verb != "rollback" else args.to_version),
        policy_key=args.policy_key, at=args.at)
    print(f"recorded approval #{approval_id}. The pointer did not move: "
          "promotion is a separate act.")
    return 0


def _cmd_promote(args) -> int:
    if args.dry_run:
        approval = registry.approval_for(args.version, args.policy_key)
        print("  approved by "
              + (f"{approval['approved_by']} on {approval['at']}"
                 if approval else "nobody — this promotion would be refused"))
        print("dry run: nothing written.")
        return 0
    seq = registry.promote(
        version_id=args.version, actor=args.by, reason=args.reason,
        sources=_sources(args.version if args.verb != "rollback" else args.to_version), expected_seq=args.expect_seq,
        policy_key=args.policy_key, at=args.at)
    print(f"policy {args.policy_key!r} is now at version #{args.version}, "
          f"seq {seq}")
    return 0


def _cmd_rollback(args) -> int:
    if args.dry_run:
        print("dry run: nothing written.")
        return 0
    seq = registry.rollback(
        to_version_id=args.to_version, actor=args.by, reason=args.reason,
        sources=_sources(args.version if args.verb != "rollback" else args.to_version), expected_seq=args.expect_seq,
        policy_key=args.policy_key, at=args.at)
    print(f"policy {args.policy_key!r} restored to version "
          f"#{args.to_version}, seq {seq}. Nothing was deleted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy-key", default=registry.POLICY_KEY_DEFAULT,
                        help="which governed policy (default: trader).")
    sub = parser.add_subparsers(dest="verb", required=True)

    status = sub.add_parser("status",
                            help="what is in force, and is it still clean.")
    status.set_defaults(func=_cmd_status)

    def _common(p):
        p.add_argument("--by", required=True, help="who is acting.")
        p.add_argument("--reason", required=True, help="why, in one sentence.")
        p.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                       help="the date to record (default: the kernel clock).")
        p.add_argument("--dry-run", action="store_true",
                       help="print what would happen, then stop.")

    freeze = sub.add_parser(
        "freeze", help="record the live configuration as a version.")
    freeze.add_argument("--parent-version", type=int, default=None,
                        help="the version this one derives from, if any.")
    freeze.add_argument(
        "--decision-json", default=None, metavar="JSON",
        help="decision parameters this version asserts, as a JSON object "
             "merged over the ones in force (default: exactly those). Whole "
             "keys are replaced, so pass every prior you mean to change.")
    _common(freeze)
    freeze.set_defaults(func=_cmd_freeze)

    install = sub.add_parser(
        "install", help="put a version in force where none is.")
    install.add_argument("--version", type=int, required=True)
    _common(install)
    install.set_defaults(func=_cmd_install)

    shadow_open = sub.add_parser(
        "shadow-open", help="open an experiment measuring one frozen version.")
    shadow_open.add_argument("--version", type=int, required=True)
    shadow_open.add_argument("--producer", required=True,
                             help="a name registered in shadow.PRODUCERS — "
                                  "constant_0.5 for the no-skill baseline, or "
                                  "remap_confidence for the candidate. An "
                                  "unregistered name is refused, with the list.")
    shadow_open.add_argument("--report-type", default="intraday",
                             help="which book the experiment pairs on "
                                  "(default: intraday — the actionable book). "
                                  "'morning' has produced nothing since "
                                  "2026-09-10 and 'intraday_signal' carries no "
                                  "prob, so neither can ever produce a paired "
                                  "sample; open_run measures the book and "
                                  "refuses one that cannot.")
    shadow_open.add_argument("--reason", required=True,
                             help="why, in one sentence.")
    shadow_open.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                             help="the date to record (default: the kernel clock).")
    shadow_open.add_argument("--dry-run", action="store_true",
                             help="print what would happen, then stop.")
    shadow_open.set_defaults(func=_cmd_shadow_open)

    shadow_close = sub.add_parser(
        "shadow-close", help="end a shadow run; its scores stay readable.")
    shadow_close.add_argument("--run", type=int, required=True)
    shadow_close.add_argument("--reason", required=True,
                              help="why it is being ended, in one sentence.")
    shadow_close.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                              help="the date to record (default: the kernel clock).")
    shadow_close.add_argument("--dry-run", action="store_true",
                              help="print what would happen, then stop.")
    shadow_close.set_defaults(func=_cmd_shadow_close)

    shadow_emit = sub.add_parser(
        "shadow-emit", help="write the challenger's forecasts for one date.")
    shadow_emit.add_argument("--run", type=int, required=True)
    shadow_emit.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    shadow_emit.add_argument("--horizon", type=int, default=None,
                             help="override the horizon, in trading days. "
                                  "Default: inherit what the champion declared "
                                  "for each code, because a challenger graded "
                                  "over a different window than the champion "
                                  "is a comparison of two questions. Setting "
                                  "this re-introduces that mismatch.")
    shadow_emit.add_argument("--dry-run", action="store_true",
                             help="print the panel it would use, then stop.")
    shadow_emit.set_defaults(func=_cmd_shadow_emit)

    shadow_score = sub.add_parser(
        "shadow-score", help="grade the challenger's matured forecasts.")
    shadow_score.add_argument("--run", type=int, default=None,
                              help="only this run (default: every run).")
    shadow_score.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                              help="grade as of this date (default: the kernel clock).")
    shadow_score.add_argument("--dry-run", action="store_true",
                              help="say why this verb has no dry run, then stop.")
    shadow_score.set_defaults(func=_cmd_shadow_score)

    gate = sub.add_parser(
        "gate", help="ask for a verdict on a version, and record it.")
    gate.add_argument("--version", type=int, required=True)
    gate.add_argument("--report-type", default="morning",
                      help="which book to compare on (default: morning).")
    gate.add_argument("--today", default=None, metavar="YYYY-MM-DD",
                      help="when the question is asked (default: the kernel "
                           "clock). It cannot shorten the window — that comes "
                           "from the version's own frozen_at.")
    gate.set_defaults(func=_cmd_gate)

    variant_build = sub.add_parser(
        "variant-build",
        help="build a policy variant from a candidate; moves no pointer.")
    variant_build.add_argument("--candidate", type=int, required=True,
                               help="the learning candidate to build from.")
    variant_build.add_argument("--parent", type=int, required=True,
                               help="the frozen version the candidate was "
                                    "distilled against; the variant inherits "
                                    "it and changes only what the delta names.")
    variant_build.add_argument("--by", required=True,
                               help="who is building it. A person: this writes "
                                    "a frozen version.")
    variant_build.add_argument("--reason", default=None,
                               help="why, in one sentence (default: derived "
                                    "from the candidate and the change).")
    variant_build.add_argument("--dry-run", action="store_true",
                               help="check the guards and print the change, "
                                    "then stop.")
    variant_build.set_defaults(func=_cmd_variant_build)

    approve = sub.add_parser("approve", help="authorise a version.")
    approve.add_argument("--version", type=int, required=True)
    approve.add_argument("--gate-decision", type=int, default=None,
                         help="cite this gate decision instead of the newest "
                              "eligible one.")
    _common(approve)
    approve.set_defaults(func=_cmd_approve)

    promote = sub.add_parser("promote", help="move the pointer to a version.")
    promote.add_argument("--version", type=int, required=True)
    promote.add_argument("--expect-seq", type=int, default=None,
                         help="only act if the pointer is still at this seq.")
    _common(promote)
    promote.set_defaults(func=_cmd_promote)

    rollback = sub.add_parser("rollback", help="restore an earlier version.")
    rollback.add_argument("--to-version", type=int, required=True)
    rollback.add_argument("--expect-seq", type=int, default=None,
                          help="only act if the pointer is still at this seq.")
    _common(rollback)
    rollback.set_defaults(func=_cmd_rollback)

    args = parser.parse_args(argv)

    # The path is printed because it is a fixed location that does not read
    # TMPDIR: a person about to move the active policy should see which
    # database they are moving it in.
    print(f"policy target: {MEMORY_DB_PATH}")
    try:
        return args.func(args)
    except (registry.PolicyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
