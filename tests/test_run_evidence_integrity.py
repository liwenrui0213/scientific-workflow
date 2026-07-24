from __future__ import annotations

import copy
from pathlib import Path
import platform
import sys
import tempfile
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.budget import manifest_budget_commitment
from tools.studyctl.git_state import git_state, git_tracked_state
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from tools.studyctl.models import ValidationError, utc_now
from tools.studyctl.run_registry import execute_run, migrate_legacy_run_ledger
from tools.studyctl.validation import (
    effective_run_epistemic_mode,
    errors_only,
    object_schema_issues,
    validate_study,
)
from tools.studyctl.workspace import evaluate_changes


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
        self.fill_evidence_inference(item)
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

    def test_v5_exploratory_run_with_intact_dependencies_finalizes_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths, output=".objects/result.txt")

        finalized_path = finalize_evidence(paths, self.populated_draft(paths, manifest))
        finalized = load_json(finalized_path)

        self.assertEqual(manifest["schema_version"], 5)
        self.assertEqual(
            manifest["epistemic_role"],
            {
                "mode": "exploratory",
                "confirmation_id": None,
                "confirmation_sha256": None,
                "slot_id": None,
            },
        )
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_frozen_v3_run_projects_only_as_exploratory(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        manifest_path = paths.runs / str(manifest["run_id"]) / "manifest.json"
        legacy = copy.deepcopy(manifest)
        legacy["schema_version"] = 3
        legacy.pop("epistemic_role")
        legacy.pop("control_binding")
        legacy.pop("intent_binding")
        legacy["integrity"]["manifest_sha256"] = nested_record_digest(
            legacy,
            "integrity",
            "manifest_sha256",
        )

        self.assertEqual(
            errors_only(
                object_schema_issues(paths.root, "run", manifest_path, legacy)
            ),
            [],
        )
        self.assertEqual(effective_run_epistemic_mode(legacy), "exploratory")

        # Model an intact pre-upgrade repository: the durable ledger binds the
        # exact historical V3 bytes, so every full-Study V5 integration branch
        # must accept the Run without manufacturing confirmatory provenance.
        atomic_write_json(manifest_path, legacy, overwrite=True, mode=0o444)
        ledger_path = paths.study / "RUNS.ledger.json"
        ledger = load_json(ledger_path)
        ledger["runs"][str(legacy["run_id"])]["manifest_sha256"] = sha256_file(
            manifest_path
        )
        ledger["ledger_sha256"] = record_digest(ledger, "ledger_sha256")
        atomic_write_json(ledger_path, ledger, overwrite=True, mode=0o444)
        self.assertEqual(errors_only(validate_study(paths)), [])

        forged = copy.deepcopy(legacy)
        forged["epistemic_role"] = {
            "mode": "confirmatory",
            "confirmation_id": "CONF-0001",
            "confirmation_sha256": "a" * 64,
            "slot_id": "SLOT-001",
        }
        messages = [
            issue.message
            for issue in errors_only(
                object_schema_issues(paths.root, "run", manifest_path, forged)
            )
        ]
        self.assertIn(
            "$: additional property is not allowed: 'epistemic_role'",
            messages,
        )
        self.assertEqual(effective_run_epistemic_mode(forged), "exploratory")

    def test_full_study_validation_replays_confirmation_integrity(self) -> None:
        paths = self.initialize_approved_with_claim()
        paths.confirmations.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.confirmations / "CONF-0001.json", {})

        messages = [issue.message for issue in errors_only(validate_study(paths))]

        self.assertTrue(
            any("Confirmation study_id does not match" in message for message in messages),
            messages,
        )

    def test_frozen_pre_budget_v2_run_remains_usable_and_charges_output_bytes(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.set_hard_budget(
            paths,
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1e-8,
        )
        self.add_proposed_claim(paths)
        self.approve(paths)
        # This is a genuinely frozen pre-ledger fixture: build every V2 field
        # and dependency directly instead of creating a current Run and
        # deleting newer fields.  That makes accidental V2 contract drift
        # observable to this test.
        (paths.study / "RUNS.ledger.json").unlink()
        run_id = "RUN-000001"
        run_directory = paths.runs / run_id
        governance = run_directory / "governance"
        governance.mkdir(parents=True)

        profile_source = self.root / "scientific-workflow/repository-profile.json"
        profile_snapshot = governance / "repository-profile.json"
        profile_snapshot.write_bytes(profile_source.read_bytes())
        profile_snapshot.chmod(0o444)

        stdout_path = run_directory / "stdout.log"
        stderr_path = run_directory / "stderr.log"
        stdout_path.write_bytes(b"4\n")
        stderr_path.write_bytes(b"")

        output_paths = [
            self.root / ".objects/v2-a.bin",
            self.root / ".objects/v2-b.bin",
        ]
        output_payloads = [b"1234", b"123456"]
        for output_path, payload in zip(output_paths, output_payloads, strict=True):
            output_path.write_bytes(payload)
            output_path.chmod(0o444)

        def file_record(path: Path) -> dict[str, object]:
            return {
                "path": path.resolve().relative_to(paths.root.resolve()).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }

        change_state = evaluate_changes(paths)
        self.assertEqual(change_state["outcome"], "PASS")
        self.assertTrue(change_state["git"]["available"])
        ledger_relative = (paths.study / "RUNS.ledger.json").relative_to(
            paths.root
        ).as_posix()
        tracked_state = git_tracked_state(
            self.root,
            exclude_paths=[ledger_relative],
        )
        cohort_fields = {
            "fixture": "frozen-pre-budget-v2",
            "hardware_class": "test-cpu",
            "precision": "exact-integer",
        }
        approval = load_json(paths.brief_approval)
        self.assertIsInstance(approval, dict)
        timestamp = utc_now()
        v2: dict[str, object] = {
            "schema_version": 2,
            "study_id": paths.study_id,
            "run_id": run_id,
            "purpose": "frozen pre-budget V2 compatibility fixture",
            "status": "succeeded",
            "execution": {
                "argv": [sys.executable, "-c", "print(4)"],
                "cwd": str(self.root.resolve()),
                "cwd_relative": ".",
                "started_at": timestamp,
                "ended_at": timestamp,
                "duration_seconds": 0.0,
                "exit_code": 0,
                "seed": None,
            },
            "git": git_state(self.root),
            "code_state": {
                "before": tracked_state,
                "after": copy.deepcopy(tracked_state),
                "changed_during_run": False,
            },
            "change_scope": {
                "repository_profile": file_record(profile_snapshot),
                "changeset": None,
                "validation": None,
                "before": change_state,
                "after": copy.deepcopy(change_state),
                "evidence_eligible": True,
            },
            "brief": {
                "path": paths.brief.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(paths.brief),
                "approval_sha256": approval["approval_sha256"],
            },
            "formal_artifacts": [],
            "formalization": {
                "changed_paths": [],
                "declared_changed_paths": [],
                "actual_changed_paths": [
                    item["path"] for item in change_state["changed_paths"]
                ],
                "scientific_critical": False,
                "shared_across_runs": False,
                "artifacts_unchanged_during_run": True,
                "outcome": "PASS",
                "requirements": [],
            },
            "cohort": {
                "cohort_id": "COHORT-001",
                "fields": cohort_fields,
                "fingerprint_sha256": sha256_json(cohort_fields),
            },
            "environment": {
                "python_executable": sys.executable,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "hardware_class": "test-cpu",
                "precision": "exact-integer",
                "environment_variables": {},
            },
            "budget": {
                "estimated_gpu_hours": 0.25,
                "estimated_cpu_hours": 0.5,
            },
            "inputs": [],
            "outputs": [
                {
                    **file_record(output_path),
                    "present": True,
                    "classification": "ordinary",
                    "pinned": False,
                }
                for output_path in output_paths
            ],
            "logs": {
                "stdout": file_record(stdout_path),
                "stderr": file_record(stderr_path),
            },
            "integrity": {
                "sealed_at": timestamp,
                "manifest_sha256": None,
            },
        }
        v2["integrity"]["manifest_sha256"] = nested_record_digest(
            v2, "integrity", "manifest_sha256"
        )
        manifest_path = run_directory / "manifest.json"
        atomic_write_json(manifest_path, v2, mode=0o444)
        frozen_bytes = manifest_path.read_bytes()

        legacy_issues = validate_study(paths)
        self.assertEqual(errors_only(legacy_issues), [])
        self.assertTrue(
            any(
                issue.level == "WARNING"
                and "legacy Run history has not yet been indexed" in issue.message
                for issue in legacy_issues
            ),
            [issue.render() for issue in legacy_issues],
        )
        commitment = manifest_budget_commitment(v2)
        self.assertAlmostEqual(commitment["gpu_hours"], 0.25, delta=1e-15)
        self.assertAlmostEqual(commitment["cpu_hours"], 0.5, delta=1e-15)
        self.assertAlmostEqual(commitment["storage_gb"], 1e-8, delta=1e-18)
        self.assertEqual(sum(record["size"] for record in v2["outputs"]), 10)
        with self.assertRaisesRegex(ValidationError, "Run ledger is missing"):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [run_id],
            )
        self.assertEqual(manifest_path.read_bytes(), frozen_bytes)

        migrated_ledger = migrate_legacy_run_ledger(paths)
        self.assertEqual(
            migrated_ledger,
            paths.study / "RUNS.ledger.json",
        )
        self.assertTrue(migrated_ledger.is_file())
        self.assertEqual(errors_only(validate_study(paths)), [])
        finalized = finalize_evidence(paths, self.populated_draft(paths, v2))
        self.assertEqual(load_json(finalized)["status"], "finalized")
        self.assertEqual(errors_only(validate_study(paths)), [])
        self.assertEqual(manifest_path.read_bytes(), frozen_bytes)

        with self.assertRaisesRegex(
            ValidationError, "hard storage gb budget exceeded"
        ):
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(4)"],
                purpose="prove frozen V2 storage remains charged",
                estimated_storage_gb=1e-12,
            )
        self.assertEqual(manifest_path.read_bytes(), frozen_bytes)

    def test_resealed_v2_missing_a_v2_required_field_is_rejected(self) -> None:
        paths = self.initialize_approved_with_claim()
        current = self.successful_run(paths)
        manifest_path = paths.runs / str(current["run_id"]) / "manifest.json"
        malformed = copy.deepcopy(current)
        malformed["schema_version"] = 2
        malformed.pop("epistemic_role")
        malformed.pop("control_binding")
        malformed.pop("intent_binding")
        malformed["brief"].pop("snapshot")
        malformed["brief"].pop("approval_snapshot")
        malformed["budget"] = {"estimated_cpu_hours": 0.0}
        malformed.pop("failure")
        malformed["integrity"]["manifest_sha256"] = nested_record_digest(
            malformed, "integrity", "manifest_sha256"
        )
        atomic_write_json(manifest_path, malformed, mode=0o444)

        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("estimated_gpu_hours" in message for message in messages),
            messages,
        )

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
        legacy.pop("epistemic_role")
        legacy.pop("control_binding")
        legacy.pop("intent_binding")
        legacy.pop("execution_boundary")
        legacy.pop("change_scope")
        legacy.pop("failure")
        legacy["execution"].pop("cwd_relative")
        legacy["brief"].pop("snapshot")
        legacy["brief"].pop("approval_snapshot")
        legacy["budget"] = {
            "estimated_gpu_hours": manifest["budget"]["estimated_gpu_hours"],
            "estimated_cpu_hours": manifest["budget"]["estimated_cpu_hours"],
        }
        legacy["formalization"].pop("declared_changed_paths")
        legacy["formalization"].pop("actual_changed_paths")
        legacy["formalization"].pop("artifacts_unchanged_during_run")
        legacy["integrity"]["manifest_sha256"] = nested_record_digest(
            legacy,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, legacy, mode=0o444)
        (paths.study / "RUNS.ledger.json").unlink()

        issues = validate_study(paths)
        self.assertEqual(errors_only(issues), [])
        self.assertTrue(
            all(issue.level == "WARNING" for issue in issues),
            [issue.render() for issue in issues],
        )
        self.assertTrue(
            any(
                issue.level == "WARNING"
                and "legacy V1 Run is historical and Evidence-ineligible" in issue.message
                for issue in issues
            ),
            [issue.render() for issue in issues],
        )
        self.assertTrue(
            any(
                issue.level == "WARNING"
                and "legacy Run history has not yet been indexed" in issue.message
                for issue in issues
            ),
            [issue.render() for issue in issues],
        )
        migrate_legacy_run_ledger(paths)
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
