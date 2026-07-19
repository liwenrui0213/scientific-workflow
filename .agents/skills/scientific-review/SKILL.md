---
name: scientific-review
description: Independently review and try to falsify a Claim-to-Evidence scientific Study. Use after `studyctl review-packet` when checking protected conditions, mathematical or algorithmic implementation mapping, experiment fairness, Cohort compatibility, contradictory Evidence, reproducibility, formalization debt, or overclaiming before a human Verdict.
---

# Scientific Review

1. Start a fresh top-level Codex session with the project `scientific-reviewer` agent or equivalent read-only permissions. Do not use the implementation session as the final reviewer.
2. Open `generated/REVIEW_PACKET.json`, then inspect its referenced Brief, approval, formal artifacts, source symbols, tests, Run manifests and logs, Evidence versions, Claims, Checkpoint, and diff. Never trust STATUS or packet summaries without checking their sources.
3. Try to falsify implementation fidelity and every Claim. Check protected-condition hashes, evaluator and split changes, mathematical mapping, fair baselines, exact Cohort fields, missing contradictions, dirty or irreproducible Runs, uncertainty, scope, and both under- and over-formalization.
4. Produce structured JSON matching `scientific-workflow/schemas/review.schema.json`. Every material finding must cite exact file paths and symbols plus applicable commit, Run ID, Evidence ID, Claim ID, or Checkpoint ID.
5. Import and render it with `python -m tools.studyctl review-render STUDY_ID --file PATH`. Treat `generated/REVIEW.md` as non-authoritative.

Do not edit scientific code, Evidence, Claims, Checkpoints, generated review files, or the human Verdict. Do not turn implementation acceptance into scientific acceptance.
