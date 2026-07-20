---
name: scientific-review
description: Independently review and try to falsify a Claim-to-Evidence scientific Study. Use after `studyctl review-packet` when checking protected conditions, mathematical or algorithmic implementation mapping, experiment fairness, Cohort compatibility, contradictory Evidence, reproducibility, formalization debt, or overclaiming before a human Verdict.
---

# Scientific Review

1. Start a fresh top-level Codex session with the project `scientific-reviewer` agent or equivalent read-only permissions. Do not use the implementation session as the final reviewer.
2. Open `generated/REVIEW_PACKET.json`, then inspect its referenced current repository profile, change-scope report, `formal/CHANGESET.json`, Brief, approval, formal artifacts, source symbols, tests, Run manifests and logs, each Run's governance and formal-artifact snapshots, Evidence versions, Claims, Checkpoint, and actual Git diff. Never trust STATUS, CHANGES, or packet summaries without checking their sources, and do not substitute current formal files for the immutable versions that governed an older Run.
3. Try to falsify implementation fidelity and every Claim. Check that the profile matches the host repository; adopted code/tests occupy native configured roots; actual committed, staged, unstaged, and untracked paths satisfy the active CHANGESET and protected enforcement policy; its immutable base anchor is valid; and the commit-bound validation proof records successful native commands. Self-reported changed paths are context, never evidence that scope was clean.
4. Also check protected-condition hashes, evaluator and split changes, mathematical mapping, fair baselines, exact Cohort fields, missing contradictions, dirty or irreproducible Runs, uncertainty, scope, and both under- and over-formalization. Recompute eligibility from sealed state; re-hash inputs, outputs, logs, governance snapshots, and formal-artifact snapshots; compare those snapshots with the manifest rather than with later live revisions; reject missing declared outputs or undeclared mutable Study scripts; and reject any legacy V1 Run used as Evidence.
5. Produce structured JSON matching `scientific-workflow/schemas/review.schema.json`. Every material finding must cite exact file paths and symbols plus applicable profile or CHANGESET hash, commit, Run ID, Evidence ID, Claim ID, or Checkpoint ID. Return or save this result outside the repository; the read-only reviewer must not write `generated/`.
6. Hand the JSON to the caller. In a separate trusted write-enabled session, the caller may import and render it with `python -m tools.studyctl review-render STUDY_ID --file PATH`. Treat `generated/REVIEW.md` as non-authoritative.

Do not edit scientific code, Evidence, Claims, Checkpoints, generated review files, or the human Verdict. Do not turn implementation acceptance into scientific acceptance.
