# Claim-to-Evidence Scientific Workflow, V2

This repository uses Python 3.11 or newer and a local, deterministic workflow for long-running computational research. `studyctl` records and checks facts; it does not infer scientific conclusions. V2 adds an explicit repository-adaptation contract and Git-backed governance for scientific code and test changes.

For a persistent investigation, the normal human interface is a scientific idea stated in ordinary language plus an explicit request to start or continue research. Codex turns that request into the internal Study structure; the scientist does not need to choose new IDs, edit templates, or manually route workflow stages. One-off scientific discussion remains ordinary conversation and creates no Study.

## The two chains

The authority chain is:

```text
Human Brief -> optional Formal Artifacts -> immutable Run
```

The scientific interpretation chain is:

```text
Work -> Run -> Evidence -> Claim -> human Verdict
```

`work/active/` is a mutable scratch space. A Run records what actually executed under fixed code, inputs, configuration, environment and Cohort fields. Evidence states an explicit analysis of one or more Runs, including scope, uncertainty, limitations and contradictions. It also records a small inference argument: let \(O\) denote the reported observations, \(C\) the addressed Claim, and \(A\) the declared auxiliary assumptions. The Evidence must explain why \(O\), conditional on \(A\), changes support for \(C\); list live competing explanations; and state observations or failures that would overturn its assessment. This is not a proof that \(C\) is true. A Claim may reference only a finalized, hash-pinned Evidence version. A Verdict separately judges implementation and scientific Claims.

Runs also have an epistemic role. Every ordinary and legacy Run is
`exploratory`: it may discover a hypothesis, narrow a candidate, or support an
`under_test` or carefully scoped `partially_supported` Claim. Exploration does
not require a Confirmation Record or other preregistration; important
calculations still run through `studyctl run`. Only when a result is being promoted to the
high-strength `numerically_supported` state does the workflow require a small
pre-confirmatory-Run Confirmation Record followed by new `confirmatory` Runs. The record
freezes the exact Claim statement and scope, selected candidate, protocol,
evaluator, held-out conditions, analysis rule, and planned Run slots. This is a
deterministic time-and-hash boundary, not a new human approval gate.

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

Exploratory Runs can still be recorded when Git is unavailable, but their host change scope cannot be verified and they are ineligible for Evidence. Likewise, allowlisted host code or tests must be committed before an Evidence-producing Run; staged, unstaged, or untracked host changes make the sealed Run Evidence-ineligible. Study-state edits remain possible because Brief, Claims, Evidence drafts, and Run records naturally evolve during research.

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

Edit the new draft and obtain a new approval. Evaluator, data-split or acceptance-criteria changes also reuse this Brief approval gate after the protected artifact is updated.

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

The result is `PASS`, `ADVISORY`, or `BLOCKED`, with the smallest missing artifact. Policy defaults require an active `formal/PROTOCOL.json` at 10 GPU-hours, `METHOD.md` before scientific-critical shared code enters Evidence, `EVALUATOR.json` plus renewed Brief approval for protected evaluator changes, and `PLAN.json` only for genuine parallel dependencies or multi-worker orchestration.

The command uses flags and configured path patterns; it never guesses scientific meaning from arbitrary code.

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
validation. A pre-sequence Study must use the explicit
`migrate-evidence-sequence` command; the migration records that deletion before
the sequence existed cannot be proven absent.

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

Arguments after `--` are preserved as an argument vector and are never interpreted through a shell. The command runs from the profile's `run_cwd`; the manifest stores both its machine-local absolute path and portable profile-relative path. Before the child process starts, `studyctl` first reserves a never-reused ID and budget in the Study ledger, builds and fsyncs a complete hidden Run tree, atomically publishes its `running` Manifest, and binds that Manifest back into the ledger. Only then may it invoke the child. It later atomically replaces the Manifest with a read-only `succeeded`, `failed`, `interrupted`, or `incomplete` record. Output checking or hashing failures are sealed as visible `incomplete` records. If terminal replacement itself fails, the `running` Manifest and ledger reservation remain visible instead of disappearing from accounting. The terminal logs are sealed read-only whenever finalization can complete.

The manifest points to immutable per-Run copies of the repository profile, CHANGESET, validation proof, and active formal artifacts. It also classifies actual Git changes before and after execution and records whether the Run is Evidence-eligible. Later revisions of `METHOD`, `PROTOCOL`, or other active formal files therefore do not invalidate older Runs; changing a formal artifact during the Run does make that Run ineligible. `running` and `incomplete` Runs cannot enter Evidence. Validation scans allocated `RUN-*` directories as well as existing manifests, so a missing Manifest is an explicit registry error rather than an invisible orphan.

Every mutable file statically visible in the command argv—including literal paths, quoted paths, and a directly executed local `python -m` module—must also be supplied with `--input`, so mutable code or configuration under `work/` or an ignored scratch directory cannot bypass provenance. Clean tracked files are already pinned by the recorded commit. General dynamic imports, generated path expressions, subprocesses, and obfuscated runtime file access cannot be discovered completely; the researcher or Agent must explicitly declare those mutable dependencies or move them into clean tracked host roots. Every declared output must use a new path below `object_root`; every produced regular file is hashed and sealed read-only. A missing declared output makes the Run Evidence-ineligible, and that declared path is thereafter reserved. Within one Study, normalized output ownership is checked inside the serialized registration transaction and becomes visible with the `running` Manifest, so concurrent registrars cannot claim the same still-absent path; validation independently rejects duplicate ownership across manifests. If an absent file appears later, or an existing output could not be hash-pinned, further Run admission fails closed until the retained bytes are resolved. Evidence creation and finalization re-check input, output, stdout, stderr, governance-snapshot, and formal-artifact-snapshot file types, sizes, and hashes. A missing or altered dependency makes the Run ineligible; it is not repaired by editing its immutable manifest.

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
-> freeze one minimal Confirmation Record
-> execute new confirmatory Runs in its planned slots
-> create confirmatory Evidence
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
stopping and exclusion rules; and every planned slot. Leave `bindings`, code
state, formal-artifact bindings, freshness, watermarks, freeze time, and digests
out of the draft: finalization derives and adds them from live files and
verified Run history. Do not edit the generated `created_at` or Claim bindings.
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

Confirmatory Evidence may be authored after results are available, but its
result-independent fields are recomputed from the frozen record. It must include
every Evidence-eligible terminal attempt, account for every planned slot, and
list integrity-ineligible attempts as explicit exclusions. Missing slots,
omitted eligible attempts, changed Claim scope, stale candidate/protocol/
evaluator bindings, any changed frozen analysis-plan field, or Runs from multiple
Confirmation Records prevent finalization. Evidence containing both roles is
`mixed` and records the exploratory and confirmatory Run IDs separately.

Create an Evidence draft from terminal Runs:

```bash
python -m tools.studyctl evidence-new SC-0001 \
  --id EVID-0001 \
  --claim CLAIM-0001 \
  --run RUN-000001 \
  --run RUN-000002
```

Edit the reported draft. Explicitly fill its question, Run roles, analysis method, result, scope, uncertainty, limitations and assessment. Also complete `inference.observation_to_claim`, `inference.auxiliary_assumptions`, `inference.competing_explanations`, and `inference.falsification_conditions`; each list needs at least one substantive entry before finalization. For multiple Cohort fingerprints, list changed fields and a compatibility justification. Seal it with:

```bash
python -m tools.studyctl evidence-finalize SC-0001 \
  --file <study_root>/SC-0001/evidence/EVID-0001.v0001.json
```

Update `CLAIMS.json` with the finalized `{evidence_id, version, sha256}` reference. Do not omit contradictory Evidence.

Evidence schema V2 enforces this argument without introducing a separate
artifact type. A result may be exact while its interpretation still depends on
an implementation mapping, measurement validity, model assumptions, or an
exclusion of alternative mechanisms. The reasoning bridge must therefore
connect the actual observations to the exact addressed Claim rather than merely
repeat either one. Auxiliary assumptions state what must hold for that bridge;
competing explanations state other mechanisms consistent with the observations;
falsification conditions identify concrete future observations, integrity
failures, or discriminating checks that would make the current assessment no
longer defensible. Finalized schema V1 Evidence remains immutable historical
Evidence and is not retroactively upgraded.

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

`profile-validate` checks repository adaptation. `validate-changes` executes and pins the host validation contract. `check-changes` regenerates `generated/CHANGES.json` from Git. `validate` checks schemas, IDs, immutable digests, approval freshness, references, profile/CHANGESET/validation state, actual change scope, Confirmation bindings and slot coverage, Run dependency integrity and eligibility, Evidence basis, Claim evidence strength, Cohort compatibility, Checkpoint links, and Verdict structure. `context` regenerates the bounded `generated/ACTIVE_CONTEXT.json` selector; `status` regenerates `generated/STATUS.md`. Generated files are projections and are never authoritative.

### Bounded active context and automatic compaction pressure

The current working set is a projection, not the whole Study history. After
validation, run `studyctl context` and start from
`generated/ACTIVE_CONTEXT.json`. It contains only bounded locators for
Frontier-selected Claims and the Frontier itself: IDs, short previews, counts,
and content hashes rather than full semantic payloads. The approved Brief,
active formal artifacts, and latest Checkpoint are represented by
path/hash/size and compact count summaries. A separate bounded Confirmation
index exposes editable drafts, pending/running slots, and records awaiting
Evidence; resume these locators before creating a new Confirmation. Inspect only the authoritative
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
active Claim. Legacy Claims without a lifecycle are interpreted as active, but
schema-V2 Claims must make the lifecycle explicit.

Claims schema V1 is retained only so historical bytes can still be validated
and audited. Because V1 had no structural size limits, it is never accepted as
active context: `context`, Run/Evidence growth, review-packet generation and
compaction preparation fail closed, while `status` emits only a bounded
migration notice. Migration is deliberately semantic rather than automatic:

1. preserve and commit the exact V1 `CLAIMS.json` (or otherwise pin its hash);
2. inspect Claims by selected ID and decide which remain active, which are
   retired, and which are superseded;
3. construct a schema-V2 `CLAIMS.json` with a small Frontier, bounded current
   questions/actions, explicit lifecycles, and no silently discarded Claim;
4. keep omitted historical content recoverable from Git or an immutable
   archived source, then run `validate`, `context`, and `status`.

The CLI cannot choose those lifecycle or scientific-meaning decisions, so it
does not provide an automatic truncating migrator. An ID-less resolver reports
the Study as migration-required instead of allocating a replacement Study.

`policy.json` defines soft and hard thresholds for active, total-authoritative,
and terminal Claims; `CLAIMS.json` bytes; Frontier questions/actions/decisions;
serialized active-selector size; Runs and Evidence since the latest Checkpoint;
and files/bytes under `work/active/`. `status`
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
compact content-addressed references for non-Frontier Claims, and watermarks
that reset Run/Evidence pressure. `compact-prepare` includes only the latest
Checkpoint reference, never every historical Frontier.

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
# Use research-compaction to update Evidence/Claims, then rerun compact-prepare
# before binding the final plan to the current hashes.
python -m tools.studyctl compact-finalize SC-0001 \
  --plan studies/SC-0001/work/COMPACTION_PLAN.json
```

The plan must match `scientific-workflow/schemas/compaction-plan.schema.json`, live outside `work/active/`, and pin the current compaction-input, Claims hash, and constant-size Evidence inventory binding (`total_count` plus the canonical full `inventory_sha256`). It never copies the complete Evidence path/hash map. Finalization recomputes that binding and also rechecks the repository-profile hash, consequential host-scope fingerprint/count, and complete `work/active/` inventory; drift requires a new prepare step.

`COMPACTION_INPUT.json` does not copy an unbounded history into the Agent
context. Collections that can grow with Runs, Evidence, Cohorts, formal
artifacts, failed directions, or `work/active/` are bounded indexes. Each index
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
scratch files and never deletes historical Runs, Evidence, Claims, or output
objects.

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
4. Recreate the recorded Python/runtime, hardware class, precision and selected environment fields.
5. Execute the recorded `execution.argv` directly as an argument vector, not as a reconstructed shell string. Compare output hashes and inspect `stdout.log` and `stderr.log`.

New Runs use manifest schema V4 and require an explicit epistemic role. V1,
V2, and V3 predate that contract and are permanently interpreted as
exploratory even if a later copy is decorated with confirmation-looking fields.
Immutable pre-budget V2 Runs keep their earlier Evidence semantics and are also
conservatively charged for declared output bytes. V1 remains historical and
Evidence-ineligible because it predates the repository-profile, change-scope,
validation-proof, and dependency-integrity contract. V3 retains its original
ledger and budget semantics. Compatibility views never rewrite old Manifest
bytes.

Finalized Evidence created before epistemic roles existed remains immutable and
is interpreted conservatively as exploratory. For a legacy Claim, preserve its
Evidence references, set `evidence_basis` to the computed conservative basis,
and change `numerically_supported` to a scoped `partially_supported` state unless
new Runs under a frozen Confirmation satisfy the strong gate. This semantic
migration requires scientific judgment; never rewrite or relabel old Run or
Evidence bytes.

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

A pre-sequence Evidence history has the analogous explicit migration:

```bash
python -m tools.studyctl migrate-evidence-sequence SC-0001
```

It validates canonical continuous versions and Checkpoint watermarks before
creating the monotone counter. Its origin record states the unavoidable lower
assurance for deletions that may have happened before migration.

A pre-sequence Checkpoint history uses the separate explicit migration:

```bash
python -m tools.studyctl migrate-checkpoint-sequence SC-0001
```

It accepts only the currently visible, schema-valid, digest-valid, contiguous
chain from `CHECKPOINT-000001`. Its origin explicitly records that deletion
before sequence initialization cannot be disproved from local files alone.

The current implementation cannot automatically classify a process killed by `SIGKILL` or power loss. A crash before launch leaves a never-reused ledger reservation; a crash after launch authorization leaves the `running` Manifest and reservation. Both fail closed until explicit recovery. A terminal Manifest may be durable before its matching ledger update; the next locked registration can reconcile that one-way transition, while a missing Manifest always blocks rather than guessing. The workflow also cannot prove human identity cryptographically, and checks only that a Cohort compatibility justification exists—not whether its scientific argument is sound. Local SHA-256 digests detect inconsistent bytes but are not authenticated signatures or an external rollback anchor; an actor who can replace an entire Study and all of its history is outside this local protocol. Filesystem checks still have an unavoidable time-of-check/time-of-use race against a malicious concurrent local process. GPU-hour and CPU-hour values remain self-reported reservations: their cumulative limits are enforced, but arbitrary schedulers are not independently metered. Declared-output storage is measured after execution. Git detects committed, staged, unstaged, and untracked repository paths, while external and dynamically resolved inputs still must be declared with `--input`. Project hooks remain small guardrails, not a complete security boundary; repository-profile validation, actual-diff checks, immutable snapshots, tests, clean review context, and human review are the enforcement layers.
