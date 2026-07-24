---
name: scientific-review
description: Independently review and try to falsify a Claim-to-Evidence scientific Study before a human Verdict. Use after `python -m tools.studyctl review-packet STUDY_ID`.
---

# Scientific Review

## Authoritative inputs

Start a fresh top-level session with the project `scientific-reviewer` agent or
equivalent read-only permissions. Open `generated/REVIEW_PACKET.json` as an
index, then inspect its referenced profile, actual Git state, CHANGESET,
validation proof, Brief and approval, formal artifacts, source and tests, sealed
ExperimentIntents, ControlGraphSpecs, active PLAN, Run records and Artifacts,
Observations, Evidence versions, Claims, Checkpoint, graph-record sequence, and
diff. Use each Run's immutable snapshots, not later live files.

Read [the adversarial review rubric](references/adversarial-review-rubric.md)
before assigning severity, Claim scope, checks, or human questions.

## Workflow

1. Trace the cognitive graph from EvidenceGap through exact ExperimentIntent,
   Observation, Evidence, and scoped Claim. Separately trace the control graph
   through exact Intent reference, finalized ControlGraphSpec, byte-identical
   active PLAN, source, validation, Run, and Artifact. Verify the
   Run/Artifact-to-Observation provenance bridge.
2. Recompute change-scope and Run eligibility from authoritative state. Re-hash
   dependencies and snapshots. Check validation, protected conditions,
   evaluator/split, mathematical mapping, baselines, Cohorts, uncertainty,
   contradictions, and formalization. For promoted Observations, verify source
   Runs, dispositions, Registry trigger, fingerprint, Cohorts, uncertainty,
   anomalies, and failures. Challenge each Evidence inference, assumptions,
   alternatives, and falsification conditions. For high-strength Claims, audit
   the frozen Confirmation and every attempt, including ordering, hashes,
   held-out freshness, slots, failures, and exclusions.
3. Perform the smallest independent checks needed to test high-risk assertions;
   distinguish a verified defect from a risk or unanswered question.
4. Produce structured JSON matching `review.schema.json`. Give every material
   finding exact source references and a proportionate recommendation.

## Hard gates

- Never trust generated summaries or an implementer's explanation without
  checking their sources. Self-reported paths are context, not clean-scope proof.
- Reject a `formal/PLAN.json` that is not the exact activated finalized
  ControlGraphSpec, a Plan whose Intent reference is stale or inexact, or any
  direct inference from Plan/Run completion to Claim support. The current
  runtime lacks node-to-Run binding; do not invent node coverage.
- Reject missing declared outputs, altered sealed records, undeclared mutable
  Study dependencies, incompatible unproved Cohorts, and legacy V1 Runs used as
  Evidence.
- Do not edit scientific code, Study state, Evidence, Claims, Checkpoints,
  generated review files, or the human Verdict.
- If this top-level session participated in implementation or interpretation,
  return a fresh-session handoff instead of reviewing its own work.
- Do not equate implementation acceptance, numerical agreement, or absence of a
  discovered counterexample with scientific acceptance.
- Treat legacy/unbound Runs as exploratory. Reject relabeling, cherry-picked
  slots, concealed attempts, or unlabeled mixed Evidence.
- Reject a mutable/latest Observation reference, a stale Observation hash,
  omitted source Run, hidden exclusion or minority observation, duplicate
  analysis under a new ID, or any `supports`/`contradicts` semantics assigned
  directly to an Observation.
- Reject unregistered promotion, stale/reinterpreted Registry bindings, or an
  extension without independent review and explicit human adoption.

## Output and handoff

Return the structured review JSON outside the repository. Hand it to a separate
trusted write-enabled caller, which may run
`python -m tools.studyctl review-render <STUDY_ID> --file <PATH>`; the rendered Markdown remains
non-authoritative. Surface human questions; never record the Verdict. Only
after explicit human selection may a separate write-enabled Agent record the
decision. Return corrections to `scientific-study`; do not implement them here.
