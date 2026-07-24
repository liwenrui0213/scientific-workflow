from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import compaction_pressure
from tools.studyctl.evidence import (
    create_evidence_draft,
    finalize_evidence,
    recover_evidence_sequence,
)
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
        self.assertEqual(sequence["schema_version"], 3)
        self.assertEqual(sequence["study_id"], paths.study_id)
        self.assertEqual(sequence["high_water_mark"], 0)
        self.assertEqual(sequence["finalized_count"], 0)
        self.assertEqual(
            sequence["sequence_sha256"],
            record_digest(sequence, "sequence_sha256"),
        )
        self.assertEqual(evidence_sequence_issues(paths), [])

    def test_open_draft_is_writable_without_breaking_finalization_sequence(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )

        self.assertNotEqual(draft_path.stat().st_mode & 0o222, 0)
        self.assertEqual(evidence_sequence_issues(paths), [])

    def test_pre_clean_break_sequence_schema_is_rejected_explicitly(self) -> None:
        paths = self.initialize()
        sequence = load_evidence_sequence(paths)
        assert sequence is not None
        sequence["schema_version"] = 2
        sequence["sequence_sha256"] = record_digest(
            sequence, "sequence_sha256"
        )
        atomic_write_json(paths.evidence_sequence, sequence, mode=0o444)

        with self.assertRaisesRegex(ValidationError, "schema_version is unsupported"):
            load_evidence_sequence(paths)

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
        sequence = load_evidence_sequence(paths)
        assert sequence is not None

        paths.evidence_sequence.unlink()
        with self.assertRaisesRegex(ValidationError, "Evidence sequence is missing"):
            compaction_pressure(paths)
        self.assertTrue(evidence_sequence_issues(paths))

        write_evidence_sequence(paths, sequence, overwrite=False)
        corrupted = load_json(paths.evidence_sequence)
        corrupted["sequence_sha256"] = "0" * 64
        atomic_write_json(paths.evidence_sequence, corrupted, mode=0o444)
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

    def test_finalized_inventory_detects_deletion(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        sequence = load_evidence_sequence(paths)
        assert sequence is not None

        self.assertEqual(sequence["finalized_count"], 1)
        target = (
            paths.evidence
            / f"{evidence['evidence_id']}.v{evidence['version']:04d}.json"
        )
        target.unlink()

        messages = [issue.message for issue in evidence_sequence_issues(paths)]
        self.assertTrue(
            any("finalized Evidence" in message for message in messages),
            messages,
        )

    def test_interrupted_finalization_has_explicit_forward_recovery(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        draft = load_json(draft_path)
        draft["addresses"]["question"] = "Does the exact result equal four?"
        draft["runs"][0]["role"] = "supporting"
        draft["analysis"]["method"] = (
            "Compare the exact recorded result with four."
        )
        draft["result"] = {"value": 4}
        draft["scope"] = "The deterministic fixture."
        draft["uncertainty"] = "No sampling uncertainty."
        draft["limitations"] = ["No broader generalization is asserted."]
        self.fill_evidence_inference(draft)
        draft["assessment"] = "supports"
        atomic_write_json(draft_path, draft)

        with patch(
            "tools.studyctl.evidence.advance_finalized_evidence_sequence",
            side_effect=OSError("simulated sequence update interruption"),
        ):
            with self.assertRaisesRegex(OSError, "sequence update interruption"):
                finalize_evidence(paths, draft_path)

        self.assertEqual(load_json(draft_path)["status"], "finalized")
        self.assertTrue(evidence_sequence_issues(paths))
        recover_evidence_sequence(paths)
        self.assertEqual(evidence_sequence_issues(paths), [])
        self.assertEqual(load_evidence_sequence(paths)["finalized_count"], 1)

    def test_sequence_and_finalized_record_must_remain_sealed(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        evidence_path = (
            paths.evidence
            / f"{evidence['evidence_id']}.v{evidence['version']:04d}.json"
        )

        paths.evidence_sequence.chmod(0o644)
        sequence_messages = [
            issue.message for issue in evidence_sequence_issues(paths)
        ]
        self.assertTrue(
            any(
                "Evidence sequence must be sealed read-only" in message
                for message in sequence_messages
            ),
            sequence_messages,
        )
        paths.evidence_sequence.chmod(0o444)

        evidence_path.chmod(0o644)
        record_messages = [
            issue.message for issue in evidence_sequence_issues(paths)
        ]
        self.assertTrue(
            any(
                "finalized Evidence must be sealed read-only" in message
                for message in record_messages
            ),
            record_messages,
        )


if __name__ == "__main__":
    unittest.main()
