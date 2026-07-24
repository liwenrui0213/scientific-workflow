---
name: research-compaction
description: Compact a scientific Study into finite active context without deleting history. Use for compaction pressure, Frontier clarification, or a new immutable Checkpoint; not for status summaries or garbage collection.
---

# Research Compaction

## Authoritative inputs

Validate the repository profile, then run
`python -m tools.studyctl check-changes <STUDY_ID>`,
`python -m tools.studyctl status <STUDY_ID>`, and
`python -m tools.studyctl compact-prepare <STUDY_ID>`. Use this Skill at soft/hard pressure
or an explicit scientific checkpoint. Generated files are bounded indexes;
read question-selected authority by ID, including graph records and sequence,
Runs/Artifacts, Observations, Evidence, Claims, failures, formal artifacts,
Checkpoint, and Workspace sources.

Read [semantic compaction](references/semantic-compaction.md) before selecting
decisive Evidence, representative failures, Claim revisions, or the new
Frontier.

## Workflow

1. Keep simple observations inline. Promote only under a registered trigger,
   preserving source Runs, exclusions, anomalies, failures, uncertainty,
   assumptions, and limitations.
2. Update draft or new-version Evidence and Claims without inventing results.
   Pin exact Observations; preserve decisive support, contradiction, anomalies,
   failures, and inference arguments. Never rewrite finalized history or
   relabel exploratory/mixed support as confirmatory. Mark obsolete Claims
   `retired` or `superseded` by a new active Claim.
3. When total or terminal Claim pressure remains high, seal explicit
   lifecycles first. A later compaction may remove them from `CLAIMS.json` only
   after a Checkpoint created their immutable full records. Never rewrite those
   records, break supersession, drop an active Claim, or auto-select lifecycle.
4. Keep the Frontier limited to Claims under active test, unresolved questions,
   and genuinely blocking human decisions. Represent structured future work
   through an ExperimentIntent and ControlGraphSpec rather than an action list
   inside the Frontier. Move resolved history into Evidence, failed directions,
   or Checkpoints.
5. Rerun `python -m tools.studyctl compact-prepare <STUDY_ID>`, then write a
   schema-valid plan outside `work/active/`. Bind the preparation input, Claims
   hash, and constant-size Evidence inventory. Carry Frontier once, name every
   proposed scratch archive, and never copy the full Evidence path/hash map.
6. Keep `work/active/` unchanged, then run
   `python -m tools.studyctl compact-finalize <STUDY_ID> --plan <PATH>`. If the
   profile, host fingerprint, inventory, binding, hash, or reference changed,
   prepare again.
7. For a truncated index, inspect one relevant batch, finalize without deleting
   history, and prepare again. Finalization still hashes the full inventory.

## Hard gates

- Stop preparation when host change scope is blocked.
- Compaction is semantic organization, never deletion or a way to make
  contradictory results disappear.
- Archive only selected Study scratch files. Never archive authoritative
  graph records/sequence, active PLAN, adopted host files, Runs/Artifacts,
  Observations, Evidence, output objects, unique anomalies, or references.
- Never treat generated projections as sources of truth or silently widen a
  Claim while summarizing it.
- Never omit a confirmatory attempt, treat an unlisted locator as absent, or
  collapse exploratory and confirmatory Evidence into one unlabeled result.
- Observation has no Claim assessment; never mutate it or hide exclusions.
- Never change a protected condition or authorized Claim scope through
  compaction. Open the normal human-authorized revision path instead.

## Output and handoff

Produce one immutable Checkpoint with Frontier-selected Claim snapshots,
non-Frontier Claim refs, decisive/contradictory Evidence, reached Observations,
one Frontier object, watermarks, budget, and the previous link. Regenerate
status; return archived scratch and required human decisions.
Hand the compacted state back to `scientific-study`, or prepare the explicit
fresh-session `scientific-review` handoff when the review boundary is ready. Do
not invoke garbage collection as part of compaction.
