# Semantic Compaction

Compaction preserves epistemic state while reducing active context. It is not a
ranking of attractive results and not a deletion policy. Its Checkpoint is a
hash-bound derivative of the source Runs, Observations, Evidence, Claims,
Frontier, formal artifacts, and logical-ledger bindings selected at that
boundary. It is useful for resumption and exact review scope, but it neither
replaces those sources nor creates scientific support. Checkpoint v5 also pins
the exact graph-record-sequence locator observed at finalization.

## Promote observations only when useful

Treat `Run / Artifact -> inline Observation -> Claim-specific Evidence` as the
default. Create a standalone Observation Record only when at least one
condition in the active versioned promotion-trigger Registry applies. Inspect
it with `python -m tools.studyctl observation-trigger-list STUDY_ID`; do not
invent an unregistered trigger or use independent review as a generic escape
hatch. The initial Registry covers aggregation across Runs, multi-Claim reuse,
complex analysis, Cohort crossings, anomalies or failures, Frontier or
confirmatory use, independent review, cross-Checkpoint reuse, and material
context deduplication.

If a materially distinct reason is not registered, record it as a proposal and
stop promotion. Explain why existing triggers are insufficient, the expected
benefit, and abuse risks. A reviewed semantic extension becomes usable only
after explicit human adoption and a protected append-only Registry update. A
new structural trigger also requires a deterministic validator. Never treat a
Reviewer recommendation alone as authority to change workflow policy.

An Observation answers what the declared computation found. It binds every
source Run, selection and exclusion rules, distribution and boundary results,
uncertainty, anomalies, representative failures, analysis assumptions, and
limitations. Observation v3 also binds exactly the finalized Intent refs
derived from each source Run's independent `intent_binding`. It never answers whether a Claim is
supported. When two Claims
reuse one Observation, write separate Evidence Arguments and allow their
assessments to differ.

## Select decisive Evidence

Treat Evidence as decisive when it materially changes at least one of:

- the status or scope of an active Claim;
- the plausibility of a live competing explanation;
- the current Frontier;
- the next high-value experiment;
- a protected risk or human decision.

Preserve each Evidence version's observation-to-Claim bridge, auxiliary
assumptions, competing explanations, and falsification conditions. A new
Evidence version may strengthen that argument, but compaction must not rewrite
a finalized record, erase an alternative explanation, or weaken the conditions
that would overturn its assessment merely to simplify the active view.
Evidence v5 additionally pins the addressed Claim statement-and-scope digest
and the finalized Intent-ref set derived from its source Runs; do not
carry an old argument across a changed Claim specification.

Retain all contradictory finalized Evidence even when it is statistically weak
or inconvenient. Label its limitations; do not erase it by averaging it into a
positive aggregate.

## Preserve representative failures

Separate three layers:

1. **Failure/attempt fact:** an immutable Run records what happened through its
   sealed status, logs, declared outputs, and integrity bindings. This includes
   `failed`, `interrupted`, or `incomplete` Runs and a zero-exit Run whose
   declared output is recorded absent. A missing output makes the Run
   Evidence-ineligible but does not erase the attempt.
2. **Candidate explanation:** a proposed cause or mechanism remains a
   hypothesis in the Workspace or an explicit competing explanation in
   Evidence until discriminating observations support it.
3. **Reusable lesson:** a statement such as “method X fails under condition Y”
   is a scoped Claim and requires Claim-specific Evidence.

Keep an exact Run reference when the failure is unique, recurs under a
recognizable signature, marks a scope boundary, invalidates an assumption, or
prevents likely repeated work. For repeated equivalent attempts, retain the
smallest representative Run set without deleting the rest of the immutable
history. Do not call distinct mechanisms duplicates merely because their
headline metric is similar, and do not treat a free-form “failed direction” note
as quasi-authoritative.

The Checkpoint field `representative_failures` accepts only immutable Runs whose
status is `failed`, `interrupted`, or `incomplete`. A zero-exit Run with an
absent declared output remains preserved through the Run ledger and bounded Run
inventory; do not mislabel it merely to place it in that Checkpoint field.

## Maintain a small diverse Frontier

The Frontier is a decision surface, not a leaderboard. Retain only candidates or
questions that are active and non-redundant. Preserve alternatives with distinct
strengths, risks, mechanisms, or scope, including a candidate that is important
because it may falsify the current direction.

The Frontier stores its summary, active Claim IDs, open questions, and blocking
human decisions. When a durable statement of why evidence is requested is
warranted, use an ExperimentIntent. Create a ControlGraphSpec/active PLAN only
when a formalization gate or genuine parallel/control dependencies require one;
when present, it must exact-bind the finalized Intent. Do not place executable
actions in the Frontier. A compaction plan carries the current Frontier object
once and must not duplicate its open questions elsewhere.

When several candidates differ across multiple objectives, avoid collapsing
them into one scalar score unless the Brief defines that utility. A candidate is
strictly dominated only when another candidate is at least as good on every
authorized objective and better on at least one, under compatible Evidence.

## Revise Claims conservatively

- Add support only from finalized, hash-pinned Evidence.
- Add contradiction whenever applicable.
- Narrow scope before strengthening wording when support is heterogeneous.
- Mark a Claim inconclusive when Evidence cannot discriminate live explanations.
- Never convert “no counterexample observed” into proof.
- Preserve the previous Claim state through versioned records. A Checkpoint may
  pin or snapshot those records for compact resumption; it is not their source
  of truth.

## Archive scratch safely

Archive a `work/active/` file only when its durable content has been promoted to
an authoritative artifact or it is no longer needed to reproduce an active
decision. The plan must identify the exact file. Generated projections, adopted
host code, finalized ExperimentIntents or ControlGraphSpecs, the graph-record
sequence, an active PLAN when one exists, Runs, Artifacts, Observations,
Evidence, Claims, formal artifacts, and human records are not scratch.

Observation sequence v2 and Evidence sequence v3 bind the complete finalized
record count and inventory digest as well as their creation high-water marks.
A deletion or unindexed finalization is therefore a ledger mismatch, not
compaction material. Only the explicit recovery command for exactly one
uniquely reconstructable interrupted finalization may advance either sequence.

## Compaction self-check

Before finalizing, ask:

1. Can every active Claim still be traced to support and contradiction?
2. Can every promoted Observation be traced to all source Runs and exclusions?
3. Is every unique anomaly or failure boundary still discoverable?
4. Does the Frontier contain only live, distinct choices?
5. Did any wording become stronger while detail was removed?
6. Can every Checkpoint item be traced back to its source record without using
   the Checkpoint itself as scientific support?
7. Do Observation/Evidence Intent refs exactly match their Intent-bound source
   Runs, and does each Evidence still address the same Claim statement/scope?
8. Would a fresh Agent know which EvidenceGap remains and whether it needs an
   ExperimentIntent, a formalization-required ControlGraphSpec, or a human
   decision?
