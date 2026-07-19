---
name: research-compaction
description: Compact a long-running scientific Study into finite active context without deleting history. Use when Run and work-note history has grown, the Frontier needs clarification, Claims or Evidence need disciplined updates, representative failures must be preserved, or a new immutable Checkpoint is needed.
---

# Research Compaction

1. Run `python -m tools.studyctl compact-prepare STUDY_ID`. Treat `generated/COMPACTION_INPUT.json` as an index, then inspect authoritative source artifacts for every proposed semantic update.
2. Update draft or new-version Evidence and `CLAIMS.json` without inventing results. Preserve decisive supporting Runs, all contradictory Evidence, unique anomalies, and representative failed Runs or failed-direction records.
3. Keep the Frontier small: current Claims under active test, unresolved questions, immediate next actions, and human decisions only. Move resolved historical detail into Evidence, failed directions, or Checkpoints.
4. Write a JSON plan matching `scientific-workflow/schemas/compaction-plan.schema.json`. Bind it to the compaction-input, Claims, and Evidence hashes; list only explicit files below `work/active/` for archival.
5. Run `python -m tools.studyctl compact-finalize STUDY_ID --plan PATH`. If hashes or references changed, prepare again instead of bypassing the check.

Compaction is semantic organization, not deletion. Never archive authoritative records, delete Runs or output objects, hide contradictory Evidence, or treat generated STATUS files as sources of truth.
