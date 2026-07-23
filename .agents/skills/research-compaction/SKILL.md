---
name: research-compaction
description: Compact a scientific Study into finite active context without deleting history. Use when history has grown, the Frontier needs clarification, Claims or Evidence need updates, representative failures need preservation, or a new immutable Checkpoint is needed. Do not use for status summaries or garbage collection.
---

# Research Compaction

## Authoritative inputs

Validate the repository profile, then run `studyctl check-changes`, `status`,
and `compact-prepare`. Invoke this Skill whenever status reports soft or hard
compaction pressure, as well as at an explicit scientific checkpoint. Treat
Treat generated files as bounded indexes; an unlisted item is not absent.
Inspect only question-selected Runs, Observations, Evidence, Claims, failures,
formal artifacts, Checkpoint, and work sources. Read authority by ID.

Read [semantic compaction](references/semantic-compaction.md) before selecting
decisive Evidence, representative failures, Claim revisions, or the new
Frontier.

## Workflow

1. Keep simple observations inline; inspect the active promotion-trigger
   Registry and promote only when a registered condition applies. Preserve every
   source Run, exclusion, anomaly, failure, uncertainty, assumption, and limitation.
2. Update draft or new-version Evidence and Claims without inventing results.
   Pin exact Observation versions when used. Preserve decisive
   support, contradictions, anomalies, and representative failures. Evidence
   basis is immutable; preserve its inference argument
   without rewriting finalized history. Claim basis is
   recomputed from its supporting Evidence; compaction cannot relabel
   exploratory or mixed support as confirmatory. Mark no-longer-current
   Claims `retired`, or mark them `superseded` with a new active Claim ID. The
   new Checkpoint seals these lifecycle decisions; never reactivate them after
   that boundary.
3. When total or terminal Claim pressure remains high, seal explicit
   `retired`/`superseded` lifecycles first. Only after that Checkpoint succeeds
   may a later compaction remove those terminal records from current
   `CLAIMS.json`; first verify that finalization created the immutable full
   Claim record referenced by the Checkpoint under `checkpoints/claim-records/`.
   A sealed terminal Claim is immutable in full, not only in lifecycle. Never
   rewrite it under the same ID, break a historical supersession chain, drop an
   active Claim, remove a referenced record, or auto-select a lifecycle.
4. Keep the Frontier limited to Claims under active test, unresolved questions,
   immediate next actions, and genuinely blocking human decisions. Move
   resolved history into Evidence, failed directions, or Checkpoints.
5. After all Observation, Evidence, and Claim edits, rerun `compact-prepare`, then write a
   schema-valid compaction plan outside `work/active/`. Bind it to that final
   preparation input, Claims hash, and constant-size Evidence inventory binding
   (`total_count` plus full `inventory_sha256`), and name every scratch file
   proposed for archival explicitly. Never copy the full Evidence path/hash map
   into the plan.
6. Keep `work/active/` unchanged, then run `studyctl compact-finalize`. If the
   profile, host fingerprint, inventory, binding, hash, or reference changed,
   prepare again.
7. When an index is truncated, inspect and compact one relevant batch, finalize
   without deleting history, then rerun `compact-prepare` for the next batch.
   Finalization recomputes the full inventory hash, including entries omitted
   from the bounded projection.

## Hard gates

- Stop preparation when host change scope is blocked.
- Compaction is semantic organization, never deletion or a way to make
  contradictory results disappear.
- Archive only selected Study scratch files. Never archive authoritative
  records, adopted host source or tests, Runs, Observations, Evidence, output
  objects, unique anomalies, or referenced material.
- Never treat generated projections as sources of truth or silently widen a
  Claim while summarizing it.
- Never omit a confirmatory attempt from Confirmation/Evidence accounting or
  treat an unlisted bounded locator as absent. Never collapse exploratory and
  confirmatory Evidence into one unlabeled result or override the Claim basis
  recomputed from its current supporting Evidence.
- Observation has no Claim assessment; never mutate it or hide exclusions.
- Never change a protected condition or authorized Claim scope through
  compaction. Open the normal human-authorized revision path instead.

## Output and handoff

Produce one immutable Checkpoint with only the Frontier-selected active Claim
snapshots, compact hash references for non-Frontier Claims, decisive and
contradictory Evidence, exact locators for the Observations reached through that
Evidence, a small diverse Frontier, open questions, next actions, pressure
watermarks, budget state, and the previous Checkpoint link. Regenerate status through
`studyctl`; return the archived scratch list and any human decision required.
Hand the compacted state back to `scientific-study`, or prepare the explicit
fresh-session `scientific-review` handoff when the review boundary is ready. Do
not invoke garbage collection as part of compaction.
