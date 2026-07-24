---
name: research-compaction
description: Compact a scientific Study into finite active context without deleting history. Use for compaction pressure, Frontier clarification, or a new immutable derived Checkpoint; not for status summaries or garbage collection.
---

# Research Compaction

## Authoritative inputs

Validate the profile. Using `python -m tools.studyctl`, run
`check-changes <STUDY_ID>`, `status <STUDY_ID>`, and
`compact-prepare <STUDY_ID>`. Use this Skill at soft/hard pressure or an explicit compaction
boundary. A `BLOCKED` host scope must be preserved in prepared state;
compaction remains available but never authorizes that scope. Read
question-selected authority by ID: graph records and sequence, Runs/Artifacts,
Observations, Evidence, Claims, formal artifacts, and Workspace sources.
Generated files and Checkpoints are derivatives, not scientific authority. In
`ACTIVE_CONTEXT.json`, finalized graph locators occur under `graph_records`;
mutable Intent/Plan drafts occur under
`workspace.graph_record_drafts`.

Read [semantic compaction](references/semantic-compaction.md) before selecting
decisive Evidence, representative failures, Claim revisions, or the new
Frontier.

## Workflow

1. Promote an Observation only under an exact Registry trigger. Preserve source
   Runs, exclusions, anomalies, failed/interrupted/incomplete attempts,
   uncertainty, limitations, and Intent refs derived from each Run's independent
   Intent binding.
2. Update only drafts or new Evidence/Claim versions. Preserve contradictions,
   inference, alternatives, the addressed Claim statement/scope digest, and
   source-Run-derived Intent refs. Never relabel exploratory/mixed work.
3. Mark obsolete Claims `retired` or `superseded` by a new active Claim. Seal
   lifecycle records before later removing terminal Claims from `CLAIMS.json`;
   never drop active Claims, break links, rewrite history, or choose lifecycle
   automatically.
4. Keep Frontier to active tests, unresolved questions, and blocking human
   decisions. Add Intent or PLAN only when its durable boundary or formal
   control complexity is warranted. Keep occurrence facts in Runs and reusable
   lessons in Evidence-backed Claims. Use the deterministic occurrence locator
   so an empty `representative_failures` list cannot hide failed/ineligible
   attempts or finalized-but-undispositioned Evidence.
5. Rerun `compact-prepare`; write a schema-valid plan outside `work/active/`
   binding its input, Claims hash, and constant-size Evidence inventory. Carry
   Frontier once and name each scratch archive.
6. Keep `work/active/` unchanged; run `compact-finalize --plan <PATH>`. Prepare
   again if any profile, host fingerprint, inventory, binding, hash, or
   reference changed. For truncated indexes, inspect one batch and repeat
   without deleting history.

## Hard gates

- Compaction organizes semantics; it never deletes history, hides
  contradictions, widens Claims, or changes protected conditions.
- Archive only selected Study scratch. Never archive graph authority, active
  PLAN, adopted host files, Runs/Artifacts, Observations, Evidence, output
  objects, unique anomalies, or references.
- Preserve every confirmation attempt. Observation has no Claim assessment.
- Observation sequence v2 and Evidence sequence v3 must match the complete
  finalized inventory. Only the explicit one-record forward-recovery command
  may repair one uniquely interrupted finalization.
- Preserve failures by exact Run reference. Run is fact, cause is hypothesis,
  and reusable lesson is an Evidence-backed scoped Claim; free-form “failed
  direction” notes are not authority.

## Output and handoff

Produce one immutable derived Checkpoint containing selected Claim snapshots,
other Claim refs, decisive/contradictory Evidence, reached Observations,
Frontier, logical-ledger bindings, exact graph-sequence locator, budget, and
previous link. It is finite resumption context, not Evidence or scientific
truth. Regenerate status and report archived scratch and human decisions. Hand
off to `scientific-study` or fresh-session `scientific-review`; never perform
garbage collection as compaction.
