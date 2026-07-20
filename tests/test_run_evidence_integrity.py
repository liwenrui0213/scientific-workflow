from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import atomic_write_json, load_json, nested_record_digest
from tools.studyctl.models import ValidationError
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import errors_only, validate_study


class RunEvidenceIntegrityTests(WorkflowTestCase):
    def approved_with_claim(self, study_id: str) -> object:
        paths = self.initialize(study_id)
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        self.approve(paths)
        return paths

    def populated_draft(
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
        item["addresses"]["question"] = "Does the recorded computation equal four?"
        item["runs"][0]["role"] = "supporting"
        item["analysis"]["method"] = "Check the exact recorded output and provenance."
        item["result"] = {"value": 4, "comparison": "equal"}
        item["scope"] = "the deterministic fixture"
        item["uncertainty"] = "No sampling uncertainty."
        item["limitations"] = ["No broader scientific generalization is claimed."]
        item["assessment"] = "supports"
        atomic_write_json(draft, item)
        return draft

    def recorded_path(self, record: dict[str, object]) -> Path:
        path = Path(str(record["path"]))
        return path if path.is_absolute() else self.root / path

    def work_script(self, paths: object, name: str = "compute.py") -> Path:
        script = paths.active_work / name
        script.write_text("print(2 + 2)\n", encoding="utf-8")
        return script

    def test_v2_run_with_intact_dependencies_finalizes_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths, output=".objects/result.txt")

        finalized_path = finalize_evidence(paths, self.populated_draft(paths, manifest))
        finalized = load_json(finalized_path)

        self.assertEqual(manifest["schema_version"], 2)
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_declared_but_absent_output_is_evidence_ineligible(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(4)"],
            purpose="declare an output without producing it",
            output_paths=[".objects/missing.txt"],
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )

        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["outputs"][0]["present"], False)
        self.assertFalse(manifest["change_scope"]["evidence_eligible"])
        with self.assertRaisesRegex(ValidationError, "declared Run output was not produced"):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [str(manifest["run_id"])],
            )

    def test_evidence_finalization_rehashes_logs_and_outputs(self) -> None:
        paths = self.initialize_approved_with_claim()
        cases = (("stdout", "EVID-0001"), ("output", "EVID-0002"))
        for case, evidence_id in cases:
            with self.subTest(case=case):
                manifest = self.successful_run(
                    paths,
                    output=f".objects/{case}-result.txt",
                )
                draft = self.populated_draft(
                    paths,
                    manifest,
                    evidence_id=evidence_id,
                )
                record = (
                    manifest["logs"]["stdout"]
                    if case == "stdout"
                    else manifest["outputs"][0]
                )
                target = self.recorded_path(record)
                target.chmod(0o644)
                if case == "stdout":
                    target.write_text("5\n", encoding="utf-8")
                    expected = "stdout log hash mismatch"
                else:
                    target.write_text("tampered\n", encoding="utf-8")
                    expected = "Run output size mismatch"

                with self.assertRaisesRegex(ValidationError, expected):
                    finalize_evidence(paths, draft)

                self.assertEqual(load_json(draft)["status"], "draft")

    def test_evidence_finalization_rejects_missing_or_symlinked_dependency(self) -> None:
        paths = self.initialize_approved_with_claim()
        cases = (("missing stderr", "EVID-0001"), ("symlinked output", "EVID-0002"))
        for case, evidence_id in cases:
            with self.subTest(case=case):
                manifest = self.successful_run(
                    paths,
                    output=f".objects/{evidence_id}-result.txt",
                )
                draft = self.populated_draft(
                    paths,
                    manifest,
                    evidence_id=evidence_id,
                )
                if case == "missing stderr":
                    self.recorded_path(manifest["logs"]["stderr"]).unlink()
                    expected = "stderr log is unavailable"
                else:
                    output = self.recorded_path(manifest["outputs"][0])
                    replacement = output.with_name("replacement.txt")
                    replacement.write_text("4\n", encoding="utf-8")
                    output.unlink()
                    output.symlink_to(replacement.name)
                    expected = "Run output uses a symbolic-link path"

                with self.assertRaisesRegex(ValidationError, expected):
                    finalize_evidence(paths, draft)

                self.assertEqual(load_json(draft)["status"], "draft")

    def test_study_command_file_must_be_declared_as_input(self) -> None:
        paths = self.initialize_approved_with_claim()
        script = self.work_script(paths)
        relative = script.relative_to(self.root.resolve()).as_posix()

        with self.assertRaisesRegex(
            ValidationError,
            "command references mutable or uncommitted file.*not.*declared Run inputs",
        ):
            execute_run(
                paths,
                argv=[sys.executable, relative],
                purpose="execute undeclared Study work script",
                cohort_id="COHORT-001",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_embedded_work_file_reference_must_be_declared_as_input(self) -> None:
        paths = self.initialize_approved_with_claim()
        script = self.work_script(paths, "indirect.py")
        relative = script.relative_to(self.root.resolve()).as_posix()

        with self.assertRaisesRegex(ValidationError, "not.*declared Run inputs"):
            execute_run(
                paths,
                argv=[
                    sys.executable,
                    "-c",
                    f"exec(open({relative!r}, encoding='utf-8').read())",
                ],
                purpose="reject an undeclared embedded work dependency",
            )

        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_ignored_python_module_must_be_declared_as_input(self) -> None:
        paths = self.initialize_approved_with_claim()
        scratch = self.root / "scratch"
        scratch.mkdir()
        module = scratch / "compute.py"
        module.write_text("print(2 + 2)\n", encoding="utf-8")
        with (self.root / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("scratch/\n")
        self.commit_all("ignore local scratch modules")

        with self.assertRaisesRegex(ValidationError, "not.*declared Run inputs"):
            execute_run(
                paths,
                argv=[sys.executable, "-m", "scratch.compute"],
                purpose="reject an undeclared ignored Python module",
            )

        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_declared_study_input_must_remain_available_and_hash_stable(self) -> None:
        paths = self.initialize_approved_with_claim()
        cases = (
            ("modified", "EVID-0001"),
            ("missing", "EVID-0002"),
            ("symlink", "EVID-0003"),
        )
        for case, evidence_id in cases:
            with self.subTest(case=case):
                script = self.work_script(paths, f"{case}.py")
                relative = script.relative_to(self.root.resolve()).as_posix()
                manifest = execute_run(
                    paths,
                    argv=[sys.executable, relative],
                    purpose="execute declared Study work script",
                    input_paths=[relative],
                    cohort_id="COHORT-001",
                    hardware_class="test-cpu",
                    precision="exact-integer",
                )
                draft = self.populated_draft(
                    paths,
                    manifest,
                    evidence_id=evidence_id,
                )
                if case == "modified":
                    script.write_text("print(5)\n", encoding="utf-8")
                    expected = "Run input.*(size|hash) mismatch"
                elif case == "missing":
                    script.unlink()
                    expected = "Run input is unavailable"
                else:
                    replacement = script.with_name("replacement.py")
                    replacement.write_text("print(4)\n", encoding="utf-8")
                    script.unlink()
                    script.symlink_to(replacement.name)
                    expected = "Run input uses a symbolic-link path"

                with self.assertRaisesRegex(ValidationError, expected):
                    finalize_evidence(paths, draft)

                self.assertEqual(load_json(draft)["status"], "draft")

    def test_declared_absolute_input_outside_repository_is_hash_pinned(self) -> None:
        paths = self.initialize_approved_with_claim()
        with tempfile.TemporaryDirectory() as external_directory:
            external = Path(external_directory) / "input.txt"
            external.write_text("4\n", encoding="utf-8")
            manifest = execute_run(
                paths,
                argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text().strip())",
                    str(external),
                ],
                purpose="consume a declared external scientific input",
                input_paths=[str(external)],
                cohort_id="COHORT-001",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["inputs"][0]["path"], str(external.resolve()))
            self.assertFalse(manifest["inputs"][0]["changed_during_run"])
            self.assertTrue(manifest["change_scope"]["evidence_eligible"])

    def test_run_snapshots_formal_artifacts_across_later_method_revisions(self) -> None:
        paths = self.initialize_approved_with_claim()
        method = paths.formal / "METHOD.md"
        method.write_text("# Method\n\nUse exact integer addition.\n", encoding="utf-8")
        manifest = self.successful_run(paths)
        draft = self.populated_draft(paths, manifest)
        method.write_text("# Method\n\nUse an unrelated operation.\n", encoding="utf-8")

        finalized = finalize_evidence(paths, draft)
        self.assertEqual(load_json(finalized)["status"], "finalized")

        snapshot = self.recorded_path(manifest["formal_artifacts"][0])
        self.assertIn(
            f"runs/{manifest['run_id']}/formal-artifacts/",
            manifest["formal_artifacts"][0]["path"],
        )
        snapshot.chmod(0o644)
        snapshot.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "formal artifact.*(size|hash) mismatch"):
            create_evidence_draft(
                paths,
                "EVID-0002",
                ["CLAIM-0001"],
                [str(manifest["run_id"])],
            )

    def test_legacy_v1_run_validates_as_history_but_cannot_enter_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        manifest_path = paths.runs / str(manifest["run_id"]) / "manifest.json"
        legacy = copy.deepcopy(manifest)
        legacy["schema_version"] = 1
        legacy.pop("change_scope")
        legacy["formalization"].pop("declared_changed_paths")
        legacy["formalization"].pop("actual_changed_paths")
        legacy["integrity"]["manifest_sha256"] = nested_record_digest(
            legacy,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, legacy, mode=0o444)

        issues = validate_study(paths)
        self.assertEqual(errors_only(issues), [])
        self.assertTrue(
            any(
                issue.level == "WARNING"
                and "legacy V1 Run is historical and Evidence-ineligible" in issue.message
                for issue in issues
            ),
            [issue.render() for issue in issues],
        )
        with self.assertRaisesRegex(
            ValidationError,
            "legacy V1 Run is historical and Evidence-ineligible",
        ):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [str(manifest["run_id"])],
            )
        self.assertEqual(load_json(manifest_path), legacy)


if __name__ == "__main__":
    unittest.main()
