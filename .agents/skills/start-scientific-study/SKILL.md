---
name: start-scientific-study
description: Create or revise a repository-native Study draft from a natural-language idea. Use for an explicit new persistent investigation or a draft selected by resolve-study. Do not use for one-off discussion; an ID-less continuation never creates a Study. Route approved Studies to scientific-study.
---

# Start Scientific Study

## Authoritative inputs

1. Apply the routing matrix in `AGENTS.md` before intake. Answer a one-off
   discussion directly. For an ID-less continue/resume request, run `PYTHON -m
   tools.studyctl resolve-study`: reuse a unique draft, hand a unique approved
   Study to `scientific-study`, and on failure ask once without initializing.
   Resolve a named Study by passing its ID to the same command. A named
   unapproved Study is a same-draft revision, not a new Study.
2. Read `scientific-workflow/repository-profile.json`,
   `scientific-workflow/policy.json`, and the Brief and Claims templates.
3. From `docs/scientific-agent-workflow.md`, read the sections **Route before
   intake**, **Start directly
   from a scientific idea**, **Just-in-time alignment**, and **Manual
   initialization fallback**. Read other sections only when the current intake
   decision depends on them.
4. Validate the profile with `PYTHON -m tools.studyctl profile-validate`. Let
   `PYTHON` denote one repository-supported Python 3.11-or-newer command that
   can run `-m tools.studyctl`; use it consistently.
5. Inspect only the host code, tests, documentation, manifests, and prior Study
   state needed to interpret the idea. Repository evidence outranks an Agent
   guess, but only the human can authorize protected scientific intent.

## Just-in-time alignment

Classify each ambiguity before asking the human:

- **Blocking now:** The answer changes scientific intent, a protected condition,
  hard budget, or permission for an immediate consequential action, and neither
  repository evidence nor a safe reversible default resolves it. Ask.
- **Consequential later:** The answer matters at a later method, protocol,
  compute, Evidence, or interpretation boundary. Record and defer it.
- **Non-blocking:** State a conservative reversible interpretation as an
  unconfirmed Agent inference and continue.

Inspect and draft before asking. At a boundary, ask one compact batch of at most
three questions, each with the current interpretation and blocking reason. Use
one follow-up batch only for a genuinely new material branch. Never repeat an
unresolved question or ask for repository facts and safely researchable choices.

Unless the Study root is unsafe, create or revise the best reversible `DRAFT`
before asking. Record the blocker, pause only its affected action, and continue
safe low-cost work when useful. This is not a requirements interview.

Read [alignment cases](references/alignment-cases.md) only when an ambiguity
could reasonably fit more than one class, or when a proposed Claim is difficult
to make falsifiable without changing the human's intent.

## Workflow

1. For an explicit new persistent investigation, translate the prompt into a precise scientific question, proposed testable
   Claims and scopes, non-goals, human-supplied assumptions, Agent-inferred
   assumptions, protected conditions, required Evidence, resource limits,
   escalation conditions, and open questions. For a routed draft, inspect and
   revise those same fields in its existing Brief and Claims. Define every
   nonstandard mathematical symbol.
2. Only on the explicit-new route, allocate the next unused `SC-NNNN` below the configured `study_root`, then
   run `PYTHON -m tools.studyctl init STUDY_ID --title "TITLE"`. On a concurrent
   ID collision, rescan and retry once. Preserve and report a partial directory
   instead of deleting or reusing it. Never run `init` on a draft-continuation route.
3. Replace every Brief placeholder while preserving metadata. Add only
   `proposed` Claims with lifecycle `active`, empty Evidence arrays, explicit
   scope, uncertainty, and limitations. Increment the Claims revision and timestamp. Put unresolved
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
- Do not turn a one-off scientific conversation or an ID-less continuation into
  a new Study. Zero, multiple, or invalid resolver candidates require one
  concise question and no write.
- Do not invent a hard budget, acceptance threshold, dataset split, evaluator
  principle, baseline change, or permission. Record absent values as `None
  stated`; leave absent numeric limits as `null` in the visible hard-budget
  block. Both `null` and numeric zero authorize no positive declared use.
- Stop before `approve-brief`, scientific source edits, formal artifacts, Runs,
  compute spending, Evidence, or claims of scientific support.
- Route an approved Study to `scientific-study`; keep an unapproved draft in
  this Skill under the same ID. If workflow infrastructure is missing, use
  `bootstrap-scientific-workflow` only after authorization.

## Output and handoff

Return the reused or new Study ID and paths, concise interpretation, human-supplied versus
Agent-inferred assumptions, and only decisions that block authorization. If no
decision blocks approval, provide the exact interactive approval command and do
not manufacture a question. If alignment is required, provide the single
bounded question batch instead; revise the same draft after the answer. Hand a
freshly approved Study to `scientific-study`.
