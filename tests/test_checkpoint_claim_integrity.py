from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.checkpoint_sequence import (
    load_checkpoint_sequence,
    write_checkpoint_sequence,
)
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.gc import garbage_collection_report
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from tools.studyctl.models import StudyPaths, ValidationError, utc_now
from tools.studyctl.validation import validate_study


class CheckpointClaimIntegrityTests(WorkflowTestCase):
    def write_compaction_plan(self, paths: StudyPaths, *, name: str) -> Path:
        compaction_input = prepare_compaction(paths)
        state = load_json(compaction_input)
        claims = load_json(paths.claims)
        plan = {
            "schema_version": 2,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": [],
            "decisive_evidence": [],
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "representative_failures": [],
            "budget_state": state["budget_totals"],
        }
        destination = paths.work / name
        atomic_write_json(destination, plan)
        return destination

    @staticmethod
    def error_messages(paths: StudyPaths) -> list[str]:
        return [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]

    def seal_retired_claim(
        self,
        paths: StudyPaths,
        *,
        statement: str = "The archived Claim records a terminal scientific result.",
    ) -> tuple[Path, dict[str, Any], Path]:
        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="active")
        claims = load_json(paths.claims)
        retired = claims["claims"][1]
        retired["statement"] = statement
        retired["lifecycle"] = "retired"
        retired["updated_at"] = utc_now()
        claims["frontier"]["claim_ids"] = ["CLAIM-0001"]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        plan = self.write_compaction_plan(paths, name="seal-retired-claim.json")
        checkpoint_path = finalize_compaction(paths, plan)
        checkpoint = load_json(checkpoint_path)
        ref = next(
            item
            for item in checkpoint["inactive_claim_refs"]
            if item["claim_id"] == "CLAIM-0002"
        )
        record_path = paths.root / ref["record_path"]
        self.assertTrue(record_path.is_file())
        self.assertEqual(record_path.stat().st_mode & 0o222, 0)
        return checkpoint_path, ref, record_path

    def rewrite_checkpoint_tail(
        self,
        paths: StudyPaths,
        checkpoint_path: Path,
        checkpoint: dict[str, Any],
    ) -> None:
        checkpoint["checkpoint_sha256"] = record_digest(
            checkpoint, "checkpoint_sha256"
        )
        atomic_write_json(checkpoint_path, checkpoint, mode=0o444)

        sequence = load_json(paths.checkpoint_sequence)
        self.assertEqual(
            sequence["latest_checkpoint"]["checkpoint_id"],
            checkpoint["checkpoint_id"],
        )
        sequence["latest_checkpoint"]["sha256"] = checkpoint[
            "checkpoint_sha256"
        ]
        write_checkpoint_sequence(paths, sequence)

    def test_checkpoint_sequence_uses_clean_break_schema_v2(self) -> None:
        paths = self.initialize()
        sequence = load_checkpoint_sequence(paths)
        assert sequence is not None
        self.assertEqual(sequence["schema_version"], 2)

        sequence["schema_version"] = 1
        sequence["sequence_sha256"] = record_digest(
            sequence, "sequence_sha256"
        )
        atomic_write_json(paths.checkpoint_sequence, sequence, mode=0o444)
        with self.assertRaisesRegex(ValidationError, "schema_version is unsupported"):
            load_checkpoint_sequence(paths)

    def test_deleted_checkpoint_tail_is_detected_and_id_is_not_reused(self) -> None:
        paths = self.initialize_approved_with_claim()
        first_path = finalize_compaction(
            paths,
            self.write_compaction_plan(paths, name="first-checkpoint.json"),
        )
        first_bytes = first_path.read_bytes()
        self.assertEqual(first_path.name, "CHECKPOINT-000001.json")

        checkpoint_selector = build_active_selector(paths)["latest_checkpoint"]
        self.assertIsNotNone(checkpoint_selector)
        self.assertEqual(checkpoint_selector["size"], first_path.stat().st_size)

        first_path.unlink()
        deletion_errors = self.error_messages(paths)
        self.assertTrue(
            any(
                "visible Checkpoints do not match the monotone sequence" in message
                for message in deletion_errors
            ),
            deletion_errors,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "visible Checkpoints do not match the monotone sequence",
        ):
            prepare_compaction(paths)

        first_path.write_bytes(first_bytes)
        os.chmod(first_path, 0o444)
        self.assertEqual(self.error_messages(paths), [])
        second_path = finalize_compaction(
            paths,
            self.write_compaction_plan(paths, name="second-checkpoint.json"),
        )
        self.assertEqual(second_path.name, "CHECKPOINT-000002.json")
        self.assertTrue(first_path.is_file())

    def test_missing_checkpoint_sequence_fails_closed_without_reconstruction(self) -> None:
        paths = self.initialize_approved_with_claim()
        finalize_compaction(
            paths,
            self.write_compaction_plan(paths, name="checkpoint-before-loss.json"),
        )
        paths.checkpoint_sequence.unlink()

        with self.assertRaisesRegex(ValidationError, "Checkpoint sequence is missing"):
            prepare_compaction(paths)
        self.assertTrue(
            any(
                "Checkpoint sequence is missing" in message
                for message in self.error_messages(paths)
            )
        )

    def test_terminal_claim_full_content_rewrite_is_rejected_after_seal(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.seal_retired_claim(paths)

        claims = load_json(paths.claims)
        retired = next(
            claim
            for claim in claims["claims"]
            if claim["claim_id"] == "CLAIM-0002"
        )
        retired["statement"] = "A rewritten terminal Claim with different content."
        retired["scope"] = "a silently broadened scope"
        retired["updated_at"] = utc_now()
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        errors = self.error_messages(paths)
        self.assertTrue(
            any(
                "terminal Claim CLAIM-0002 content changed after it was sealed"
                in message
                for message in errors
            ),
            errors,
        )

    def test_historical_supersession_chain_rejects_retired_successor(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="active")
        claims = load_json(paths.claims)
        first, second = claims["claims"]
        first["lifecycle"] = "superseded"
        first["superseded_by"] = "CLAIM-0002"
        first["updated_at"] = utc_now()
        claims["frontier"]["claim_ids"] = ["CLAIM-0002"]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        finalize_compaction(
            paths,
            self.write_compaction_plan(paths, name="seal-supersession.json"),
        )

        claims = load_json(paths.claims)
        second = next(
            claim
            for claim in claims["claims"]
            if claim["claim_id"] == "CLAIM-0002"
        )
        second["lifecycle"] = "retired"
        second["updated_at"] = utc_now()
        claims["claims"] = [second]
        claims["frontier"]["claim_ids"] = []
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        errors = self.error_messages(paths)
        self.assertTrue(
            any(
                "historical Claim supersession chain from CLAIM-0001 must end "
                "at an active Claim; CLAIM-0002 has lifecycle 'retired'"
                in message
                for message in errors
            ),
            errors,
        )

    def test_archived_claim_record_missing_schema_field_is_rejected(self) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint_path, ref, record_path = self.seal_retired_claim(paths)

        claims = load_json(paths.claims)
        claims["claims"] = [
            claim
            for claim in claims["claims"]
            if claim["claim_id"] != "CLAIM-0002"
        ]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        malformed = load_json(record_path)
        malformed.pop("statement")
        malformed_digest = sha256_json(malformed)
        malformed_path = record_path.with_name(
            f"{ref['claim_id']}.{malformed_digest}.json"
        )
        atomic_write_json(malformed_path, malformed, overwrite=False, mode=0o444)

        checkpoint = load_json(checkpoint_path)
        checkpoint_ref = next(
            item
            for item in checkpoint["inactive_claim_refs"]
            if item["claim_id"] == "CLAIM-0002"
        )
        checkpoint_ref["sha256"] = malformed_digest
        checkpoint_ref["record_path"] = malformed_path.relative_to(
            paths.root
        ).as_posix()
        self.rewrite_checkpoint_tail(paths, checkpoint_path, checkpoint)

        errors = self.error_messages(paths)
        self.assertTrue(
            any(
                "missing required property 'statement'" in message
                for message in errors
            ),
            errors,
        )

    def test_archived_claim_record_must_be_read_only_and_not_hard_linked(self) -> None:
        paths = self.initialize_approved_with_claim()
        _, _, record_path = self.seal_retired_claim(paths)

        os.chmod(record_path, 0o644)
        writable_errors = self.error_messages(paths)
        self.assertTrue(
            any("must be sealed read-only" in message for message in writable_errors),
            writable_errors,
        )

        os.chmod(record_path, 0o444)
        alias = record_path.with_name("hard-link-alias.json")
        os.link(record_path, alias)
        hard_link_errors = self.error_messages(paths)
        self.assertTrue(
            any("must not be hard-linked" in message for message in hard_link_errors),
            hard_link_errors,
        )

    def test_noncanonical_archived_claim_record_path_is_rejected(self) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint_path, _, record_path = self.seal_retired_claim(paths)
        checkpoint = load_json(checkpoint_path)
        checkpoint_ref = next(
            item
            for item in checkpoint["inactive_claim_refs"]
            if item["claim_id"] == "CLAIM-0002"
        )
        canonical = Path(checkpoint_ref["record_path"])
        checkpoint_ref["record_path"] = (
            canonical.parent.as_posix() + "/./" + record_path.name
        )
        self.rewrite_checkpoint_tail(paths, checkpoint_path, checkpoint)

        errors = self.error_messages(paths)
        self.assertTrue(
            any(
                "Checkpoint Claim record path is not the canonical "
                "content-addressed path"
                in message
                for message in errors
            ),
            errors,
        )

    def test_gc_reads_run_reference_from_pruned_archived_claim_record(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.commit_all("record approved Study before GC integrity Run")
        output = ".objects/archived-claim-reference.txt"
        manifest = self.successful_run(paths, output=output)

        before = garbage_collection_report(paths)
        self.assertEqual(
            [item["path"] for item in before["candidates"]],
            [output],
        )

        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="active")
        claims = load_json(paths.claims)
        archived = claims["claims"][1]
        archived["statement"] = (
            f"The archived provenance is recorded by {manifest['run_id']}."
        )
        archived["lifecycle"] = "retired"
        archived["updated_at"] = utc_now()
        claims["frontier"]["claim_ids"] = ["CLAIM-0001"]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        finalize_compaction(
            paths,
            self.write_compaction_plan(paths, name="seal-gc-reference.json"),
        )

        claims = load_json(paths.claims)
        claims["claims"] = [
            claim
            for claim in claims["claims"]
            if claim["claim_id"] != "CLAIM-0002"
        ]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        self.assertEqual(self.error_messages(paths), [])

        after = garbage_collection_report(paths)
        self.assertEqual(after["candidates"], [])
        retained = {item["path"]: item for item in after["retained"]}
        self.assertEqual(
            retained[output]["reason"],
            "Run is referenced by Observation, Evidence, Claim, or Verdict",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
