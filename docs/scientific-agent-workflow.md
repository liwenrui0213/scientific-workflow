# Claim-to-Evidence Scientific Workflow, V1

This repository uses a local, deterministic workflow for long-running computational research. `studyctl` records and checks facts; it does not infer scientific conclusions.

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

## Start the first Study

Use Python 3.11 or newer. From the repository root:

```bash
python -m tools.studyctl init SC-0001 --title "Short study title"
```

Edit `studies/SC-0001/BRIEF.md` and replace every placeholder. Keep the machine-readable metadata block. Then a human, in an interactive terminal, approves the exact displayed hash:

```bash
python -m tools.studyctl approve-brief SC-0001
```

This is a procedural local approval, not cryptographic identity. Reviewer identity comes from `STUDYCTL_REVIEWER`, then local Git configuration, then the local account. Codex is blocked from invoking this command by the project hook.

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

When a Run uses newly changed scientific-critical code, pass `--changed-path PATH` and/or `--scientific-critical` to `studyctl run`; pass `--shared-across-runs` when the implementation is being reused. These declarations are sealed into the Run and rechecked when Evidence is finalized, so a required METHOD cannot be bypassed between execution and interpretation.

Formal inventory includes every regular file below `formal/`. Known policy artifacts use their stricter readiness checks; an additional JSON artifact is active only with `"status": "active"` or `"finalized"`, and an additional Markdown artifact (for example `MODEL.md`) uses a `Status: active` or `Status: finalized` line. Other files remain visible as stale/draft inventory.

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

Arguments after `--` are preserved as an argument vector and are never interpreted through a shell. The terminal manifest and logs are sealed read-only even when the command fails or is interrupted. For Git worktrees, the manifest fingerprints the tracked commit/diff before and after execution; a tracked-code change blocks formal Evidence and GC candidacy. Large outputs should live below ignored `.objects/`; the manifest stores their paths, sizes and hashes.

Pass every data, configuration, checkpoint, or mutable script file that is not fully fixed by the recorded clean Git commit as a repeated `--input`. Paths appearing only inside command arguments are preserved but are not automatically interpreted as input artifacts.

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
  --file studies/SC-0001/evidence/EVID-0001.v0001.json
```

Update `CLAIMS.json` with the finalized `{evidence_id, version, sha256}` reference. Do not omit contradictory Evidence.

## Validate and inspect active state

```bash
python -m tools.studyctl validate SC-0001
python -m tools.studyctl status SC-0001
```

`validate` checks schemas, IDs, immutable digests, approval freshness, references, Cohort compatibility, Checkpoint links and Verdict structure. `status` regenerates `generated/STATUS.md`; generated files are projections and are never authoritative.

## Compaction is not garbage collection

Compaction keeps active context finite while preserving all history. It updates semantic organization, archives only explicitly named scratch files, and creates an immutable Checkpoint:

```bash
python -m tools.studyctl compact-prepare SC-0001
# Use the repository research-compaction skill to update Evidence/Claims and write plan.json.
python -m tools.studyctl compact-finalize SC-0001 --plan plan.json
```

The plan must match `scientific-workflow/schemas/compaction-plan.schema.json` and pin the current compaction-input, Claims and Evidence hashes.

Garbage collection is storage triage. V1 only reports candidates and never deletes:

```bash
python -m tools.studyctl gc SC-0001 --dry-run
```

Referenced, pinned, baseline, unique-anomaly and non-reproducible objects are always retained.

## Independent review and the final human gate

Generate review inputs without a favorable conclusion:

```bash
python -m tools.studyctl review-packet SC-0001 --base-ref main
```

Start a fresh top-level Codex task for the review, set it to read-only, and invoke the repository `scientific-review` skill. The reviewer must inspect source artifacts and return JSON matching `review.schema.json`; it must not edit code, Claims, Evidence or Verdicts. Save that JSON outside the reviewer session, then deterministically import and render it:

```bash
python -m tools.studyctl review-render SC-0001 --file /path/to/review.json
```

After reviewing both structured findings and sources, a human prepares a Verdict from `scientific-workflow/templates/VERDICT.json` and records it interactively:

```bash
python -m tools.studyctl verdict SC-0001 --file /path/to/verdict.json
```

Implementation acceptance and scientific acceptance are independent fields. Only the human may assign `accepted_within_scope`, `rejected`, or `requires_more_evidence`.

## Recover or reproduce a Run

1. Open `studies/SC-0001/runs/RUN-000001/manifest.json` and verify its integrity with `studyctl validate`.
2. Restore the recorded Git commit when available. Treat a dirty or Git-unavailable Run as an explicit reproducibility deviation.
3. Restore inputs by their recorded paths and SHA-256 values, plus the recorded Brief and formal-artifact versions.
4. Recreate the recorded Python/runtime, hardware class, precision and selected environment fields.
5. Execute the recorded `execution.argv` directly as an argument vector, not as a reconstructed shell string. Compare output hashes and inspect `stdout.log` and `stderr.log`.

V1 cannot recover from `SIGKILL` or power loss beyond leaving an incomplete Run directory, cannot prove human identity cryptographically, and checks only that a Cohort compatibility justification exists—not whether its scientific argument is sound. Compute estimates are self-reported: hard-budget fields are hash-protected and displayed, but V1 does not automatically interpret or enforce their numerical limits. Untracked files cannot be detected through Git state and therefore must be declared with `--input`. Project hooks must be trusted with `/hooks` and remain guardrails; deterministic validation and immutable records are the enforcement layer. Until this directory is initialized as a Git worktree, start Codex and run commands from the repository root so the project hook path resolves correctly; a Git worktree lets the hook locate the root from subdirectories.
