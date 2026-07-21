---
name: scientific-study
description: Resume and execute an existing repository-native Claim-to-Evidence scientific Study after its Brief has been human-approved. Use when the user names an existing Study or asks to continue its long-running research, progressively formalize consequential decisions, execute important computations as immutable Runs, and connect Runs to Evidence and Claims without crossing protected conditions silently. For a new natural-language idea without an existing Study, use start-scientific-study instead.
---

# Scientific Study

## Authoritative inputs

1. Confirm that the named Study exists and its active Brief has a fresh human
   approval. Route a new idea or unapproved intake draft to
   `start-scientific-study`.
2. Validate `scientific-workflow/repository-profile.json`; never infer host
   paths from a generic layout. If workflow infrastructure is missing, invoke
   `bootstrap-scientific-workflow` only after the user authorizes installation
   or migration.
3. Read only the bounded active context: approved Brief and approval,
   `CLAIMS.json`, active `formal/` artifacts, latest Checkpoint, and current
   Frontier. Do not load all historical Runs or notes by default.
4. Treat repository source, tests, actual Git state, sealed Runs, finalized
   Evidence, and hash-pinned records as authoritative for what happened. Treat
   generated projections and Agent explanations only as indexes or hypotheses.

## Align only at material boundaries

Do not ask the human to resolve every uncertainty. First inspect the approved active context and repository evidence, then classify the ambiguity:

- continue with a stated conservative, reversible assumption when that cannot change the approved scientific meaning or protected conditions;
- record the question and defer it when it matters only at a later method, evaluator, protocol, compute, Evidence, or interpretation boundary; or
- ask now only when different plausible answers would materially change the authorized Claim, a protected condition, the hard budget, or an immediate expensive or hard-to-reverse action, and no safe reversible default exists.

At one boundary, ask one compact batch of at most three independent questions. State the current interpretation and why each answer is required. Use at most one follow-up batch, only if the answer exposes a genuinely new material branch; never repeat or rephrase the same unresolved question. If alignment remains unresolved, pause only the blocked action and continue safe independent read-only or low-cost reversible work when useful.

Read [research strategy](references/research-strategy.md) when choosing among
competing hypotheses or experiments, deciding whether Evidence is discriminating,
or deciding whether to continue, compact, review, or escalate. Do not load it
for a purely mechanical resume, validation, or rendering operation.

## Workflow

1. Keep provisional derivations, ideas, scripts, failures, and plans under
   `work/active/`. Promote adopted production code, experiment configurations,
   and tests to the profile's native roots; never use `work/` as an alternative
   production tree or sole Claim support.
2. Before host edits, commit the approved intake, enter the required Study
   branch and linked worktree policy, and create the narrowest
   `formal/CHANGESET.json` with `studyctl changeset-new`. After explicit base
   synchronization, use `changeset-renew`; never hand-edit the anchor.
3. Before consequential work, run `studyctl check-formalization` with honest
   GPU-hour, CPU-hour, storage, and semantic flags. Create only the smallest
   required artifact. Treat every registered reservation—including failed,
   interrupted, incomplete, or still-running work—as consuming the Study's
   cumulative hard budget.
4. After source, test, or experiment edits, run focused checks, commit the
   allowlisted changes, then run `studyctl validate-changes` and
   `studyctl check-changes`.
5. Execute consequential calculations through `studyctl run`. Declare all
   mutable or external inputs and every new output below the configured
   `object_root`; preserve the Run and its sealed snapshots. Never treat a
   `running` or `incomplete` Run as Evidence-ready.
6. Create Evidence only from eligible sealed Runs (current V3, or an intact
   pre-budget V2 Run after explicit ledger migration). Never use V1,
   `running`, or `incomplete` Runs. Fill analysis, result,
   scope, uncertainty, limitations, assessment, Run roles, and any Cohort
   compatibility justification; seal with `evidence-finalize`.
7. Update Claims only with finalized `{evidence_id, version, sha256}` references.
   Preserve contradictory Evidence and representative failed directions.

## Hard gates

- An allowlist or `--changed-path` declaration never overrides the actual Git
  diff, protected paths, another Study, or repository enforcement.
- Dirty or non-Git host scope, stale or failed native validation, undeclared
  mutable dependencies, missing or altered outputs, incompatible Cohorts, or
  changed sealed snapshots make a Run ineligible for Evidence.
- Never silently change the Brief, evaluator principles, data split, acceptance
  criteria, hard budget, or authorized Claim scope. Open a new Brief version
  where appropriate and align with the human at that boundary.
- Numerical success is not proof, and implementation acceptance is not
  scientific acceptance. Do not upgrade a Claim beyond its finalized Evidence.

## Output and handoff

Leave the Study with valid active records, explicit open questions, and a small
next-action Frontier. Invoke `research-compaction` when history obscures current
state, and `scientific-review` only after a review packet is prepared for a
fresh read-only reviewer. Stop for human action only at a protected boundary,
hard-budget decision, unresolved material ambiguity, or final Verdict.
