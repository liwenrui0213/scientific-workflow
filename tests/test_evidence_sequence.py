from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
from unittest.mock import patch
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import compaction_pressure
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.evidence import create_evidence_draft, migrate_evidence_sequence
from tools.studyctl.evidence_sequence import (
    empty_evidence_sequence,
    load_evidence_sequence,
    write_evidence_sequence,
)
from tools.studyctl.hashing import atomic_write_json, load_json, record_digest
from tools.studyctl.models import ValidationError
from tools.studyctl.validation import evidence_sequence_issues


class EvidenceSequenceTests(WorkflowTestCase):
    def test_init_creates_digest_bound_zero_high_water_mark(self) -> None:
        paths = self.initialize()

        sequence = load_evidence_sequence(paths)

        self.assertIsNotNone(sequence)
        assert sequence is not None
        self.assertEqual(sequence["study_id"], paths.study_id)
        self.assertEqual(sequence["high_water_mark"], 0)
        self.assertEqual(sequence["origin"]["kind"], "native")
        self.assertEqual(
            sequence["sequence_sha256"],
            record_digest(sequence, "sequence_sha256"),
        )
        self.assertEqual(evidence_sequence_issues(paths), [])

    def test_deletion_and_retry_cannot_reduce_pressure_high_water_mark(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        first = create_evidence_draft(
            paths, "EVID-0001", ["CLAIM-0001"], [manifest["run_id"]]
        )
        self.assertEqual(load_evidence_sequence(paths)["high_water_mark"], 1)
        first.unlink()

        pressure = compaction_pressure(paths)
        metric = next(
            item
            for item in pressure["metrics"]
            if item["name"] == "evidence_records_since_checkpoint"
        )
        self.assertEqual(metric["observed"], 1)

        retry = create_evidence_draft(
            paths, "EVID-0001", ["CLAIM-0001"], [manifest["run_id"]]
        )
        self.assertEqual(retry.name, "EVID-0001.v0001.json")
        self.assertEqual(load_evidence_sequence(paths)["high_water_mark"], 2)
        metric = next(
            item
            for item in compaction_pressure(paths)["metrics"]
            if item["name"] == "evidence_records_since_checkpoint"
        )
        self.assertEqual(metric["observed"], 2)

    def test_semantic_failure_does_not_reserve_but_publish_failure_burns(self) -> None:
        paths = self.initialize_approved_with_claim()
        with self.assertRaises(ValidationError):
            create_evidence_draft(
                paths, "EVID-0001", ["CLAIM-0001"], ["RUN-999999"]
            )
        self.assertEqual(load_evidence_sequence(paths)["high_water_mark"], 0)

        manifest = self.successful_run(paths)
        with patch(
            "tools.studyctl.evidence.atomic_write_json",
            side_effect=OSError("simulated crash after sequence fsync"),
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                create_evidence_draft(
                    paths,
                    "EVID-0001",
                    ["CLAIM-0001"],
                    [manifest["run_id"]],
                )
        self.assertEqual(load_evidence_sequence(paths)["high_water_mark"], 1)
        self.assertFalse((paths.evidence / "EVID-0001.v0001.json").exists())

        create_evidence_draft(
            paths, "EVID-0001", ["CLAIM-0001"], [manifest["run_id"]]
        )
        self.assertEqual(load_evidence_sequence(paths)["high_water_mark"], 2)

    def test_concurrent_creations_are_serialized_without_undercount(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)

        def create(number: int) -> str:
            return create_evidence_draft(
                paths,
                f"EVID-{number:04d}",
                ["CLAIM-0001"],
                [manifest["run_id"]],
            ).name

        with ThreadPoolExecutor(max_workers=4) as pool:
            names = list(pool.map(create, range(1, 5)))

        self.assertEqual(
            sorted(names),
            [f"EVID-{number:04d}.v0001.json" for number in range(1, 5)],
        )
        self.assertEqual(load_evidence_sequence(paths)["high_water_mark"], 4)
        self.assertEqual(len(list(paths.evidence.glob("EVID-*.v*.json"))), 4)

    def test_missing_corrupt_and_rolled_back_sequence_fail_closed(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        create_evidence_draft(
            paths, "EVID-0001", ["CLAIM-0001"], [manifest["run_id"]]
        )

        paths.evidence_sequence.unlink()
        with self.assertRaisesRegex(ValidationError, "Evidence sequence is missing"):
            compaction_pressure(paths)
        self.assertTrue(evidence_sequence_issues(paths))

        migrate_evidence_sequence(paths)
        corrupted = load_json(paths.evidence_sequence)
        corrupted["sequence_sha256"] = "0" * 64
        atomic_write_json(paths.evidence_sequence, corrupted)
        with self.assertRaisesRegex(ValidationError, "digest is invalid"):
            compaction_pressure(paths)

        write_evidence_sequence(paths, empty_evidence_sequence(paths))
        with self.assertRaisesRegex(ValidationError, "below the visible"):
            compaction_pressure(paths)
        messages = [issue.message for issue in evidence_sequence_issues(paths)]
        self.assertIn(
            "Evidence sequence high_water_mark is below the visible Evidence record count",
            messages,
        )

    def test_explicit_legacy_migration_records_assurance_gap_and_rejects_gap(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        draft = create_evidence_draft(
            paths, "EVID-0001", ["CLAIM-0001"], [manifest["run_id"]]
        )
        paths.evidence_sequence.unlink()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = studyctl_main(
                [
                    "--root",
                    str(self.root),
                    "migrate-evidence-sequence",
                    paths.study_id,
                ]
            )
        self.assertEqual(exit_code, 0, stderr.getvalue())
        migrated = load_evidence_sequence(paths)
        self.assertEqual(migrated["high_water_mark"], 1)
        self.assertEqual(migrated["origin"]["kind"], "legacy_migration")
        self.assertEqual(
            migrated["origin"]["pre_migration_deletion_assurance"],
            "unverifiable_before_sequence_initialization",
        )

        paths.evidence_sequence.unlink()
        item = load_json(draft)
        item["version"] = 2
        gap_path = paths.evidence / "EVID-0001.v0002.json"
        atomic_write_json(gap_path, item)
        draft.unlink()
        with self.assertRaisesRegex(ValidationError, "version gap"):
            migrate_evidence_sequence(paths)


if __name__ == "__main__":
    unittest.main()
