from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest

from tests.helpers import WorkflowTestCase, completed_process
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import atomic_write_json, load_json, sha256_file
from tools.studyctl.models import ValidationError, errors_only, study_paths
from tools.studyctl.run_registry import execute_run
from tools.studyctl.workspace import (
    change_state_evidence_eligible,
    changeset_issues,
    changeset_path,
    create_changeset,
    evaluate_changes,
    load_repository_profile,
    repository_profile_issues,
    repository_profile_path,
    run_change_validation,
)


class RepositoryAdaptationTests(WorkflowTestCase):
    def profile(self) -> dict[str, object]:
        value = load_json(repository_profile_path(self.root))
        self.assertIsInstance(value, dict)
        return value

    def write_profile(self, value: dict[str, object]) -> None:
        atomic_write_json(repository_profile_path(self.root), value)

    def configure_host_roots(self) -> None:
        profile = self.profile()
        profile["source_roots"] = ["src"]
        profile["test_roots"] = ["tests"]
        profile["experiment_roots"] = ["experiments"]
        profile["scientific_critical_patterns"] = ["src/critical/**"]
        self.write_profile(profile)
        for relative in ("src", "tests", "experiments"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        self.commit_all("configure host repository roots")

    def create_study_branch(self, slug: str = "adaptation") -> None:
        result = completed_process(
            ["git", "switch", "-c", f"study/{self.study_id}/{slug}"],
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_git_add(self, *paths: str) -> None:
        result = completed_process(["git", "add", *paths], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def populated_evidence_draft(
        self,
        paths: object,
        manifest: dict[str, object],
        *,
        evidence_id: str = "EVID-0001",
    ) -> Path:
        draft = create_evidence_draft(
            paths,
            evidence_id,
            ["CLAIM-0001"],
            [str(manifest["run_id"])],
        )
        item = load_json(draft)
        self.assertIsInstance(item, dict)
        item["addresses"]["question"] = "Does the deterministic result equal four?"
        item["runs"][0]["role"] = "supporting"
        item["analysis"]["method"] = "Check the recorded exact integer result."
        item["result"] = {"value": 4, "comparison": "equal"}
        item["scope"] = "the deterministic fixture"
        item["uncertainty"] = "No sampling uncertainty."
        item["limitations"] = ["No broader scientific generalization is claimed."]
        self.fill_evidence_inference(item)
        item["assessment"] = "supports"
        atomic_write_json(draft, item)
        return draft

    def test_repository_profile_adapts_study_object_and_run_roots(self) -> None:
        profile = self.profile()
        profile["study_root"] = "research/studies"
        profile["object_root"] = "research/objects"
        profile["run_cwd"] = "project"
        self.write_profile(profile)
        (self.root / "project").mkdir()
        (self.root / "research" / "objects").mkdir(parents=True)
        (self.root / "research" / "objects" / ".gitignore").write_text(
            "*\n!.gitignore\n",
            encoding="utf-8",
        )
        self.commit_all("adapt workflow repository roots")

        self.assertEqual(errors_only(repository_profile_issues(self.root)), [])
        paths = self.initialize()

        self.assertEqual(
            paths.study,
            self.root.resolve() / "research" / "studies" / self.study_id,
        )
        self.assertFalse((self.root / "studies" / self.study_id).exists())
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        self.approve(paths)
        output = "research/objects/value.txt"
        output_path = self.root.resolve() / output
        manifest = execute_run(
            paths,
            argv=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(output_path)!r}).write_text('4\\n', encoding='utf-8')"
                ),
            ],
            purpose="exercise adapted repository roots",
            output_paths=[output],
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )

        self.assertEqual(
            manifest["execution"]["cwd"],
            str(self.root.resolve() / "project"),
        )
        self.assertEqual(manifest["execution"]["cwd_relative"], "project")
        self.assertEqual(manifest["outputs"][0]["path"], output)
        self.assertTrue(output_path.is_file())
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])

    def test_repository_profile_rejects_escape_nul_and_shell_string_commands(self) -> None:
        original = self.profile()
        cases = (
            ("absolute root", lambda item: item.__setitem__("study_root", "/tmp/studies"), "stay inside"),
            ("parent escape", lambda item: item.__setitem__("object_root", "../objects"), "stay inside"),
            ("root object store", lambda item: item.__setitem__("object_root", "."), "must not be the repository root"),
            ("overlapping roots", lambda item: item.__setitem__("object_root", "studies/objects"), "must not overlap"),
            ("NUL path", lambda item: item.__setitem__("run_cwd", "project\x00escape"), "NUL byte"),
            ("backslash path", lambda item: item.__setitem__("study_root", "research\\studies"), "POSIX '/'"),
            ("missing cwd", lambda item: item.__setitem__("run_cwd", "missing-project"), "must exist"),
            ("unignored object root", lambda item: item.__setitem__("object_root", "visible-objects"), "must be ignored"),
            (
                "shell command string",
                lambda item: item.__setitem__("commands", {"test": "python -m unittest"}),
                "argv array",
            ),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                candidate = load_json(repository_profile_path(self.root))
                self.assertIsInstance(candidate, dict)
                mutate(candidate)
                self.write_profile(candidate)

                issues = repository_profile_issues(self.root)

                self.assertTrue(issues, f"{label} unexpectedly passed validation")
                self.assertTrue(
                    any(expected in issue.message for issue in issues),
                    [issue.message for issue in issues],
                )
                self.write_profile(original)

        self.assertEqual(errors_only(repository_profile_issues(self.root)), [])

    def test_profile_rejects_unprotected_workflow_root(self) -> None:
        profile = self.profile()
        profile["workflow_roots"].append("internal-governance")
        self.write_profile(profile)

        issues = repository_profile_issues(self.root)

        self.assertTrue(
            any("every workflow_root must be covered" in issue.message for issue in issues),
            [issue.message for issue in issues],
        )

    def test_unclassified_repository_path_is_never_a_study_output(self) -> None:
        paths = self.initialize()
        self.create_study_branch("unclassified")
        create_changeset(paths, ["rogue/**"])
        target = self.root / "rogue" / "solver.py"
        target.parent.mkdir()
        target.write_text("VALUE = 4\n", encoding="utf-8")

        state = evaluate_changes(paths, require_validation=False)

        self.assertEqual(state["outcome"], "BLOCKED")
        self.assertIn(
            ("rogue/solver.py", "unclassified_path"),
            {(item["path"], item["rule"]) for item in state["violations"]},
        )

    def test_repository_profile_and_changeset_reject_symbolic_links(self) -> None:
        profile_path = repository_profile_path(self.root)
        target = profile_path.with_name("repository-profile.target.json")
        profile_path.rename(target)
        profile_path.symlink_to(target.name)

        profile_issues = repository_profile_issues(self.root)

        self.assertTrue(
            any("symbolic link" in issue.message for issue in profile_issues),
            [issue.message for issue in profile_issues],
        )
        profile_path.unlink()
        target.rename(profile_path)
        original_profile = self.profile()
        object_target = self.root / "real-objects"
        object_target.mkdir()
        object_link = self.root / "linked-objects"
        object_link.symlink_to(object_target.name, target_is_directory=True)
        linked_profile = self.profile()
        linked_profile["object_root"] = "linked-objects"
        self.write_profile(linked_profile)

        linked_issues = repository_profile_issues(self.root)
        self.assertTrue(
            any("symbolic-link component" in issue.message for issue in linked_issues),
            [issue.message for issue in linked_issues],
        )
        self.write_profile(original_profile)
        object_link.unlink()
        object_target.rmdir()
        paths = self.initialize()
        changeset_target = paths.formal / "changeset.target.json"
        changeset_target.write_text("{}\n", encoding="utf-8")
        changeset_path(paths).symlink_to(changeset_target.name)

        issues = changeset_issues(paths)
        self.assertTrue(
            any("symbolic link" in issue.message for issue in issues),
            [issue.message for issue in issues],
        )

    def test_changeset_requires_study_branch_and_safe_unique_allow_patterns(self) -> None:
        paths = self.initialize()

        with self.assertRaisesRegex(ValidationError, "branch matching"):
            create_changeset(paths, ["src/**"])
        self.assertFalse(changeset_path(paths).exists())

        self.create_study_branch("changeset-validation")
        for patterns, message in (
            ([], "at least one"),
            (["../src/**"], "stay inside"),
            (["src/**", "src/**"], "must not be repeated"),
        ):
            with self.subTest(patterns=patterns):
                with self.assertRaisesRegex(ValidationError, message):
                    create_changeset(paths, patterns)
                self.assertFalse(changeset_path(paths).exists())

    def test_source_and_test_changes_require_changeset(self) -> None:
        self.configure_host_roots()
        paths = self.initialize()
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        (self.root / "tests" / "test_solver.py").write_text(
            (
                "import unittest\n\n"
                "class SolverSmokeTest(unittest.TestCase):\n"
                "    def test_fixture(self):\n"
                "        self.assertEqual(2 + 2, 4)\n"
            ),
            encoding="utf-8",
        )

        state = evaluate_changes(paths)
        records = {item["path"]: item for item in state["changed_paths"]}
        rules = {(item["path"], item["rule"]) for item in state["violations"]}

        self.assertEqual(state["outcome"], "BLOCKED")
        self.assertEqual(records["src/solver.py"]["classification"], "source")
        self.assertEqual(records["tests/test_solver.py"]["classification"], "test")
        self.assertIn(("src/solver.py", "missing_changeset"), rules)
        self.assertIn(("tests/test_solver.py", "missing_changeset"), rules)

    def test_protected_path_blocks_even_when_changeset_allowlist_matches_everything(self) -> None:
        paths = self.initialize()
        self.create_study_branch("protected-precedence")
        create_changeset(paths, ["**"])
        protected = self.root / ".codex" / "config.toml"
        protected.parent.mkdir(parents=True)
        protected.write_text('model = "unauthorized"\n', encoding="utf-8")
        state = evaluate_changes(paths)
        protected_violations = [
            item for item in state["violations"] if item["path"] == ".codex/config.toml"
        ]

        self.assertEqual(state["outcome"], "BLOCKED")
        self.assertEqual(
            {item["rule"] for item in protected_violations},
            {"protected_path"},
        )
        self.assertFalse(
            any(item["rule"] == "outside_allowlist" for item in protected_violations)
        )

    def test_default_agent_governance_paths_are_not_study_outputs(self) -> None:
        paths = self.initialize()
        self.create_study_branch("protected-governance")
        create_changeset(paths, ["**"])
        targets = (
            self.root / "AGENTS.md",
            self.root / ".agents" / "skills" / "scientific-study" / "SKILL.md",
            self.root / "docs" / "scientific-agent-workflow.md",
        )
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("unauthorized Study governance change\n", encoding="utf-8")

        state = evaluate_changes(paths)
        rules = {(item["path"], item["rule"]) for item in state["violations"]}

        self.assertEqual(state["outcome"], "BLOCKED")
        for target in targets:
            relative = target.relative_to(self.root).as_posix()
            self.assertIn((relative, "protected_path"), rules)

    def test_actual_git_scope_reports_committed_staged_unstaged_and_unicode_untracked_paths(self) -> None:
        self.configure_host_roots()
        tracked = self.root / "src" / "baseline.py"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("VALUE = 1\n", encoding="utf-8")
        self.commit_all("add baseline source")
        paths = self.initialize()
        self.create_study_branch("git-states")
        create_changeset(paths, ["src/**", "tests/**"])
        committed = self.root / "src" / "committed.py"
        committed.write_text("VALUE = 2\n", encoding="utf-8")
        self.commit_all("commit Study source and contract")

        tracked.write_text("VALUE = 3\n", encoding="utf-8")
        staged = self.root / "tests" / "test_staged.py"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("import unittest\n", encoding="utf-8")
        self.assert_git_add("tests/test_staged.py")
        unicode_path = self.root / "src" / "算法.py"
        unicode_path.write_text("VALUE = 4\n", encoding="utf-8")

        state = evaluate_changes(paths, require_validation=False)
        records = {item["path"]: item for item in state["changed_paths"]}

        self.assertEqual(state["outcome"], "PASS")
        self.assertEqual(records["src/committed.py"]["states"], ["committed"])
        self.assertEqual(records["src/baseline.py"]["states"], ["unstaged"])
        self.assertEqual(records["tests/test_staged.py"]["states"], ["staged"])
        self.assertEqual(records["src/算法.py"]["states"], ["untracked"])
        self.assertTrue(records["src/committed.py"]["tracked"])
        self.assertTrue(records["src/baseline.py"]["tracked"])
        self.assertTrue(records["tests/test_staged.py"]["tracked"])
        self.assertFalse(records["src/算法.py"]["tracked"])
        self.assertEqual(
            [item["path"] for item in state["changed_paths"]],
            sorted(item["path"] for item in state["changed_paths"]),
        )
        self.assertFalse(change_state_evidence_eligible(state))

    def test_run_cannot_bypass_actual_source_diff_by_omitting_changed_paths(self) -> None:
        self.configure_host_roots()
        paths = self.initialize_approved_with_claim()
        (self.root / "src" / "unscoped.py").write_text("VALUE = 4\n", encoding="utf-8")
        marker = ".objects/process-started.txt"

        with self.assertRaisesRegex(ValidationError, "missing_changeset"):
            execute_run(
                paths,
                argv=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({marker!r}).write_text('ran')",
                ],
                purpose="must be blocked before process start",
                output_paths=[marker],
                changed_paths=[],
            )

        self.assertFalse((self.root / marker).exists())
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_run_rejects_output_outside_configured_object_root_before_execution(self) -> None:
        paths = self.initialize_approved_with_claim()
        outside = "results/value.txt"

        with self.assertRaisesRegex(ValidationError, "configured object_root"):
            execute_run(
                paths,
                argv=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({outside!r}).write_text('4')",
                ],
                purpose="invalid output boundary",
                output_paths=[outside],
            )

        self.assertFalse((self.root / outside).exists())
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_run_rejects_symbolic_link_components_below_object_root(self) -> None:
        paths = self.initialize_approved_with_claim()
        real = self.root / ".objects" / "real"
        real.mkdir()
        link = self.root / ".objects" / "linked"
        link.symlink_to(real.name, target_is_directory=True)

        with self.assertRaisesRegex(ValidationError, "symbolic-link component"):
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(4)"],
                purpose="reject aliased output directory",
                output_paths=[".objects/linked/value.txt"],
            )

        self.assertFalse((real / "value.txt").exists())
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_run_refuses_to_overwrite_and_seals_a_new_output(self) -> None:
        paths = self.initialize_approved_with_claim()
        output = ".objects/immutable.txt"
        output_path = self.root / output
        output_path.write_text("old\n", encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "new and immutable"):
            self.successful_run(paths, output=output)

        self.assertEqual(output_path.read_text(encoding="utf-8"), "old\n")
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])
        output_path.unlink()
        manifest = self.successful_run(paths, output=output)

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(output_path.read_text(encoding="utf-8"), "4\n")
        self.assertEqual(output_path.stat().st_mode & 0o777, 0o444)

    def test_committed_changeset_scope_is_hash_pinned_and_evidence_eligible(self) -> None:
        self.configure_host_roots()
        paths = self.initialize_approved_with_claim()
        self.create_study_branch("eligible-implementation")
        changeset = create_changeset(paths, ["src/**", "tests/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        (self.root / "tests" / "test_solver.py").write_text(
            (
                "import unittest\n\n"
                "class SolverSmokeTest(unittest.TestCase):\n"
                "    def test_fixture(self):\n"
                "        self.assertEqual(2 + 2, 4)\n"
            ),
            encoding="utf-8",
        )
        self.commit_all("record scoped implementation and Study state")

        proof = run_change_validation(paths)
        self.assertTrue(proof["passed"], proof)

        manifest = self.successful_run(paths)
        profile_record = manifest["change_scope"]["repository_profile"]
        changeset_record = manifest["change_scope"]["changeset"]
        validation_record = manifest["change_scope"]["validation"]

        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertEqual(manifest["change_scope"]["before"]["outcome"], "PASS")
        self.assertEqual(manifest["change_scope"]["after"]["outcome"], "PASS")
        governance = f"studies/{self.study_id}/runs/{manifest['run_id']}/governance"
        self.assertEqual(
            profile_record["path"],
            f"{governance}/repository-profile.json",
        )
        self.assertEqual(
            profile_record["sha256"],
            sha256_file(repository_profile_path(self.root)),
        )
        self.assertEqual(
            changeset_record["path"],
            f"{governance}/CHANGESET.json",
        )
        self.assertEqual(changeset_record["sha256"], sha256_file(changeset))
        self.assertEqual(
            validation_record["path"],
            f"{governance}/VALIDATION.json",
        )
        actual_paths = set(manifest["formalization"]["actual_changed_paths"])
        self.assertIn("src/solver.py", actual_paths)
        self.assertIn("tests/test_solver.py", actual_paths)
        self.assertEqual(manifest["formalization"]["declared_changed_paths"], [])

        # The Run carries immutable governance snapshots. Rotating the mutable
        # current proof after sealing must not erase the Run's provenance.
        (paths.formal / "VALIDATION.json").unlink()
        evidence = self.finalized_supporting_evidence(paths, [manifest])

        self.assertEqual(evidence["status"], "finalized")
        self.assertEqual(evidence["runs"][0]["run_id"], manifest["run_id"])

        validation_snapshot = self.root / str(validation_record["path"])
        validation_snapshot.chmod(0o644)
        validation_snapshot.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "validation snapshot.*(size|hash) mismatch"):
            create_evidence_draft(
                paths,
                "EVID-0002",
                ["CLAIM-0001"],
                [str(manifest["run_id"])],
            )

    def test_post_run_protected_write_is_sealed_but_rejected_by_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        command = (
            "from pathlib import Path; "
            "Path('.codex').mkdir(exist_ok=True); "
            "Path('.codex/config.toml').write_text('model = \\\"changed\\\"\\n')"
        )

        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", command],
            purpose="write a protected path during execution",
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        post_rules = {item["rule"] for item in manifest["change_scope"]["after"]["violations"]}

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["change_scope"]["before"]["outcome"], "PASS")
        self.assertEqual(manifest["change_scope"]["after"]["outcome"], "BLOCKED")
        self.assertIn("protected_path", post_rules)
        self.assertFalse(manifest["change_scope"]["evidence_eligible"])
        manifest_path = paths.runs / str(manifest["run_id"]) / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o444)

        with self.assertRaisesRegex(ValidationError, "blocked change scope"):
            self.populated_evidence_draft(paths, manifest)

    def test_non_git_run_is_exploratory_but_cannot_finalize_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        shutil.rmtree(self.root / ".git")

        manifest = self.successful_run(paths)

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["change_scope"]["before"]["outcome"], "ADVISORY")
        self.assertFalse(manifest["change_scope"]["before"]["git"]["available"])
        self.assertFalse(manifest["change_scope"]["evidence_eligible"])

        with self.assertRaisesRegex(ValidationError, "unverifiable or blocked change scope"):
            self.populated_evidence_draft(paths, manifest)

    def test_other_study_dirty_state_prevents_formal_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        other = self.initialize("SC-0002")
        other_note = other.active_work / "concurrent.md"
        other_note.write_text("uncommitted concurrent Study work\n", encoding="utf-8")

        manifest = self.successful_run(paths)

        self.assertEqual(manifest["status"], "succeeded")
        self.assertTrue(
            any(
                item["classification"] == "other_study"
                for item in manifest["change_scope"]["before"]["changed_paths"]
            )
        )
        self.assertFalse(manifest["change_scope"]["evidence_eligible"])
        with self.assertRaisesRegex(ValidationError, "blocked change scope"):
            self.populated_evidence_draft(paths, manifest)

    def test_absolute_output_path_is_rejected_before_execution(self) -> None:
        paths = self.initialize_approved_with_claim()
        output = (self.root / ".objects" / "absolute.txt").resolve()

        with self.assertRaisesRegex(ValidationError, "repository-relative"):
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(4)"],
                purpose="reject an absolute output path",
                output_paths=[str(output)],
            )

        self.assertFalse(output.exists())
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])


if __name__ == "__main__":
    unittest.main()
