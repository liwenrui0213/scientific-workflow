from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.approval import begin_brief_revision
from tools.studyctl.budget import (
    parse_brief_hard_budget,
    replace_brief_hard_budget,
)
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.hashing import (
    atomic_write_json,
    file_record as real_file_record,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
)
from tools.studyctl.models import ValidationError, WorkflowError
from tools.studyctl.run_ledger import migrate_legacy_ledger
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import (
    errors_only,
    parse_brief_metadata,
    run_index,
    validate_study,
)


class HardBudgetAndRunLifecycleTests(WorkflowTestCase):
    def _approved_with_budget(
        self,
        *,
        gpu_hours: float | int | None,
        cpu_hours: float | int | None,
        storage_gb: float | int | None,
    ):
        paths = self.initialize()
        self.fill_brief(paths)
        self.set_hard_budget(
            paths,
            gpu_hours=gpu_hours,
            cpu_hours=cpu_hours,
            storage_gb=storage_gb,
        )
        self.add_proposed_claim(paths)
        self.approve(paths)
        return paths

    def _run(
        self,
        paths,
        *,
        code: str = "print(4)",
        gpu_hours: float = 0.0,
        cpu_hours: float = 0.0,
        storage_gb: float = 0.0,
        outputs: list[str] | None = None,
    ):
        return execute_run(
            paths,
            argv=[sys.executable, "-c", code],
            purpose="hard-budget and lifecycle fixture",
            cohort_id="COHORT-001",
            estimated_gpu_hours=gpu_hours,
            estimated_cpu_hours=cpu_hours,
            estimated_storage_gb=storage_gb,
            output_paths=outputs or [],
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def test_visible_budget_block_is_the_only_budget_authority(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        text = paths.brief.read_text(encoding="utf-8")

        self.assertEqual(
            parse_brief_hard_budget(text),
            {"gpu_hours": 0.0, "cpu_hours": 1.0, "storage_gb": 1.0},
        )
        self.assertNotIn("hard_budget", parse_brief_metadata(text))

        with self.assertRaisesRegex(ValidationError, "non-negative finite"):
            replace_brief_hard_budget(
                text,
                gpu_hours=-1,
                cpu_hours=1,
                storage_gb=1,
            )
        huge_budget = text.replace(
            '"gpu_hours": 0.0',
            '"gpu_hours": ' + "1" * 401,
            1,
        )
        paths.brief.write_text(huge_budget, encoding="utf-8")
        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("non-negative finite" in message for message in messages),
            messages,
        )

        paths.brief.write_text(text, encoding="utf-8")
        budget_block = text[
            text.index("<!-- STUDYCTL-HARD-BUDGET-BEGIN -->") :
            text.index("<!-- STUDYCTL-HARD-BUDGET-END -->")
            + len("<!-- STUDYCTL-HARD-BUDGET-END -->")
        ]
        paths.brief.write_text(text + "\n" + budget_block + "\n", encoding="utf-8")
        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("exactly one visible" in message for message in messages),
            messages,
        )

    def test_budget_json_rejects_ambiguous_or_nonfinite_values(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        original = paths.brief.read_text(encoding="utf-8")
        cases = {
            "boolean": '{"gpu_hours": true, "cpu_hours": 1, "storage_gb": 1}',
            "nonfinite": '{"gpu_hours": NaN, "cpu_hours": 1, "storage_gb": 1}',
            "duplicate": '{"gpu_hours": 0, "gpu_hours": 1, "cpu_hours": 1, "storage_gb": 1}',
            "missing": '{"gpu_hours": 0, "cpu_hours": 1}',
            "extra": '{"gpu_hours": 0, "cpu_hours": 1, "storage_gb": 1, "tokens": 1}',
            "negative": '{"gpu_hours": -1, "cpu_hours": 1, "storage_gb": 1}',
        }
        pattern = re.compile(
            r"(?s)(<!-- STUDYCTL-HARD-BUDGET-BEGIN -->\s*```json\s*)"
            r"\{.*?\}(\s*```\s*<!-- STUDYCTL-HARD-BUDGET-END -->)"
        )
        for label, payload in cases.items():
            with self.subTest(case=label):
                candidate = pattern.sub(r"\1" + payload + r"\2", original, count=1)
                paths.brief.write_text(candidate, encoding="utf-8")
                self.assertTrue(errors_only(validate_study(paths)))

    def test_approved_legacy_budget_migrates_to_unapproved_visible_draft(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        current = paths.brief.read_text(encoding="utf-8")
        metadata = parse_brief_metadata(current)
        metadata["hard_budget"] = {
            "gpu_hours": 0,
            "cpu_hours": None,
            "storage_gb": 1.25,
        }
        metadata_pattern = re.compile(
            r"(?s)<!--\s*STUDYCTL-METADATA-BEGIN\s*\{.*?\}\s*"
            r"STUDYCTL-METADATA-END\s*-->"
        )
        legacy_metadata = (
            "<!-- STUDYCTL-METADATA-BEGIN\n"
            + json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
            + "\nSTUDYCTL-METADATA-END -->"
        )
        resource_pattern = re.compile(
            r"(?s)^## Resource Budget\s*$.*?(?=^## Escalation Conditions\s*$)",
            re.MULTILINE,
        )
        legacy = resource_pattern.sub(
            "## Resource Budget\n\n"
            "Advisory prose said 99 GPU hours, but the approved hidden machine "
            "authority is the metadata object below.\n\n",
            current,
            count=1,
        )
        legacy = metadata_pattern.sub(legacy_metadata, legacy, count=1)
        paths.brief.write_text(legacy, encoding="utf-8")
        approval = {
            "schema_version": 1,
            "study_id": paths.study_id,
            "brief": {
                "path": paths.brief.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(paths.brief),
            },
            "protected_artifacts": {
                "evaluator": None,
                "dataset_split": None,
                "acceptance_criteria": None,
            },
            "approved_at": "2026-01-01T00:00:00Z",
            "reviewer": {
                "identity": "Legacy Human Reviewer",
                "source": "local_account",
            },
            "repository": {
                "available": True,
                "commit": None,
                "dirty": False,
                "status": [],
                "status_sha256": "0" * 64,
            },
            "approval_sha256": "",
        }
        approval["approval_sha256"] = record_digest(
            approval, "approval_sha256"
        )
        atomic_write_json(paths.brief_approval, approval, mode=0o444)
        old_brief = paths.brief.read_bytes()
        old_approval = paths.brief_approval.read_bytes()

        with self.assertRaisesRegex(ValidationError, "visible STUDYCTL-HARD-BUDGET"):
            self._run(paths)
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

        begin_brief_revision(paths)
        history = paths.study / "brief-history"
        self.assertEqual((history / "BRIEF.v0001.md").read_bytes(), old_brief)
        self.assertEqual(
            (history / "BRIEF.approval.v0001.json").read_bytes(), old_approval
        )
        self.assertFalse(paths.brief_approval.exists())
        migrated = paths.brief.read_text(encoding="utf-8")
        self.assertEqual(migrated.count("STUDYCTL-HARD-BUDGET-BEGIN"), 1)
        self.assertEqual(
            parse_brief_hard_budget(migrated),
            {"gpu_hours": 0.0, "cpu_hours": None, "storage_gb": 1.25},
        )
        self.assertNotIn("hard_budget", parse_brief_metadata(migrated))
        self.assertIn("Brief version: 2", migrated)
        self.assertIn("[REPLACE:", migrated)
        with self.assertRaisesRegex(ValidationError, "Brief approval"):
            self._run(paths)

    def test_zero_hard_budget_blocks_positive_request_before_process_start(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=0,
            cpu_hours=1,
            storage_gb=1,
        )

        real_popen = subprocess.Popen
        child_command = [sys.executable, "-c", "print(4)"]
        child_starts: list[list[str]] = []

        def observe_popen(*args, **kwargs):
            if args and args[0] == child_command:
                child_starts.append(args[0])
            return real_popen(*args, **kwargs)

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            side_effect=observe_popen,
        ):
            with self.assertRaisesRegex(
                ValidationError, "hard gpu hours budget exceeded"
            ):
                self._run(paths, gpu_hours=0.001)

        self.assertEqual(child_starts, [])
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_null_hard_budget_is_unset_authority_not_unlimited_compute(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=None,
            cpu_hours=1,
            storage_gb=1,
        )

        with self.assertRaisesRegex(
            ValidationError, "hard gpu hours budget exceeded"
        ):
            self._run(paths, gpu_hours=0.001)
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_exact_cumulative_limit_passes_and_next_request_is_blocked(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )

        first = self._run(paths, gpu_hours=0.4)
        second = self._run(paths, gpu_hours=0.6)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(second["budget"]["committed_after"]["gpu_hours"], 1.0)

        with self.assertRaisesRegex(
            ValidationError, "hard gpu hours budget exceeded"
        ):
            self._run(paths, gpu_hours=0.000001)
        self.assertEqual(len(list(paths.runs.glob("RUN-*"))), 2)

    def test_failed_run_still_consumes_its_reserved_budget(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )

        failed = self._run(paths, code="raise SystemExit(3)", gpu_hours=0.5)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["execution"]["exit_code"], 3)

        with self.assertRaisesRegex(
            ValidationError, "hard gpu hours budget exceeded"
        ):
            self._run(paths, gpu_hours=0.500001)

    def test_corrupted_terminal_budget_fails_closed_before_next_run(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        manifest = self._run(paths, gpu_hours=0.5)
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        corrupted = load_json(manifest_path)
        corrupted["budget"]["requested"]["gpu_hours"] = 0.0
        atomic_write_json(manifest_path, corrupted, overwrite=True, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError, "invalid terminal manifest digest"
        ):
            self._run(paths, gpu_hours=0.1)

    def test_concurrent_registrars_cannot_oversubscribe_remaining_budget(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        barrier = threading.Barrier(2)

        def attempt() -> str:
            barrier.wait(timeout=5)
            try:
                manifest = self._run(
                    paths,
                    code="import time; time.sleep(0.2); print(4)",
                    cpu_hours=0.75,
                )
            except ValidationError:
                return "blocked"
            return str(manifest["status"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = sorted(executor.map(lambda _: attempt(), range(2)))

        self.assertEqual(results, ["blocked", "succeeded"])
        self.assertEqual(len(list(paths.runs.glob("RUN-*"))), 1)

    def test_concurrent_registrars_cannot_claim_the_same_absent_output(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        output = ".objects/reserved-but-absent.bin"
        outer_check_complete = threading.Event()
        release_late_registrar = threading.Event()
        real_check = __import__(
            "tools.studyctl.run_registry", fromlist=["_require_new_output_paths"]
        )._require_new_output_paths
        late_thread_calls = 0
        late_result: dict[str, object] = {}

        def controlled_check(root, raw_paths):
            nonlocal late_thread_calls
            real_check(root, raw_paths)
            if threading.current_thread().name == "late-output-registrar":
                late_thread_calls += 1
                if late_thread_calls == 1:
                    outer_check_complete.set()
                    if not release_late_registrar.wait(timeout=10):
                        raise RuntimeError("timed out waiting for the first Run")

        def register_late() -> None:
            try:
                late_result["manifest"] = self._run(paths, outputs=[output])
            except BaseException as exc:  # captured for the test thread
                late_result["error"] = exc

        with patch(
            "tools.studyctl.run_registry._require_new_output_paths",
            side_effect=controlled_check,
        ):
            thread = threading.Thread(
                target=register_late,
                name="late-output-registrar",
            )
            thread.start()
            self.assertTrue(outer_check_complete.wait(timeout=10))
            first = self._run(paths, outputs=[output])
            self.assertEqual(first["run_id"], "RUN-000001")
            self.assertFalse(first["outputs"][0]["present"])
            release_late_registrar.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("manifest", late_result)
        error = late_result.get("error")
        self.assertIsInstance(error, ValidationError)
        self.assertIn("already claimed by RUN-000001", str(error))
        self.assertFalse((paths.runs / "RUN-000002").exists())

    def test_locked_registration_rechecks_physical_output_novelty(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        output = ".objects/appeared-after-preflight.bin"
        outer_check_complete = threading.Event()
        release_registrar = threading.Event()
        real_check = __import__(
            "tools.studyctl.run_registry", fromlist=["_require_new_output_paths"]
        )._require_new_output_paths
        registrar_calls = 0
        result: dict[str, object] = {}

        def controlled_check(root, raw_paths):
            nonlocal registrar_calls
            real_check(root, raw_paths)
            if threading.current_thread().name == "physical-race-registrar":
                registrar_calls += 1
                if registrar_calls == 1:
                    outer_check_complete.set()
                    if not release_registrar.wait(timeout=10):
                        raise RuntimeError("timed out waiting for the raced file")

        def register() -> None:
            try:
                result["manifest"] = self._run(paths, outputs=[output])
            except BaseException as exc:  # captured for the test thread
                result["error"] = exc

        with patch(
            "tools.studyctl.run_registry._require_new_output_paths",
            side_effect=controlled_check,
        ):
            thread = threading.Thread(
                target=register,
                name="physical-race-registrar",
            )
            thread.start()
            self.assertTrue(outer_check_complete.wait(timeout=10))
            (paths.root / output).write_bytes(b"unowned-race")
            release_registrar.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("manifest", result)
        error = result.get("error")
        self.assertIsInstance(error, ValidationError)
        self.assertIn("must be new and immutable", str(error))
        self.assertFalse((paths.runs / "RUN-000001").exists())

    def test_locked_registration_rechecks_output_parent_symlinks(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        output = ".objects/raced-parent/result.bin"
        outer_check_complete = threading.Event()
        release_registrar = threading.Event()
        module = __import__(
            "tools.studyctl.run_registry", fromlist=["_require_output_root"]
        )
        real_check = module._require_output_root
        registrar_calls = 0
        result: dict[str, object] = {}

        def controlled_check(root, object_root, raw_paths):
            nonlocal registrar_calls
            real_check(root, object_root, raw_paths)
            if threading.current_thread().name == "symlink-race-registrar":
                registrar_calls += 1
                if registrar_calls == 1:
                    outer_check_complete.set()
                    if not release_registrar.wait(timeout=10):
                        raise RuntimeError("timed out waiting for the raced symlink")

        def register() -> None:
            try:
                result["manifest"] = self._run(paths, outputs=[output])
            except BaseException as exc:  # captured for the test thread
                result["error"] = exc

        with patch(
            "tools.studyctl.run_registry._require_output_root",
            side_effect=controlled_check,
        ):
            thread = threading.Thread(
                target=register,
                name="symlink-race-registrar",
            )
            thread.start()
            self.assertTrue(outer_check_complete.wait(timeout=10))
            target = paths.root / ".objects/real-parent"
            target.mkdir()
            (paths.root / ".objects/raced-parent").symlink_to(
                target, target_is_directory=True
            )
            release_registrar.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("manifest", result)
        error = result.get("error")
        self.assertIsInstance(error, ValidationError)
        self.assertIn("symbolic-link component", str(error))
        self.assertFalse((paths.runs / "RUN-000001").exists())

    def test_output_ownership_normalizes_lexical_aliases(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        first = self._run(
            paths,
            outputs=[".objects/alias-parent/../normalized-claim.bin"],
        )
        self.assertEqual(first["outputs"][0]["path"], ".objects/normalized-claim.bin")

        with self.assertRaisesRegex(
            ValidationError, "already claimed by RUN-000001"
        ):
            self._run(paths, outputs=[".objects/normalized-claim.bin"])
        self.assertFalse((paths.runs / "RUN-000002").exists())

    def test_validation_rejects_forged_duplicate_output_ownership(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        first = self._run(paths, outputs=[".objects/first-claim.bin"])
        second = self._run(paths, outputs=[".objects/second-claim.bin"])
        second_path = paths.runs / second["run_id"] / "manifest.json"
        forged = load_json(second_path)
        forged["outputs"][0]["path"] = first["outputs"][0]["path"]
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged, "integrity", "manifest_sha256"
        )
        atomic_write_json(second_path, forged, overwrite=True, mode=0o444)

        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("claimed by multiple Runs" in message for message in messages),
            messages,
        )
        with self.assertRaisesRegex(ValidationError, "claimed by multiple Runs"):
            run_index(paths)

    def test_tracked_ledger_updates_do_not_look_like_scientific_code_changes(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        self.commit_all("track approved Study and genesis Run ledger")

        manifest = self._run(paths, cpu_hours=0.25)

        self.assertFalse(manifest["code_state"]["changed_during_run"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])

    def test_running_manifest_exists_before_child_process_starts(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        real_popen = subprocess.Popen
        observed_statuses: list[str] = []
        child_command = [sys.executable, "-c", "print(4)"]

        def observe_popen(*args, **kwargs):
            if args and args[0] == child_command:
                manifests = sorted(paths.runs.glob("RUN-*/manifest.json"))
                self.assertEqual(len(manifests), 1)
                observed_statuses.append(str(load_json(manifests[0])["status"]))
            return real_popen(*args, **kwargs)

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            side_effect=observe_popen,
        ):
            manifest = self._run(paths)

        self.assertEqual(observed_statuses, ["running"])
        self.assertEqual(manifest["status"], "succeeded")

    def test_registration_failure_never_publishes_an_orphan_run(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )

        real_popen = subprocess.Popen
        child_command = [sys.executable, "-c", "print(4)"]
        child_starts: list[list[str]] = []

        def observe_popen(*args, **kwargs):
            if args and args[0] == child_command:
                child_starts.append(args[0])
            return real_popen(*args, **kwargs)

        with patch(
            "tools.studyctl.run_registry._snapshot_formal_artifacts",
            side_effect=RuntimeError("injected snapshot failure"),
        ), patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            side_effect=observe_popen,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected snapshot failure"):
                self._run(paths, cpu_hours=0.25)

        self.assertEqual(child_starts, [])
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])
        self.assertEqual(list(paths.runs.glob(".*.registration.tmp")), [])
        self.assertEqual(
            [issue.message for issue in errors_only(validate_study(paths))], []
        )
        successor = self._run(paths, cpu_hours=0.25)
        self.assertEqual(successor["run_id"], "RUN-000002")

    def test_illegal_output_after_execution_is_sealed_incomplete(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        output = ".objects/illegal-link"
        code = (
            "from pathlib import Path; "
            f"Path({output!r}).symlink_to('missing-target')"
        )

        with self.assertRaisesRegex(ValidationError, "symbolic-link component"):
            self._run(paths, code=code, outputs=[output])

        manifest_paths = list(paths.runs.glob("RUN-*/manifest.json"))
        self.assertEqual(len(manifest_paths), 1)
        manifest = load_json(manifest_paths[0])
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["failure"]["phase"], "finalization")
        self.assertIsNotNone(manifest["integrity"]["manifest_sha256"])
        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertEqual(messages, [])
        with self.assertRaisesRegex(ValidationError, "only terminal Runs"):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [manifest["run_id"]],
            )

    def test_partial_safe_outputs_remain_charged_when_another_output_is_illegal(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1e-8,
        )
        valid = ".objects/four-bytes.bin"
        illegal = ".objects/illegal-sibling"
        code = (
            "from pathlib import Path; "
            f"Path({valid!r}).write_bytes(b'four'); "
            f"Path({illegal!r}).symlink_to('missing-target')"
        )

        with self.assertRaisesRegex(ValidationError, "symbolic-link component"):
            self._run(paths, code=code, outputs=[valid, illegal])
        manifest = load_json(next(paths.runs.glob("RUN-*/manifest.json")))
        self.assertEqual(manifest["status"], "incomplete")
        self.assertTrue(manifest["outputs"][0]["present"])
        self.assertEqual(manifest["outputs"][0]["size"], 4)
        self.assertFalse(manifest["outputs"][1]["present"])
        self.assertAlmostEqual(
            manifest["budget"]["actual_output_storage_gb"],
            4e-9,
            delta=1e-18,
        )
        valid_path = paths.root / valid
        self.assertEqual(valid_path.stat().st_mode & 0o222, 0)
        with self.assertRaisesRegex(
            ValidationError, "hard storage gb budget exceeded"
        ):
            self._run(paths, storage_gb=7e-9)

        os.chmod(valid_path, 0o644)
        valid_path.write_bytes(b"0123456789")
        with self.assertRaisesRegex(
            ValidationError,
            "retained Run output integrity blocks budget admission",
        ):
            self._run(paths)
        self.assertFalse((paths.runs / "RUN-000002").exists())

    def test_declared_absent_output_cannot_appear_after_run_sealing(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1e-8,
        )
        output = ".objects/late-output.bin"
        manifest = self._run(paths, outputs=[output])
        self.assertEqual(manifest["status"], "succeeded")
        self.assertFalse(manifest["outputs"][0]["present"])

        late_path = paths.root / output
        late_path.write_bytes(b"0123456789")
        with self.assertRaisesRegex(
            ValidationError,
            "retained Run output integrity blocks budget admission",
        ):
            self._run(paths, storage_gb=1e-9)
        self.assertFalse((paths.runs / "RUN-000002").exists())

    def test_unhashable_existing_output_is_sealed_and_blocks_successor(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1e-8,
        )
        output = ".objects/unhashable-output.bin"
        target = (paths.root / output).absolute()

        def fail_target_record(path, root):
            if Path(path).absolute() == target:
                raise WorkflowError("injected output hash failure")
            return real_file_record(path, root)

        with patch(
            "tools.studyctl.run_registry.file_record",
            side_effect=fail_target_record,
        ):
            with self.assertRaisesRegex(ValidationError, "cannot record Run output"):
                self._run(
                    paths,
                    code=(
                        "from pathlib import Path; "
                        f"Path({output!r}).write_bytes(b'four')"
                    ),
                    outputs=[output],
                )

        manifest = load_json(paths.runs / "RUN-000001" / "manifest.json")
        self.assertEqual(manifest["status"], "incomplete")
        self.assertFalse(manifest["outputs"][0]["present"])
        self.assertEqual(manifest["outputs"][0]["size"], 4)
        self.assertIsNone(manifest["outputs"][0]["sha256"])
        self.assertEqual(target.stat().st_mode & 0o222, 0)

        os.chmod(target, 0o644)
        target.write_bytes(b"0123456789")
        with self.assertRaisesRegex(
            ValidationError,
            "retained Run output integrity blocks budget admission",
        ):
            self._run(paths, storage_gb=1e-9)
        self.assertFalse((paths.runs / "RUN-000002").exists())

    def test_terminal_seal_failure_leaves_visible_running_record_and_blocks_output_run(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        output = ".objects/seal-failure.txt"
        with patch(
            "tools.studyctl.run_registry._seal_terminal_manifest",
            side_effect=RuntimeError("injected seal failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected seal failure"):
                self._run(
                    paths,
                    code=f"from pathlib import Path; Path({output!r}).write_text('x')",
                    outputs=[output],
                )
        manifest = load_json(next(paths.runs.glob("RUN-*/manifest.json")))
        self.assertEqual(manifest["status"], "running")
        self.assertIsNone(manifest["integrity"]["manifest_sha256"])
        with self.assertRaisesRegex(
            ValidationError, "unresolved output-producing running Run"
        ):
            self._run(paths)

    def test_registration_staging_debris_is_visible_to_validation(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        debris = paths.runs / ".RUN-000001.crash.registration.tmp"
        debris.mkdir()
        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertIn(
            "unfinished Run registration staging directory is present",
            messages,
        )

    def test_process_start_failure_is_sealed_as_failed_execution_fact(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        child_command = [sys.executable, "-c", "print(4)"]
        real_popen = subprocess.Popen

        def fail_only_fixture(*args, **kwargs):
            if args and args[0] == child_command:
                raise OSError("cannot exec fixture")
            return real_popen(*args, **kwargs)

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            side_effect=fail_only_fixture,
        ):
            manifest = self._run(paths)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["execution"]["exit_code"], 127)
        self.assertEqual(manifest["failure"]["phase"], "execution")
        self.assertTrue(
            (paths.runs / manifest["run_id"] / "manifest.json").is_file()
        )

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_background_descendant_cannot_mutate_output_after_run_returns(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        sentinel = ".objects/background-sentinel.txt"
        descendant = (
            "import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); "
            "time.sleep(0.6); "
            f"Path({sentinel!r}).write_text('late', encoding='utf-8')"
        )
        leader = (
            "import subprocess,sys; "
            f"child=subprocess.Popen([sys.executable, '-c', {descendant!r}], "
            "stdout=subprocess.PIPE, text=True); "
            "assert child.stdout.readline() == 'ready\\n'"
        )
        manifest = self._run(paths, code=leader, outputs=[sentinel])
        self.assertEqual(manifest["status"], "succeeded")
        time.sleep(0.8)
        self.assertFalse((paths.root / sentinel).exists())

    @unittest.skipUnless(os.name == "posix", "requires POSIX signal semantics")
    def test_cli_maps_signal_terminated_child_to_shell_exit_status(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        result = studyctl_main(
            [
                "--root",
                str(paths.root),
                "run",
                paths.study_id,
                "--purpose",
                "signal exit mapping fixture",
                "--",
                sys.executable,
                "-c",
                "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
            ]
        )
        self.assertEqual(result, 128 + signal.SIGTERM)

    def test_actual_storage_overrun_is_incomplete_and_blocks_future_runs(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1e-9,
        )
        output = ".objects/four.txt"
        code = (
            "from pathlib import Path; "
            f"Path({output!r}).write_bytes(b'four')"
        )

        manifest = self._run(paths, code=code, outputs=[output])
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["failure"]["type"], "HardBudgetExceeded")
        self.assertAlmostEqual(
            manifest["budget"]["actual_output_storage_gb"],
            4e-9,
            delta=1e-18,
        )
        self.assertAlmostEqual(
            manifest["budget"]["requested"]["storage_gb"],
            4e-9,
            delta=1e-18,
        )
        self.assertEqual(
            [issue.message for issue in errors_only(validate_study(paths))], []
        )

        with self.assertRaisesRegex(
            ValidationError, "hard storage gb budget exceeded"
        ):
            self._run(paths)

    def test_orphan_run_directory_is_reported_and_blocks_registry_reads(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        orphan = paths.runs / "RUN-000001"
        orphan.mkdir()

        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("missing a regular manifest.json" in message for message in messages),
            messages,
        )
        with self.assertRaisesRegex(ValidationError, "Run registry structure"):
            run_index(paths)

    def test_replacing_runs_directory_cannot_reset_budget_or_run_ids(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        manifest = self._run(paths, cpu_hours=0.75)
        ledger_path = paths.study / "RUNS.ledger.json"
        ledger_before = load_json(ledger_path)
        removed = paths.root / ".objects" / "removed-runs-fixture"
        shutil.move(paths.runs, removed)
        paths.runs.mkdir()

        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("references missing Run Manifest" in message for message in messages),
            messages,
        )
        with self.assertRaisesRegex(
            ValidationError, "references missing Run Manifest"
        ):
            self._run(paths, cpu_hours=0.25)
        self.assertFalse((paths.runs / "RUN-000001").exists())
        self.assertEqual(load_json(ledger_path), ledger_before)
        self.assertEqual(ledger_before["high_water_mark"], 1)
        self.assertAlmostEqual(
            ledger_before["runs"]["RUN-000001"]["commitment"]["cpu_hours"],
            0.75,
            delta=1e-15,
        )

        paths.runs.rmdir()
        shutil.move(removed, paths.runs)
        successor = self._run(paths, cpu_hours=0.25)
        self.assertEqual(successor["run_id"], "RUN-000002")

    def test_corrupt_run_ledger_blocks_new_registration(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        self._run(paths, cpu_hours=0.5)
        ledger_path = paths.study / "RUNS.ledger.json"
        ledger = load_json(ledger_path)
        ledger["ledger_sha256"] = "0" * 64
        atomic_write_json(ledger_path, ledger, overwrite=True, mode=0o444)

        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertIn("Run ledger digest is invalid", messages)
        with self.assertRaisesRegex(ValidationError, "Run ledger digest"):
            self._run(paths, cpu_hours=0.1)

    def test_nonfinite_projected_ledger_commitment_fails_closed(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        self._run(paths, cpu_hours=0.5)
        ledger_path = paths.study / "RUNS.ledger.json"
        ledger = load_json(ledger_path)
        ledger["runs"]["RUN-000001"]["commitment"]["cpu_hours"] = 10**1000
        ledger["ledger_sha256"] = record_digest(ledger, "ledger_sha256")
        atomic_write_json(ledger_path, ledger, overwrite=True, mode=0o444)

        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("non-negative finite number" in message for message in messages),
            messages,
        )
        with self.assertRaisesRegex(ValidationError, "non-negative finite number"):
            self._run(paths, cpu_hours=0.1)

    def test_ordinary_run_never_reconstructs_a_missing_ledger(self) -> None:
        paths = self._approved_with_budget(
            gpu_hours=1,
            cpu_hours=1,
            storage_gb=1,
        )
        (paths.study / "RUNS.ledger.json").unlink()

        with self.assertRaisesRegex(ValidationError, "Run ledger is missing"):
            self._run(paths, cpu_hours=0.1)
        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_legacy_ledger_migration_refuses_existing_state_and_id_gaps(self) -> None:
        paths = self.initialize()
        with self.assertRaisesRegex(ValidationError, "already exists"):
            migrate_legacy_ledger(paths, {})

        (paths.study / "RUNS.ledger.json").unlink()
        dummy = paths.root / "missing-legacy-manifest.json"
        with self.assertRaisesRegex(ValidationError, "Run-ID gap"):
            migrate_legacy_ledger(
                paths,
                {"RUN-000002": (dummy, {"schema_version": 2})},
            )
        self.assertFalse((paths.study / "RUNS.ledger.json").exists())


if __name__ == "__main__":
    unittest.main()
