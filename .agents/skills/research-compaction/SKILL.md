---
name: research-compaction
description: Compact a long-running scientific Study into finite active context without deleting history. Use when Run and work-note history has grown, the Frontier needs clarification, Claims or Evidence need disciplined updates, representative failures must be preserved, or a new immutable Checkpoint is needed.
---

# Research Compaction

1. Validate `scientific-workflow/repository-profile.json`, then run `python -m tools.studyctl check-changes STUDY_ID` and `python -m tools.studyctl compact-prepare STUDY_ID`. Preparation must stop if the host repository change scope is blocked. Treat generated change and compaction projections as indexes, then inspect authoritative source artifacts for every proposed semantic update.
2. Update draft or new-version Evidence and `CLAIMS.json` without inventing results. Preserve decisive supporting Runs, all contradictory Evidence, unique anomalies, and representative failed Runs or failed-direction records.
3. Keep the Frontier small: current Claims under active test, unresolved questions, immediate next actions, and human decisions only. Move resolved historical detail into Evidence, failed directions, or Checkpoints.
4. Write a JSON plan matching `scientific-workflow/schemas/compaction-plan.schema.json` at `studies/STUDY_ID/work/COMPACTION_PLAN.json` (or the profile-adapted equivalent), not below `work/active/`. Bind it to the compaction-input, Claims, and Evidence hashes; list only explicit files below `work/active/` for archival.
5. Do not change `work/active/` between prepare and finalize. Run `python -m tools.studyctl compact-finalize STUDY_ID --plan PATH`. Finalization rechecks the repository-profile hash, the host consequential-path fingerprint, and the complete `work/active/` inventory. If any binding, hash, or reference changed, prepare again instead of bypassing the check. Generated Study projections are intentionally outside the host fingerprint.

Compaction is semantic organization, not deletion. Archive only Study scratch material selected from `work/active/`; never move adopted host code or tests into the Study archive. Never archive authoritative records, delete Runs or output objects, hide contradictory Evidence, or treat generated STATUS or CHANGES files as sources of truth.
