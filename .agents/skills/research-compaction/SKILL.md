---
name: research-compaction
description: Compact a long-running scientific Study into finite active context without deleting history. Use when Run and work-note history has grown, the Frontier needs clarification, Claims or Evidence need disciplined updates, representative failures must be preserved, or a new immutable Checkpoint is needed. Do not use for an ordinary status summary or garbage collection.
---

# Research Compaction

## Authoritative inputs

Validate the repository profile, then run `studyctl check-changes` and
`studyctl compact-prepare`. Treat `COMPACTION_INPUT.json`, STATUS, and CHANGES as
indexes. Inspect sealed Runs, finalized Evidence, Claims, failed directions,
formal artifacts, Checkpoints, and actual `work/active/` files before making a
semantic judgment.

Read [semantic compaction](references/semantic-compaction.md) before selecting
decisive Evidence, representative failures, Claim revisions, or the new
Frontier.

## Workflow

1. Update draft or new-version Evidence and Claims without inventing results.
   Preserve decisive support, all contradictions, unique anomalies, and
   representative failed Runs or failed-direction records.
2. Keep the Frontier limited to Claims under active test, unresolved questions,
   immediate next actions, and genuinely blocking human decisions. Move
   resolved history into Evidence, failed directions, or Checkpoints.
3. Write a schema-valid compaction plan outside `work/active/`. Bind it to the
   preparation input, Claims, and Evidence hashes, and name every scratch file
   proposed for archival explicitly.
4. Keep `work/active/` unchanged, then run `studyctl compact-finalize`. If the
   profile, host fingerprint, inventory, binding, hash, or reference changed,
   prepare again.

## Hard gates

- Stop preparation when host change scope is blocked.
- Compaction is semantic organization, never deletion or a way to make
  contradictory results disappear.
- Archive only selected Study scratch files. Never archive authoritative
  records, adopted host source or tests, Runs, Evidence, output objects, unique
  anomalies, or referenced material.
- Never treat generated projections as sources of truth or silently widen a
  Claim while summarizing it.
- Never change a protected condition or authorized Claim scope through
  compaction. Open the normal human-authorized revision path instead.

## Output and handoff

Produce one immutable Checkpoint with hash-bound active Claims, decisive and
contradictory Evidence, a small diverse Frontier, open questions, next actions,
budget state, and the previous Checkpoint link. Regenerate status through
`studyctl`; return the archived scratch list and any human decision required.
Hand the compacted state back to `scientific-study`, or prepare the explicit
fresh-session `scientific-review` handoff when the review boundary is ready. Do
not invoke garbage collection as part of compaction.
