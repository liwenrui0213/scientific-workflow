---
name: scientific-review
description: Independently review and try to falsify a Claim-to-Evidence scientific Study before a human Verdict. Use after `python -m tools.studyctl review-packet STUDY_ID`.
---

# Scientific Review

## Authoritative inputs

Use a fresh top-level `scientific-reviewer` session or equivalent read-only
permissions. Treat `generated/REVIEW_PACKET.json` as an index. Inspect its
profile, Git state and diff, CHANGESET, validation, Brief, source/tests, Runs and
Artifacts, Observations, Evidence, Claims, logical-ledger files, and referenced
formal records. Inspect Intent, ControlGraphSpec, and PLAN only when present or
required by the applicable gate. Use immutable Run snapshots, not later live
files. Checkpoints and sequences authenticate continuity but do not supply
scientific support. In `ACTIVE_CONTEXT.json`, mutable drafts belong only under
`workspace.graph_record_drafts`; finalized locators belong under
`graph_records`.

Read [the adversarial review rubric](references/adversarial-review-rubric.md)
before assigning severity, Claim scope, checks, or human questions.

## Workflow

1. Trace each Claim backward through Claim-specific Evidence, exact Observation
   when used, and eligible Runs/Artifacts. If Intent or PLAN exists or was
   required, separately trace the EvidenceGap, exact Intent reference,
   finalized graph, byte-identical PLAN, source, validation, and Run.
2. For Run v5, inspect the independent `intent_binding` first, then distinguish
   exact `control_binding` from `null`; active PLAN does not bind ordinary Runs.
   Verify graph, Intent consistency, node ID, and node-spec hash.
   Observation v3 and Evidence v5 must reproduce Intent refs derived from their
   source Runs; Evidence must also bind the addressed Claim
   statement/scope digest.
3. Recompute scope and eligibility; re-hash inputs, outputs, logs, governance,
   and formal snapshots. Check protected conditions, evaluator/split,
   mathematical mapping, baselines, Cohorts, uncertainty, contradictions,
   exclusions, Registry promotion, frozen outcome contracts, and complete
   confirmation campaigns. A high-strength Claim must have bounded scope and
   an explicit resolved/scope-limited synthesis for contradictory Evidence.
   Challenge every inference, assumption, alternative, and falsifier. Keep
   immutable failure facts, candidate causes, and Evidence-backed lessons
   separate.
4. Perform the smallest independent checks needed for high-risk assertions.
   Report verified defects separately from risks and open questions. Produce
   `review.schema.json`-valid JSON with exact references and proportionate
   recommendations.

## Hard gates

- Never trust a projection, implementation explanation, or self-reported scope
  without its authority. Do not review your own implementation.
- Report a gate-required missing PLAN. Reject stale/inexact PLAN bindings, but
  do not require PLAN for ordinary work. ControlGraph v2 has open topology and
  opaque control policies; `studyctl` does not execute the whole graph. Never
  infer `control_binding`, graph completion, or Claim support.
- Reject Evidence use of missing outputs, altered seals, undeclared mutable
  dependencies, unproved Cohorts, ineligible/legacy Runs, concealed attempts,
  or mislabeled exploratory/mixed work. Preserve failed attempts as facts.
- Observation has no Claim assessment. Reject mutable/stale references, hidden
  exclusions, duplicate analysis, unregistered promotion, or Registry drift.
- Observation/Evidence sequences must match the complete finalized inventory.
  Explicit recovery moves forward by exactly one uniquely reconstructable
  interrupted finalization; it never deletes or rolls back history.
- Do not edit code or Study state, and never equate implementation or numerical
  success with scientific acceptance.
- Verify that any Verdict binds immutable archived Review and Review Packet
  digests, or carries an explicit no-Review waiver.

## Output and handoff

Return the structured review JSON outside the repository. Hand it to a separate
trusted write-enabled caller, which may run
`python -m tools.studyctl review-render <STUDY_ID> --file <PATH>`. Surface human
questions but never record the Verdict. Import must archive the exact Review
and packet that a later Verdict binds. Only explicit human selection permits a
separate write-enabled Agent to record it. Return corrections to
`scientific-study`; do not implement them here.
