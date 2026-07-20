---
name: bootstrap-scientific-workflow
description: >
  Install, adapt, migrate, or upgrade the repository-native Claim-to-Evidence
  scientific workflow in an existing computational-science software repository.
  Use only when the user explicitly asks to bootstrap or adapt the workflow,
  not to start or continue a scientific Study.
---

# Bootstrap Scientific Workflow

Adapt the workflow to the repository instead of imposing a second project
layout. The installed system must preserve the host repository's native source,
test, experiment, command, environment, and data-management conventions.

This bootstrap Skill is a distribution-time capability: invoke it explicitly
from a personal/global Skill installation, plugin, or framework source checkout.
It installs the repo-local runtime Skills; do not assume an unbootstrapped target
can discover this Skill inside itself.

## Boundaries

- Do not execute the scientific Study itself.
- Do not approve a Brief or sign a Verdict.
- Do not overwrite existing `AGENTS.md`, `.codex/config.toml`, hooks, skills,
  experiment tracking, or artifact storage. Merge or integrate deliberately.
- Do not change scientific application behavior merely to install the workflow.
- Do not add a database, service, dashboard, orchestration framework, or runtime
  dependency unless the repository already requires it or the user approves it.
- Do not infer an unsafe repository layout when inspection leaves a material
  ambiguity. Align just in time on that ambiguity and continue after resolution.

## Workflow

### 1. Inspect before editing

Read applicable instructions and identify:

- repository root, language, packages, monorepo boundaries, and build system;
- source, test, benchmark, example, experiment, and generated-code roots;
- normal focused and full validation commands as literal argument vectors;
- current experiment tracker and large-output store, if any;
- protected configuration, vendored code, generated files, and secret-bearing paths;
- Git default branch and the repository's branch or worktree conventions;
- existing workflow files that should be extended rather than duplicated.

Use repository evidence such as manifests, CI definitions, test configuration,
and existing commands. Do not classify paths solely from directory names.

### 2. Propose the minimal adaptation

Summarize the inferred mapping before consequential edits. Ask the human only
when two plausible mappings would materially change scientific behavior,
write authority, validation, or storage. Reversible naming details do not need
approval.

The adaptation must produce
`scientific-workflow/repository-profile.json` with:

- `study_root`: repository-managed Study state;
- `object_root`: large immutable Run outputs, which must be ignored by Git;
- `run_cwd`: working directory for scientific commands;
- native `source_roots`, `test_roots`, and `experiment_roots`;
- workflow, generated, vendor, protected, and scientific-critical patterns;
- focused/full validation commands as argv arrays, never shell strings;
- Git base ref, required Study branch convention, and separate linked-worktree requirement.

Install the workflow at the Git worktree root. Reject a nested workflow root
unless path translation is explicitly implemented; do not classify Git-top-level
paths as if they were relative to a nested directory.

Treat that profile as bootstrap authority: a Study CHANGESET must never
authorize the Study to rewrite its own repository profile, `AGENTS.md`,
repository Skills, Codex policy, workflow schemas/templates, or deterministic
enforcement code.

Create or merge the Git-ignore rule required for `object_root`, and verify it
with profile validation. Treat missing configured source/test/experiment roots
as unresolved adaptation warnings, not harmless placeholders.

Treat `study_root` and `object_root` as initial-install choices. If Studies or
objects already exist, do not edit those roots directly: retain them or require
an explicit reviewed migration preserving every recorded path, manifest, hash,
and external pointer. V2 does not provide an automatic root migration.

### 3. Install or upgrade the thin protocol

Install only the missing compatible pieces:

- concise global invariants in `AGENTS.md`;
- `studyctl`, schemas, policy, and templates;
- `start-scientific-study`, `scientific-study`, `research-compaction`, and
  `scientific-review` repository skills;
- minimal human-gate hook protections;
- tests and one practical workflow guide.

Preserve existing authoritative systems. For example, reference MLflow Runs,
DVC objects, scheduler job IDs, Hydra configurations, or native test commands
instead of replacing them.

### 4. Establish code and test write governance

The installed workflow must distinguish:

- informal prototypes and notes under the Study's `work/active/`;
- accepted scientific source code under the host-native source roots;
- accepted tests under the host-native test roots;
- experiment configurations under host-native experiment roots;
- large Run outputs under the configured object store;
- immutable Run manifests and Evidence under the Study root.

Source, test, or experiment edits require a Study-specific branch and, when
configured, a linked worktree, plus `formal/CHANGESET.json`. The CHANGESET
records component-aware allowed paths, a fixed base commit, branch, and native
validation argv. Protect workflow enforcement roots from all Study contracts.
Install `validate-changes` so committed host changes must pass those commands
and produce a commit/profile/CHANGESET-bound proof before an Evidence-producing
Run. Install `changeset-renew` for explicit base synchronization and archive
prior contracts. Actual Git changes are authoritative; Agent-declared paths are
only planning hints.

### 5. Validate the installation

Run, in order:

1. repository-profile validation;
2. syntax/compile checks for installed tooling;
3. focused workflow tests;
4. the representative lifecycle fixture;
5. the host repository's normal full validation where practical;
6. an idempotence check showing a second bootstrap would make no material change.

Verify at least these failure cases:

- a changed Brief invalidates approval;
- a source/test edit without a CHANGESET is blocked;
- a protected path remains blocked even if allowlisted;
- an uncommitted host-code state cannot enter formal Evidence;
- host code without a current successful validation proof cannot enter Evidence;
- a Run output outside `object_root` is rejected before execution;
- missing or altered inputs, outputs, logs, governance snapshots, and
  formal-artifact snapshots block Evidence;
- a later formal-method revision does not invalidate an older Run because the
  Run carries immutable formal and governance snapshots;
- immutable V1 Run manifests remain readable history but cannot enter Evidence;
- incompatible Cohorts cannot be silently combined;
- compaction preserves referenced and contradictory material;
- Agent-facing paths cannot perform human approval or Verdict operations.

## Completion report

Report:

- detected repository conventions and the installed profile mapping;
- files added or merged, explicitly separating pre-existing user changes;
- where prototypes, production code, tests, Study state, and large outputs live;
- exact validation commands and observed results;
- migration decisions, limitations, and any remaining human action.

The bootstrap is complete only when the installed workflow is usable from a
clean checkout and repository-native scientific code and tests remain governed
by their existing toolchain.
