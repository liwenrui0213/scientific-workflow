# Adversarial Scientific Review Rubric

Review by attempting to find the smallest concrete observation that would make
the implementation or Claim unacceptable. Absence of a discovered defect is not
proof of correctness.

## Review each link independently

Trace:

```text
Brief requirement
-> formal method or protocol
-> source symbol and test
-> sealed Run
-> finalized Evidence
-> scoped Claim
```

At every link, check both identity and meaning. Matching IDs and hashes show
which object was used; they do not prove that a mathematical derivation,
implementation, comparison, or interpretation is scientifically valid.

## Severity

- **critical:** Evidence or a human decision could rely on corrupted,
  unauthorized, fabricated, irreproducible, or materially different work; or a
  protected condition was violated.
- **major:** A material Claim, implementation path, comparison, uncertainty, or
  scope is unsupported or likely wrong, but authoritative history remains
  intact.
- **minor:** A localized weakness reduces clarity, coverage, or reproducibility
  without presently changing a material conclusion.
- **info:** A bounded observation, residual risk, or optional improvement that
  does not establish a defect.

Severity follows impact and evidence, not writing intensity. State uncertainty
when the available record cannot distinguish defect from risk.

## Falsification probes

Choose probes relevant to the Claim:

- recompute a simple analytic, limiting, symmetry, conservation, or dimensional
  case;
- compare declared equations or algorithms with the implemented symbols;
- inspect whether a performance gain weakens tolerances, workload, precision,
  data, or evaluator conditions;
- test an ablation or negative control implied by the claimed mechanism;
- inspect seed selection, excluded Runs, stopping decisions, and contradictory
  Evidence for selection bias;
- for confirmatory Evidence, compare the frozen time, Claim/candidate/protocol/
  evaluator bindings, held-out history, planned slots, and analysis rule with
  every workflow-visible attempt; treat a truncated attempt index as a prompt
  to inspect its bound source inventory, not as proof that omitted attempts do
  not exist;
- check whether Cohort differences invalidate aggregation;
- distinguish maximum error, norm, RMS, mean, variance, confidence interval, and
  effect size according to their actual mathematical definitions;
- verify that numerical convergence is not being presented as scientific
  validation or mathematical proof.

Use the smallest independent computation needed to challenge a material claim.
Do not rerun expensive work without authorization.

## Claim-scope checks

For every Claim, identify the exact population, parameter range, hardware,
precision, dataset, model version, discretization, and uncertainty conditions to
which its Evidence applies. Flag extrapolation beyond those conditions. When
Evidence is mixed, prefer a narrower supported Claim over a global average that
hides a failure region.

Treat the repository's held-out freshness as a workflow-observed property, not
proof that nobody accessed the condition elsewhere. Distinguish exploratory,
confirmatory, and mixed support. Treat `not_applicable` as a scientific judgment:
reject boilerplate or an explanation that does not show why no independent
condition can meaningfully exist, and surface that judgment to the human. A post-result label, an incomplete planned-slot
set, an omitted eligible attempt, or a drifted analysis rule cannot provide
confirmatory support.

## Required finding quality

Every material finding must include:

1. the violated requirement or inference;
2. direct source references;
3. the observed fact;
4. why it affects implementation or scientific interpretation;
5. the smallest defensible remediation or human decision.

Do not write a favorable summary before completing the trace. Do not elevate a
speculation to a defect, and do not soften a verified defect into an open
question.

## Human-review questions

Escalate questions that require scientific value judgment, acceptance of model
form assumptions, risk tolerance, protected-condition changes, budget expansion,
or final interpretation. Phrase each question around a concrete decision and
the Evidence that makes it necessary.
