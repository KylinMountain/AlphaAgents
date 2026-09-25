# Continuous account and learning branches (M2-B)

A frozen decision (`decision_lab.py`) answers a local question. This adapter
continues the original walk-forward engine for several sessions after a sealed
end-of-session checkpoint. Every branch owns its book, settlements, reviews,
handbook history, captures and a fresh model journal. It never merges into the
source or live account and does not replay the parent's later answers.

## 1. Create a clean prefix and checkpoint

Use the same source checkout, dependency lock, effective configuration and model
for prefix and branches. New checkpoints are emitted by the runner only after a
complete window. Prefix state must be fresh: nonempty account tables or private
memory directories are rejected before any decision. An arbitrary historical
directory is not a restore point.

```bash
uv run python scripts/walk_bootstrap.py --target /tmp/prefix --corpus /path/to/corpus
ALPHAAGENTS_LLM_MODE=record uv run python scripts/walk_forward.py \
  --target /tmp/prefix --start 2026-01-05 --days 10 --trader default \
  --decider llm --run-id prefix-01 --checkpoint-out /tmp/checkpoint-01
```

Preserve your intended strategy flags, including selection architecture,
sector membership, tools, exits and synthetic close buys. The example does not
change defaults into a recommended trading strategy. `--checkpoint-out` disables
note merging, fixes the declared model client (no client failover/SDK retries),
uses one logical agent attempt, and rejects `--keep-going` and formal/frozen-
direction experiment manifests. Failed/incomplete prefixes do not publish a
usable checkpoint. Missing close-review inputs or interpretations in model-backed
branches count as lost learning even when valuation succeeds. Ordinary runs
without these options retain their prior checkpoint-free behavior.

The checkpoint must be outside the prefix and must not exist. It pins full
copies of the available corpus databases once, which can require substantial
disk space. SQLite backup includes committed WAL pages; an uncommitted account
transaction or detectable concurrent mutation refuses capture. Keep writers
stopped: this is a quiescent runner boundary, not a multi-process live snapshot.

## 2. Start bounded independent trajectories

```bash
uv run python scripts/walk_branch.py run \
  --checkpoint /tmp/checkpoint-01 --output /tmp/branches-01 \
  --days 5 --trials 3 --max-branch-sessions 15 --timeout 7200 --jobs 1 --live
```

Without `--arms` this repeats the unmodified control, useful for measuring
same-configuration variability. Each branch starts at the next pinned corpus
session; dates cannot be skipped or replayed. `--live` explicitly authorizes
provider calls. A placeholder checkpoint requires `--mechanical`, which denies
network requests in each child. Mechanical results are plumbing checks, not
samples of a model's decisions or of a learning strategy.

Limits: 1--30 sessions per branch, 1--8 trials per arm, at most 16 branches,
1--200 declared branch-sessions, at most 2 simultaneous branches and an explicit
1--21600-second branch deadline. These bound work and wall time, not a guaranteed
token or monetary cost. Within a branch the dates always run sequentially.
`--seed` shuffles task scheduling; it does not set the model's random seed.

## 3. Optional first-plan intervention

`--arms /path/to/arms.json` adds each intervention beside control:

```json
[
  {"name": "candidate-context", "old": "EXACT UNIQUE KNOWLEDGE FRAGMENT", "new": "REPLACEMENT FRAGMENT"}
]
```

Scope is deliberately narrow: one exact replacement in the knowledge shown to
**the first open trade plan on the first branch day**. Upstream selection and
past reviews/rule records are not edited. The changed frame stores its parent
hash and intervention. After that decision, the branch advances its own orders,
account, outcomes and learning through the existing runner. It is an initial
context perturbation, NOT permanent rule deletion and NOT a change to the
handbook update algorithm. A restriction may be relearned later.

Missing/ambiguous text fails at that plan before that plan's provider call;
upstream research may already have consumed calls. If no eligible plan occurs,
the branch is incomplete, not a successful intervention. This experiment does
not isolate upstream or long-term rule-update effects. Use the single-decision
lab to identify a useful exact intervention first.

For two arms (control plus one change), three trials and five days, declare at
least **30 branch-sessions**. Each branch is a separate trajectory, not a paired
set of identical model random draws. Do not call all observations across one
trajectory independent samples.

## 4. Artifacts and interpretation

The output contains `manifest.json`, `summary.json`, and a private directory for
each branch: `task.json`, `branch.json`, `worker.log`, `storage/` and the original
runner's `report/` with equity, decisions and run metadata. The restored cash, holdings counts and valuation fields must match the
checkpoint mark before the first continuation decision or fill. Performance metrics are
for the continuation suffix, not newly generated evidence about the prefix.

Failed, timed-out or partially learned trajectories remain in the denominator.
The command exits nonzero if any trajectory is incomplete or the checkpoint/
source verification fails. A branch directory is never reused or overwritten;
`walk_resume.py` refuses branch directories rather than splice old answers onto
a different history. Repeat with a new output directory. No automatic winner,
rule promotion or claim of alpha is produced. More orders is not a success metric.

Only the journaled agent requests have observed usage. Other model clients are
outside that journal; interrupted/unrecorded requests may also be missing. Read
usage as a lower bound where incomplete, never as total provider billing.

## Contract and limitations

- Private copied state: `memory.db` (all tables), `traders/`, `chroma/`,
  `variants/`, plus carried window start, measured review, exposure history and
  pending thesis signals. Reports, `.env`, credentials and parent model journals
  are not copied. This is the current runner contract, not a complete live
  process image. Session events keep historical run IDs; new calls get new IDs.
  Measured reviews query the explicit parent/child lineage, so a new run ID
  neither erases parent samples nor incorporates unrelated runs.
- Source/dependency hashes cover application, prompt and runner sources and
  trader YAMLs. Runtime comparison covers effective config, declared model,
  selected budget settings, Python and principal installed dependencies.
  Provider-side routing, model weights and service defaults cannot be certified.
- The checkpoint pins all market-history dates; the existing replay clock and
  readers enforce visibility. Content hashes do not make historical membership,
  synthetic close data or a model's training information point-in-time true.
- Corpus links in children point only at the pinned copies and the runner opens
  them read-only. Private writable state is copied, never hardlinked. Hashes are
  checked again afterward. This is application isolation, not an OS security
  sandbox, a signature or protection against a hostile same-user process.
- Existing seven-run journals are not imported. New code invalidates source
  identity: retain the matching checkout or generate a new prefix. An obsolete
  checkpoint is never silently migrated to different learning logic.
- M2's remaining online/replay strategy convergence and scheduler/control-plane
  isolation, and M3 behavioral promotion, are not delivered by this adapter.
