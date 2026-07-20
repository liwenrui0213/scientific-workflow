# Semantic Compaction

Compaction preserves epistemic state while reducing active context. It is not a
ranking of attractive results and not a deletion policy.

## Select decisive Evidence

Treat Evidence as decisive when it materially changes at least one of:

- the status or scope of an active Claim;
- the plausibility of a live competing explanation;
- the current Frontier;
- the next high-value experiment;
- a protected risk or human decision.

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
host code, Runs, Evidence, Claims, formal artifacts, and human records are not
scratch.

## Compaction self-check

Before finalizing, ask:

1. Can every active Claim still be traced to support and contradiction?
2. Is every unique anomaly or failure boundary still discoverable?
3. Does the Frontier contain only live, distinct choices?
4. Did any wording become stronger while detail was removed?
5. Would a fresh Agent know the next action and why it matters?
