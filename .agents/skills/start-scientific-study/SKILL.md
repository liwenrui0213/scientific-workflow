---
name: start-scientific-study
description: Turn a human's natural-language computational-science idea into a new repository-native Study draft. Use when the user asks to investigate, test, compare, optimize, or begin a new scientific idea without naming an existing Study. Inspect the repository, initialize the next Study, draft the Brief and proposed Claims, align just in time only when a material ambiguity has no safe reversible default, and stop before approval or implementation. Do not use to resume an existing Study; use scientific-study instead.
---

# Start Scientific Study

## Just-in-time alignment

Classify each ambiguity before asking the human:

- **Blocking now:** Different plausible answers would materially change the scientific question, desired Claim, protected conditions, hard budget, or permission for the immediate expensive or hard-to-reverse action; repository inspection cannot resolve it; and no safe reversible default exists. Ask for alignment.
- **Consequential later:** The answer matters only when selecting a method, formalizing an evaluator or protocol, spending substantial compute, or interpreting Evidence. Record it as an open question and defer it to that boundary.
- **Non-blocking:** A conservative, reversible provisional interpretation exists. State it as an unconfirmed Agent inference and continue.

At each decision boundary, inspect available sources and draft the best current interpretation before asking. Ask at most one compact batch of up to three independent questions. For each question, state the current interpretation and why the answer blocks authorization or the immediate action. Do not ask for facts available in the repository or choices the Agent can research safely.

Use at most one follow-up batch, and only when the human's answer creates a genuinely new material branch. Never repeat or merely rephrase an unresolved question. If a blocker remains, keep the Study in `DRAFT`, pause only the blocked action, and continue safe read-only or low-cost reversible work when useful; otherwise wait. Re-align later only when new Evidence creates a new blocker or the Study reaches a new protected, expensive, or hard-to-reverse boundary.

1. Read `AGENTS.md`, `docs/scientific-agent-workflow.md`, `scientific-workflow/policy.json`, and the Brief and Claims templates. Let `PYTHON` denote a repository-supported Python 3.11-or-newer command, confirm that it can run `-m tools.studyctl`, and use that interpreter consistently below and in the returned approval command.
2. Inspect only the code, tests, and documentation needed to interpret the idea. Do not implement the idea yet.
3. Translate the prompt into a precise scientific question, proposed testable Claims and scopes, non-goals, human-supplied assumptions, agent-inferred assumptions requiring confirmation, protected conditions, required Evidence, resource limits, escalation conditions, and open questions. Define every nonstandard mathematical symbol.
4. Apply the alignment policy above. Draft first; do not ask the human to fill workflow files or conduct a general requirements interview. Keep the authorization draft proportional and reviewable. Treat method selection, benchmark design, baseline implementation, evaluator details, and hardware as non-blocking or consequential-later questions whenever they can be investigated safely and progressively formalized.
5. Never invent a hard budget, numerical acceptance threshold, dataset split, evaluator principle, baseline change, or permission. Record missing information as `None stated`. Default to read-only research and low-cost reversible exploration; do not interpret an omitted budget as permission for expensive compute.
6. Allocate the next unused `SC-NNNN` identifier by scanning `studies/SC-*`, then run `PYTHON -m tools.studyctl init STUDY_ID --title "TITLE"`. Never overwrite or merge into an existing Study. If another process takes the ID, rescan and retry once with the next ID. If initialization leaves a partial directory, preserve it and report the failure instead of deleting or reusing it.
7. Replace every Brief placeholder while preserving its metadata block. In `CLAIMS.json`, add only `proposed` Claims with empty Evidence arrays, explicit scope, uncertainty, and limitations. Increment its revision and update its timestamp. Copy unresolved scientific questions to `frontier.open_questions`; add to `frontier.human_decisions_required` only decisions that truly block Brief approval or the immediate next action. Defer other consequential choices to progressive formalization.
8. Run `PYTHON -m tools.studyctl status STUDY_ID` and `PYTHON -m tools.studyctl validate STUDY_ID`. Repair every deterministic validation error except the expected missing-approval error; report that draft state accurately rather than calling it a full pass.
9. Stop before invoking `approve-brief`, editing scientific source code, creating formal artifacts, executing Runs, spending compute, or claiming Evidence or scientific support.
10. Return the Study ID and paths, a concise interpretation, human-supplied versus inferred assumptions, only decisions that block authorization, and the exact interactive approval command. If no decision blocks approval, say so directly and do not manufacture a question. If alignment is required, present the single bounded question batch instead of the approval command. After the human answers, revise the same unapproved draft, rerun the checks, and either request approval or apply the one permitted follow-up batch.
11. After a fresh human approval exists, hand the Study to `scientific-study` for research and execution.

If the user names an existing Study or asks to continue or resume one, do not initialize another Study; use `scientific-study`. If the workflow tooling or templates are missing, stop and request repository bootstrap.
