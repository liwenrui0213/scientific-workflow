---
name: scientific-study
description: Resume and execute an existing repository-native Claim-to-Evidence scientific Study after its Brief has been human-approved. Use when the user names an existing Study or asks to continue its long-running research, progressively formalize consequential decisions, execute important computations as immutable Runs, and connect Runs to Evidence and Claims without crossing protected conditions silently. For a new natural-language idea without an existing Study, use start-scientific-study instead.
---

# Scientific Study

## Align only at material boundaries

Do not ask the human to resolve every uncertainty. First inspect the approved active context and repository evidence, then classify the ambiguity:

- continue with a stated conservative, reversible assumption when that cannot change the approved scientific meaning or protected conditions;
- record the question and defer it when it matters only at a later method, evaluator, protocol, compute, Evidence, or interpretation boundary; or
- ask now only when different plausible answers would materially change the authorized Claim, a protected condition, the hard budget, or an immediate expensive or hard-to-reverse action, and no safe reversible default exists.

At one boundary, ask one compact batch of at most three independent questions. State the current interpretation and why each answer is required. Use at most one follow-up batch, only if the answer exposes a genuinely new material branch; never repeat or rephrase the same unresolved question. If alignment remains unresolved, pause only the blocked action and continue safe independent read-only or low-cost reversible work when useful.

1. Confirm that the named Study exists and its active Brief has a fresh human approval. If it is a new idea or an unapproved intake draft, use `start-scientific-study` instead. Then read only the bounded active context: `BRIEF.md`, its current approval, `CLAIMS.json`, active files under `formal/`, the latest Checkpoint, and the Frontier in `CLAIMS.json`. Do not load all historical Runs or notes by default.
2. Put provisional derivations, ideas, scripts, failures, and plans under `work/active/`. Never cite `work/` alone as final Claim support.
3. Apply the boundary-alignment policy above before escalating. Before expensive, shared, scientifically critical, parallel, or hard-to-reverse work, run `python -m tools.studyctl check-formalization STUDY_ID` with explicit estimates and change flags. Create only the smallest artifact it requires.
4. Execute consequential calculations with `python -m tools.studyctl run STUDY_ID --purpose "..." [options] -- COMMAND ARG...`. Declare changed critical paths with `--changed-path`, and use `--scientific-critical` or `--shared-across-runs` when applicable; these declarations bind the later Evidence gate. Preserve the exact Run ID and never edit a sealed manifest.
5. Use `evidence-new` to scaffold a draft from terminal Runs. State the analysis, result, scope, uncertainty, limitations, assessment, and any Cohort compatibility justification yourself; then seal it with `evidence-finalize`.
6. Update Claims only with finalized `{evidence_id, version, sha256}` references. Preserve contradictory Evidence and representative failed directions.
7. If work would change the Brief, evaluator principles, data split, acceptance criteria, hard budget, or authorized Claim scope, stop that action. Use `brief-new-version` where appropriate and request the bounded human alignment needed for a new approval; never infer permission. Continue unaffected safe work when useful.
