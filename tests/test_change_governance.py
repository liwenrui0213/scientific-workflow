from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from tests.helpers import WorkflowTestCase, completed_process
from tools.studyctl.hashing import atomic_write_json, load_json, record_digest, sha256_file
from tools.studyctl.models import ValidationError, study_paths
from tools.studyctl.rendering import active_formal_artifacts
from tools.studyctl.review import create_review_packet
from tools.studyctl.workspace import (
    changeset_path,
    create_changeset,
    evaluate_changes,
    repository_profile_issues,
    repository_profile_path,
    renew_changeset,
    run_change_validation,
)


class ChangeGovernanceTests(WorkflowTestCase):
    def profile(self) -> dict[str, object]:
        value = load_json(repository_profile_path(self.root))
        self.assertIsInstance(value, dict)
        return value

    def configure_host(
        self,
        *,
        command: list[str] | None = None,
    ) -> None:
        profile = self.profile()
        profile["source_roots"] = ["src"]
        profile["test_roots"] = ["tests"]
        profile["experiment_roots"] = ["experiments"]
        profile["scientific_critical_patterns"] = ["src/**"]
        profile["commands"] = {
            "host_validation": command
            or [sys.executable, "-c", "assert 2 + 2 == 4"]
        }
        atomic_write_json(repository_profile_path(self.root), profile)
        for relative in ("src", "tests", "experiments"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        self.commit_all("configure host repository")

    def create_study_branch(self, slug: str) -> None:
        result = completed_process(
            ["git", "switch", "-c", f"study/{self.study_id}/{slug}"],
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def rewrite_changeset(self, paths: object, **updates: object) -> dict[str, object]:
        path = changeset_path(paths)
        record = load_json(path)
        self.assertIsInstance(record, dict)
        record.update(updates)
        record["record_sha256"] = record_digest(record, "record_sha256")
        atomic_write_json(path, record)
        return record

    def test_component_globs_do_not_let_single_star_cross_directories(self) -> None:
        self.configure_host()
        paths = self.initialize()
        self.create_study_branch("component-glob")
        create_changeset(paths, ["src/*.py"])
        nested = self.root / "src" / "nested" / "solver.py"
        nested.parent.mkdir()
        nested.write_text("VALUE = 4\n", encoding="utf-8")

        state = evaluate_changes(paths, require_validation=False)

        self.assertEqual(state["outcome"], "BLOCKED")
        self.assertIn(
            ("src/nested/solver.py", "outside_allowlist"),
            {(item["path"], item["rule"]) for item in state["violations"]},
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX filename semantics")
    def test_git_paths_preserve_whitespace_and_backslash_characters(self) -> None:
        self.configure_host()
        paths = self.initialize()
        self.create_study_branch("literal-paths")
        create_changeset(paths, ["src/**"])
        names = (" leading.py", "trailing.py ", "back\\slash.py")
        for name in names:
            (self.root / "src" / name).write_text("VALUE = 4\n", encoding="utf-8")

        state = evaluate_changes(paths, require_validation=False)
        actual = {item["path"] for item in state["changed_paths"]}

        for name in names:
            self.assertIn(f"src/{name}", actual)

    def test_handwritten_changeset_on_base_branch_is_rejected(self) -> None:
        self.configure_host()
        paths = self.initialize()
        head = completed_process(["git", "rev-parse", "HEAD"], self.root).stdout.strip()
        path = changeset_path(paths)
        record: dict[str, object] = {
            "schema_version": 1,
            "study_id": self.study_id,
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
            "base_ref": "main",
            "base_commit": head,
            "branch": "main",
            "allowed_write_patterns": ["src/**", f"studies/{self.study_id}/**"],
            "required_validation": [
                {
                    "name": "host_validation",
                    "argv": [sys.executable, "-c", "assert 2 + 2 == 4"],
                }
            ],
            "supersedes_sha256": None,
        }
        record["record_sha256"] = record_digest(record, "record_sha256")
        atomic_write_json(path, record)

        state = evaluate_changes(paths, require_validation=False)

        self.assertEqual(state["outcome"], "BLOCKED")
        self.assertIn(
            "branch_template_mismatch",
            {item["rule"] for item in state["violations"]},
        )

    def test_fixed_base_anchor_and_active_status_are_revalidated(self) -> None:
        self.configure_host()
        paths = self.initialize()
        self.create_study_branch("anchor")
        create_changeset(paths, ["src/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add Study implementation")
        head = completed_process(["git", "rev-parse", "HEAD"], self.root).stdout.strip()

        self.rewrite_changeset(paths, base_commit=head)
        forged = evaluate_changes(paths, require_validation=False)
        self.assertIn(
            "base_anchor_mismatch",
            {item["rule"] for item in forged["violations"]},
        )

        original_base = completed_process(
            ["git", "merge-base", "main", "HEAD"], self.root
        ).stdout.strip()
        self.rewrite_changeset(paths, base_commit=original_base, status="finalized")
        finalized = evaluate_changes(paths, require_validation=False)
        self.assertIn(
            "inactive_changeset",
            {item["rule"] for item in finalized["violations"]},
        )

    def test_changeset_renew_archives_stale_contract_and_reanchors_scope(self) -> None:
        self.configure_host()
        paths = self.initialize()
        self.create_study_branch("renew")
        create_changeset(paths, ["src/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add Study implementation")
        head = completed_process(["git", "rev-parse", "HEAD"], self.root).stdout.strip()
        forged = self.rewrite_changeset(paths, base_commit=head)
        stale_hash = sha256_file(changeset_path(paths))

        renewed_path = renew_changeset(paths)
        renewed = load_json(renewed_path)

        self.assertEqual(renewed["supersedes_sha256"], stale_hash)
        archived = paths.formal / "changeset-history" / f"CHANGESET.{stale_hash}.json"
        self.assertEqual(load_json(archived), forged)
        active_paths = {item["path"] for item in active_formal_artifacts(paths)}
        self.assertNotIn(
            archived.resolve().relative_to(self.root.resolve()).as_posix(),
            active_paths,
        )
        state = evaluate_changes(paths, require_validation=False)
        self.assertNotIn(
            "base_anchor_mismatch",
            {item["rule"] for item in state["violations"]},
        )

    def test_linked_worktree_policy_is_enforced_separately_from_branch(self) -> None:
        self.configure_host()
        profile = self.profile()
        profile["git"]["require_linked_worktree"] = True
        atomic_write_json(repository_profile_path(self.root), profile)
        self.commit_all("require linked worktree")
        paths = self.initialize()
        self.create_study_branch("primary-worktree")

        with self.assertRaisesRegex(ValidationError, "linked Git worktree"):
            create_changeset(paths, ["src/**"])

    def test_committed_study_intake_is_available_in_linked_worktree(self) -> None:
        self.configure_host()
        profile = self.profile()
        profile["git"]["require_linked_worktree"] = True
        atomic_write_json(repository_profile_path(self.root), profile)
        self.initialize_approved_with_claim()
        self.commit_all("commit approved Study intake")

        with tempfile.TemporaryDirectory() as parent:
            linked = Path(parent) / "SC-0001"
            result = completed_process(
                [
                    "git",
                    "worktree",
                    "add",
                    str(linked),
                    "-b",
                    "study/SC-0001/linked",
                    "HEAD",
                ],
                self.root,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            linked_paths = study_paths(linked, self.study_id)

            contract = create_changeset(linked_paths, ["src/**", "tests/**"])

            self.assertTrue(linked_paths.brief.is_file())
            self.assertTrue(linked_paths.brief_approval.is_file())
            self.assertTrue(contract.is_file())

    def test_validation_proof_is_required_and_stales_after_validated_tree_changes(self) -> None:
        self.configure_host()
        paths = self.initialize()
        self.create_study_branch("validation-proof")
        create_changeset(paths, ["src/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add implementation")

        before = evaluate_changes(paths)
        self.assertIn(
            "missing_validation_proof",
            {item["rule"] for item in before["violations"]},
        )
        proof = run_change_validation(paths)
        self.assertTrue(proof["passed"])
        self.assertEqual(evaluate_changes(paths)["outcome"], "PASS")

        (self.root / "src" / "followup.py").write_text("VALUE = 5\n", encoding="utf-8")
        self.commit_all("change implementation after validation")
        stale = evaluate_changes(paths)
        self.assertIn(
            "stale_validation_proof",
            {item["rule"] for item in stale["violations"]},
        )

    def test_study_only_commit_does_not_stale_host_validation_proof(self) -> None:
        self.configure_host()
        paths = self.initialize_approved_with_claim()
        self.create_study_branch("study-state-after-validation")
        create_changeset(paths, ["src/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add validated implementation")
        proof = run_change_validation(paths)
        self.assertTrue(proof["passed"], proof)

        claims = load_json(paths.claims)
        claims["revision"] += 1
        claims["frontier"]["summary"] = "Study-only state advanced after validation."
        atomic_write_json(paths.claims, claims)
        self.commit_all("advance Study state only")

        state = evaluate_changes(paths)
        self.assertEqual(state["outcome"], "PASS", state)
        self.assertNotIn(
            "stale_validation_proof",
            {item["rule"] for item in state["violations"]},
        )

    def test_failed_repository_validation_never_authorizes_evidence(self) -> None:
        self.configure_host(command=[sys.executable, "-c", "raise SystemExit(7)"])
        paths = self.initialize()
        self.create_study_branch("failed-validation")
        create_changeset(paths, ["src/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add unvalidated implementation")

        proof = run_change_validation(paths)
        state = evaluate_changes(paths)

        self.assertFalse(proof["passed"])
        self.assertIn(
            "stale_validation_proof",
            {item["rule"] for item in state["violations"]},
        )

    def test_validation_command_that_mutates_repository_cannot_pass(self) -> None:
        mutation = (
            "from pathlib import Path; "
            "Path('src/solver.py').write_text('VALUE = 5\\n', encoding='utf-8')"
        )
        self.configure_host(command=[sys.executable, "-c", mutation])
        paths = self.initialize()
        self.create_study_branch("mutating-validation")
        create_changeset(paths, ["src/**"])
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add implementation")

        proof = run_change_validation(paths)

        self.assertFalse(proof["repository_state_unchanged"])
        self.assertFalse(proof["passed"])
        self.assertIn(
            "stale_validation_proof",
            {item["rule"] for item in evaluate_changes(paths)["violations"]},
        )

    def test_validation_command_cannot_mutate_existing_untracked_governance_bytes(self) -> None:
        mutation = (
            "from pathlib import Path; "
            "Path('studies/SC-0001/formal/CHANGESET.json').write_text('{}\\n', encoding='utf-8')"
        )
        self.configure_host(command=[sys.executable, "-c", mutation])
        paths = self.initialize()
        self.create_study_branch("mutating-untracked-governance")
        (self.root / "src" / "solver.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_all("add implementation")
        # Create CHANGESET after the commit so it is an existing untracked
        # governance file whose Git status category will not change when the
        # validator mutates its bytes.
        create_changeset(paths, ["src/**"])

        proof = run_change_validation(paths)

        self.assertFalse(proof["repository_state_unchanged"])
        self.assertFalse(proof["passed"])

    def test_study_cannot_modify_workflow_enforcement_code(self) -> None:
        self.configure_host()
        paths = self.initialize()
        self.create_study_branch("enforcement")
        create_changeset(paths, ["**"])
        target = self.root / "tools" / "studyctl" / "gate.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ALLOW = True\n", encoding="utf-8")

        state = evaluate_changes(paths, require_validation=False)

        self.assertIn(
            ("tools/studyctl/gate.py", "protected_path"),
            {(item["path"], item["rule"]) for item in state["violations"]},
        )

    def test_nested_workflow_root_inside_larger_git_repository_is_rejected(self) -> None:
        nested = self.root / "nested-workflow"
        shutil.copytree(self.root / "scientific-workflow", nested / "scientific-workflow")

        issues = repository_profile_issues(nested)

        self.assertTrue(
            any("must equal the Git worktree root" in item.message for item in issues),
            [item.message for item in issues],
        )

    def test_review_packet_uses_profile_base_ref_by_default(self) -> None:
        paths = self.initialize_approved_with_claim()
        profile = self.profile()
        profile["git"]["base_ref"] = "HEAD"
        atomic_write_json(repository_profile_path(self.root), profile)
        self.commit_all("set repository-specific review base")

        packet = load_json(create_review_packet(paths))

        self.assertEqual(packet["git_diff_metadata"]["base_ref"], "HEAD")


if __name__ == "__main__":
    unittest.main()
