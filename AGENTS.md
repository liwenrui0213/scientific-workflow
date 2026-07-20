# Repository Instructions

## Mathematical communication

Define every nonstandard symbol before use. Explain formulas and principles with mathematically rigorous, logically self-contained reasoning.

## Claim-to-Evidence scientific workflow

- When the user gives a new scientific idea without a Study ID, use `start-scientific-study`: draft the Study from the prompt instead of asking the user to fill workflow files. Stop at the human Brief-approval gate.
- Align just in time: inspect and draft before asking; ask only when a material ambiguity has no safe reversible default. At one decision boundary, ask one batch of at most three questions and never repeat the same unresolved question.
- Begin research or execution only from an approved Brief, active Claims, active formal artifacts, latest Checkpoint, and current Frontier.
- Use `work/` for informal exploration. Final Claims must not depend only on `work/` material.
- Before expensive, shared, scientifically consequential, or difficult-to-reverse work, run `python -m tools.studyctl check-formalization STUDY_ID` with the applicable flags. Create only the smallest required formal artifact.
- Treat completed Runs as immutable. Claims reference finalized Evidence; Evidence references reproducible Runs.
- Preserve contradictory Evidence and representative failures. Never silently aggregate incompatible Cohorts.
- Compact periodically; compaction organizes history and never deletes it.
- Never silently change the Brief, protected conditions, evaluator, data split, acceptance criteria, hard budget, Claim scope, or final scientific interpretation.
- The implementer is not the final reviewer. Run final scientific review in a fresh top-level read-only session. The human owns the final Verdict.

Use [the workflow guide](docs/scientific-agent-workflow.md) and the repository skills in `.agents/skills/` for the detailed operating sequence.
