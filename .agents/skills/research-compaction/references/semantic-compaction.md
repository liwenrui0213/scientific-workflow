# Semantic Compaction

Compaction preserves epistemic state while reducing active context. It is not a
ranking of attractive results and not a deletion policy.

## Promote observations only when useful

Treat `Run -> Evidence` as the default. Create an Observation Record only when
at least one condition in the active versioned promotion-trigger Registry
applies. Inspect it with `python -m tools.studyctl observation-trigger-list
STUDY_ID`; do not invent an unregistered trigger or use independent review as a
generic escape hatch. The initial Registry covers aggregation across Runs,
multi-Claim reuse, complex analysis, Cohort crossings, anomalies or failures,
Frontier or confirmatory use, independent review, cross-Checkpoint reuse, and
material context deduplication.

If a materially distinct reason is not registered, record it as a proposal and
stop promotion. Explain why existing triggers are insufficient, the expected
benefit, and abuse risks. A reviewed semantic extension becomes usable only
after explicit human adoption and a protected append-only Registry update. A
new structural trigger also requires a deterministic validator. Never treat a
Reviewer recommendation alone as authority to change workflow policy.

An Observation answers what the declared computation found. It binds every
source Run, selection and exclusion rules, distribution and boundary results,
uncertainty, anomalies, representative failures, analysis assumptions, and
limitations. It never answers whether a Claim is supported. When two Claims
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

Retain all contradictory finalized Evidence even when it is statistically weak
or inconvenient. Label its limitations; do not erase it by averaging it into a
positive aggregate.

## Preserve representative failures

Keep a failure when it is unique, recurs under a recognizable signature, marks a
scope boundary, invalidates an assumption, or prevents likely repeated work.
For repeated equivalent failures, keep a failed-direction record and the
smallest set of Runs that demonstrates the signature. Do not call distinct
mechanisms duplicates merely because their headline metric is similar.

## Maintain a small diverse Frontier

The Frontier is a decision surface, not a leaderboard. Retain only candidates or
questions that are active and non-redundant. Preserve alternatives with distinct
strengths, risks, mechanisms, or scope, including a candidate that is important
because it may falsify the current direction.

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
- Preserve the previous Claim state through versioned records and Checkpoints.

## Archive scratch safely

Archive a `work/active/` file only when its durable content has been promoted to
an authoritative artifact or it is no longer needed to reproduce an active
decision. The plan must identify the exact file. Generated projections, adopted
host code, Runs, Observations, Evidence, Claims, formal artifacts, and human
records are not scratch.

## Compaction self-check

Before finalizing, ask:

1. Can every active Claim still be traced to support and contradiction?
2. Can every promoted Observation be traced to all source Runs and exclusions?
3. Is every unique anomaly or failure boundary still discoverable?
4. Does the Frontier contain only live, distinct choices?
5. Did any wording become stronger while detail was removed?
6. Would a fresh Agent know the next action and why it matters?
