---
name: bootstrap-scientific-workflow
description: >
  Install, adapt, migrate, or upgrade the repository-native Claim-to-Evidence
  scientific workflow in an existing computational-science software repository.
  Use only when the user explicitly asks to bootstrap or adapt the workflow,
  not to start or continue a scientific Study.
---

# Bootstrap Scientific Workflow

Compile the Claim-to-Evidence protocol into the host repository's existing
layout and toolchain. Do not impose a parallel source tree or perform the
scientific Study itself.

This is a distribution-time Skill. It installs the repo-local runtime Skills;
an unbootstrapped target cannot be expected to discover this Skill inside
itself.

## Authoritative inputs

1. Read applicable repository instructions, manifests, CI and test
   configuration, experiment tracking, artifact storage, Git conventions, and
   existing workflow files before editing.
2. Read **Adapt the workflow to the host repository**, **Where
   research-produced files belong**, and **Govern host code and test changes**
   in `docs/scientific-agent-workflow.md`. Use the checked-in profile schema,
   policy, templates, `studyctl`, and tests for exact protocol details rather
   than copying them into this Skill.
3. Treat current repository evidence and existing authoritative systems as
   integration targets. Do not classify paths solely from familiar directory
   names.

## Workflow

1. **Inspect.** Identify the Git/worktree root, package boundaries, native
   source/test/experiment roots, normal validation commands, generated and
   protected areas, output storage, and any existing equivalent mechanism.
2. **Map.** Propose the smallest repository profile that maps Study state,
   large immutable outputs, native scientific code and tests, commands, and Git
   isolation to the host conventions. Ask only when two plausible mappings
   materially change scientific behavior, write authority, validation, or
   storage; choose reversible naming details without interrupting the human.
3. **Install or migrate.** Extend existing mechanisms instead of creating
   duplicates. Merge concise `AGENTS.md` invariants, the profile and protocol
   assets, deterministic `studyctl` gates, four runtime Skills, minimal hook
   protections, tests, and the practical guide. Reuse native systems such as
   experiment trackers, schedulers, configuration managers, or object stores by
   recording their identifiers and hashes.
4. **Govern host changes.** Keep provisional work inside the Study, adopted
   implementation and tests in native host roots, large payloads in the
   configured ignored object store, and authoritative research state in the
   Study root. Ensure actual Git changes—not Agent declarations—control scope,
   validation, and Evidence eligibility.
5. **Validate.** Run profile validation, syntax checks, focused workflow tests,
   the representative lifecycle fixture, practical host validation, and an
   idempotence check. Exercise the protocol's existing negative tests rather
   than reproducing their full matrix in prose. Do not report completion from
   generated files alone.

## Hard gates

- Never approve a Brief, sign a Verdict, initialize a Study, or execute its
  research as part of bootstrap.
- Never overwrite existing instructions, Codex configuration, hooks, Skills,
  scientific tooling, experiment history, or artifact storage. Merge,
  integrate, or request an explicit reviewed migration.
- Never change scientific application behavior merely to install the workflow.
- Never let a Study authorize changes to workflow governance or enforcement.
- Never add a database, service, dashboard, orchestration framework, or runtime
  dependency unless the repository already requires it or the user approves it.
- Treat established Study and object roots as data-migration boundaries. Do not
  move existing records or payloads without an explicit plan preserving paths,
  hashes, manifests, and external pointers.
- If repository inspection leaves a material unsafe mapping unresolved, align
  just in time and stop only the affected installation step.

## Output and handoff

Report the detected conventions, installed profile mapping, files merged or
added, locations for prototypes/production code/tests/Study state/large
outputs, exact validation commands and observed results, migration decisions,
known limitations, and remaining human action. Separate pre-existing user
changes from bootstrap changes.

Completion requires a usable clean-checkout workflow, passing relevant
validation, a successful representative lifecycle, and no material second-run
changes. Hand the adapted repository back to the human for review; starting a
Study is a separate `start-scientific-study` request.
