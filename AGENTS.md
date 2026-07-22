# Repository Instructions

## Mathematical communication

Define every nonstandard symbol before use. Explain formulas and principles with mathematically rigorous, logically self-contained reasoning.

## Claim-to-Evidence scientific workflow

- Treat `scientific-workflow/repository-profile.json` as the repository adaptation contract. Validate it before assuming Study, output, source, test, experiment, working-directory, or validation-command paths.

### Study Skill routing

| User request | Required route |
|---|---|
| One-off scientific discussion, explanation, or brainstorming | Answer directly. Do not create or modify a Study. |
| Explicit request to start, create, or persistently investigate a new scientific question | Use `start-scientific-study`; create one draft and stop at Brief approval. |
| Named existing Study ID | Run `python -m tools.studyctl resolve-study STUDY_ID`. Use `start-scientific-study` to revise the same unapproved draft, `scientific-study` for a fresh approved Brief, and report missing or invalid state instead of creating a replacement. |
| ID-less request to continue or resume previous/current research | Run `python -m tools.studyctl resolve-study`. Follow its unique draft or approved route. On zero, multiple, or invalid candidates, ask once and never initialize a new Study. |

- A Verdict records human interpretation; it does not by itself close a Study or remove it from ID-less resolution.
- Align just in time: inspect and draft before asking; ask only when a material ambiguity has no safe reversible default. At one decision boundary, ask one batch of at most three questions and never repeat the same unresolved question.
- Begin research or execution only after `studyctl validate` and from generated `ACTIVE_CONTEXT.json`: it contains bounded Claim and Frontier locators (IDs, short previews, counts and hashes), path/hash/size selectors for the approved Brief, active formal artifacts, and latest Checkpoint, plus resumable Confirmation drafts, pending/running slots, and records awaiting Evidence. Resume those locators before creating replacements. Read authoritative source sections by current question or ID; generated projections are never authority.
- Keep prototypes and informal exploration in the Study's `work/`. Move adopted production code, experiment configurations, and tests into the host repository roots declared by the profile. Final Claims must not depend only on `work/` material.
- Before modifying host code or tests, use the Study branch and optional linked-worktree policy from the profile, then create `formal/CHANGESET.json` with `studyctl changeset-new`. The actual Git diff is authoritative; self-reported paths never grant write permission. Workflow enforcement code and configured protected paths are never Study outputs.
- Commit allowlisted host code, tests, and experiment assets; run `studyctl validate-changes` to execute the profile's native validation commands and seal `formal/VALIDATION.json`; then require `studyctl check-changes` to pass before an Evidence-producing Run. Renew a stale base anchor only through `studyctl changeset-renew`.
- Before expensive, shared, scientifically consequential, or difficult-to-reverse work, run `python -m tools.studyctl check-formalization STUDY_ID` with the applicable flags. Create only the smallest required formal artifact.
- Treat completed Runs as immutable. Claims reference finalized Evidence; Evidence references reproducible Runs.
- Finalized new-version Evidence must state the observation-to-Claim reasoning bridge, auxiliary assumptions, competing explanations, and conditions that would overturn its assessment. A result summary alone is not a scientific argument.
- Treat every Run as exploratory by default. Create and freeze a minimal Confirmation Record only before promoting a result to a high-strength Claim; only new Runs bound to its exact Claim, candidate, protocol, evaluator, held-out conditions, and unused slots are confirmatory. Never relabel an exploratory or legacy Run as confirmatory.
- Store declared Run outputs only as new repository-relative regular files below the Git-ignored profile `object_root`; every declared output must exist and remain hash-stable. Declare all mutable or external runtime dependencies as inputs. Runs carry immutable governance and formal-artifact snapshots; Runs with unverifiable non-Git scope, other-Study changes, dirty host changes, missing validation proof, changed inputs, missing outputs, or altered snapshots/logs cannot enter Evidence.
- Preserve contradictory Evidence and representative failures. Never silently aggregate incompatible Cohorts.
- Exploratory Evidence may support `under_test` or scoped `partially_supported` Claims. A `numerically_supported` Claim requires complete confirmatory Evidence with either workflow-observed fresh held-out conditions or an explicitly justified `not_applicable` condition; mixed Evidence must expose its exploratory and confirmatory parts and cannot hide omitted confirmation attempts.
- Treat `status` compaction pressure as the deterministic trigger: compact at soft pressure before discretionary growth; at hard pressure, compact before another Run, Evidence draft, or review. Compaction remains available and organizes history without deleting it.
- Never silently change the Brief, protected conditions, evaluator, data split, acceptance criteria, hard budget, Claim scope, or final scientific interpretation.
- The implementer is not the final reviewer. Run final scientific review in a fresh top-level read-only session. The human owns the final Verdict.

Use [the workflow guide](docs/scientific-agent-workflow.md) and the repository skills in `.agents/skills/` for the detailed operating sequence.
