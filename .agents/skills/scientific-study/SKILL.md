---
name: scientific-study
description: Resume an existing approved Claim-to-Evidence Study selected by ID or unambiguous resolve-study. Use for ongoing research, immutable Runs, Evidence, and Claims. Route new investigations and drafts to start-scientific-study; answer one-off discussion directly.
---

# Scientific Study

## Authoritative inputs

1. Apply the `AGENTS.md` routing matrix. For ID-less continuation, run
   `python -m tools.studyctl resolve-study`. Continue its unique approved Study;
   send a unique draft to `start-scientific-study` under the same ID. On zero,
   multiple, or invalid candidates, ask once; never run `init`. Resolve named
   Studies likewise. A Verdict does not close the Study. Unsupported record
   schemas fail closed; pin a compatible workflow or use reviewed migration,
   never replacement or history rewriting.
2. Require a fresh human-approved Brief and a valid
   `scientific-workflow/repository-profile.json`. Invoke bootstrap only when
   workflow infrastructure is missing and the user authorizes installation.
3. Run `python -m tools.studyctl validate <STUDY_ID>` and
   `python -m tools.studyctl context <STUDY_ID>`, then start from
   `generated/ACTIVE_CONTEXT.json`. It locates selected active Claims, the
   Frontier, Brief, formal
   artifacts, Checkpoint, bounded graph records, decisive Observations, and
   resumable Confirmation work. Resume those before creating replacements.
   Read authority only for the current question or ID; open complete
   `CLAIMS.json` only to validate or update it. `STATUS.md` is not authority.
4. Repository source, tests, Git state, sealed Runs, finalized Observation and
   Evidence records, and hash-pinned records establish what happened. Generated
   views and Agent explanations are indexes or hypotheses, never authority.

## Align only at material boundaries

Inspect first. Use a stated conservative, reversible assumption when scientific
meaning and protected conditions cannot change. Defer later-boundary questions.
Ask only when plausible answers change an authorized Claim, protected
condition, hard budget, or immediate consequential action and no safe default
exists. Ask at most three questions once; pause only the blocked action.

Read [research strategy](references/research-strategy.md) only when selecting
hypotheses or discriminating experiments.

## Workflow

1. Keep raw exploration under `work/active/`, outside the cognitive graph;
   promote adopted code, configurations, and tests to host roots. When
   warranted, finalize an ExperimentIntent (why), then its exact-bound
   ControlGraphSpec (how). The Agent may freely revise drafts.
2. Before host edits, commit approved intake, follow the Study branch/worktree
   policy, and create the narrowest `formal/CHANGESET.json` with
   `changeset-new`. Renew a synchronized base only through `changeset-renew`.
3. Before consequential work, run `check-formalization` with honest compute,
   storage, and semantic flags. Create only the smallest required artifact.
   Every registered reservation, including failed or incomplete work, consumes
   the cumulative hard budget.
4. After host edits, run focused tests, commit allowlisted changes, then run
   `validate-changes` and `check-changes`.
5. Execute consequential calculations through
   `python -m tools.studyctl run <STUDY_ID>`.
   Declare mutable
   or external inputs and all new outputs below `object_root`. A `running` or
   `incomplete` Run cannot enter Evidence. Runs default to exploratory.
6. Keep observations inline unless the versioned Registry shows an applicable
   promotion trigger. New triggers require review, human adoption, and
   protected maintenance. Observations bind Registry, Runs, and analysis,
   without Claim assessment.
7. Finalize one-Claim Evidence from eligible Runs or an exact Observation.
   Before numerical support, freeze the confirmation contract and disclose the
   complete campaign, uncertainty, limitations, inference, alternatives, and
   falsifiers. Do not confirm routine exploration.
8. Update Claims only with finalized Evidence refs; preserve contradictions.
   Remove a Frontier Claim only by retiring it, or supersede it with a new ID.
   Lifecycle edits remain provisional until Checkpoint sealing.
9. Regenerate status after each consequential batch. At soft compaction
   pressure, invoke `research-compaction` before discretionary growth. At hard
   pressure, compact before another Run, Evidence draft, or review packet;
   validation, status, and compaction remain available.

## Hard gates

- A one-off discussion creates no Study. An explicit new persistent
  investigation belongs to `start-scientific-study`.
- An allowlist never overrides the actual Git diff, protected paths, another
  Study, or workflow enforcement.
- Dirty or unverifiable host scope, stale validation, undeclared dependencies,
  changed outputs, incompatible Cohorts, or altered snapshots make a Run
  ineligible for Evidence.
- Never silently change the Brief, evaluator principles, data split,
  acceptance criteria, hard budget, or authorized Claim scope. Use the normal
  human-authorized revision path.
- Once an immutable Checkpoint seals `retired` or `superseded`, that lifecycle
  and its replacement link are terminal. Before sealing, correct a mistaken
  draft explicitly; afterwards use a new Claim ID instead of rewriting history.
- Numerical success is not proof; implementation acceptance is not scientific
  acceptance. Control-graph completion does not evaluate
  `assessment_semantics` or update a Claim. V1 freezes these semantics for
  audit but does not mechanically bind typed criterion results into Evidence.
  Do not upgrade a Claim beyond finalized Evidence.
- Never rewrite a finalized ExperimentIntent or ControlGraphSpec, activate a
  Plan for a superseded Intent, or silently change its assessment criteria.
- Observation has no Claim assessment. Evidence pins its exact version/hash and
  remains Claim-specific.
- Never relabel an exploratory or legacy Run as confirmatory. Confirmatory
  Evidence covers every current-campaign slot and attempt; mixed Evidence
  labels both bases.

## Output and handoff

Preserve records, questions, and Frontier. Use `research-compaction` under
pressure and `scientific-review` on a packet in a fresh read-only session. For Verdict, show decisions with rationale,
scope, and conditions. Explicit human adoption authorizes version-2 input and
`python -m tools.studyctl verdict <STUDY_ID> --agent-initiated --file <PATH>`;
otherwise ask
once. Silence, completion, review success, or Agent advice is not acceptance.
