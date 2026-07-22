# Research Strategy

Use this reference when the next scientific action requires judgment rather than
mechanical validation.

## Decompose the Claim

For each active Claim, identify:

- assumptions required for the Claim to be meaningful;
- competing explanations consistent with current Evidence;
- observable outcomes that distinguish those explanations;
- scope dimensions along which the Claim could fail;
- implementation, numerical, statistical, and model-form uncertainty.

Do not split a Claim merely to create more artifacts. Split it when distinct
parts can be contradicted by different observations or require different
evidence standards.

## Choose the next experiment

Prefer an experiment that most reduces an important uncertainty per unit of
cost, subject to the Brief and formalization gates. In qualitative terms, rank a
candidate higher when it:

1. can distinguish at least two live explanations;
2. can expose a plausible failure rather than only confirm the current best;
3. controls a known confounder;
4. is reproducible within the current Cohort or deliberately establishes a new
   Cohort;
5. is cheaper or more reversible than alternatives with similar information;
6. changes the next decision if its outcome differs.

Do not optimize only the easiest visible metric. A performance improvement is
not eligible when it is obtained by weakening accuracy, convergence, invariants,
data comparability, or another protected condition.

## Build discriminating Evidence

Select controls according to the proposed mechanism:

- use a baseline to measure improvement relative to the existing method;
- use an ablation to remove the proposed causal component;
- use a negative control to reveal leakage or evaluator bias;
- use boundary and limiting cases to challenge scope;
- use convergence or resolution sequences for numerical methods;
- use independent seeds or resampling only when stochastic variability is part
  of the question;
- use an independent implementation or analytic case when correlated software
  error is a material risk.

Record both expected and disconfirming outcomes before an expensive Run when
doing so prevents post-hoc reinterpretation.

Keep ordinary search exploratory. Once exploration has selected a candidate
and the next purpose is to promote a Claim to high-strength support, stop using
the explored results as if they were an independent test. Freeze the smallest
Confirmation Record, reserve new Run slots, and use conditions not already
consumed by the search when the scientific setting admits a held-out test. A
confirmation that reuses an observed condition is still auditable but must not
be described as fresh held-out support.

Promote Runs into Evidence when they answer a named question with eligible,
reproducible observations and an explicit analysis, scope, uncertainty, and
limitations. State how the observations bear on the exact Claim, the auxiliary
assumptions required by that inference, live competing explanations, and
concrete conditions that would overturn the assessment. Do not promote every
Run merely because it completed, and do not leave a result that changes an
active Claim or important boundary only in raw logs.

## Separate correctness questions

Ask separately:

1. **Implementation verification:** Did the code implement the declared method?
2. **Numerical verification:** Did the computation solve the declared
   mathematical problem within the stated error controls?
3. **Scientific validation:** Does the model and Evidence support the Claim in
   the declared real or scientific scope?

Passing an earlier layer never proves a later one.

## Decide whether to continue

Continue when a feasible experiment can materially distinguish live hypotheses,
shrink important uncertainty, or test a relevant boundary.

Compact when history has grown but the active scientific state can be expressed
without losing support, contradiction, or representative failures.

Review when the implementation and Evidence chain are mature enough for an
independent attempt at falsification, or before a human Verdict.

Escalate when the next useful action changes protected intent, exceeds a hard
budget, requires a new evaluator principle, or has no safe reversible default.

Stop or mark inconclusive when remaining experiments cannot change the decision
within the authorized scope or budget. A contradicted hypothesis is a valid
research result, not a workflow failure.
