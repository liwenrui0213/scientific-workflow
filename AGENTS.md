# Repository Instructions

## Mathematical communication

Define every nonstandard symbol before use. Explain formulas and principles with mathematically rigorous, logically self-contained reasoning.

## Claim-to-Evidence scientific workflow

- Treat `scientific-workflow/repository-profile.json` as the repository adaptation contract. Validate it before assuming Study, output, source, test, experiment, working-directory, or validation-command paths.
- When the user gives a new scientific idea without a Study ID, use `start-scientific-study`: draft the Study from the prompt instead of asking the user to fill workflow files. Stop at the human Brief-approval gate.
- Align just in time: inspect and draft before asking; ask only when a material ambiguity has no safe reversible default. At one decision boundary, ask one batch of at most three questions and never repeat the same unresolved question.
- Begin research or execution only from an approved Brief, active Claims, active formal artifacts, latest Checkpoint, and current Frontier.
- Keep prototypes and informal exploration in the Study's `work/`. Move adopted production code, experiment configurations, and tests into the host repository roots declared by the profile. Final Claims must not depend only on `work/` material.
- Before modifying host code or tests, use the Study branch and optional linked-worktree policy from the profile, then create `formal/CHANGESET.json` with `studyctl changeset-new`. The actual Git diff is authoritative; self-reported paths never grant write permission. Workflow enforcement code and configured protected paths are never Study outputs.
- Commit allowlisted host code, tests, and experiment assets; run `studyctl validate-changes` to execute the profile's native validation commands and seal `formal/VALIDATION.json`; then require `studyctl check-changes` to pass before an Evidence-producing Run. Renew a stale base anchor only through `studyctl changeset-renew`.
- Before expensive, shared, scientifically consequential, or difficult-to-reverse work, run `python -m tools.studyctl check-formalization STUDY_ID` with the applicable flags. Create only the smallest required formal artifact.
- Treat completed Runs as immutable. Claims reference finalized Evidence; Evidence references reproducible Runs.
- Store declared Run outputs only as new repository-relative regular files below the Git-ignored profile `object_root`; every declared output must exist and remain hash-stable. Declare all mutable or external runtime dependencies as inputs. Runs carry immutable governance and formal-artifact snapshots; Runs with unverifiable non-Git scope, other-Study changes, dirty host changes, missing validation proof, changed inputs, missing outputs, or altered snapshots/logs cannot enter Evidence.
- Preserve contradictory Evidence and representative failures. Never silently aggregate incompatible Cohorts.
- Compact periodically; compaction organizes history and never deletes it.
- Never silently change the Brief, protected conditions, evaluator, data split, acceptance criteria, hard budget, Claim scope, or final scientific interpretation.
- The implementer is not the final reviewer. Run final scientific review in a fresh top-level read-only session. The human owns the final Verdict.

Use [the workflow guide](docs/scientific-agent-workflow.md) and the repository skills in `.agents/skills/` for the detailed operating sequence.
