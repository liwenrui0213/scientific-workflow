---
name: scientific-study
description: Resume an existing approved Claim-to-Evidence Study selected by ID or unambiguous resolve-study. Use for ongoing research, immutable Runs, Evidence, and Claims. Route new investigations and drafts to start-scientific-study; answer one-off discussion directly.
---

# Scientific Study

## Authoritative inputs

Apply the `AGENTS.md` routing matrix. For ID-less continuation, run
`python -m tools.studyctl resolve-study`: follow its unique route, or ask once
on zero, multiple, or invalid candidates; never run `init`. Resolve named
Studies likewise. A Verdict does not close the Study.

Require an approved Brief and
`scientific-workflow/repository-profile.json`. Using
`python -m tools.studyctl`, run `validate <STUDY_ID>` and
`context <STUDY_ID>`, then begin from `generated/ACTIVE_CONTEXT.json`.
`context` revalidates authority and preserves the prior valid projection on
inconsistency. Resume selected active Claims, Frontier, records, Checkpoint, and work
before replacement. Mutable Intent/Plan drafts occur only under
`workspace.graph_record_drafts`; finalized locators occur under
`graph_records`. Generated views and Checkpoints are bounded indexes, not
scientific authority.

Repository state, sealed Runs, records, and sequences establish what happened;
execution and continuity do not support a Claim.

Read [research strategy](references/research-strategy.md) only when selecting
hypotheses or discriminating experiments.

## Workflow

1. Inspect first and ask once only when a material scientific choice has no
   safe reversible default. Keep raw exploration in `work/active/`; promote
   adopted code, configurations, and tests to profile host roots.
2. Use `changeset-new` before host edits, follow the Study branch/worktree
   policy, and use `changeset-renew` for a stale base. Test and commit
   allowlisted changes, then run `validate-changes` and `check-changes`.
3. Before consequential work, run `check-formalization` with honest flags.
   Finalize ExperimentIntent only for a durable reason-for-evidence boundary;
   `--intent` may bind it directly without PLAN. Use ControlGraphSpec/PLAN only
   when gated or operationally warranted. ControlGraph v2 keeps topology open;
   `studyctl` validates references but does not execute it.
   Explicitly `plan-deactivate` a completed or abandoned topology; the
   lifecycle event remains immutable, while ordinary Runs never inherit or
   snapshot an active PLAN without `--plan-node`.
4. Run calculations through `python -m tools.studyctl run`, declaring mutable/external inputs
   and new outputs below `object_root`. Runs are exploratory by default. Use
   `--intent ID --intent-version N` for a durable why without a PLAN, and
   `--plan-node` only to bind one Run v5 exactly to its graph, Intent, node, and
   node-spec hash; both Intent bindings must agree. An active PLAN never binds
   ordinary Runs implicitly. A
   missing output is recorded absent and makes the Run Evidence-ineligible
   without erasing the attempt or budget reservation.
5. Keep observations inline unless the exact versioned Registry trigger
   applies. Observation v3 binds source Runs, analysis, Registry, and the exact
   finalized Intent refs derived from each Run's binding, but has no Claim
   assessment.
6. Finalize Claim-specific Evidence only from eligible Runs or an exact
   Observation. Evidence v5 binds the addressed Claim statement/scope digest
   and those source-Run-derived Intent refs. Disclose inference, assumptions,
   alternatives, falsifiers, uncertainty, limitations, contradictions, and all
   campaign attempts. Abandoned campaigns remain historical or negative
   context, never supporting confirmatory basis.
7. Update Claims only through finalized Evidence. `numerically_supported`
   requires bounded scope, frozen Confirmation outcome contracts, complete
   active-campaign coverage, traceable Observation/Artifact results, and an
   explicit synthesis for contradictory Evidence. An old supporting Evidence
   remains immutable after campaign abandonment but loses high-strength
   eligibility. Preserve failure facts in Runs; treat causes as hypotheses and reusable lessons as scoped
   Evidence-backed Claims. Treat a nonterminal Run only as `in_progress`; do
   not infer missing output or Evidence ineligibility until terminal state.
   Retire a Frontier Claim or supersede it; lifecycle changes become terminal
   when sealed by Checkpoint.
8. Regenerate status. At soft compaction pressure, invoke
   `research-compaction` before discretionary growth; at hard pressure, compact
   before another Run, Evidence draft, or review packet. For one uniquely
   interrupted Observation/Evidence/Confirmation finalization, use only its explicit
   one-record forward-recovery command.

## Hard gates

- One-off discussion creates no Study; new persistent work belongs to
  `start-scientific-study`.
- Actual Git diff, protected paths, workflow enforcement, hard budget, Brief,
  protected conditions, evaluator, split, acceptance criteria, and Claim scope
  cannot be overridden or silently changed.
- Dirty/unverifiable scope, stale validation, undeclared dependencies, altered
  snapshots, changed or absent outputs, or unproved Cohorts bar Evidence use.
- PLAN/Run completion is control, not support. Never rewrite finalized graph
  records, demand unnecessary PLANs, or upgrade a Claim beyond Evidence.
- Abandon a Confirmation campaign only from a decision-only JSON object with
  exactly `input_version: 1`, the derived `campaign_id`, a non-empty
  `rationale`, and
  `authorization: {source: explicit_user_instruction, instruction: ...}`.
  Invoke `confirmation-abandon STUDY_ID CONFIRMATION_ID --file PATH` only after
  that explicit instruction. Never infer authorization, select individual
  slots or records, or describe cooperative instruction provenance as
  cryptographic identity proof. Preserve the immutable abandonment locator,
  and require the generated predecessor binding plus a non-empty
  `restart_rationale` before freezing a new campaign for the same exact Claim
  version. `CONFIRMATIONS.sequence.json` binds authority; use
  `recover-confirmation-sequence` for one interrupted
  publish and `migrate-confirmation-sequence` only for non-empty pre-sequence
  v2/v3 history. Treat a pre-transition draft as stale, not resumable; archive
  it explicitly with
  `confirmation-draft-discard --reason ...` before creating a replacement for
  the same exact Claim-version set.
- Never relabel exploratory or legacy Runs as confirmatory, conceal attempts,
  assign Claim semantics to Observation, or rewrite finalized history.

## Output and handoff

Preserve records and Frontier. Hand compaction to `research-compaction` and
final review to `scientific-review` in a fresh read-only session. Only explicit human adoption authorizes
`python -m tools.studyctl verdict <STUDY_ID> --agent-initiated --file <PATH>`;
silence, completion, review success, or Agent advice does not. Imported Review
and Verdict occurrences must remain bound by
`REVIEW_VERDICTS.sequence.json`; never reconstruct a missing lower history.
An interactive decision file that requests a Review waiver still requires the
separate typed waiver phrase in that terminal session.
