# Repository Instructions

## Mathematical communication

Define every nonstandard symbol before use. Explain formulas and principles with mathematically rigorous, logically self-contained reasoning.

## Claim-to-Evidence scientific workflow

- Start from the approved Brief, active Claims, active formal artifacts, latest Checkpoint, and current Frontier.
- Use `work/` for informal exploration. Final Claims must not depend only on `work/` material.
- Before expensive, shared, scientifically consequential, or difficult-to-reverse work, run `python -m tools.studyctl check-formalization STUDY_ID` with the applicable flags. Create only the smallest required formal artifact.
- Treat completed Runs as immutable. Claims reference finalized Evidence; Evidence references reproducible Runs.
- Preserve contradictory Evidence and representative failures. Never silently aggregate incompatible Cohorts.
- Compact periodically; compaction organizes history and never deletes it.
- Never silently change the Brief, protected conditions, evaluator, data split, acceptance criteria, hard budget, Claim scope, or final scientific interpretation.
- The implementer is not the final reviewer. Run final scientific review in a fresh top-level read-only session. The human owns the final Verdict.

Use [the workflow guide](docs/scientific-agent-workflow.md) and the repository skills in `.agents/skills/` for the detailed operating sequence.
