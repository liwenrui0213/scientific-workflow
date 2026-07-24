---
name: scientific-study
description: Resume an existing approved Claim-to-Evidence Study selected by ID or unambiguous resolve-study. Use for ongoing research, immutable Runs, Evidence, and Claims. Route new investigations and drafts to start-scientific-study; answer one-off discussion directly.
---

# Scientific Study

## Authoritative inputs

Apply the `AGENTS.md` routing matrix. For ID-less continuation, run
`python -m tools.studyctl resolve-study`: continue its unique approved Study,
send a unique draft to `start-scientific-study`, and ask once on zero, multiple,
or invalid candidates; never run `init`. Resolve a named Study likewise. A
Verdict does not close the Study.

Require a fresh approved Brief and valid
`scientific-workflow/repository-profile.json`. Using
`python -m tools.studyctl`, run `validate <STUDY_ID>` and
`context <STUDY_ID>`, then begin from `generated/ACTIVE_CONTEXT.json`. Resume its selected active
Claims, Frontier, formal records, latest derived Checkpoint, and pending work
before creating replacements. Mutable Intent/Plan drafts occur only under
`workspace.graph_record_drafts`; finalized locators occur under
`graph_records`. Generated views and Checkpoints are bounded indexes, not
scientific authority.

Repository source, tests, Git state, sealed Runs, finalized Observation and
Evidence records, and their sequences establish what happened. The Run ledger
plus Observation, Evidence, Checkpoint, and graph-record sequences form one
logical integrity family, but neither execution nor continuity supports a
Claim.

Read [research strategy](references/research-strategy.md) only when selecting
hypotheses or discriminating experiments.

## Workflow

1. Inspect first and ask once only when a material scientific choice has no
   safe reversible default. Keep raw exploration in `work/active/`; promote
   adopted code, configurations, and tests to profile host roots.
2. Use `python -m tools.studyctl changeset-new` before host edits, follow the Study branch/worktree
   policy, and use `changeset-renew` for a stale base. After edits, run focused
   tests, commit allowlisted changes, then run `validate-changes` and
   `check-changes`.
3. Before consequential work, run `check-formalization` with honest flags.
   Finalize an ExperimentIntent only when a durable reason-for-evidence boundary
   is warranted; its `assessment_semantics` may be `null`, and `--intent` may
   bind it directly to a Run without a PLAN. Create and activate
   a ControlGraphSpec/PLAN only when the gate or real orchestration complexity
   requires it. ControlGraph v2 leaves topology, node kinds, and retry policy
   open; `studyctl` validates references but does not execute the graph.
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
   finalized Intent refs derived from each Run's independent binding, but has no Claim
   assessment.
6. Finalize Claim-specific Evidence only from eligible Runs or an exact
   Observation. Evidence v5 binds the addressed Claim statement/scope digest
   and those source-Run-derived Intent refs. Disclose inference, assumptions,
   alternatives, falsifiers, uncertainty, limitations, contradictions, and all
   current confirmation-campaign attempts.
7. Update Claims only through finalized Evidence. `numerically_supported`
   requires bounded scope, frozen Confirmation outcome contracts, complete
   campaign coverage, traceable Observation/Artifact results, and an explicit
   synthesis for contradictory Evidence. Preserve failure facts in
   Runs; treat causes as hypotheses and reusable lessons as scoped
   Evidence-backed Claims. Retire a Frontier Claim or supersede it with a new
   ID; lifecycle changes become terminal when sealed by Checkpoint.
8. Regenerate status. At soft compaction pressure, invoke
   `research-compaction` before discretionary growth; at hard pressure, compact
   before another Run, Evidence draft, or review packet. For one uniquely
   interrupted Observation/Evidence finalization, use only its explicit
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
- Never relabel exploratory or legacy Runs as confirmatory, conceal attempts,
  assign Claim semantics to Observation, or rewrite finalized history.

## Output and handoff

Preserve records, questions, and Frontier. Use `research-compaction` under
pressure and `scientific-review` in a fresh read-only session. For Verdict,
present decision, rationale, scope, and conditions. Only explicit human adoption
authorizes
`python -m tools.studyctl verdict <STUDY_ID> --agent-initiated --file <PATH>`;
silence, completion, review success, or Agent advice does not.
