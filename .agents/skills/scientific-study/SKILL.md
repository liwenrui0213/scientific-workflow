---
name: scientific-study
description: Run or resume a repository-native Claim-to-Evidence scientific Study. Use for long-running scientific research that must preserve a human-approved Brief, progressively formalize consequential decisions, execute important computations as immutable Runs, and connect Runs to Evidence and Claims without crossing protected conditions silently.
---

# Scientific Study

1. Read only the bounded active context first: `BRIEF.md`, its current approval, `CLAIMS.json`, active files under `formal/`, the latest Checkpoint, and the Frontier in `CLAIMS.json`. Do not load all historical Runs or notes by default.
2. Put provisional derivations, ideas, scripts, failures, and plans under `work/active/`. Never cite `work/` alone as final Claim support.
3. Before expensive, shared, scientifically critical, parallel, or hard-to-reverse work, run `python -m tools.studyctl check-formalization STUDY_ID` with explicit estimates and change flags. Create only the smallest artifact it requires.
4. Execute consequential calculations with `python -m tools.studyctl run STUDY_ID --purpose "..." [options] -- COMMAND ARG...`. Declare changed critical paths with `--changed-path`, and use `--scientific-critical` or `--shared-across-runs` when applicable; these declarations bind the later Evidence gate. Preserve the exact Run ID and never edit a sealed manifest.
5. Use `evidence-new` to scaffold a draft from terminal Runs. State the analysis, result, scope, uncertainty, limitations, assessment, and any Cohort compatibility justification yourself; then seal it with `evidence-finalize`.
6. Update Claims only with finalized `{evidence_id, version, sha256}` references. Preserve contradictory Evidence and representative failed directions.
7. If work would change the Brief, evaluator principles, data split, acceptance criteria, hard budget, or Claim scope, stop. Use `brief-new-version` where appropriate and escalate for a new human approval; never infer permission.
