---
name: scientific-review
description: Independently review and try to falsify a Claim-to-Evidence scientific Study before a human Verdict. Use after `python -m tools.studyctl review-packet STUDY_ID`.
---

# Scientific Review

## Authoritative inputs

Use a fresh read-only `scientific-reviewer` session. Treat
`generated/REVIEW_PACKET.json` as an index. Inspect its
profile, Git state and diff, CHANGESET, validation, Brief, source/tests, Runs and
Artifacts, Observations, Evidence, Claims, logical-ledger files, and referenced
formal records. Inspect Intent, ControlGraphSpec, and PLAN only when present or
gate-required. Use immutable Run snapshots, not later live
files. Checkpoints and sequences authenticate continuity but do not supply
scientific support. In `ACTIVE_CONTEXT.json`, mutable drafts belong only under
`workspace.graph_record_drafts`; finalized locators belong under
`graph_records`. A current Review Packet is generated from a committed, clean
scientific worktree and exact-binds the commit, Brief, latest Checkpoint,
complete active-Claim and finalized-Evidence inventories, and ACTIVE_CONTEXT.
Any identity change requires a new packet and Review.

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
   confirmation campaigns. Inspect any immutable whole-campaign abandonment:
   its unfinished slots are not pending work and its Runs cannot supply new
   supporting confirmatory strength, although negative and failure history
   remains available. A restarted campaign must bind the exact predecessor
   abandonment. A high-strength Claim must have bounded scope and
   an explicit resolved/scope-limited synthesis for contradictory Evidence.
   Challenge every inference, assumption, alternative, and falsifier. Keep
   failure facts, candidate causes, and Evidence-backed lessons separate.
4. Independently check high-risk assertions. Separate verified defects, risks,
   and open questions. Produce
   `review.schema.json`-valid JSON with exact references and proportionate
   recommendations.

## Hard gates

- Never trust a projection, explanation, or self-reported scope without its
  authority. Do not review your own implementation.
- Report a gate-required missing PLAN. Reject stale/inexact PLAN bindings, but
  do not require PLAN for ordinary work. ControlGraph v2 has open topology and
  opaque control policies; `studyctl` does not execute the whole graph. Never
  infer `control_binding`, graph completion, or Claim support.
- Reject Evidence use of missing outputs, altered seals, undeclared mutable
  dependencies, unproved Cohorts, ineligible Runs, concealed attempts,
  or mislabeled exploratory/mixed work. Preserve failed attempts as facts.
- Observation has no Claim assessment. Reject mutable/stale references, hidden
  exclusions, duplicate analysis, unregistered promotion, or Registry drift.
- Observation/Evidence sequences must match the complete finalized inventory.
  Explicit recovery moves forward by exactly one uniquely reconstructable
  interrupted finalization; it never deletes or rolls back history.
- Do not edit code or Study state, and never equate implementation or numerical
  success with scientific acceptance.
- Verify that any current Verdict binds immutable archived Review and Review
  Packet digests for its exact judged scope, and require
  `REVIEW_VERDICTS.sequence.json` to bind every visible imported Review and
  Verdict. A no-Review waiver is a separate human decision with an explicit
  reason and exact authorization text; the absence or staleness of Review never
  implies a waiver.

## Output and handoff

Return the structured review JSON outside the repository. Hand it to a
trusted write-enabled caller, which may run
`python -m tools.studyctl review-render <STUDY_ID> --file <PATH>`. Surface human
questions but never record the Verdict. Import archives the exact Review and
packet that a later Verdict binds. It replays schema, summaries, current
authority and self-digest; a changed Study requires
a fresh packet and Review. Explicit human selection permits a
write-enabled Agent to record it. Historical Verdict v1/v2 records remain
read-only compatibility artifacts only when the sequence already binds them
or an explicit pre-sequence migration adopts them; they do not define the
requirements for a new Verdict. Return corrections to
`scientific-study`; do not implement them here.
