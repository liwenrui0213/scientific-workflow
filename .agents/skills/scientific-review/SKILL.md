---
name: scientific-review
description: Independently review and try to falsify a Claim-to-Evidence scientific Study. Use after `studyctl review-packet` when checking protected conditions, mathematical or algorithmic implementation mapping, experiment fairness, Cohort compatibility, contradictory Evidence, reproducibility, formalization debt, or overclaiming before a human Verdict.
---

# Scientific Review

## Authoritative inputs

Start a fresh top-level session with the project `scientific-reviewer` agent or
equivalent read-only permissions. Open `generated/REVIEW_PACKET.json` as an
index, then inspect its referenced profile, actual Git state, CHANGESET,
validation proof, Brief and approval, formal artifacts, source and tests, sealed
Run records and logs, Evidence versions, Claims, Checkpoint, and diff. For an
older Run, use its immutable governance and formal snapshots rather than later
live files.

Read [the adversarial review rubric](references/adversarial-review-rubric.md)
before assigning severity, judging Claim scope, selecting independent checks,
or deciding which questions require human review.

## Workflow

1. Trace each material Brief requirement and Claim through method, source symbol,
   validation, Run, Evidence, and scope. Try to falsify both implementation
   fidelity and scientific interpretation.
2. Recompute change-scope and Run eligibility from authoritative state. Re-hash
   inputs, outputs, logs, governance snapshots, and formal snapshots. Check the
   base anchor, native validation, protected conditions, evaluator and split,
   mathematical mapping, baselines, Cohorts, uncertainty, contradictions, and
   formalization debt.
3. Perform the smallest independent checks needed to test high-risk assertions;
   distinguish a verified defect from a risk or unanswered question.
4. Produce structured JSON matching `review.schema.json`. Give every material
   finding exact source references and a proportionate recommendation.

## Hard gates

- Never trust generated summaries or an implementer's explanation without
  checking their sources. Self-reported paths are context, not clean-scope proof.
- Reject missing declared outputs, altered sealed records, undeclared mutable
  Study dependencies, incompatible unproved Cohorts, and legacy V1 Runs used as
  Evidence.
- Do not edit scientific code, Study state, Evidence, Claims, Checkpoints,
  generated review files, or the human Verdict.
- If the current top-level session participated in implementation, Run
  interpretation, Evidence authoring, or Claim updates, do not perform the final
  review in that session or disguise self-review as a child-agent review.
  Return the prepared packet path and an explicit fresh-session handoff, then
  stop.
- Do not equate implementation acceptance, numerical agreement, or absence of a
  discovered counterexample with scientific acceptance.

## Output and handoff

Return the structured review JSON outside the repository. Hand it to a separate
trusted write-enabled caller, which may run `studyctl review-render`; the rendered
Markdown remains non-authoritative. Surface critical human questions explicitly
and leave the final implementation and scientific Verdict to the human. If
material findings require correction, hand them back to `scientific-study`; the
reviewer does not implement its own recommendations.
