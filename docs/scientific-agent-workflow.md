# Claim-to-Evidence Scientific Workflow, V2

This repository uses Python 3.11 or newer and a local, deterministic workflow for long-running computational research. `studyctl` records and checks facts; it does not infer scientific conclusions. V2 adds an explicit repository-adaptation contract and Git-backed governance for scientific code and test changes.

For a persistent investigation, the normal human interface is a scientific idea stated in ordinary language plus an explicit request to start or continue research. Codex turns that request into the internal Study structure; the scientist does not need to choose new IDs, edit templates, or manually route workflow stages. One-off scientific discussion remains ordinary conversation and creates no Study.

## Authoritative lanes and their bridge

The Research Workspace is outside both authoritative graphs:

```text
Research Workspace (mutable, non-authoritative)
idea / conjecture / disposable code / failed exploration / provisional draft
```

The cognitive graph records why evidence is needed and what is learned:

```text
EvidenceGap -> ExperimentIntent -> Observation -> Evidence -> Claim
```

The control graph records how a finalized Intent is executed:

```text
exact ExperimentIntent reference
-> ControlGraphSpec
-> active formal/PLAN.json
-> Run / Artifact
```

The cross-graph bridge is explicit:

```text
Run / Artifact -> provenance-bound interpretation -> Observation
```

The approved Human Brief governs both graphs, but neither Workspace material nor
control completion is scientific support. `work/active/` is mutable scratch. A
Run records what actually executed under fixed code, inputs, configuration,
environment, and Cohort fields; it belongs to the execution/provenance layer.
Simple observations remain inline in Evidence. An optional Observation Record
is created only when analysis needs reuse, aggregation, independent review, or
long-lived auditability. It records what was computed without assigning
`supports` or `contradicts` semantics.

More precisely, let \(R_1,\ldots,R_n\) be immutable Runs, let
\(S\subseteq\{1,\ldots,n\}\) index the Runs selected by a declared analysis
method \(A\), and define \(O=A(\{R_i:i\in S\})\). Here \(O\) is an Observation:
the selected Runs, selection rules, aggregation, results, uncertainty,
anomalies, failures, assumptions, and limitations. For one addressed Claim
\(C\), Evidence records a separate inference argument explaining why \(O\),
conditional on declared auxiliary assumptions, changes support for \(C\). It
also lists live competing explanations and observations or failures that would
overturn its assessment. This is not a proof that \(C\) is true. Each new
Evidence Argument addresses exactly one Claim; multiple Claims may reuse the
same exact Observation hash while reaching different assessments. A Claim may
reference only finalized, hash-pinned Evidence. A Verdict separately judges
implementation and scientific Claims.

### Cognitive graph, control graph, and Research Workspace

The mutable Research Workspace is deliberately outside both authoritative
graphs. Free-form hypotheses, abandoned explanations, logs, disposable code,
and provisional plans may coexist or contradict one another under
`work/active/`; their presence does not make them scientific knowledge.

The cognitive graph contains auditable scientific objects and relations:
EvidenceGaps, ExperimentIntents, Observations, Evidence, and Claims.
An `ExperimentIntent` is the versioned boundary between an EvidenceGap and
action. It states why a computation is requested, the exact approved Brief and
optional Claim specification it addresses, the observations and evidence
required, and typed assessment semantics. Rigorous here means explicit,
versioned, scoped, and traceable; it does not mean the target Claim is already
supported.

ExperimentIntent schema v1 criteria use only equality/inequality or finite
numeric ordering operators. They are frozen audit contracts that prevent silent threshold
changes. The current Evidence schema does not yet carry a typed
Intent-and-criterion result map or mechanically execute its aggregation rule.
Consequently, these criteria do not by themselves authorize a Claim update:
Claim-specific Evidence must still state the observation-to-Claim bridge,
counterevidence, uncertainty, and scope. A future schema may make that
application deterministic without changing the Intent/control separation.

The control graph contains prospective actions and dependencies. A
`ControlGraphSpec` states how one exact finalized Intent version could be
realized: nodes, conditional edges, executor, resource estimates, and completion
contract. Resource estimates are prospective metadata, not budget authority or
a reservation; each actual Run independently passes the cumulative Brief
budget gate. Its `realizes_intent` field is an exact
`{intent_id, version, sha256}` reference. One Intent may have several
alternative control graphs. A revised Intent or graph creates a new version
with an exact `previous_ref`; finalized records are never edited in place.

The permission model is asymmetric:

- an Agent may explore freely in `work/active/`;
- an Agent may propose and revise structured Intent and Plan drafts;
- an Agent may choose a Plan topology freely within safety and data-access
  boundaries; declared resources remain non-authorizing estimates;
- finalization freezes exact semantics and topology, and activation is an
  explicit operation;
- an Agent cannot reinterpret execution success as Claim support, silently
  change a frozen threshold, rewrite history, or bypass Evidence and the human
  Verdict.

The distinction between `assessment_semantics` and control completion is
essential. If every solver and validator process exits successfully but an
observed convergence order misses its threshold, the control graph completed;
the resulting Evidence may contradict the target Claim or remain
inconclusive. No ControlGraphSpec transition updates a Claim directly.

ControlGraph schema v1 records are strictly validated prospective DAGs. Iteration is
encapsulated in a bounded `loop` node with a maximum iteration count, progress
metric, continue condition, and exhaustion action. `plan-activate` materializes
the selected immutable graph byte-for-byte as `formal/PLAN.json`, which
subsequent Runs snapshot. V1 does not yet claim that `studyctl` schedules the
whole graph or deterministically binds each Run to a node; that requires a
separate typed control runtime and node-level execution ledger.

Finalized graph records are committed through
`GRAPH_RECORDS.sequence.json`. Its monotone high-water mark equals the exact
visible finalized-record count, and its inventory digest binds both each
record's canonical content digest and exact file digest. This detects deletion
of a latest or sole version, whole-ID rollback, byte replacement, and
unindexed additions. The local sequence is a durable authority, not an
authenticated external transparency log; coordinated rollback of the records
and the sequence remains outside the local self-digest threat model.

Runs remain execution and provenance records even when their manifest
classifies them as `exploratory` or `confirmatory`. That classification
controls how later Evidence may use a Run; it does not place the Run in the
cognitive graph or make it support a Claim by itself. Exploratory Runs may
discover hypotheses or narrow candidates without a Confirmation Record. Only
when a result is being promoted to the high-strength
`numerically_supported` state does the workflow require a small
pre-confirmatory-Run Confirmation Record followed by new `confirmatory` Runs.
The record freezes the exact Claim statement and scope, selected candidate,
protocol, evaluator, held-out conditions, analysis rule, and planned Run slots.
This is a deterministic time-and-hash boundary, not a new human approval gate.

## Thin Skills, thick protocol

The five repository Skills are intentionally small. Each Skill is a **routing
contract**: it states when the capability applies, the authoritative inputs for
that phase, the expected workflow posture, the hard gates that must be invoked,
and the output or handoff. A Skill does not restate the complete scientific
workflow and is not itself an enforcement boundary. Its metadata routes the
task; its body is loaded only for the selected phase.

The durable behavior lives in **protocol sources**:

| Layer | Responsibility |
|---|---|
| `AGENTS.md` | Small, always-applicable repository invariants and authority boundaries |
| Repository profile, policy, schemas, and templates | Repository adaptation, formalization thresholds, valid record shapes, and human-facing starting points |
| `studyctl`, Git state, hashes, and immutable snapshots | Deterministic state transitions, actual-diff scope, provenance, integrity, and eligibility checks |
| Human authorization and independent review | Scientific intent, protected-condition changes, implementation acceptance, and final interpretation; a write-enabled Agent may record an explicitly authorized Verdict |

This separation keeps routine prompts and research context small while making
important constraints testable. Increasing a Skill's length does not strengthen
a gate: Brief approval freshness, Run immutability, Evidence eligibility,
Cohort-field compatibility and declared justification, compaction bindings, and
Verdict ownership are enforced by deterministic records and commands. The
project hook rejects several obvious Agent attempts earlier, but it remains a
guardrail rather than a security boundary.

### Selective references

A routing contract should load only the protocol material needed at the current
boundary. Its stable headings are `Authoritative inputs`, `Workflow`, `Hard
gates`, and `Output and handoff`. One level of conditional references may add
judgment guidance for:

- just-in-time alignment when intent is materially ambiguous;
- research strategy when choosing the next informative investigation;
- semantic compaction when deciding what belongs in the active Frontier; or
- adversarial review when attempting to falsify implementation or Claims.

The current selective references are:

| Skill | Conditional reference | Load when |
|---|---|---|
| `start-scientific-study` | `references/alignment-cases.md` | An ambiguity fits more than one alignment class, or a Claim is hard to make falsifiable without changing intent |
| `scientific-study` | `references/research-strategy.md` | Comparing hypotheses or experiments, judging discrimination, or deciding whether to continue, compact, review, or escalate |
| `research-compaction` | `references/semantic-compaction.md` | Selecting decisive or contradictory Evidence, representative failures, Claim revisions, or the Frontier |
| `scientific-review` | `references/adversarial-review-rubric.md` | Assigning severity, judging Claim scope, selecting independent checks, or preparing human questions |

The bootstrap Skill has no separate judgment reference: repository adaptation
details remain authoritative here and in the profile, schemas, and validators.

Do not load all four references, every historical Run, or this entire guide by
default. Do not build deep reference chains. Open a conditional reference only
when its named decision is active, then inspect the authoritative Study and code
artifacts needed to apply it. Reference prose may guide scientific judgment, but
it cannot override the approved Brief, repository profile, policy, schemas,
actual Git state, or `studyctl` results.

### Deterministic gates and semantic judgment

Put a rule in the lowest layer that can check it reliably:

- use schemas and validators for IDs, fields, references, hashes, and allowed
  state transitions;
- use Git and CHANGESET/VALIDATION records for host-code scope and native test
  proof;
- use `studyctl` for formalization, Run sealing, Evidence finalization,
  compaction, human-only Brief approval, and explicitly authorized Verdict recording;
- use the hook only for early denial of obvious Agent-side gate violations and malformed Agent-initiated Verdict commands;
- use independent review and human judgment for mathematical fidelity,
  experimental fairness, information value, uncertainty, and Claim scope.

If a semantic decision cannot be reduced to a sound deterministic check, do not
encode a misleading proxy merely to automate it. Record the decision and its
evidence, expose it to falsification, and escalate only at the material boundary.

### Behavioral forward testing

Deterministic tests verify the protocol machinery. Skills additionally need
**forward scenarios**: representative prompts run from a fresh context to see
whether the Agent actually follows the routing contract. Scenarios should test
observable behavior, for example whether the Agent:

- drafts before asking and aligns only when no safe reversible default exists;
- stops at a protected boundary, and at a Verdict boundary either follows an explicit human decision or asks once instead of inventing acceptance;
- preserves contradictory Evidence and representative failures during
  compaction; and
- follows source artifacts during review instead of trusting generated views.

The checked-in catalog at
`tests/fixtures/skill_contracts/pressure-scenarios.json` records these prompts,
expected actions, forbidden actions, and the invariant under pressure. Its
schema and Skill routing contracts are checked with:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_skill_contracts
```

This deterministic test proves that the catalog is well formed and that the
Skills keep their required routing structure and direct references. It does not
execute a model or prove that an Agent passed the scenarios. A behavioral
forward test still requires running the relevant catalog cases in fresh
sessions and inspecting the observed actions.

Treat a failed forward scenario as evidence for the smallest targeted Skill or
reference change, followed by a deterministic regression test whenever the
failure can be made machine-checkable. Do not expand every Skill pre-emptively
with generic warnings. Forward scenarios are model-behavior checks, not formal
proofs or security tests; rerun the relevant scenarios after material changes to
a Skill, its references, the workflow protocol, or the model used to execute it.

## Adapt the workflow to the host repository

The workflow is installed *into* a scientific software repository; it must not impose a parallel source tree or assume that every project uses the same commands. `scientific-workflow/repository-profile.json` is the adaptation contract. It declares:

- `study_root` and `object_root` for research state and large Run outputs;
- `source_roots`, `test_roots`, and `experiment_roots` for adopted host code, tests, and experiment configurations;
- `workflow_roots`, protected/generated/vendor patterns, and scientific-critical path patterns;
- `run_cwd`, so registered commands execute from the host project's expected directory;
- repository-native validation commands as literal argument arrays; and
- the Git base ref, required Study branch pattern, and an independent switch for whether a linked Study worktree is required.

Paths are repository-relative and may not escape the repository. Commands are argument arrays rather than shell strings. Validate the profile before starting a Study:

```bash
python -m tools.studyctl profile-validate
```

The profile is a bootstrap/migration authority, not a Study-owned artifact. Its own path is always protected by `studyctl`, even if a modified profile attempts to remove that protection; a scientific Study must not rewrite its governance to make a Run pass. Every workflow root must be covered by a protected pattern, and `object_root` contents must be ignored by Git. Missing configured source, test, or experiment roots are adaptation warnings; resolve them before the first real Study so scientific files cannot fall into the hard-blocked `other` classification.

The checked-in profile maps Study state to `studies/`, large outputs to `.objects/`, the framework implementation to `tools/studyctl/`, and tests to `tests/`. `AGENTS.md`, repository Skills, Codex policy, the workflow guide/templates/schemas, and `tools/studyctl/**` are protected governance or enforcement paths: a scientific Study cannot authorize changes to them, even though this framework repository develops those files. A host project should map scientific implementation to its native roots—for example `src/` and `packages/solver/`—tests to `test/`, and reusable experiment configurations to `experiments/`. Only the explicit bootstrap/upgrade workflow may change workflow governance or enforcement code. The semantic roles stay the same while physical paths follow the host repository.

If the profile or workflow tooling is absent, install or migrate it with the explicitly invoked `bootstrap-scientific-workflow` skill. That bootstrap Skill must come from a personal/global installation, a plugin, or this framework source repository; an unbootstrapped target cannot discover its own future repo-local bootstrap Skill. Bootstrap installs the four runtime Skills and merges the smallest compatible profile, commands, ignore rules, and runtime workflow. Ordinary Study execution must not guess missing paths.

Choose `study_root` and `object_root` during initial installation. Once Studies or output objects exist, changing either value is a data migration rather than an ordinary profile edit. V2 has no automatic root migrator: keep the roots or perform an explicitly reviewed migration that preserves recorded paths, manifests, hashes, and external pointers before updating the profile.

### Where research-produced files belong

| Product | Location | Lifecycle and authority |
|---|---|---|
| Notes, provisional derivations, disposable scripts, prototype code | `<study_root>/STUDY_ID/work/active/` | Mutable scratch space; may be archived by compaction; cannot directly support a Claim |
| Finalized ExperimentIntent records | `<study_root>/STUDY_ID/intents/` | Immutable cognitive contracts: why evidence is requested and how observations will be assessed |
| Finalized ControlGraphSpec records | `<study_root>/STUDY_ID/control-plans/` | Immutable prospective control topology bound to an exact Intent |
| Brief, Claims, optional formal artifacts, Run manifests, Evidence, Checkpoints | The Study below `<study_root>` | Versioned or immutable authoritative research state according to object type |
| Adopted production implementation | A configured `source_root` | Normal host code; reviewed, tested, and committed with the repository |
| Adopted unit, integration, regression, convergence, or scientific validation tests | A configured `test_root` | Normal host tests; must use the host framework and validation commands |
| Reusable experiment configurations or launch code | A configured `experiment_root` | Normal host experiment assets; governed like source code |
| Checkpoints, arrays, trajectories, profiler traces, and other large Run outputs | Below configured, Git-ignored `object_root` | Local payload or pointer metadata for an external store; the Run manifest records the declared path and hash |
| Deterministic STATUS, CHANGES, COMPACTION_INPUT, REVIEW_PACKET, and REVIEW views | Study `generated/` | Regenerable projections, never sources of truth |

The promotion rule is simple: keep a candidate in Study `work/` while it is disposable; move it into the appropriate host-native source, test, or experiment root once other code, Runs, or researchers should depend on it. Do not leave production modules or tests under `work/`, and do not place exploratory notes in host source directories.

All managed Study paths are repository-confined. The Study root, Study directory, authoritative files, and every descendant of `formal/`, `work/`, `runs/`, `evidence/`, `checkpoints/`, and `generated/` must be ordinary files/directories without symbolic-link components. Commands fail closed when that layout changes after initialization. Large payloads use the separately confined `object_root`; the remaining unavoidable concurrent time-of-check/time-of-use race is treated as a documented local-process risk.

### Govern host code and test changes

Study state may be drafted on the intake branch, but host source/test changes use a dedicated Study branch matching the profile. A linked worktree is recommended for isolation and can be made mandatory with `git.require_linked_worktree`; it is not confused with the separate branch requirement. The approved Study intake state must exist in the commit from which the worktree is created. After human approval, first commit the Brief, approval, proposed Claims, and other intake records according to the host repository's review policy; then create the Study branch/worktree from that commit. With the default branch template:

```bash
git add studies/SC-0001
git commit -m "Initialize SC-0001 research brief"
git worktree add ../project-SC-0001 -b study/SC-0001/method-a HEAD
cd ../project-SC-0001

python -m tools.studyctl changeset-new SC-0001 \
  --allow 'src/solver/**' \
  --allow 'tests/solver/**'
```

`changeset-new` creates `<study_root>/SC-0001/formal/CHANGESET.json`, pinning an immutable base commit, Study branch, permitted component-aware path patterns, and repository-native validation commands. `*` matches one path component; use `**` only when recursive scope is intended. Use the narrowest practical allowlist. Protected enforcement, generated, vendor, output-object, unclassified, and other-Study paths remain forbidden even if an allow pattern is broad. Another Study may appear in the diagnostic inventory, but any other-Study diff makes the current Run ineligible for formal Evidence. Use one isolated Study branch/worktree rather than mixing Study histories.

Before a consequential Run, Evidence finalization, compaction, review, or handoff, check the real repository state:

```bash
python -m tools.studyctl check-changes SC-0001
```

The command compares the fixed CHANGESET base commit to `HEAD`, plus staged, unstaged, and untracked paths, against the profile and CHANGESET. This actual Git diff is authoritative. `--changed-path` flags are useful declarations for progressive formalization, but they neither grant write access nor conceal an omitted path.

Before a Run can use changed host source, tests, or experiment assets as Evidence-producing code, commit the allowlisted change and execute the repository-native validators recorded in the profile:

```bash
python -m tools.studyctl validate-changes SC-0001
python -m tools.studyctl check-changes SC-0001
```

`validate-changes` never uses a shell string. It records the exact argv, exit code, output hashes/tails, validation commit, profile hash, CHANGESET hash, validated path set, and validated source/test/experiment tree hash in `formal/VALIDATION.json`. The proof remains valid across later commits that change only the current Study state, provided the validation commit remains an ancestor of `HEAD`. It becomes stale when the validated host tree, profile, CHANGESET, command set, branch, or ancestry changes. A Run copies the exact proof into its own immutable directory. If the Study branch is explicitly rebased or synchronized with a moving base ref, renew the fixed anchor through `changeset-renew`; the previous contract and validation proof are archived as history rather than active formal context:

```bash
python -m tools.studyctl changeset-renew SC-0001
python -m tools.studyctl validate-changes SC-0001
```

Exploratory Runs can still be recorded when Git is unavailable, but their host change scope cannot be verified and they are ineligible for Evidence. Likewise, allowlisted host code or tests must be committed before an Evidence-producing Run; staged, unstaged, or untracked host changes make the sealed Run Evidence-ineligible. Study-state edits remain possible because Brief, Claims, ExperimentIntent and ControlGraphSpec drafts, Evidence drafts, and Run records naturally evolve during research.

## Route before intake

Scientific content alone is not consent to create persistent workflow state.
Route by the user's requested action:

| Request | Route |
|---|---|
| One-off discussion, explanation, derivation, critique, or brainstorming | Answer directly; do not create or modify a Study |
| Explicit request to start, create, or persistently investigate a new question | Use `start-scientific-study` and allocate one new draft |
| Named existing Study | Run `resolve-study STUDY_ID`; revise an unapproved draft with `start-scientific-study`, resume a fresh approved Study with `scientific-study`, and report missing or invalid state |
| ID-less request to continue or resume previous/current research | Run `python -m tools.studyctl resolve-study` before selecting either Study Skill |

`resolve-study` is deterministic and read-only. It validates the repository
profile, safely classifies direct `SC-NNNN` Study directories as `draft`,
`approved`, or `invalid`, and succeeds only for one unambiguous valid candidate.
A `draft` is either the exact initialized/revision Brief placeholder state, or
has no validation error except its missing human Brief approval. Both are
routed to `start-scientific-study` under the same ID so intake can be completed
without allocating a replacement. Any other malformed or unsafe state fails
closed. An `approved` candidate has a fresh approval and is routed to
`scientific-study`. A human Verdict is an interpretation record, not an
automatic closed-state marker, so it does not exclude an otherwise valid Study.

```bash
python -m tools.studyctl resolve-study
# Or validate and route an explicitly named existing Study:
python -m tools.studyctl resolve-study SC-0001
```

The successful JSON names the `study_id`, `phase`, and `skill`. With zero or
multiple candidates, or with one invalid candidate, the command fails with a
concise candidate summary. Codex then asks one bounded routing question. It
must not convert an unresolved continuation into a new Study or run `init` as a
fallback. Naming a Study on the next turn resolves the route; naming an
unapproved draft does not bounce between Skills—the start Skill revises that
same draft through approval.

## Start directly from a scientific idea

Give Codex the idea, goal, and any constraints you already know. For example:

```text
研究在现有 VMC 模型中加入等变 attention，目标是在保持精度的同时降低
Laplacian 计算成本。请直接建立研究任务并准备后续研究。
```

When the user explicitly requests a new persistent investigation and does not name an existing Study ID, Codex uses the repository `start-scientific-study` skill. It will:

1. inspect only enough of the repository to interpret the idea;
2. allocate the next Study ID and initialize the Study;
3. draft the Brief, non-goals, protected conditions, Evidence requirements, and proposed Claims;
4. distinguish human-supplied assumptions from Agent-inferred assumptions;
5. record open scientific questions and ask only about ambiguities that truly block authorization;
6. regenerate STATUS and run deterministic checks; and
7. stop before approval, implementation, formalization, Runs, or compute use.

The response should contain the Study ID, a concise interpretation, blocking decisions if any, and—when no blocker remains—one approval command. Correct the interpretation in chat if needed; Codex revises the same draft instead of opening another Study. The internal path is:

```text
Natural-language idea
  -> Agent-drafted Brief and proposed Claims
  -> human review and Brief approval
  -> scientific-study execution loop
```

The Brief is still an authority boundary, but it is an Agent-produced internal record rather than a form the scientist must author. Missing budgets, thresholds, data splits, evaluator principles, or baseline permissions are recorded explicitly; Codex must not invent them. Only omissions that change the research intent, a protected condition, the hard budget, or permission for the immediate next action block authorization. Technical choices that Codex can safely investigate—such as candidate methods, benchmark design, baseline implementation, evaluator details, or hardware—remain non-blocking open questions until a later progressive-formalization boundary. An omitted budget never authorizes expensive compute.

### Just-in-time alignment

Codex does not run a general requirements interview. It first inspects available repository evidence and drafts the best current interpretation, then classifies each ambiguity:

| Ambiguity | Treatment |
|---|---|
| It changes the scientific question, desired Claim, protected conditions, hard budget, or permission for the immediate consequential action, and no safe reversible default exists | Ask now |
| It matters only when choosing a method, formalizing an evaluator or protocol, spending substantial compute, or interpreting Evidence | Record and align at that later boundary |
| A conservative, reversible provisional interpretation exists | Record it as an unconfirmed assumption and continue |

At one decision boundary, Codex asks at most one compact batch of three questions. Each question states the current interpretation and why the answer is needed. It may ask one follow-up batch only if the answer creates a genuinely new material branch; it must not repeat or rephrase the same unresolved question.

If alignment remains unresolved, the Study stays in `DRAFT`. Only the blocked action pauses; safe read-only or low-cost reversible investigation may continue. Codex aligns again later only when new Evidence exposes a new material ambiguity or the Study reaches a protected, expensive, or hard-to-reverse boundary.

After reviewing the draft, a human approves the exact displayed hash in an interactive terminal:

```bash
python -m tools.studyctl approve-brief SC-0001
```

Codex is blocked from invoking this human-only command. After approval, say `继续执行 SC-0001` or otherwise ask Codex to continue; the `scientific-study` skill takes over automatically.

## Manual initialization fallback

From the repository root:

```bash
python -m tools.studyctl init SC-0001 --title "Short study title"
```

This command-line path is intended for automation, recovery, or users who explicitly prefer manual control. Edit `<study_root>/SC-0001/BRIEF.md`, replace every placeholder, and keep the machine-readable metadata block. Then approve the exact displayed hash:

```bash
python -m tools.studyctl approve-brief SC-0001
```

This is a procedural local approval, not cryptographic identity. Reviewer identity comes from `STUDYCTL_REVIEWER`, then local Git configuration, then the local account.

The visible `STUDYCTL-HARD-BUDGET` JSON block is the sole numeric authority for
the Study's lifetime GPU-hour, CPU-hour, and decimal-GB limits. Do not repeat
different hard numbers in prose or hidden metadata. A numeric zero and `null`
both authorize no positive declared use; use `null` when the human has not yet
set a limit. Changing the block changes the Brief hash and therefore requires a
new approval.

To change an approved Brief safely:

```bash
python -m tools.studyctl brief-new-version SC-0001
```

Edit the new draft and obtain a new approval. Evaluator, data-split or
acceptance-criteria changes also reuse this Brief approval gate after the
protected artifact is updated. An Evaluator defines how an observed quantity
is computed. The Brief or a finalized ExperimentIntent owns why that quantity
matters and how it will be assessed; a control implementation cannot introduce
or relax a scientific threshold.

## Progressive formalization

Informal exploration is the default. Formalize only when a decision becomes consequential: scientifically meaningful, shared, expensive, difficult to reverse, needed for reproducibility or Claim interpretation, or entering Evidence or Review.

Check before such work and supply facts explicitly:

```bash
python -m tools.studyctl check-formalization SC-0001 \
  --estimated-gpu-hours 12 \
  --estimated-cpu-hours 2 \
  --estimated-storage-gb 4 \
  --changed-path models/new_method.py \
  --scientific-critical
```

The result is `PASS`, `ADVISORY`, or `BLOCKED`, with the smallest missing
artifact. Policy defaults require an active `formal/PROTOCOL.json` at 10
GPU-hours, `METHOD.md` before scientific-critical shared code enters Evidence,
`EVALUATOR.json` plus renewed Brief approval for protected evaluator changes,
and `PLAN.json` only for genuine parallel dependencies or multi-worker
orchestration. An active PLAN is ready only when it is the byte-identical
materialization of a current finalized ControlGraphSpec whose exact
ExperimentIntent is still current. Arbitrary or independently authored
`formal/PLAN.json` files are invalid.

Formalization debt is derived governance status computed from the current
policy, consequential scope, and active artifacts. It is not an author-owned
field in `CLAIMS.json`, and editing a Claim cannot clear a missing
formalization gate.

For a nontrivial evidence-seeking computation, create the cognitive and control
contracts separately:

```bash
python -m tools.studyctl intent-new SC-0001 \
  --id INTENT-0001 \
  --evidence-gap-id GAP-0001 \
  --evidence-gap "Three mesh levels have not been compared." \
  --objective "Estimate mesh discretization error." \
  --requested-observation observed_convergence_order \
  --requested-observation richardson_error_estimate \
  --evidence-requirement "Three provenance-bound mesh solutions." \
  --claim CLAIM-0001

# Complete assessment_semantics.criteria in the returned draft.
python -m tools.studyctl intent-finalize SC-0001 --file <intent-draft>

python -m tools.studyctl plan-new SC-0001 \
  --id CG-0001 --intent INTENT-0001 --intent-version 1 \
  --executor slurm --cpu-hours 80 --parallel-workers 3

# Complete nodes, edges, and terminal completion nodes in the returned draft.
python -m tools.studyctl plan-finalize SC-0001 --file <plan-draft>
python -m tools.studyctl plan-activate SC-0001 --id CG-0001 --version 1
```

Intent and Plan drafts remain mutable Workspace files. Finalization writes new
read-only, versioned records under `intents/` and `control-plans/`; it never
turns the draft itself into authority. `formal/PLAN.json` contains only the
currently activated complete ControlGraphSpec. Historical Plan records stay
outside `formal/` so every later Run does not recursively copy the entire Plan
history.

`GRAPH_RECORDS.sequence.json` binds every finalized Intent and Plan version by
canonical-record and exact-file digest. Finalization publishes the sealed
record, then advances this sequence while holding the Study authority lock. If
the process stops between those commits, all ordinary graph operations fail
closed. After verifying the one additional record, recover only forward:

```bash
python -m tools.studyctl recover-graph-record-sequence SC-0001
```

The command uses flags and configured path patterns; it never guesses scientific meaning from arbitrary code. In particular, the parallel-Plan gate is enforced only when the caller truthfully supplies `--parallel-workers` or `--has-parallel-dependencies`; the current runtime does not infer scheduler topology from arbitrary Run argv, and `studyctl run` is not yet a whole-graph executor.

`check-formalization` and `run` also enforce the approved cumulative hard
budget. Registration serializes budget checking and reservation, so concurrent
Agents cannot both spend the same remaining allowance. Exact equality with a
limit passes; any positive excess blocks before the child process starts.
Succeeded, failed, interrupted, incomplete, and still-running Runs all retain
their reservation, so retrying a failed experiment cannot silently reset the
budget. Storage is charged as the larger of its declared estimate and the
actual size of declared outputs; an unexpected storage overrun is preserved as
an `incomplete` Run and blocks later Runs until the human authorizes a larger
Brief budget.

`<study_root>/<STUDY_ID>/RUNS.ledger.json` is the durable identity and budget
index. Its high-water mark is monotone, it contains an entry or aborted
tombstone for every allocated Run ID, and its digest is validated before
admission. The ledger lives outside `runs/`, and one Study-directory lock
serializes Run budget/identity transitions with Brief approval/revision,
Verdict recording, and compaction finalization. Replacing the entire `runs/`
directory therefore cannot create a second budget namespace or reuse
`RUN-000001`, and a concurrent Brief revision cannot cross a compaction or Run
authority snapshot. A missing/corrupt ledger or a ledger entry whose Manifest
disappeared blocks validation and all new Runs.

`<study_root>/<STUDY_ID>/EVIDENCE.sequence.json` applies the same monotone
principle to Evidence creation pressure. Creating an Evidence draft durably
advances its digest-bound high-water mark before publishing the draft. A
failed publication burns that count rather than rolling it back, and deleting
an unreferenced draft therefore cannot make compaction pressure decrease. A
missing, corrupt, or rolled-back sequence blocks Evidence growth and
validation. The current runtime does not reconstruct a missing sequence from
visible Evidence files.

`<study_root>/<STUDY_ID>/CHECKPOINTS.sequence.json` binds the monotone
Checkpoint high-water mark and latest Checkpoint digest. Validation and active
context generation reject a missing, renamed, truncated, rolled-back, or
unindexed Checkpoint chain; the next ID is derived from this authority rather
than from whichever files remain visible.

When a Run uses newly changed scientific-critical code, pass `--changed-path PATH` and/or `--scientific-critical` to `studyctl run`; pass `--shared-across-runs` when the implementation is being reused. These declarations are sealed as formalization context. `studyctl` independently derives critical paths from the actual Git changes, so omitting a declaration cannot bypass a required METHOD or CHANGESET.

Formal inventory includes current regular files below `formal/`; superseded `changeset-history/` records remain historical provenance and never re-enter active context. Known policy artifacts use their stricter readiness checks; an additional JSON artifact is active only with `"status": "active"` or `"finalized"`, and an additional Markdown artifact (for example `MODEL.md`) uses a `Status: active` or `Status: finalized` line. Other current files remain visible as stale/draft inventory.

## Execute and interpret a Run

Route important calculations through the Run registry:

```bash
python -m tools.studyctl run SC-0001 \
  --purpose "Evaluate the protected baseline" \
  --cohort COHORT-001 \
  --input data/fixed-input.json \
  --input config/baseline.json \
  --output .objects/SC-0001/baseline.json \
  --baseline-output .objects/SC-0001/baseline.json \
  --seed 17 \
  -- python scripts/evaluate.py --config config/baseline.json
```

Arguments after `--` are preserved as an argument vector and are never interpreted through a shell. The command runs from the profile's `run_cwd`; the manifest stores both its machine-local absolute path and portable profile-relative path. Before the child process starts, `studyctl` first reserves a never-reused ID and budget in the Study ledger, builds and fsyncs a complete hidden Run tree, atomically publishes its `running` Manifest, and binds that Manifest back into the ledger. Only then may it invoke the child inside a sealed execution boundary. It later atomically replaces the Manifest with a read-only `succeeded`, `failed`, `interrupted`, or `incomplete` record. Output checking or hashing failures are sealed as visible `incomplete` records. If terminal replacement itself fails, the `running` Manifest and ledger reservation remain visible instead of disappearing from accounting. The terminal logs are sealed read-only whenever finalization can complete.

The manifest points to immutable per-Run copies of the repository profile, CHANGESET, validation proof, and active formal artifacts. It also classifies actual Git changes before and after execution and records whether the Run is Evidence-eligible. Later revisions of `METHOD`, `PROTOCOL`, or other active formal files therefore do not invalidate older Runs; changing a formal artifact during the Run does make that Run ineligible. `running` and `incomplete` Runs cannot enter Evidence. Validation scans allocated `RUN-*` directories as well as existing manifests, so a missing Manifest is an explicit registry error rather than an invisible orphan.

The sealed boundary is an enforcement mechanism, not merely an endpoint comparison. The child receives a newly constructed allowlisted environment, a private `HOME` and temporary directory, no network access, read access to the runtime, profile-declared source roots, direct command files, and declared inputs, and persistent write access only to declared outputs. Repository-wide host writes are denied, so a command cannot temporarily replace approved code and restore it before final hashing. An inherited variable such as `HELDOUT_PATH=/tmp/heldout.json` is removed, and a target outside the read allowlist is inaccessible. The Manifest records the backend name and version, policy format and digest, portable effective environment and its digest, and access claims in `execution_boundary`; a missing, unknown, internally inconsistent, or non-sealed boundary is never Evidence-eligible.

Execution is backend-neutral above that boundary. The protected repository profile supplies an ordered `execution.backend_preference`, while `studyctl run --execution-backend BACKEND` may select one allowed implementation explicitly. V1 provides two implementations:

- `macos-seatbelt` generates a Seatbelt policy and invokes `sandbox-exec`.
- `linux-bubblewrap` creates isolated mount, PID, IPC, UTS, and network namespaces with Bubblewrap, requests cgroup isolation where supported, drops all capabilities, mounts runtime/code/declared inputs read-only, and gives the process a private object store. GPU device classes are exposed only when standard CUDA/NVIDIA, HIP/ROCm, or oneAPI visibility variables request them; the scheduler's host device cgroup and permissions remain the allocation authority. A non-setuid installation also uses the kernel's unprivileged user-namespace mechanism. Only declared regular output files are copied from that private store to the host after all same-session descendants exit.

Backend availability is capability-based, not inferred from an OS name alone. The selected implementation must pass a real isolation probe before Run-ID and budget reservation. A missing executable, disabled Linux user namespaces, a prohibited network namespace, or an unusable nested Seatbelt therefore rejects the Run before the scientific command starts. Automatic selection may try the next profile-approved implementation, but it never falls back to endpoint hashes.

On Slurm or PBS, invoke `studyctl run` inside an interactive allocation or batch script already running on the compute node. The sealed child intentionally has no scheduler network access, so wrapping an outer `srun`, `sbatch`, or `qsub` submission inside the child is not equivalent. The V1 Linux backend requires `bwrap` and a successful namespace capability probe on the compute node; non-setuid installations normally rely on unprivileged user namespaces. Sites that prohibit the required isolation need a future reviewed backend, such as an OCI/Apptainer capsule or a trusted-worker protocol; they remain fail-closed rather than receiving a weaker Run label.

These backends are not yet complete content-addressed execution capsules. Profile `source_roots`, the Python/runtime prefix, selected system paths, allocation-visible device interfaces, and `execution.trusted_read_only_paths` remain trusted readable ambient scope; undeclared data or default weights placed there are not excluded by the policy. Extra cluster runtime roots such as a compiler, MPI, CUDA, or environment-module prefix must be explicitly named in the protected profile and are recorded in the boundary summary. The full generated backend policy is not retained with the Run, only its digest and summary, and Python dependencies or external programs are not independently content-pinned by this field. The boundary therefore enforces the declared host access boundary but does not by itself prove a complete, independently reconstructable computation image.

Every mutable file statically visible in the command argv—including literal paths, quoted paths, and a directly executed local `python -m` module—must also be supplied with `--input`, so mutable code or configuration under `work/` or an ignored scratch directory cannot bypass provenance. Static discovery remains an early diagnostic, while the selected backend supplies the enforced host boundary described above. Profile `source_roots` are trusted code roots and must not contain held-out data or other undeclared scientific inputs; profile validation forbids them or trusted runtime roots from overlapping `object_root`. Every declared output must use a new path below `object_root`; every produced regular file is hashed and sealed read-only. A missing declared output makes the Run Evidence-ineligible, and that declared path is thereafter reserved. Within one Study, normalized output ownership is checked inside the serialized registration transaction and becomes visible with the `running` Manifest, so concurrent registrars cannot claim the same still-absent path; validation independently rejects duplicate ownership across manifests. If an absent file appears later, or an existing output could not be hash-pinned, further Run admission fails closed until the retained bytes are resolved. Evidence creation and finalization re-check input, output, stdout, stderr, governance-snapshot, and formal-artifact-snapshot file types, sizes, and hashes. A missing or altered dependency makes the Run ineligible; it is not repaired by editing its immutable manifest.

Every declared `--output` must be a repository-relative path resolving below the configured, Git-ignored `object_root`; absolute outputs and outputs elsewhere are rejected before the computation starts. A declared output path must be new: `studyctl` refuses to overwrite an existing file and makes a produced regular file read-only after hashing it. Directory-shaped results must first be packaged into one immutable file, or represented by a hashed pointer manifest to an external artifact store. Bootstrap must merge an ignore rule for the chosen object root, and profile validation checks it. The manifest stores output paths, sizes, retention classes, and hashes.

An `--input` may be repository-root-relative or an absolute external scientific-data path; external inputs are canonicalized and content-hashed. `--output` and output-retention flags are repository-root-relative and must remain below `object_root`. Command arguments after `--` are interpreted by the program from the configured `run_cwd`. If the program itself receives an output path and `run_cwd` is not the repository root, pass the corresponding run-directory-relative or absolute path to the program while registering the repository-relative path with `--output`.

Pass every data, configuration, checkpoint, dynamically imported module, or mutable script file that is not fully fixed by the recorded clean Git commit as a repeated `--input`. Static checks are a safety net, not dependency tracing.

Every retention flag must repeat a declared `--output` path. Use `--pin-output PATH`, `--baseline-output PATH`, or `--unique-anomaly-output PATH` before execution so the sealed manifest carries GC protection; a baseline and unique-anomaly classification are mutually exclusive, while either may also be pinned.

### Confirm only when promoting a strong Claim

Do not pre-register routine exploration. The default command shown above
creates an exploratory Run. After exploration identifies a candidate worth a
strong test, use this one-way sequence:

```text
explore freely
-> select a candidate and explicit Claim
-> start one minimal Confirmation campaign for the exact Claim version
-> freeze its first Confirmation Record
-> execute new confirmatory Runs in its planned slots
-> create campaign-complete confirmatory Evidence
-> promote the Claim
```

Create a draft that copies the current Claim statement and scope:

```bash
python -m tools.studyctl confirmation-new SC-0001 \
  --id CONF-0001 \
  --claim CLAIM-0001
```

Edit only the author-owned fields in the returned draft: candidate descriptions
and paths; held-out status, rationale, and paths; analysis and decision rules;
stopping and exclusion rules; and every planned slot. For a continuation, also
fill the generated campaign disclosure fields described below. Leave
`campaign_id`, campaign sequence, predecessor, `bindings`, code state,
formal-artifact bindings, freshness, watermarks, freeze time, and digests out of
the draft: finalization derives and adds them from live files and verified Run
history. Do not edit the generated `created_at` or Claim bindings.
The compact draft is authoring input; `confirmation.schema.json` validates only
the derived immutable record produced by finalization.
The conservative draft default is `not_held_out`, so `not_applicable` must be
chosen explicitly and explained. Active or finalized `PROTOCOL.json` and
`EVALUATOR.json` are frozen automatically. Then freeze the record before any
registered slot runs:

Each slot freezes its exact argument vector, candidate ID, seed,
`hardware_class`, precision, custom Cohort fields, and declared input paths.
Run flags omitted at execution resolve through repository-policy defaults, and
those resolved values must still equal the frozen slot.

```bash
python -m tools.studyctl confirmation-finalize SC-0001 \
  --file <returned-confirmation-draft>
```

Freezing is not human approval and does not claim that data is secret. It
creates an immutable, hash-addressed record of what this repository workflow
had observed before the confirmatory Runs. If the workflow has already used a
declared held-out binding, the record marks it reused rather than fresh.
External access that was not recorded by this repository cannot be proved
absent and remains a review limitation.

Each confirmatory execution must match one frozen slot exactly:

```bash
python -m tools.studyctl run SC-0001 \
  --mode confirmatory \
  --confirmation CONF-0001 \
  --slot SLOT-001 \
  --purpose "Confirm CLAIM-0001 under the frozen protocol" \
  --cohort COHORT-002 \
  --input config/confirmatory.json \
  --output .objects/SC-0001/confirmatory-slot-001.json \
  --seed 17 \
  -- python scripts/evaluate.py --config config/confirmatory.json
```

The Confirmation ID, hash, and slot are written into the initial `running`
Manifest before the child process starts. A published `running`, succeeded,
failed, interrupted, or incomplete attempt consumes the slot. A slot cannot be
retried by hiding a failure; change the design explicitly and freeze a new
Confirmation Record instead. Exploratory and legacy Runs cannot be edited or
re-labeled into confirmatory Runs.

All Confirmations for the same exact Claim version belong to one derived
campaign. Let \(I\) be a Claim ID, let \(S\) be its statement, let \(Q\) be its
scope, and define the version digest
\(h=\operatorname{SHA256}(\operatorname{canonicalJSON}(\{S,Q\}))\). The
campaign identity is derived from the sorted set of pairs \((I,h)\) frozen by
the Confirmation; it is not chosen by the author. A Claim version cannot be
placed into a second campaign by adding or removing another Claim from the
Confirmation set.

A later Confirmation can be finalized only after every slot in all preceding
campaign records has exactly one terminal attempt. Its draft must then disclose
one of two transitions:

- `replication`: give a non-empty rationale and an explicit list of differences;
  it does not invalidate or supersede the predecessor.
- `corrective_supersession`: give the rationale and differences, set
  `supersedes` to the generated predecessor reference, and state a non-empty
  `invalidity_reason`.

The predecessor, sequence, and campaign identity are recomputed at finalization,
so editing those fields cannot detach a retry from its history. A frozen but
unexecuted predecessor remains pending; creating a new record is not an
administrative way to cancel its slots.

Confirmatory Evidence may be authored after results are available, but its
result-independent fields are recomputed from the complete campaign, not only
from the Confirmation named by the selected Run. It must include every
Evidence-eligible terminal attempt across every campaign record, account for
every planned slot, and list integrity-ineligible attempts as explicit
exclusions. Slot locators are qualified, for example
`CONF-0001/SLOT-001`, so repeated local slot names cannot collide. The
`confirmation_campaign` projection records every immutable Confirmation hash,
sequence, transition rationale, differences, supersession, and invalidity
reason; `analysis.registered_plans` binds every frozen analysis-plan digest.
Missing slots, omitted eligible attempts, changed Claim scope, stale
candidate/protocol/evaluator bindings, a changed current frozen analysis-plan
field, or Runs from different campaigns prevent finalization. Every included
confirmatory Run must be classified as supporting, contradictory, or a failed
direction rather than left as context. Evidence containing both exploratory and
confirmatory roles is `mixed` and records the two Run sets separately.

A finalized Evidence record keeps the campaign high-water sequence it actually
audited, so extending the campaign does not mutate or invalidate that historical
Evidence. However, after a new Confirmation is frozen, older Evidence no longer
covers the current campaign and cannot by itself satisfy the
`numerically_supported` gate. Promotion becomes valid again only after new
Evidence discloses the now-complete campaign.

### Optional Observation Records

The default bridge is `Run / Artifact -> inline Observation -> Claim-specific
Evidence`. Do not create one standalone Observation Record per Run. Promote
analysis into `observations/OBS-NNNN.vNNNN.json` only when at least one
registered trigger applies. Inspect the active registry rather than assuming
that a hard-coded list is complete:

```bash
python -m tools.studyctl observation-trigger-list SC-0001
```

Registry version 1 contains the initial conditions: multiple Runs, reuse by
multiple Claims, complex analysis, multiple Cohorts, anomalies or failures,
Frontier dependency, confirmatory use, independent review, reuse across
Checkpoints, and material context deduplication.

Create and edit a draft, then seal it:

```bash
python -m tools.studyctl observation-new SC-0001 \
  --id OBS-0001 \
  --run RUN-000001 \
  --run RUN-000002 \
  --trigger multiple_runs \
  --trigger independent_review

python -m tools.studyctl observation-finalize SC-0001 \
  --file <study_root>/SC-0001/observations/OBS-0001.v0001.json
```

The draft must disclose its promotion rationale, exact Run hashes and
dispositions, Cohort fingerprints, method and implementation/evaluator hashes
when applicable, inclusion/exclusion/aggregation rules, results, distribution
or boundary cases, three uncertainty categories, scope, anomalies,
representative failures, analysis assumptions, and limitations. Cross-Cohort
analysis requires an explicit compatibility justification. Excluded, anomalous,
and representative-failure Runs require a rationale and remain visible.

Every draft also binds the exact promotion-trigger Registry
`{version, sha256}`. Registry snapshots live under
`scientific-workflow/observation-trigger-registries/`, form a contiguous
hash-linked chain, and are append-only: a later version may add a trigger but
cannot remove or reinterpret an existing trigger. Old Observations therefore
retain their original promotion meaning after the Registry grows.

An unregistered reason is a proposal, not a usable trigger. The Agent must first
explain why no existing condition covers it, the expected benefit, and the
foreseeable abuse risks. A fresh independent Reviewer may recommend the proposal,
but review alone does not change workflow policy. After explicit human adoption,
a separate workflow-maintenance change may append a Registry version:

- a semantic extension records its definition, endorsed independent-review
  rationale, reviewer-independence statement, explicit human-adoption rationale,
  and authorization statement;
- a structural extension additionally requires a supported deterministic
  validator in `studyctl`; prose cannot impersonate a mechanical check;
- confirmatory use must be explicitly allowed by the registered definition;
- a generic `other` trigger and arbitrary unregistered strings remain invalid.

The Registry and its governance metadata are protected workflow sources rather
than Study outputs. Their hashes prove which reviewed rule an Observation used;
they do not cryptographically authenticate the identities behind the recorded
review and human-authorization statements. Repository review and the required
separation of roles remain responsible for those identities.

Finalization computes an analysis fingerprint over the exact Run references,
Cohorts, selection dispositions, and analysis method. A finalized Observation is
immutable; a correction creates a new version. A different Observation ID with
the same analysis fingerprint is rejected so callers reuse the existing record.
Observation has no Claim ID or assessment field and therefore cannot itself
support or contradict a Claim.

Create an Evidence draft from terminal Runs:

```bash
python -m tools.studyctl evidence-new SC-0001 \
  --id EVID-0001 \
  --claim CLAIM-0001 \
  --run RUN-000001 \
  --run RUN-000002
```

To reuse a promoted Observation, bind its exact version while retaining the Run
subset used by the Claim-specific argument:

```bash
python -m tools.studyctl evidence-new SC-0001 \
  --id EVID-0002 \
  --claim CLAIM-0001 \
  --run RUN-000001 \
  --observation-id OBS-0001 \
  --observation-version 1
```

The selected Evidence Runs must be an exact-hash subset of the Observation Runs
whose disposition is `included` or `anomaly`; an excluded or
representative-failure Run cannot silently become the basis of the Evidence.
The Evidence reference pins `{observation_id, version, sha256}`. Omitting the
Observation flags keeps the observation inline and creates no new artifact.

Edit the reported draft. Explicitly fill its question, Run roles, analysis method, result, scope, uncertainty, limitations and assessment. Also complete `inference.observation_to_claim`, `inference.auxiliary_assumptions`, `inference.competing_explanations`, and `inference.falsification_conditions`; each list needs at least one substantive entry before finalization. For multiple Cohort fingerprints, list changed fields and a compatibility justification. Seal it with:

```bash
python -m tools.studyctl evidence-finalize SC-0001 \
  --file <study_root>/SC-0001/evidence/EVID-0001.v0001.json
```

Update `CLAIMS.json` with the finalized `{evidence_id, version, sha256}` reference. Do not omit contradictory Evidence.

The current Evidence schema enforces this argument whether the observation is
inline or hash-referenced. A result may be exact while its interpretation still
depends on an implementation mapping, measurement validity, model assumptions,
or an exclusion of alternative mechanisms. The reasoning bridge must therefore
connect the actual observations to the exact addressed Claim rather than merely
repeat either one. Auxiliary assumptions state what must hold for that bridge;
competing explanations state other mechanisms consistent with the observations;
falsification conditions identify concrete future observations, integrity
failures, or discriminating checks that would make the current assessment no
longer defensible.

Set the Claim's `evidence_basis` to the basis computed from its supporting
Evidence. `under_test` and scoped `partially_supported` may rely on exploratory
or mixed support. `numerically_supported` requires at least one finalized
Evidence record containing a complete confirmatory component, with a
workflow-observed fresh held-out
condition, or an explicit and valid `not_applicable` held-out status. A mixed
record may satisfy this gate only through its complete confirmatory component;
its exploratory component adds context but no confirmatory strength.

For `not_applicable`, deterministic validation proves only that the rationale
was nonblank, frozen before the Runs, and bound to no held-out path. Whether an
independent condition is genuinely inapplicable is a scientific judgment that
the independent reviewer must challenge and the human must interpret.

Evidence finalization rejects a Run whose sealed `change_scope.evidence_eligible` is false. A successful scientific command is therefore not enough: its host implementation scope must also be reproducible and governed.

## Validate and inspect active state

```bash
python -m tools.studyctl profile-validate
python -m tools.studyctl validate-changes SC-0001  # when host code/tests changed
python -m tools.studyctl check-changes SC-0001
python -m tools.studyctl validate SC-0001
python -m tools.studyctl context SC-0001
python -m tools.studyctl status SC-0001
```

`profile-validate` checks repository adaptation. `validate-changes` executes and pins the host validation contract. `check-changes` regenerates `generated/CHANGES.json` from Git. `validate` checks schemas, IDs, immutable digests, approval freshness, ExperimentIntent and ControlGraphSpec lineage and exact bindings, active PLAN materialization, profile/CHANGESET/validation state, actual change scope, Confirmation bindings and slot coverage, Run dependency integrity and eligibility, Observation source/fingerprint integrity, Evidence basis and Observation bindings, Claim evidence strength, Cohort compatibility, Checkpoint links, and Verdict structure. `context` regenerates the bounded `generated/ACTIVE_CONTEXT.json` selector; `status` regenerates `generated/STATUS.md`. Generated files are projections and are never authoritative.

### Bounded active context and automatic compaction pressure

The current working set is a projection, not the whole Study history. After
validation, run `studyctl context` and start from
`generated/ACTIVE_CONTEXT.json`. It contains only bounded locators for
Frontier-selected Claims and the Frontier itself: IDs, short previews, counts,
and content hashes rather than full semantic payloads. The approved Brief,
active formal artifacts, and latest Checkpoint are represented by
path/hash/size and compact count summaries. A separate bounded Confirmation
index exposes editable drafts, pending/running slots, and records awaiting
Evidence. The `graph_records` index exposes its sequence high-water/inventory
binding, bounded exact locators for the latest finalized ExperimentIntent and
ControlGraphSpec per ID, plus a separate `workspace_drafts` index whose
assurance is `mutable_non_authoritative`. It preserves full-history counts and
inventory hashes but never embeds record bodies. `decisive_observations` contains only short result previews,
Run/Cohort counts, exact hashes, paths, and the Evidence IDs that use each
Observation; it never injects complete Observation contents by default. Resume
these locators before creating a new Confirmation. Inspect only the authoritative
source sections or IDs needed by the current question. Load older Runs,
Evidence, Checkpoints, retired Claims, or work notes only by an explicit ID or
question. `STATUS.md` is a bounded human-facing projection, not the default
machine context.

Claims have two orthogonal states. `state` records epistemic support, while
`lifecycle` records whether the Claim is `active`, `retired`, or `superseded`.
Only active Claims may appear in the Frontier. Retirement and supersession are
provisional edits until the next immutable Checkpoint seals them; after that
boundary they are terminal. A replacement receives a new Claim ID and the old
Claim records `superseded_by`, and every supersession chain must end at an
active Claim. The current Claims schema requires an explicit lifecycle.

The current runtime accepts only the current Claims, ExperimentIntent,
ControlGraphSpec, Observation, Evidence, and Checkpoint schemas. Any unsupported schema version fails closed before normal Study
operation: the current CLI does not validate it as a retained historical schema
variant, infer missing Evidence semantics, or rewrite it in place. Keep an older
installation on a Git-pinned compatible workflow, or perform an explicit,
reviewed offline migration on an isolated copy and validate every migrated
record, reference, digest, and immutable-history binding before adopting the
current runtime. Never allocate a replacement Study to bypass incompatibility.

`policy.json` defines soft and hard thresholds for active, total-authoritative,
and terminal Claims; `CLAIMS.json` bytes; Frontier questions and human
decisions; serialized active-selector size; Runs and Evidence since the latest
Checkpoint; and files/bytes under `work/active/`. `status`
reports every observed value and threshold. Soft pressure deterministically
requests semantic compaction. Hard pressure prevents another Run, Evidence
draft, or review packet, while validation, status, `compact-prepare`, and
`compact-finalize` remain usable so the Study cannot self-lock. The trigger is
automatic; choosing what the science means remains an Agent/reviewer judgment.

`status` and `context` also write `generated/COMPACTION_DUE.json`. Run and
Evidence preflights persist the projected advisory in a repository-external
runtime cache, so crossing a soft threshold becomes visible without dirtying
the scientific Git worktree.

Each new Checkpoint stores only Frontier-selected active Claim snapshots,
compact content-addressed references for non-Frontier Claims, exact Observation
refs reached through decisive or contradictory Evidence, and watermarks for
Run, Observation, and Evidence creation. `compact-prepare` includes only the
latest Checkpoint reference, never every historical Frontier.

After a Checkpoint seals `retired` or `superseded` Claims, a later semantic
compaction may remove those terminal records from the current `CLAIMS.json`.
Finalization first writes each non-Frontier Claim once as an immutable,
content-addressed full record under `checkpoints/claim-records/`; the Checkpoint
pins its path, canonical hash, lifecycle, state, and replacement link.
Validation requires the exact content-addressed path, the complete applicable
Claim Schema, a read-only single-link regular file, and immutable full content
after a terminal lifecycle is sealed. It reconstructs historical supersession
chains from current and archived Claims, and requires every chain to end at an
active Claim. A missing, redirected, altered, or tail-truncated record is
rejected. Archived Claim references remain part of GC reachability. This is how
total/terminal Claim pressure can fall without losing semantic history; the CLI
never chooses which Claims to retire or remove.

## Compaction is not garbage collection

Compaction keeps active context finite while preserving all history. It updates semantic organization, archives only explicitly named scratch files, and creates an immutable Checkpoint:

```bash
python -m tools.studyctl compact-prepare SC-0001
# Use research-compaction to update Observations/Evidence/Claims, then rerun compact-prepare
# before binding the final plan to the current hashes.
python -m tools.studyctl compact-finalize SC-0001 \
  --plan studies/SC-0001/work/COMPACTION_PLAN.json
```

The plan must match
`scientific-workflow/schemas/compaction-plan.schema.json`, live outside
`work/active/`, and pin the current compaction-input, Claims hash, and
constant-size Evidence inventory binding (`total_count` plus the canonical full
`inventory_sha256`). It carries the current `frontier` object exactly once and
does not duplicate its open questions or maintain a separate action list. It
never copies the complete Evidence path/hash map.
`COMPACTION_INPUT.json` separately binds the complete Observation inventory and
monotone Observation sequence, so creating, changing, or deleting an
Observation after preparation makes the plan stale. Finalization recomputes
these bindings and also rechecks the repository-profile hash, consequential
host-scope fingerprint/count, and complete `work/active/` inventory; drift
requires a new prepare step.

`COMPACTION_INPUT.json` does not copy an unbounded history into the Agent
context. Collections that can grow with ExperimentIntent/ControlGraphSpec
versions, Runs, Observations, Evidence, Cohorts, formal artifacts, failed
directions, or `work/active/` are bounded indexes. Each index
contains a deterministic `items` batch plus `total_count`, `selected_count`,
`truncated`, and an `inventory_sha256` over the complete ordered inventory.
The same locator rule applies to current Claims, the Frontier, consequential
host paths, and the lower-assurance pre-ledger Manifest inventory: their source
path, size, revision, counts, and full hashes remain bound without embedding
the complete scientific text or historical map.
The selected batch is navigation, not authority: when `truncated` is true,
inspect source records by the current question, compact one relevant batch,
finalize, and prepare again. Finalization recomputes the complete
`work/active/` inventory hash, so changing an entry omitted from `items` still
invalidates the prepared plan. Compaction archives only explicitly selected
scratch files and never deletes historical Runs, Observations, Evidence, Claims,
or output objects.

Garbage collection is storage triage. V2 still only reports candidates and never deletes:

```bash
python -m tools.studyctl gc SC-0001 --dry-run
```

Referenced, pinned, baseline, unique-anomaly and non-reproducible objects are always retained. GC only considers objects below the configured `object_root`.

## Independent review and the final human gate

First inspect the current allowed and dirty change scope, then generate review inputs without a favorable conclusion:

```bash
python -m tools.studyctl validate-changes SC-0001  # if the implementation changed
python -m tools.studyctl check-changes SC-0001
python -m tools.studyctl review-packet SC-0001
```

The default review base comes from the repository profile; use `--base-ref` only for an explicit one-off override. The packet includes the repository profile and current Git change scope in addition to the scientific artifacts. It also includes bounded Confirmation Record and attempt locators plus full counts and inventory hashes. When `confirmation_records_truncated` or attempt `truncated` is true, the reviewer must inspect the referenced source inventory rather than treating unlisted records or attempts as absent. Start a fresh top-level Codex task for the review, set it to read-only, and invoke the repository `scientific-review` skill. The reviewer must check that the profile fits the host repository, compare the actual diff with `formal/CHANGESET.json`, verify the commit-bound `formal/VALIDATION.json`, confirm that production code/tests occupy their configured roots, audit every relevant confirmation attempt, and reject Evidence built from ineligible Runs. It must inspect source artifacts and return JSON matching `review.schema.json`; it must not edit code, Claims, Evidence or Verdicts. Save that JSON outside the reviewer session, then deterministically import and render it:

The review separately traces the cognitive chain
`EvidenceGap -> ExperimentIntent -> Observation -> Evidence -> Claim` and the
control chain `ExperimentIntent reference -> ControlGraphSpec -> active PLAN ->
Run / Artifact`, then checks the provenance bridge from execution outputs to
the Observation. Because the current runtime does not bind each Run to a
ControlGraph node, the reviewer must report node-level execution coverage as a
known limitation rather than infer it from names or scheduling order.

```bash
python -m tools.studyctl review-render SC-0001 --file /path/to/review.json
```

After reviewing both structured findings and sources, a separate trusted
write-enabled Agent presents an exact decision summary containing:

- the Study and selected Claim IDs;
- the implementation decision, rationale, and conditions;
- the scientific decision, accepted or rejected scope, rationale, and
  conditions.

The Agent may record the Verdict only when the user's current instruction
explicitly supplies these decisions or explicitly adopts that immediately
preceding complete summary. A generic `continue`, `finish`, `looks good`, a
passing review, silence, or the Agent's own recommendation is insufficient. If
a material field remains ambiguous, the Agent asks one bounded alignment batch.
Once the instruction is explicit, the Agent creates the version-2
decision-only input below the profile's Git-ignored `object_root` and invokes:

```bash
python -m tools.studyctl verdict SC-0001 \
  --agent-initiated \
  --file <decision-input.json>
```

`studyctl` derives the Verdict ID, timestamp, reviewer identity, current
commit, Brief/Checkpoint/Claim/Evidence hashes, recording timestamp, and
record digest. Active Claims require a fresh Checkpoint, and the mechanical
scope requires that Checkpoint to bind the current Brief approval and Claims.
Every new Verdict requires a clean Git worktree so its commit identifies the
reviewed implementation. The scope is checked again immediately before the
immutable Verdict is written.

The immutable record keeps the explicit user instruction, its canonical hash,
and `assurance: cooperative`; the Verdict record digest binds all decisions and
mechanical scope. This provenance makes
the translation auditable, but it is not a cryptographic human signature and
the project Hook does not prove that the instruction came from the user. The
protocol relies on the Agent obeying the routing contract; deployments that
need adversarial identity assurance require an external approval or signing
boundary.

`scientific-workflow/templates/VERDICT.json` is the Agent-facing version-2
decision input. It contains only the explicit instruction, Claim selection,
and decision fields; `studyctl` still generates all mechanical scope. The
interactive `python -m tools.studyctl verdict SC-0001` form remains available
as a manual compatibility path and performs a typed terminal confirmation.
The historical full-record `--file` form remains readable for compatibility,
but it is never accepted by `--agent-initiated` and cannot bypass current
authority or clean-worktree checks. Implementation acceptance and scientific
acceptance remain independent. The human owns the decision and final scope;
the Agent only translates and records the authorized content.

## Recover or reproduce a Run

1. Open the Run manifest below the configured `study_root` and verify its integrity with `studyctl validate`.
2. Read the Run-local governance snapshots for the exact repository profile, CHANGESET, and validation proof, then restore the base/Run commit and Study branch. A non-Git or dirty-host-code Run is explicitly ineligible for Evidence.
3. Restore inputs by their recorded paths and SHA-256 values; use the Run-local formal-artifact snapshots rather than whichever method files are currently active.
4. Recreate the recorded Python/runtime, hardware class and precision. Reconstruct the portable values in `execution_boundary.environment_variables`, replacing `${CAPSULE_HOME}` with a new private directory; verify `environment_sha256` and do not restore the caller's ambient environment. Historical manifests without those fields expose only their inherited-key allowlist and therefore need additional reconstruction evidence.
5. Recreate the same named backend and an isolation policy equivalent to the recorded `execution_boundary`; verify the backend version compatibility, policy format, and policy digest. Execute the recorded `execution.argv` directly as an argument vector inside that boundary, not as a reconstructed shell string. For `private-copy-out`, retain only the declared regular output paths. Compare output hashes and inspect `stdout.log` and `stderr.log`.

New Runs use manifest schema V4 and require an explicit `epistemic_role`
classification for later Evidence eligibility; despite the field name, the Run
remains an execution/provenance record rather than a cognitive node. V1, V2,
and V3 predate that contract and are permanently interpreted as exploratory
even if a later copy is decorated with confirmation-looking fields.
Immutable pre-budget V2 Runs keep their earlier Evidence semantics and are also
conservatively charged for declared output bytes. V1 remains historical and
Evidence-ineligible because it predates the repository-profile, change-scope,
validation-proof, and dependency-integrity contract. V3 retains its original
ledger and budget semantics. Compatibility views never rewrite old Manifest
bytes.

A genuinely pre-ledger Study cannot silently create a new identity namespace.
After independently checking that its visible V1/V2 history is intact and has
contiguous IDs beginning at `RUN-000001`, explicitly run:

```bash
python -m tools.studyctl ledger-migrate SC-0001
```

The migration rejects V3/V4 Runs, gaps, an empty history, or an existing ledger.
It cannot prove from local files alone that a continuous tail was never deleted
before migration; use Git, backups, scheduler records, or another external
append-only anchor to establish that historical premise.

The current implementation cannot automatically classify a process killed by `SIGKILL` or power loss. A crash before launch leaves a never-reused ledger reservation; a crash after launch authorization leaves the `running` Manifest and reservation. Both fail closed until explicit recovery. A terminal Manifest may be durable before its matching ledger update; the next locked registration can reconcile that one-way transition, while a missing Manifest always blocks rather than guessing. A graph record durable before its sequence update similarly requires the explicit one-record forward recovery above. The workflow also cannot prove human identity cryptographically, and checks only that a Cohort compatibility justification exists—not whether its scientific argument is sound. ExperimentIntent criteria are currently frozen and auditable but are not yet mechanically bound to typed Evidence criterion results. Local SHA-256 digests detect inconsistent bytes but are not authenticated signatures or an external rollback anchor; an actor who can replace an entire Study and all of its history is outside this local protocol. Filesystem checks still have an unavoidable time-of-check/time-of-use race against a malicious concurrent local process. GPU-hour and CPU-hour values remain self-reported reservations: their cumulative limits are enforced, but arbitrary schedulers are not independently metered. Declared-output storage is measured after execution. Git detects committed, staged, unstaged, and untracked repository paths, while external and dynamically resolved inputs still must be declared with `--input`. Project hooks remain small guardrails, not a complete security boundary; repository-profile validation, actual-diff checks, immutable snapshots, tests, clean review context, and human review are the enforcement layers.
