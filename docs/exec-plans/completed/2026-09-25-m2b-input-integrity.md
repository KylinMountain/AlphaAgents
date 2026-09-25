# M2-B follow-up: prove what the branch actually changed

Date: 2026-09-25
Base: main@27e72ccaab6ebb454644131793473122bd5d1731
Status: implemented; offline regression results are in the linked evidence file

This completes two input-integrity checks on top of the continuous account and
learning branch adapter. It does not broaden the experimental treatment into a
permanent rule change or provide evidence of trading improvement.

## Delivered

- Distinguish attempted intervention from actual input application. Report an
  intervention as applied only when a verified decision-input capture has the
  matching branch identity, first session, open phase, parent frame hash and
  exact intervention payload. Report the corresponding invocation and frame
  hashes. An absent or ambiguous edit cannot turn its attempted flag into proof
  that a changed prompt reached the planner. More than one matching capture is
  rejected for a one-shot treatment. A captured input is not proof that the
  provider accepted it or the model followed it; failed outcomes remain failed.
- Recheck externally supplied sector-membership bytes against the hash loaded
  at prefix start before sealing the checkpoint. A file changed during the
  prefix cannot be silently saved as if it described the inherited decisions.

The original account/learning lineage, early account reconciliation, independent
branch state, fresh provider sampling, failure denominators and no-production-
merge behavior are unchanged. The usage document explains the added distinction.

## Verification

See [m2b-final-verification.json](../../reviews/evidence/2026-09-25-trader-lifecycle/m2b-final-verification.json).
The regression asserts real captured application, rejects another run/day,
returns no application for an attempted but uncaptured intervention, and refuses
a mutated membership archive. The final full suite also reruns the real-engine
continuity, WAL backup, isolated stub learning, and parent/child review tests.
The first final-review attempt exposed a singular/plural provenance-field
mismatch in the new helper; the helper was corrected to the existing frame
contract, without relaxing the assertion.

The four changed code/test/usage blobs are taken unchanged from the successful
validation run. Other source files were checked against the already committed
M2-B core and left untouched. Audit branches, staging payloads and temporary
workflows are not part of the application commit. No force update is used.

## Still outside this delivery

Real-model learning effectiveness, the user's seven historical windows, complete
point-in-time market data, permanent handbook-policy interventions, online/replay
strategy convergence, scheduler/control-plane isolation, and M3 promotion remain
separate work. Checkpoints remain source/runtime-bound research state, not live
process snapshots. No external performance experiment was run for this patch.
