# Scientific Brief: {{STUDY_ID}} — {{TITLE}}

Brief version: 1

## Scientific Question

[REPLACE: State the precise scientific question and define all nonstandard symbols.]

## Desired Claims

[REPLACE: List the claims the study is intended to test, including scope.]

## Non-Goals

[REPLACE: State what this Study intentionally will not establish, optimize, or change.]

## Human-Supplied Assumptions

[REPLACE: Record only assumptions explicitly supplied by the human. Write "None stated" if none were supplied.]

## Agent-Inferred Assumptions Requiring Confirmation

[REPLACE: Record material assumptions inferred by the Agent and label them unconfirmed. Write "None" if no confirmation is needed.]

## Open Questions at Authorization

[REPLACE: List unresolved questions and distinguish decisions required before approval from non-blocking scientific uncertainty. Write "None" if there are no open questions.]

## Protected Conditions

[REPLACE: State evaluator principles, dataset split, acceptance criteria, baselines, precision, and any conditions that must not change silently.]

## Required Evidence

[REPLACE: Specify required comparisons, uncertainty reporting, contradictory checks, and reproducibility expectations.]

## Resource Budget

The JSON block below is the single machine-enforced source for lifetime Study
hard limits. A numeric zero authorizes no positive use; `null` leaves positive
use unauthorized until a new Brief version supplies a numeric limit. Storage
uses decimal gigabytes (`1 GB = 10^9 bytes`).

<!-- STUDYCTL-HARD-BUDGET-BEGIN -->
```json
{
  "gpu_hours": null,
  "cpu_hours": null,
  "storage_gb": null
}
```
<!-- STUDYCTL-HARD-BUDGET-END -->

[REPLACE: State advisory allocation or calendar guidance only; do not duplicate hard numeric limits in prose.]

## Escalation Conditions

[REPLACE: State which events require human attention or a new Brief version.]

<!-- STUDYCTL-METADATA-BEGIN
{
  "brief_version": 1,
  "protected_labels": [
    "evaluator_principles",
    "dataset_split",
    "acceptance_criteria",
    "hard_budget",
    "final_scientific_interpretation"
  ]
}
STUDYCTL-METADATA-END -->
