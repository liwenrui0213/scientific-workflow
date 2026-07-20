# Claim-to-Evidence Scientific Workflow, V2

This repository uses Python 3.11 or newer and a local, deterministic workflow for long-running computational research. `studyctl` records and checks facts; it does not infer scientific conclusions. V2 adds an explicit repository-adaptation contract and Git-backed governance for scientific code and test changes.

The normal human interface is a scientific idea stated in ordinary language. Codex turns that idea into the internal Study structure; the scientist does not need to choose IDs, edit templates, or manually route workflow stages.

## The two chains

The authority chain is:

```text
Human Brief -> optional Formal Artifacts -> immutable Run
```

The scientific interpretation chain is:

```text
Work -> Run -> Evidence -> Claim -> human Verdict
```

`work/active/` is a mutable scratch space. A Run records what actually executed under fixed code, inputs, configuration, environment and Cohort fields. Evidence states an explicit analysis of one or more Runs, including scope, uncertainty, limitations and contradictions. A Claim may reference only a finalized, hash-pinned Evidence version. A Verdict separately judges implementation and scientific Claims.

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

## Start directly from a scientific idea

Give Codex the idea, goal, and any constraints you already know. For example:

```text
研究在现有 VMC 模型中加入等变 attention，目标是在保持精度的同时降低
Laplacian 计算成本。请直接建立研究任务并准备后续研究。
```

When no existing Study ID is named, Codex uses the repository `start-scientific-study` skill. It will:

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
  --changed-path models/new_method.py \
  --scientific-critical
```

The result is `PASS`, `ADVISORY`, or `BLOCKED`, with the smallest missing artifact. Policy defaults require an active `formal/PROTOCOL.json` at 10 GPU-hours, `METHOD.md` before scientific-critical shared code enters Evidence, `EVALUATOR.json` plus renewed Brief approval for protected evaluator changes, and `PLAN.json` only for genuine parallel dependencies or multi-worker orchestration.

The command uses flags and configured path patterns; it never guesses scientific meaning from arbitrary code.

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

Arguments after `--` are preserved as an argument vector and are never interpreted through a shell. The command runs from the profile's `run_cwd`; the manifest stores both its machine-local absolute path and portable profile-relative path. The terminal manifest and logs are sealed read-only even when the command fails or is interrupted. The manifest points to immutable per-Run copies of the repository profile, CHANGESET, validation proof, and active formal artifacts. It also classifies actual Git changes before and after execution and records whether the Run is Evidence-eligible. Later revisions of `METHOD`, `PROTOCOL`, or other active formal files therefore do not invalidate older Runs; changing a formal artifact during the Run does make that Run ineligible.

Every mutable file statically visible in the command argv—including literal paths, quoted paths, and a directly executed local `python -m` module—must also be supplied with `--input`, so mutable code or configuration under `work/` or an ignored scratch directory cannot bypass provenance. Clean tracked files are already pinned by the recorded commit. General dynamic imports, generated path expressions, subprocesses, and obfuscated runtime file access cannot be discovered completely by V2; the researcher or Agent must explicitly declare those mutable dependencies or move them into clean tracked host roots. All declared outputs are required, must be new regular files below `object_root`, and are sealed read-only. Evidence creation and finalization re-check input, output, stdout, stderr, governance-snapshot, and formal-artifact-snapshot file types, sizes, and hashes. A missing or altered dependency makes the Run ineligible; it is not repaired by editing its immutable manifest.

Every declared `--output` must be a repository-relative path resolving below the configured, Git-ignored `object_root`; absolute outputs and outputs elsewhere are rejected before the computation starts. A declared output path must be new: `studyctl` refuses to overwrite an existing file and makes a produced regular file read-only after hashing it. Directory-shaped results must first be packaged into one immutable file, or represented by a hashed pointer manifest to an external artifact store. Bootstrap must merge an ignore rule for the chosen object root, and profile validation checks it. The manifest stores output paths, sizes, retention classes, and hashes.

An `--input` may be repository-root-relative or an absolute external scientific-data path; external inputs are canonicalized and content-hashed. `--output` and output-retention flags are repository-root-relative and must remain below `object_root`. Command arguments after `--` are interpreted by the program from the configured `run_cwd`. If the program itself receives an output path and `run_cwd` is not the repository root, pass the corresponding run-directory-relative or absolute path to the program while registering the repository-relative path with `--output`.

Pass every data, configuration, checkpoint, dynamically imported module, or mutable script file that is not fully fixed by the recorded clean Git commit as a repeated `--input`. Static checks are a safety net, not dependency tracing.

Every retention flag must repeat a declared `--output` path. Use `--pin-output PATH`, `--baseline-output PATH`, or `--unique-anomaly-output PATH` before execution so the sealed manifest carries GC protection; a baseline and unique-anomaly classification are mutually exclusive, while either may also be pinned.

Create an Evidence draft from terminal Runs:

```bash
python -m tools.studyctl evidence-new SC-0001 \
  --id EVID-0001 \
  --claim CLAIM-0001 \
  --run RUN-000001 \
  --run RUN-000002
```

Edit the reported draft. Explicitly fill its question, Run roles, analysis method, result, scope, uncertainty, limitations and assessment. For multiple Cohort fingerprints, list changed fields and a compatibility justification. Seal it with:

```bash
python -m tools.studyctl evidence-finalize SC-0001 \
  --file <study_root>/SC-0001/evidence/EVID-0001.v0001.json
```

Update `CLAIMS.json` with the finalized `{evidence_id, version, sha256}` reference. Do not omit contradictory Evidence.

Evidence finalization rejects a Run whose sealed `change_scope.evidence_eligible` is false. A successful scientific command is therefore not enough: its host implementation scope must also be reproducible and governed.

## Validate and inspect active state

```bash
python -m tools.studyctl profile-validate
python -m tools.studyctl validate-changes SC-0001  # when host code/tests changed
python -m tools.studyctl check-changes SC-0001
python -m tools.studyctl validate SC-0001
python -m tools.studyctl status SC-0001
```

`profile-validate` checks repository adaptation. `validate-changes` executes and pins the host validation contract. `check-changes` regenerates `generated/CHANGES.json` from Git. `validate` checks schemas, IDs, immutable digests, approval freshness, references, profile/CHANGESET/validation state, actual change scope, Run dependency integrity and eligibility, Cohort compatibility, Checkpoint links, and Verdict structure. `status` regenerates `generated/STATUS.md`; generated files are projections and are never authoritative.

## Compaction is not garbage collection

Compaction keeps active context finite while preserving all history. It updates semantic organization, archives only explicitly named scratch files, and creates an immutable Checkpoint:

```bash
python -m tools.studyctl compact-prepare SC-0001
# Use the repository research-compaction skill to update Evidence/Claims and write the plan.
python -m tools.studyctl compact-finalize SC-0001 \
  --plan studies/SC-0001/work/COMPACTION_PLAN.json
```

The plan must match `scientific-workflow/schemas/compaction-plan.schema.json`, live outside `work/active/`, and pin the current compaction-input, Claims and Evidence hashes. Finalization also rechecks the repository-profile hash, consequential host-scope fingerprint, and complete `work/active/` inventory; drift requires a new prepare step.

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

The default review base comes from the repository profile; use `--base-ref` only for an explicit one-off override. The packet includes the repository profile and current Git change scope in addition to the scientific artifacts. Start a fresh top-level Codex task for the review, set it to read-only, and invoke the repository `scientific-review` skill. The reviewer must check that the profile fits the host repository, compare the actual diff with `formal/CHANGESET.json`, verify the commit-bound `formal/VALIDATION.json`, confirm that production code/tests occupy their configured roots, and reject Evidence built from ineligible Runs. It must inspect source artifacts and return JSON matching `review.schema.json`; it must not edit code, Claims, Evidence or Verdicts. Save that JSON outside the reviewer session, then deterministically import and render it:

```bash
python -m tools.studyctl review-render SC-0001 --file /path/to/review.json
```

After reviewing both structured findings and sources, a human prepares a Verdict from `scientific-workflow/templates/VERDICT.json` and records it interactively:

```bash
python -m tools.studyctl verdict SC-0001 --file /path/to/verdict.json
```

Implementation acceptance and scientific acceptance are independent fields. Only the human may assign `accepted_within_scope`, `rejected`, or `requires_more_evidence`.

## Recover or reproduce a Run

1. Open the Run manifest below the configured `study_root` and verify its integrity with `studyctl validate`.
2. Read the Run-local governance snapshots for the exact repository profile, CHANGESET, and validation proof, then restore the base/Run commit and Study branch. A non-Git or dirty-host-code Run is explicitly ineligible for Evidence.
3. Restore inputs by their recorded paths and SHA-256 values; use the Run-local formal-artifact snapshots rather than whichever method files are currently active.
4. Recreate the recorded Python/runtime, hardware class, precision and selected environment fields.
5. Execute the recorded `execution.argv` directly as an argument vector, not as a reconstructed shell string. Compare output hashes and inspect `stdout.log` and `stderr.log`.

New Runs use manifest schema V2. An immutable V1 manifest is read through a transient compatibility view and remains unchanged on disk. It is retained as historical execution state and reported with a warning, but it is Evidence-ineligible because it predates the repository-profile, change-scope, validation-proof, and dependency-integrity contract. V2 does not silently “upgrade” or rewrite a V1 Run; a future explicit attestation mechanism would be required to promote one.

The current implementation cannot recover from `SIGKILL` or power loss beyond leaving an incomplete Run directory, cannot prove human identity cryptographically, and checks only that a Cohort compatibility justification exists—not whether its scientific argument is sound. `record_sha256` detects accidental or visible record corruption; it is not an authenticated signature proving that only `studyctl` could have created the bytes. Filesystem checks still have an unavoidable time-of-check/time-of-use race against a malicious concurrent local process. Compute estimates are self-reported: hard-budget fields are hash-protected and displayed, but their numerical limits are not automatically inferred from arbitrary schedulers. Git detects committed, staged, unstaged, and untracked repository paths, while external and dynamically resolved inputs still must be declared with `--input`. Project hooks remain small guardrails, not a complete security boundary; repository-profile validation, actual-diff checks, immutable snapshots, tests, clean review context, and human review are the enforcement layers.
