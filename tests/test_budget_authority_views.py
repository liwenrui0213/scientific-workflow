from __future__ import annotations

import copy
from pathlib import Path
import shutil
import sys
import threading
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.approval import begin_brief_revision
from tools.studyctl.compaction import (
    current_evidence_hashes,
    finalize_compaction,
    prepare_compaction,
)
import tools.studyctl.compaction as compaction_module
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
)
from tools.studyctl.models import ValidationError
from tools.studyctl.rendering import render_status
from tools.studyctl.run_registry import execute_run


class BudgetAuthorityViewTests(WorkflowTestCase):
    def _run_with_cpu_charge(self, paths, cpu_hours: float = 0.75):
        return execute_run(
            paths,
            argv=[sys.executable, "-c", "print(4)"],
            purpose="budget-authority projection fixture",
            cohort_id="COHORT-001",
            estimated_cpu_hours=cpu_hours,
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def _move_run_registry_out_of_view(self, paths) -> Path:
        destination = paths.root / ".objects" / "removed-run-registry"
        shutil.move(paths.runs, destination)
        paths.runs.mkdir()
        return destination

    def _approve_revised_cpu_budget(self, paths, cpu_hours: float) -> None:
        begin_brief_revision(paths)
        text = paths.brief.read_text(encoding="utf-8")
        text = text.replace(
            "[REPLACE: Review and update every affected section for Brief version 2.]",
            "This version narrows the human-authorized lifetime CPU budget.",
        )
        paths.brief.write_text(text, encoding="utf-8")
        self.set_hard_budget(
            paths,
            gpu_hours=0,
            cpu_hours=cpu_hours,
            storage_gb=1,
        )
        self.approve(paths)

    def _write_compaction_plan(self, paths, prepared_path: Path) -> Path:
        prepared = load_json(prepared_path)
        claims = load_json(paths.claims)
        plan_path = paths.work / "compaction-plan.json"
        atomic_write_json(
            plan_path,
            {
                "schema_version": 1,
                "study_id": paths.study_id,
                "compaction_input_sha256": sha256_file(prepared_path),
                "claims_sha256": sha256_file(paths.claims),
                "evidence_sha256": current_evidence_hashes(paths),
                "archive_work_files": [],
                "decisive_evidence": [],
                "contradictory_evidence": [],
                "frontier": claims["frontier"],
                "open_questions": claims["frontier"]["open_questions"],
                "next_actions": claims["frontier"]["next_actions"],
                "representative_failures": [],
                "budget_state": prepared["budget_totals"],
            },
        )
        return plan_path

    def test_status_keeps_ledger_charge_when_manifest_registry_is_missing(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._run_with_cpu_charge(paths)
        self._move_run_registry_out_of_view(paths)

        status = render_status(paths).read_text(encoding="utf-8")

        self.assertIn("Charge authority: **durable Run ledger (authoritative)**", status)
        self.assertIn("Recorded estimated CPU hours: 0.75", status)
        self.assertIn("Recorded estimated GPU hours: 0", status)
        self.assertIn("**INVALID**", status)
        self.assertIn("Run ledger references missing Run Manifest(s): RUN-000001", status)
        self.assertIn("Authority validation: **INVALID**", status)

    def test_compaction_uses_ledger_and_refuses_manifest_ledger_mismatch(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._run_with_cpu_charge(paths)

        prepared_path = prepare_compaction(paths)
        prepared = load_json(prepared_path)
        self.assertEqual(
            prepared["budget_authority"]["kind"],
            "durable_run_ledger",
        )
        self.assertEqual(
            prepared["budget_authority"]["assurance"],
            "authoritative",
        )
        self.assertAlmostEqual(
            prepared["budget_totals"]["estimated_cpu_hours"],
            0.75,
            delta=1e-15,
        )

        prepared_path.unlink()
        self._move_run_registry_out_of_view(paths)

        with self.assertRaisesRegex(
            ValidationError,
            "Run ledger references missing Run Manifest",
        ):
            prepare_compaction(paths)
        self.assertFalse(prepared_path.exists())
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_revised_budget_excess_is_visible_in_status_and_compaction(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._run_with_cpu_charge(paths)
        self._approve_revised_cpu_budget(paths, 0.5)

        status = render_status(paths).read_text(encoding="utf-8")
        violation = (
            "hard cpu hours budget exceeded: committed 0.75 + requested 0 = "
            "0.75, limit 0.5"
        )
        self.assertIn("**INVALID", status)
        self.assertIn("Authority validation: **INVALID**", status)
        self.assertIn(violation, status)
        self.assertIn(
            "Resolve the hard-budget or Run-ledger authority error before further execution.",
            status,
        )

        prepared = load_json(prepare_compaction(paths))
        self.assertEqual(
            prepared["budget_totals"]["hard_limits"],
            {"gpu_hours": 0.0, "cpu_hours": 0.5, "storage_gb": 1.0},
        )
        self.assertEqual(
            prepared["budget_totals"]["existing_hard_budget_violations"],
            [
                {
                    "resource": "cpu_hours",
                    "committed": 0.75,
                    "requested": 0.0,
                    "projected": 0.75,
                    "limit": 0.5,
                }
            ],
        )

    def test_compaction_plan_cannot_cross_brief_budget_revision(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._run_with_cpu_charge(paths)
        prepared_path = prepare_compaction(paths)
        plan_path = self._write_compaction_plan(paths, prepared_path)

        self._approve_revised_cpu_budget(paths, 0.5)

        with self.assertRaisesRegex(
            ValidationError,
            "Brief changed after compact-prepare",
        ):
            finalize_compaction(paths, plan_path)
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_compaction_serializes_concurrent_brief_revision(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._run_with_cpu_charge(paths)
        prepared_path = prepare_compaction(paths)
        prepared = load_json(prepared_path)
        plan_path = self._write_compaction_plan(paths, prepared_path)
        binding_checked = threading.Event()
        release_finalize = threading.Event()
        revision_done = threading.Event()
        failures: list[BaseException] = []
        checkpoint_paths: list[Path] = []
        original_validate = compaction_module._validate_prepared_bindings

        def pause_after_binding_check(study_paths, state):
            original_validate(study_paths, state)
            binding_checked.set()
            if not release_finalize.wait(timeout=5):
                raise RuntimeError("test did not release compaction finalization")

        def finalize_worker() -> None:
            try:
                checkpoint_paths.append(finalize_compaction(paths, plan_path))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def revision_worker() -> None:
            try:
                begin_brief_revision(paths)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                revision_done.set()

        with patch(
            "tools.studyctl.compaction._validate_prepared_bindings",
            side_effect=pause_after_binding_check,
        ):
            finalize_thread = threading.Thread(target=finalize_worker)
            finalize_thread.start()
            self.assertTrue(binding_checked.wait(timeout=5))
            revision_thread = threading.Thread(target=revision_worker)
            revision_thread.start()
            self.assertFalse(revision_done.wait(timeout=0.2))
            release_finalize.set()
            finalize_thread.join(timeout=5)
            revision_thread.join(timeout=5)

        self.assertFalse(finalize_thread.is_alive())
        self.assertFalse(revision_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(checkpoint_paths), 1)
        checkpoint = load_json(checkpoint_paths[0])
        self.assertEqual(
            checkpoint["brief"]["sha256"],
            prepared["source_hashes"]["brief"],
        )
        self.assertNotEqual(
            sha256_file(paths.brief),
            checkpoint["brief"]["sha256"],
        )
        self.assertFalse(paths.brief_approval.exists())

    def test_stale_forward_ledger_uses_terminal_charge_but_blocks_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        output = ".objects/terminal-storage-charge.bin"
        manifest = execute_run(
            paths,
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "Path('.objects/terminal-storage-charge.bin').write_bytes(b'data')",
            ],
            purpose="terminal storage reconciliation fixture",
            cohort_id="COHORT-001",
            estimated_storage_gb=0.0,
            output_paths=[output],
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        self.assertEqual(manifest["status"], "succeeded")
        self.assertAlmostEqual(
            manifest["budget"]["requested"]["storage_gb"],
            4e-9,
            delta=1e-18,
        )

        ledger_path = paths.study / "RUNS.ledger.json"
        ledger = load_json(ledger_path)
        entry = ledger["runs"]["RUN-000001"]
        entry["status"] = "running"
        entry["commitment"]["storage_gb"] = 0.0
        entry["manifest_sha256"] = "0" * 64
        ledger["ledger_sha256"] = record_digest(ledger, "ledger_sha256")
        atomic_write_json(ledger_path, ledger, overwrite=True, mode=0o444)

        status = render_status(paths).read_text(encoding="utf-8")
        self.assertIn("Recorded charged storage (decimal GB): 4e-09", status)
        self.assertIn("Authority validation: **INVALID**", status)
        self.assertIn("stale relative to visible immutable Manifests", status)
        with self.assertRaisesRegex(
            ValidationError,
            "Run ledger is stale relative to visible immutable Manifests",
        ):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                ["RUN-000001"],
            )

    def test_legacy_manifest_budget_fallback_is_marked_in_checkpoint(self) -> None:
        paths = self.initialize_approved_with_claim()
        current = self._run_with_cpu_charge(paths, cpu_hours=0.25)
        manifest_path = paths.runs / str(current["run_id"]) / "manifest.json"
        legacy = copy.deepcopy(current)
        legacy["schema_version"] = 1
        legacy.pop("change_scope")
        legacy.pop("failure")
        legacy["execution"].pop("cwd_relative")
        legacy["brief"].pop("snapshot")
        legacy["brief"].pop("approval_snapshot")
        legacy["budget"] = {
            "estimated_gpu_hours": current["budget"]["estimated_gpu_hours"],
            "estimated_cpu_hours": current["budget"]["estimated_cpu_hours"],
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

        status = render_status(paths).read_text(encoding="utf-8")
        self.assertIn(
            "Charge authority: **legacy Manifest fallback "
            "(unindexed, lower assurance)**",
            status,
        )
        self.assertIn("Authority validation: **LEGACY FALLBACK**", status)
        self.assertIn("Recorded estimated CPU hours: 0.25", status)

        prepared_path = prepare_compaction(paths)
        prepared = load_json(prepared_path)
        self.assertEqual(
            prepared["budget_authority"],
            {
                "kind": "legacy_manifest_fallback",
                "assurance": "legacy_unindexed_lower_assurance",
                "manifest_sha256": {
                    "RUN-000001": sha256_file(manifest_path),
                },
            },
        )
        self.assertEqual(
            prepared["budget_totals"]["authority"],
            "legacy_manifest_fallback",
        )
        self.assertIn(
            "lower-assurance fallback",
            prepared["budget_totals"]["authority_warning"],
        )
        self.assertAlmostEqual(
            prepared["budget_totals"]["estimated_cpu_hours"],
            0.25,
            delta=1e-15,
        )

        plan_path = self._write_compaction_plan(paths, prepared_path)
        checkpoint = load_json(finalize_compaction(paths, plan_path))
        self.assertEqual(
            checkpoint["budget_state"]["authority"],
            "legacy_manifest_fallback",
        )
        self.assertIn(
            "lower-assurance fallback",
            checkpoint["budget_state"]["authority_warning"],
        )


if __name__ == "__main__":
    unittest.main()
