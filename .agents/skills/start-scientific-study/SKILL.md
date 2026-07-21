---
name: start-scientific-study
description: Turn a human's natural-language computational-science idea into a new persistent repository-native Study draft. Use when the user asks to begin or sustain a new scientific investigation, comparison, or optimization without naming an existing Study. Inspect the repository, initialize the next Study, draft the Brief and proposed Claims, align just in time only when a material ambiguity has no safe reversible default, and stop before approval or implementation. Do not use for a one-off explanation, ordinary code change, or existing Study; use scientific-study to resume an approved Study.
---

# Start Scientific Study

## Authoritative inputs

1. Read `AGENTS.md`, `scientific-workflow/repository-profile.json`,
   `scientific-workflow/policy.json`, and the Brief and Claims templates.
2. From `docs/scientific-agent-workflow.md`, read the sections **Start directly
   from a scientific idea**, **Just-in-time alignment**, and **Manual
   initialization fallback**. Read other sections only when the current intake
   decision depends on them.
3. Validate the profile with `PYTHON -m tools.studyctl profile-validate`. Let
   `PYTHON` denote one repository-supported Python 3.11-or-newer command that
   can run `-m tools.studyctl`; use it consistently.
4. Inspect only the host code, tests, documentation, manifests, and prior Study
   state needed to interpret the idea. Repository evidence outranks an Agent
   guess, but only the human can authorize protected scientific intent.

## Just-in-time alignment

Classify each ambiguity before asking the human:

- **Blocking now:** Different plausible answers would materially change the scientific question, desired Claim, protected conditions, hard budget, or permission for the immediate expensive or hard-to-reverse action; repository inspection cannot resolve it; and no safe reversible default exists. Ask for alignment.
- **Consequential later:** The answer matters only when selecting a method, formalizing an evaluator or protocol, spending substantial compute, or interpreting Evidence. Record it as an open question and defer it to that boundary.
- **Non-blocking:** A conservative, reversible provisional interpretation exists. State it as an unconfirmed Agent inference and continue.

At each decision boundary, inspect available sources and draft the best current interpretation before asking. Ask at most one compact batch of up to three independent questions. For each question, state the current interpretation and why the answer blocks authorization or the immediate action. Do not ask for facts available in the repository or choices the Agent can research safely.

Use at most one follow-up batch, and only when the human's answer creates a genuinely new material branch. Never repeat or merely rephrase an unresolved question. If a blocker remains, keep the Study in `DRAFT`, pause only the blocked action, and continue safe read-only or low-cost reversible work when useful; otherwise wait. Re-align later only when new Evidence creates a new blocker or the Study reaches a new protected, expensive, or hard-to-reverse boundary.

Except when profile or tooling ambiguity makes the Study root itself unsafe, do
not ask before creating and validating the best reversible `DRAFT`. Put the
blocker and current interpretation in that draft, then ask the bounded alignment
batch. This Skill is not a pre-draft requirements interview.

Read [alignment cases](references/alignment-cases.md) only when an ambiguity
could reasonably fit more than one class, or when a proposed Claim is difficult
to make falsifiable without changing the human's intent.

## Workflow

1. Translate the prompt provisionally into a precise scientific question, proposed testable
   Claims and scopes, non-goals, human-supplied assumptions, Agent-inferred
   assumptions, protected conditions, required Evidence, resource limits,
   escalation conditions, and open questions. Define every nonstandard
   mathematical symbol.
2. Allocate the next unused `SC-NNNN` below the configured `study_root`, then
   run `PYTHON -m tools.studyctl init STUDY_ID --title "TITLE"`. On a concurrent
   ID collision, rescan and retry once. Preserve and report a partial directory
   instead of deleting or reusing it.
3. Replace every Brief placeholder while preserving metadata. Add only
   `proposed` Claims with empty Evidence arrays, explicit scope, uncertainty,
   and limitations. Increment the Claims revision and timestamp. Put unresolved
   scientific questions in the Frontier; reserve `human_decisions_required`
   for decisions that block approval or the immediate next action.
4. Apply the alignment policy to the written draft. Do not make the human fill
   workflow files or conduct a general requirements interview. Treat safely
   researchable method, benchmark, baseline-implementation, evaluator-detail,
   and hardware choices as deferred questions rather than intake blockers.
5. Run `PYTHON -m tools.studyctl status STUDY_ID` and
   `PYTHON -m tools.studyctl validate STUDY_ID`. Repair deterministic errors
   except the expected missing-approval result.

## Hard gates

- Do not begin a Study with unresolved profile root or object-ignore warnings.
- Do not invent a hard budget, acceptance threshold, dataset split, evaluator
  principle, baseline change, or permission. Record absent values as `None
  stated`; leave absent numeric limits as `null` in the visible hard-budget
  block. Both `null` and numeric zero authorize no positive declared use.
- Stop before `approve-brief`, scientific source edits, formal artifacts, Runs,
  compute spending, Evidence, or claims of scientific support.
- If the user names an existing Study, route to `scientific-study`. If workflow
  infrastructure is missing, use `bootstrap-scientific-workflow` only after the
  user authorizes installation or migration.

## Output and handoff

Return the Study ID and paths, concise interpretation, human-supplied versus
Agent-inferred assumptions, and only decisions that block authorization. If no
decision blocks approval, provide the exact interactive approval command and do
not manufacture a question. If alignment is required, provide the single
bounded question batch instead; revise the same draft after the answer. Hand a
freshly approved Study to `scientific-study`.
