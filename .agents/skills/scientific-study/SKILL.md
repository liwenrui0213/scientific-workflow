---
name: scientific-study
description: Resume an existing approved Claim-to-Evidence Study selected by ID or unambiguous resolve-study. Use for ongoing research, immutable Runs, Evidence, and Claims. Route new investigations and drafts to start-scientific-study; answer one-off discussion directly.
---

# Scientific Study

## Authoritative inputs

1. Apply the `AGENTS.md` routing matrix. For ID-less continuation, run
   `python -m tools.studyctl resolve-study`. Continue its unique approved Study;
   send a unique draft to `start-scientific-study` under the same ID. On zero,
   multiple, invalid, or unsafe candidates, ask once and never run `init`.
   Resolve a named continuation through the same command. A Verdict does not
   close the Study. Unsupported Claims, Evidence, or Checkpoint schemas fail
   closed. Use a Git-pinned compatible workflow or an explicit reviewed offline
   migration; never replace a Study or rewrite old records.
2. Require a fresh human-approved Brief and a valid
   `scientific-workflow/repository-profile.json`. Invoke bootstrap only when
   workflow infrastructure is missing and the user authorizes installation.
3. Run `studyctl validate` and `studyctl context`, then start from
   `generated/ACTIVE_CONTEXT.json`. It locates selected active Claims, Frontier, Brief,
   formal artifacts, Checkpoint, decisive Observation locators, editable
   Confirmation drafts, pending/running slots, and records awaiting Evidence.
   Resume those before creating another
   Confirmation. Read authoritative sources only for the current question or
   selected ID. Open complete `CLAIMS.json` only to validate or update it.
   `STATUS.md` is a human-facing projection, not authority.
4. Repository source, tests, Git state, sealed Runs, finalized Observation and
   Evidence records, and hash-pinned records establish what happened. Generated
   views and Agent explanations are indexes or hypotheses, never authority.

## Align only at material boundaries

Inspect before asking. Use a stated conservative, reversible assumption when
it cannot change approved scientific meaning or protected conditions. Defer a
question that matters only at a later method, evaluator, protocol, compute,
Evidence, or interpretation boundary. Ask now only when plausible answers
materially change an authorized Claim, protected condition, hard budget, or
immediate expensive or hard-to-reverse action and no safe default exists.

Ask one batch of at most three questions with the interpretation and blocking
reason. Never repeat an unresolved question; pause only the blocked action.

Read [research strategy](references/research-strategy.md) only when selecting
hypotheses or discriminating experiments.

## Workflow

1. Keep provisional derivations, ideas, scripts, failures, and plans under
   `work/active/`. Promote adopted code, experiment configurations, and tests
   to profile-declared host roots. Work notes cannot solely support a Claim.
2. Before host edits, commit approved intake, follow the Study branch/worktree
   policy, and create the narrowest `formal/CHANGESET.json` with
   `changeset-new`. Renew a synchronized base only through `changeset-renew`.
3. Before consequential work, run `check-formalization` with honest compute,
   storage, and semantic flags. Create only the smallest required artifact.
   Every registered reservation, including failed or incomplete work, consumes
   the cumulative hard budget.
4. After host edits, run focused tests, commit allowlisted changes, then run
   `validate-changes` and `check-changes`.
5. Execute consequential calculations through `studyctl run`. Declare mutable
   or external inputs and all new outputs below `object_root`. A `running` or
   `incomplete` Run cannot enter Evidence. Runs default to exploratory.
6. Keep observations inline unless `observation-trigger-list STUDY_ID` shows an
   applicable condition. Missing conditions require a reviewed proposal,
   explicit human adoption, and protected Registry maintenance; structural
   additions require deterministic code. Finalized Observations bind Registry,
   Runs, and analysis, without Claim assessment.
7. Finalize one-Claim Evidence from eligible Runs or an exact Observation.
   Before numerical support, freeze Claim, candidate, protocol, evaluator,
   held-out conditions, analysis, and slots; disclose the complete campaign,
   roles, exclusions, uncertainty, limitations, inference bridge, alternatives,
   and falsifiers. Do not confirm routine exploration.
8. Update Claims only with finalized `{evidence_id, version, sha256}` refs and
   preserve contradictions. Current Claims use lifecycle `active`. Remove one
   from the Frontier only by marking it `retired`, or create a replacement ID
   and mark the old one `superseded` with `superseded_by`. A lifecycle edit is
   provisional until the next immutable Checkpoint seals it.
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
  acceptance. Do not upgrade a Claim beyond finalized Evidence.
- Observation has no Claim assessment. Evidence pins its exact version/hash and
  remains Claim-specific.
- Never relabel an exploratory or legacy Run as confirmatory. Confirmatory
  Evidence covers every current-campaign slot and attempt; mixed Evidence
  labels both bases.

## Output and handoff

Keep records, questions, and Frontier. Use `research-compaction`
under pressure and `scientific-review` from a packet in a read-only
session. For Verdict, show Claims and decisions with rationale, scope, and conditions.
Explicit human selection or adoption authorizes version-2 input under profile
`object_root` and `studyctl
verdict <STUDY_ID> --agent-initiated --file <PATH>`; otherwise ask once. Silence,
completion, review success, or Agent recommendation is not acceptance.
The human decides; a write-enabled Agent records; `studyctl` binds hashes.
