from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.helpers import TTYBuffer, WorkflowTestCase
from tools.studyctl.approval import record_verdict
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.git_state import git_state
from tools.studyctl.hashing import (
    atomic_write_json,
    file_record,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from tools.studyctl.models import (
    HumanGateError,
    StudyPaths,
    ValidationError,
    WorkflowError,
    errors_only,
    utc_now,
)
from tools.studyctl.review import (
    create_review_packet,
    current_review_scope,
    import_and_render_review,
)
from tools.studyctl.review_verdict_sequence import (
    migrate_legacy_review_verdict_sequence,
    recover_unindexed_review_verdict_authority,
)
from tools.studyctl.validation import validate_study


class VerdictReviewBindingTests(WorkflowTestCase):
    @staticmethod
    def _review_document(
        paths: StudyPaths, packet_path: Path
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "study_id": paths.study_id,
            "reviewed_at": "2026-07-24T00:00:00Z",
            "reviewer": {
                "identity": "Independent Test Reviewer",
                "source": "fresh read-only test session",
            },
            "review_packet_sha256": sha256_file(packet_path),
            "summary": "The independent review inspected the exact packet.",
            "requirement_coverage": [],
            "implementation_findings": [],
            "protected_condition_findings": [],
            "cohort_findings": [],
            "reproducibility_findings": [],
            "scientific_claim_findings": [],
            "contradictory_evidence_findings": [],
            "formalization_findings": [],
            "open_questions": [],
            "recommended_human_checks": [
                "Confirm the implementation-only Verdict scope."
            ],
        }

    @staticmethod
    def _verdict_responses(
        paths: StudyPaths,
        verdict_id: str,
        *,
        authorize_waiver: bool = False,
    ) -> str:
        responses = [
            "accepted",
            "The implementation satisfies the reviewed workflow invariants.",
            "requires_more_evidence",
            "The fixture does not establish a scientific conclusion.",
            "Only the deterministic workflow fixture is judged.",
        ]
        if authorize_waiver:
            responses.extend(
                [
                    "The implementation-only fixture has no scientific Review.",
                    f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
                ]
            )
        responses.extend(
            [
                f"RECORD VERDICT {paths.study_id} {verdict_id}",
                "",
            ]
        )
        return "\n".join(responses)

    @staticmethod
    def _legacy_verdict(
        paths: StudyPaths,
        verdict_id: str,
        *,
        schema_version: int,
    ) -> dict[str, object]:
        verdict: dict[str, object] = {
            "schema_version": schema_version,
            "study_id": paths.study_id,
            "verdict_id": verdict_id,
            "created_at": utc_now(),
            "reviewer": {
                "identity": "Legacy Independent Reviewer",
                "source": "legacy_test_fixture",
            },
            "judged_scope": {
                "commit": git_state(paths.root)["commit"],
                "brief_sha256": sha256_file(paths.brief),
                "checkpoint": None,
                "claims": [],
                "evidence": [],
            },
            "implementation_verdict": {
                "decision": "accepted",
                "rationale": "The legacy implementation was accepted.",
                "conditions": [],
            },
            "scientific_verdict": {
                "decision": "requires_more_evidence",
                "rationale": "The legacy fixture did not establish a scientific result.",
                "scope": "Only the legacy deterministic fixture is judged.",
                "conditions": [],
            },
            "confirmation": {
                "typed_text": f"RECORD VERDICT {paths.study_id} {verdict_id}",
                "confirmed_at": utc_now(),
            },
            "verdict_sha256": None,
        }
        if schema_version == 2:
            verdict["review_basis"] = {
                "mode": "waived",
                "reason": "Legacy v2 waiver without a separate authorization record.",
            }
        return verdict

    @staticmethod
    def _decision_input(
        *,
        agent_initiated: bool = False,
        review_waiver: dict[str, str] | None = None,
    ) -> dict[str, object]:
        decisions: dict[str, object] = {
            "input_version": 2 if agent_initiated else 1,
            "claim_ids": [],
            "implementation_verdict": {
                "decision": "accepted",
                "rationale": "The deterministic implementation is acceptable.",
            },
            "scientific_verdict": {
                "decision": "requires_more_evidence",
                "rationale": "The fixture does not establish a scientific conclusion.",
                "scope": "Only the deterministic workflow fixture is judged.",
            },
        }
        if agent_initiated:
            decisions["authorization"] = {
                "source": "explicit_user_instruction",
                "instruction": "Record the exact implementation-only Verdict decisions.",
            }
        if review_waiver is not None:
            decisions["review_waiver"] = review_waiver
        return decisions

    def _decision_file(
        self,
        name: str,
        decisions: dict[str, object],
    ) -> Path:
        path = self.root / ".objects" / name
        atomic_write_json(path, decisions)
        return path

    @staticmethod
    def _remove_sequence_for_legacy_migration(paths: StudyPaths) -> None:
        """Model an intact Study created before Review/Verdict sequencing."""

        if paths.review_verdict_sequence.exists():
            paths.review_verdict_sequence.unlink()

    def _commit_review_scope_change(self, message: str) -> None:
        marker = self.root / "scientific-workflow" / "review-scope-marker.txt"
        marker.write_text(message + "\n", encoding="utf-8")
        self.commit_all(message)

    def _finalize_checkpoint(self, paths: StudyPaths) -> dict[str, object]:
        compaction_input = prepare_compaction(paths)
        compaction_state = load_json(compaction_input)
        claims = load_json(paths.claims)
        plan = {
            "schema_version": 2,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": [],
            "decisive_evidence": list(
                claims["claims"][0].get("supporting_evidence", [])
            ),
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "representative_failures": [],
            "budget_state": compaction_state["budget_totals"],
        }
        plan_path = paths.work / "review-binding-compaction-plan.json"
        atomic_write_json(plan_path, plan)
        return load_json(finalize_compaction(paths, plan_path))

    def _import_review(self, paths: StudyPaths) -> dict[str, object]:
        packet_path = create_review_packet(paths)
        review = self._review_document(paths, packet_path)
        source = self.root / ".objects" / "independent-review.json"
        atomic_write_json(source, review)
        import_and_render_review(paths, source)
        return review

    def _record_reviewed_verdict(
        self,
    ) -> tuple[StudyPaths, dict[str, Any]]:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze state submitted for independent Review")
        review = self._import_review(paths)
        stdout = TTYBuffer()
        destination = record_verdict(
            paths,
            stdin=TTYBuffer(self._verdict_responses(paths, "VERDICT-0001")),
            stdout=stdout,
        )
        recorded = load_json(destination)
        self.assertEqual(recorded["schema_version"], 3)
        self.assertEqual(recorded["review_basis"]["mode"], "reviewed")
        self.assertIn(
            recorded["review_basis"]["review"]["sha256"],
            stdout.getvalue(),
        )
        self.assertIn(
            recorded["review_basis"]["review_packet"]["sha256"],
            stdout.getvalue(),
        )
        self.assertEqual(
            review["review_packet_sha256"],
            recorded["review_basis"]["review_packet"]["sha256"],
        )
        archived_packet = load_json(
            paths.root / recorded["review_basis"]["review_packet"]["path"]
        )
        self.assertEqual(
            recorded["judged_scope"]["active_context"],
            archived_packet["review_scope"]["active_context"],
        )
        return paths, recorded

    def test_verdict_v3_binds_immutable_review_and_packet_archives(self) -> None:
        paths, recorded = self._record_reviewed_verdict()
        basis = recorded["review_basis"]
        review_archive = paths.root / basis["review"]["path"]
        packet_archive = paths.root / basis["review_packet"]["path"]

        self.assertTrue(review_archive.is_file())
        self.assertTrue(packet_archive.is_file())
        self.assertEqual(sha256_file(review_archive), basis["review"]["sha256"])
        self.assertEqual(sha256_file(packet_archive), basis["review_packet"]["sha256"])
        self.assertEqual(review_archive.stat().st_size, basis["review"]["size"])
        self.assertEqual(packet_archive.stat().st_size, basis["review_packet"]["size"])
        self.assertEqual(review_archive.stat().st_mode & 0o222, 0)
        self.assertEqual(packet_archive.stat().st_mode & 0o222, 0)
        self.assertEqual(
            load_json(review_archive)["review_packet_sha256"],
            sha256_file(packet_archive),
        )
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_review_packet_scope_binds_complete_current_scientific_state(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        checkpoint = self._finalize_checkpoint(paths)
        self.commit_all("freeze complete Review scope")

        packet = load_json(create_review_packet(paths))
        scope = packet["review_scope"]
        active_claim = load_json(paths.claims)["claims"][0]

        self.assertEqual(scope["commit"], git_state(paths.root)["commit"])
        self.assertEqual(scope["brief_sha256"], sha256_file(paths.brief))
        self.assertEqual(
            scope["checkpoint"],
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "sha256": checkpoint["checkpoint_sha256"],
            },
        )
        self.assertEqual(
            scope["claims"],
            [
                {
                    "claim_id": active_claim["claim_id"],
                    "sha256": sha256_json(active_claim),
                }
            ],
        )
        self.assertEqual(
            scope["evidence"],
            [
                {
                    "evidence_id": evidence["evidence_id"],
                    "version": evidence["version"],
                    "sha256": evidence["record_sha256"],
                }
            ],
        )
        self.assertEqual(
            scope["evidence_inventory_sha256"],
            packet["evidence_inventory"]["inventory_sha256"],
        )
        self.assertEqual(scope["active_context"], packet["active_context"])
        self.assertEqual(
            scope["active_context"]["selector_sha256"],
            load_json(paths.generated / "ACTIVE_CONTEXT.json")["selector_sha256"],
        )

    def test_review_packet_base_ref_override_replays_at_import(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Review base-ref override fixture")
        packet_path = create_review_packet(paths, base_ref="HEAD")
        packet = load_json(packet_path)
        source = self.root / ".objects" / "base-ref-override-review.json"
        atomic_write_json(source, self._review_document(paths, packet_path))

        import_and_render_review(paths, source)

        self.assertEqual(packet["git_diff_metadata"]["base_ref"], "HEAD")
        self.assertTrue((paths.generated / "REVIEW.json").is_file())

    def test_validation_rejects_tampered_review_archive(self) -> None:
        paths, recorded = self._record_reviewed_verdict()
        review_archive = paths.root / recorded["review_basis"]["review"]["path"]
        review_archive.chmod(0o644)
        review_archive.write_text("{}\n", encoding="utf-8")

        messages = [issue.message for issue in errors_only(validate_study(paths))]

        self.assertTrue(
            any("Verdict Review basis is invalid" in message for message in messages),
            messages,
        )
        self.assertTrue(
            any("read-only" in message or "stale" in message for message in messages),
            messages,
        )

    def test_validation_rejects_deleted_review_packet_archive(self) -> None:
        paths, recorded = self._record_reviewed_verdict()
        packet_archive = (
            paths.root / recorded["review_basis"]["review_packet"]["path"]
        )
        packet_archive.unlink()

        messages = [issue.message for issue in errors_only(validate_study(paths))]

        self.assertTrue(
            any("Verdict Review basis is invalid" in message for message in messages),
            messages,
        )
        self.assertTrue(
            any("review-history" in message for message in messages),
            messages,
        )

    def test_sequence_detects_deletion_of_complete_review_verdict_chain(
        self,
    ) -> None:
        paths, recorded = self._record_reviewed_verdict()
        basis = recorded["review_basis"]
        review_archive = paths.root / basis["review"]["path"]
        packet_archive = paths.root / basis["review_packet"]["path"]

        paths.verdict.unlink()
        review_archive.unlink()
        packet_archive.unlink()

        messages = [issue.message for issue in errors_only(validate_study(paths))]

        self.assertTrue(
            any(
                "Review/Verdict authority" in message
                and "sequence" in message
                for message in messages
            ),
            messages,
        )

    def test_sequence_recovery_advances_one_unindexed_verdict(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze one-record Verdict recovery scope")
        decisions = self._decision_input(
            review_waiver={
                "reason": "No independent Review was commissioned.",
                "source": "interactive_human_confirmation",
                "authorization_text": f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
            }
        )
        source = self._decision_file("recovery-verdict-decisions.json", decisions)
        responses = TTYBuffer(
            "\n".join(
                (
                    f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
                    f"RECORD VERDICT {paths.study_id} VERDICT-0001",
                    "",
                )
            )
        )

        with (
            patch(
                "tools.studyctl.review_verdict_sequence.advance_review_verdict_sequence",
                side_effect=WorkflowError("simulated sequence publication crash"),
            ),
            self.assertRaisesRegex(
                WorkflowError,
                "simulated sequence publication crash",
            ),
        ):
            record_verdict(
                paths,
                source,
                stdin=responses,
                stdout=TTYBuffer(),
            )

        self.assertTrue(paths.verdict.is_file())
        self.assertTrue(
            any(
                "does not match the sequence count" in issue.message
                for issue in errors_only(validate_study(paths))
            )
        )

        recovered = recover_unindexed_review_verdict_authority(paths)

        self.assertEqual(recovered["high_water_mark"], 1)
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_sequence_recovery_advances_reviewed_verdict(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze reviewed Verdict recovery scope")
        self._import_review(paths)

        with (
            patch(
                "tools.studyctl.review_verdict_sequence.advance_review_verdict_sequence",
                side_effect=WorkflowError("simulated reviewed Verdict crash"),
            ),
            self.assertRaisesRegex(
                WorkflowError,
                "simulated reviewed Verdict crash",
            ),
        ):
            record_verdict(
                paths,
                stdin=TTYBuffer(
                    self._verdict_responses(paths, "VERDICT-0001")
                ),
                stdout=TTYBuffer(),
            )

        recovered = recover_unindexed_review_verdict_authority(paths)

        self.assertEqual(recovered["high_water_mark"], 2)
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_sequence_recovery_rejects_zero_or_two_unindexed_records(
        self,
    ) -> None:
        empty_paths = self.initialize("SC-0001")
        self.fill_brief(empty_paths)
        self.approve(empty_paths)
        self.commit_all("freeze empty Verdict recovery scope")

        with self.assertRaisesRegex(
            ValidationError,
            "requires exactly one unindexed record",
        ):
            recover_unindexed_review_verdict_authority(empty_paths)

        two_paths = self.initialize("SC-0002")
        self.fill_brief(two_paths)
        self.approve(two_paths)
        self.commit_all("freeze two-record Verdict recovery scope")
        for number, destination in (
            (1, two_paths.verdict),
            (2, two_paths.study / "VERDICT.v0002.json"),
        ):
            verdict = self._legacy_verdict(
                two_paths,
                f"VERDICT-{number:04d}",
                schema_version=1,
            )
            verdict["verdict_sha256"] = record_digest(
                verdict,
                "verdict_sha256",
            )
            atomic_write_json(destination, verdict, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError,
            "requires exactly one unindexed record",
        ):
            recover_unindexed_review_verdict_authority(two_paths)

    def test_sequence_recovery_and_migration_refuse_temp_residue(self) -> None:
        paths = self.initialize()
        temporary = (
            paths.study
            / ".REVIEW_VERDICTS.sequence.json.crash.tmp"
        )
        temporary.write_text("unfinished\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValidationError,
            "recovery refuses unfinished sequence temporary files",
        ):
            recover_unindexed_review_verdict_authority(paths)

        paths.review_verdict_sequence.unlink()
        with self.assertRaisesRegex(
            ValidationError,
            "migration refuses unfinished sequence temporary files",
        ):
            migrate_legacy_review_verdict_sequence(paths)

    def test_sequence_recovery_cannot_adopt_fresh_legacy_verdict(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze legacy Verdict recovery rejection scope")
        verdict = self._legacy_verdict(
            paths,
            "VERDICT-0001",
            schema_version=1,
        )
        verdict["verdict_sha256"] = record_digest(
            verdict,
            "verdict_sha256",
        )
        atomic_write_json(paths.verdict, verdict, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError,
            "cannot adopt a legacy Verdict",
        ):
            recover_unindexed_review_verdict_authority(paths)

    def test_sequence_recovery_cannot_index_malformed_current_verdict(
        self,
    ) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze malformed current Verdict recovery scope")
        malformed = {
            "schema_version": 3,
            "study_id": paths.study_id,
            "verdict_id": "VERDICT-0001",
            "verdict_sha256": None,
        }
        malformed["verdict_sha256"] = record_digest(
            malformed,
            "verdict_sha256",
        )
        atomic_write_json(paths.verdict, malformed, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError,
            "cannot adopt an invalid Verdict",
        ):
            recover_unindexed_review_verdict_authority(paths)

        sequence = load_json(paths.review_verdict_sequence)
        self.assertEqual(sequence["high_water_mark"], 0)

    def test_sequence_recovery_replays_all_publisher_invariants(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Verdict publisher-invariant recovery scope")
        decisions = self._decision_input(
            review_waiver={
                "reason": "No independent Review was commissioned.",
                "source": "interactive_human_confirmation",
                "authorization_text": f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
            }
        )
        source = self._decision_file(
            "publisher-invariant-recovery.json",
            decisions,
        )
        responses = TTYBuffer(
            "\n".join(
                (
                    f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
                    f"RECORD VERDICT {paths.study_id} VERDICT-0001",
                    "",
                )
            )
        )
        with (
            patch(
                "tools.studyctl.review_verdict_sequence.advance_review_verdict_sequence",
                side_effect=WorkflowError("simulated Verdict sequence crash"),
            ),
            self.assertRaisesRegex(
                WorkflowError,
                "simulated Verdict sequence crash",
            ),
        ):
            record_verdict(
                paths,
                source,
                stdin=responses,
                stdout=TTYBuffer(),
            )

        candidate = load_json(paths.verdict)
        valid_confirmed_at = candidate["confirmation"]["confirmed_at"]
        candidate["verdict_id"] = "VERDICT-9999"
        candidate["confirmation"]["typed_text"] = (
            f"RECORD VERDICT {paths.study_id} VERDICT-9999"
        )
        candidate["verdict_sha256"] = record_digest(
            candidate,
            "verdict_sha256",
        )
        atomic_write_json(paths.verdict, candidate, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError,
            "deterministic next Verdict identity and path",
        ):
            recover_unindexed_review_verdict_authority(paths)

        candidate["verdict_id"] = "VERDICT-0001"
        candidate["confirmation"]["typed_text"] = (
            f"RECORD VERDICT {paths.study_id} VERDICT-0001"
        )
        candidate["confirmation"]["confirmed_at"] = "[FILLED BY STUDYCTL]"
        candidate["verdict_sha256"] = record_digest(
            candidate,
            "verdict_sha256",
        )
        atomic_write_json(paths.verdict, candidate, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError,
            "phrase or timestamp is invalid",
        ):
            recover_unindexed_review_verdict_authority(paths)

        candidate["confirmation"]["confirmed_at"] = valid_confirmed_at
        candidate["verdict_sha256"] = record_digest(
            candidate,
            "verdict_sha256",
        )
        atomic_write_json(paths.verdict, candidate, mode=0o444)
        paths.brief_approval.unlink()

        with self.assertRaisesRegex(
            ValidationError,
            "requires a fresh approved Brief",
        ):
            recover_unindexed_review_verdict_authority(paths)

        sequence = load_json(paths.review_verdict_sequence)
        self.assertEqual(sequence["high_water_mark"], 0)

    def test_sequence_recovery_cannot_adopt_malformed_review_pair(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze malformed Review recovery rejection scope")
        history = paths.study / "review-history"
        history.mkdir(parents=True)

        malformed_packet = {"not": "a valid Review packet"}
        packet_source = self.root / ".objects" / "malformed-review-packet.json"
        atomic_write_json(packet_source, malformed_packet)
        packet_digest = sha256_file(packet_source)
        packet_archive = history / f"REVIEW_PACKET-{packet_digest}.json"
        atomic_write_json(packet_archive, malformed_packet, mode=0o444)

        malformed_review = {
            "study_id": paths.study_id,
            "review_packet_sha256": packet_digest,
        }
        review_source = self.root / ".objects" / "malformed-review.json"
        atomic_write_json(review_source, malformed_review)
        review_digest = sha256_file(review_source)
        review_archive = history / f"REVIEW-{review_digest}.json"
        atomic_write_json(review_archive, malformed_review, mode=0o444)

        with self.assertRaisesRegex(
            ValidationError,
            "cannot adopt an invalid Review archive",
        ):
            recover_unindexed_review_verdict_authority(paths)

        sequence = load_json(paths.review_verdict_sequence)
        self.assertEqual(sequence["high_water_mark"], 0)

    def test_review_import_rejects_self_consistent_but_stale_packet_scope(
        self,
    ) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Review packet replay fixture")
        packet_path = create_review_packet(paths)
        packet = load_json(packet_path)
        stale_commit = "0" * 40
        packet["git_diff_metadata"]["head"] = stale_commit
        packet["review_scope"]["commit"] = stale_commit
        packet["packet_sha256"] = record_digest(packet, "packet_sha256")
        atomic_write_json(packet_path, packet)
        source = self.root / ".objects" / "stale-packet-review.json"
        atomic_write_json(source, self._review_document(paths, packet_path))

        with self.assertRaisesRegex(
            ValidationError,
            "no longer matches the current Review scope",
        ):
            import_and_render_review(paths, source)

        self.assertFalse((paths.generated / "REVIEW.json").exists())

    def test_review_import_rejects_packet_with_invalid_self_digest(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Review packet digest fixture")
        packet_path = create_review_packet(paths)
        packet = load_json(packet_path)
        packet["known_deviations"].append("tampered after packet finalization")
        atomic_write_json(packet_path, packet)
        source = self.root / ".objects" / "tampered-packet-review.json"
        atomic_write_json(source, self._review_document(paths, packet_path))

        with self.assertRaisesRegex(
            ValidationError,
            "packet_sha256 is invalid",
        ):
            import_and_render_review(paths, source)

        self.assertFalse((paths.generated / "REVIEW.json").exists())

    def test_review_import_archives_exact_source_bytes(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze exact Review byte fixture")
        packet_path = create_review_packet(paths)
        review = self._review_document(paths, packet_path)
        source = self.root / ".objects" / "noncanonical-review.json"
        source_bytes = (
            json.dumps(review, ensure_ascii=False, indent=3).encode("utf-8")
            + b"  \n"
        )
        source.write_bytes(source_bytes)

        import_and_render_review(paths, source)

        review_digest = sha256_file(source)
        archived = (
            paths.study
            / "review-history"
            / f"REVIEW-{review_digest}.json"
        )
        self.assertEqual(archived.read_bytes(), source_bytes)
        self.assertEqual(
            (paths.generated / "REVIEW.json").read_bytes(),
            source_bytes,
        )

    def test_review_recovery_rejects_packet_for_an_old_commit(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Review recovery currentness fixture")
        packet_path = create_review_packet(paths)
        source = self.root / ".objects" / "stale-recovery-review.json"
        atomic_write_json(source, self._review_document(paths, packet_path))

        with (
            patch(
                "tools.studyctl.review_verdict_sequence.advance_review_verdict_sequence",
                side_effect=WorkflowError("simulated Review sequence crash"),
            ),
            self.assertRaisesRegex(
                WorkflowError,
                "simulated Review sequence crash",
            ),
        ):
            import_and_render_review(paths, source)

        self._commit_review_scope_change("move beyond recovered Review scope")

        with self.assertRaisesRegex(
            ValidationError,
            "no longer matches the current Review scope",
        ):
            recover_unindexed_review_verdict_authority(paths)

        sequence = load_json(paths.review_verdict_sequence)
        self.assertEqual(sequence["high_water_mark"], 0)

    def test_review_import_rejects_rehashed_misleading_derived_summary(
        self,
    ) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Review packet derived-summary fixture")
        packet_path = create_review_packet(paths)
        packet = load_json(packet_path)
        packet["authority_validation"] = {
            "passed": True,
            "errors": [],
            "warnings": ["Misleading summary injected after packet generation."],
        }
        packet["packet_sha256"] = record_digest(packet, "packet_sha256")
        atomic_write_json(packet_path, packet)
        source = self.root / ".objects" / "misleading-summary-review.json"
        atomic_write_json(source, self._review_document(paths, packet_path))

        with self.assertRaisesRegex(
            ValidationError,
            "does not replay from current authoritative sources",
        ):
            import_and_render_review(paths, source)

        self.assertFalse((paths.generated / "REVIEW.json").exists())

    def test_review_packet_and_import_reject_dirty_scientific_state(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze clean independent Review scope")
        marker = self.root / "scientific-workflow" / "dirty-review-state.txt"
        marker.write_text("uncommitted scientific state\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValidationError,
            "clean scientific worktree",
        ):
            create_review_packet(paths)

        marker.unlink()
        packet_path = create_review_packet(paths)
        source = self.root / ".objects" / "dirty-state-review.json"
        atomic_write_json(source, self._review_document(paths, packet_path))
        marker.write_text(
            "different uncommitted scientific state at import\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "clean scientific worktree",
        ):
            import_and_render_review(paths, source)

        self.assertFalse((paths.generated / "REVIEW.json").exists())
        marker.unlink()
        import_and_render_review(paths, source)
        self.assertTrue((paths.generated / "REVIEW.json").is_file())

        self.commit_all("track immutable Review history fixture")
        archived_review = next(
            (paths.study / "review-history").glob("REVIEW-*.json")
        )
        archived_review.chmod(0o644)
        archived_review.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ValidationError,
            "sealed read-only",
        ):
            create_review_packet(paths)

    def test_old_review_cannot_endorse_a_new_commit(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze independent Review scope")
        self._import_review(paths)
        self._commit_review_scope_change("advance repository after Review")
        decisions = self._decision_file(
            "stale-review-decisions.json",
            self._decision_input(),
        )

        with self.assertRaisesRegex(
            ValidationError,
            "no longer matches the current Review scope",
        ):
            record_verdict(
                paths,
                decisions,
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_no_review_and_no_separate_waiver_fails_closed(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze missing Review waiver fixture")
        decisions = self._decision_file(
            "missing-review-waiver.json",
            self._decision_input(),
        )

        with self.assertRaisesRegex(
            ValidationError,
            "explicit human Review waiver",
        ):
            record_verdict(
                paths,
                decisions,
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_new_verdict_requires_available_committed_git_identity(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Git identity Verdict fixture")
        source = self._decision_file(
            "git-unavailable-verdict.json",
            self._decision_input(
                review_waiver={
                    "reason": "No independent Review was commissioned.",
                    "source": "interactive_human_confirmation",
                    "authorization_text": f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
                }
            ),
        )
        unavailable = {
            "available": False,
            "commit": None,
            "dirty": None,
            "status": [],
            "status_sha256": None,
        }

        with (
            patch("tools.studyctl.approval.git_state", return_value=unavailable),
            self.assertRaisesRegex(
                ValidationError,
                "available Git repository and a committed revision",
            ),
        ):
            record_verdict(
                paths,
                source,
                stdin=TTYBuffer(
                    f"WAIVE INDEPENDENT REVIEW {paths.study_id}\n"
                ),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_new_v3_full_verdict_cannot_omit_checkpoint_claims_or_evidence(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        self._finalize_checkpoint(paths)
        self.commit_all("freeze full-source waiver scope")
        source_verdict = self._legacy_verdict(
            paths,
            "VERDICT-0001",
            schema_version=1,
        )
        source_verdict["schema_version"] = 3
        source_verdict["review_basis"] = {
            "mode": "waived",
            "reason": "No independent Review was commissioned for this fixture.",
            "authorization": {
                "source": "explicit_user_instruction",
                "text": "I explicitly waive independent Review for this Verdict.",
                "text_sha256": sha256_json(
                    "I explicitly waive independent Review for this Verdict."
                ),
            },
        }
        source_verdict["judged_scope"]["active_context"] = (
            current_review_scope(paths)["active_context"]
        )
        source = self._decision_file(
            "omitted-scope-full-verdict.json",
            source_verdict,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "accepts only decision input",
        ):
            record_verdict(
                paths,
                source,
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_agent_verdict_requires_separate_review_waiver_authorization(
        self,
    ) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze agent Review waiver fixture")
        decisions = self._decision_input(agent_initiated=True)
        source = self._decision_file("agent-review-waiver.json", decisions)

        with self.assertRaisesRegex(
            ValidationError,
            "explicit human Review waiver",
        ):
            record_verdict(
                paths,
                source,
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
                agent_initiated=True,
            )

        decisions["review_waiver"] = {
            "reason": "No independent Review was commissioned for this fixture.",
            "source": "explicit_user_instruction",
            "authorization_text": (
                "I explicitly waive independent Review for this implementation-only "
                "Verdict."
            ),
        }
        atomic_write_json(source, decisions)
        destination = record_verdict(
            paths,
            source,
            stdin=TTYBuffer(),
            stdout=TTYBuffer(),
            agent_initiated=True,
        )
        recorded = load_json(destination)

        self.assertEqual(recorded["schema_version"], 3)
        self.assertEqual(recorded["review_basis"]["mode"], "waived")
        self.assertEqual(
            recorded["review_basis"]["authorization"]["source"],
            "explicit_user_instruction",
        )
        self.assertEqual(
            recorded["judged_scope"]["active_context"],
            current_review_scope(paths)["active_context"],
        )
        self.assertNotEqual(
            recorded["review_basis"]["authorization"]["text_sha256"],
            recorded["authorization"]["instruction_sha256"],
        )
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_explicit_waiver_can_bypass_but_not_erase_a_stale_review(
        self,
    ) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze Review before explicit waiver")
        self._import_review(paths)
        generated_review = paths.generated / "REVIEW.json"
        review_digest = sha256_file(generated_review)
        self._commit_review_scope_change("make imported Review stale")
        decisions = self._decision_input(
            agent_initiated=True,
            review_waiver={
                "reason": "The imported Review predates the current repository commit.",
                "source": "explicit_user_instruction",
                "authorization_text": (
                    "I explicitly waive the stale independent Review for this Verdict."
                ),
            },
        )
        source = self._decision_file("stale-review-waiver.json", decisions)

        destination = record_verdict(
            paths,
            source,
            stdin=TTYBuffer(),
            stdout=TTYBuffer(),
            agent_initiated=True,
        )
        recorded = load_json(destination)

        self.assertEqual(recorded["review_basis"]["mode"], "waived")
        self.assertTrue(generated_review.is_file())
        self.assertEqual(sha256_file(generated_review), review_digest)
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_verdict_without_review_records_explicit_waiver(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze no-Review Verdict scope")
        stdout = TTYBuffer()

        destination = record_verdict(
            paths,
            stdin=TTYBuffer(
                self._verdict_responses(
                    paths,
                    "VERDICT-0001",
                    authorize_waiver=True,
                )
            ),
            stdout=stdout,
        )
        recorded = load_json(destination)

        self.assertEqual(recorded["schema_version"], 3)
        self.assertIn("review_basis", recorded)
        self.assertEqual(
            set(recorded["review_basis"]),
            {"mode", "reason", "authorization"},
        )
        self.assertEqual(recorded["review_basis"]["mode"], "waived")
        self.assertGreater(len(recorded["review_basis"]["reason"].strip()), 0)
        authorization = recorded["review_basis"]["authorization"]
        self.assertEqual(
            authorization["source"],
            "interactive_human_confirmation",
        )
        self.assertEqual(
            authorization["text"],
            f"WAIVE INDEPENDENT REVIEW {paths.study_id}",
        )
        self.assertIn("Independent Review waiver:", stdout.getvalue())
        self.assertEqual(errors_only(validate_study(paths)), [])

        missing_basis = copy.deepcopy(recorded)
        missing_basis.pop("review_basis")
        missing_basis["verdict_sha256"] = record_digest(
            missing_basis, "verdict_sha256"
        )
        atomic_write_json(destination, missing_basis, mode=0o444)
        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("review_basis" in message for message in messages),
            messages,
        )

    def test_decision_file_interactive_waiver_requires_fresh_typed_confirmation(
        self,
    ) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze decision-file waiver confirmation scope")
        forged_text = "THIS FILE TEXT WAS NEVER TYPED BY THE REVIEWER"
        decisions = self._decision_input(
            review_waiver={
                "reason": "No independent Review was commissioned.",
                "source": "interactive_human_confirmation",
                "authorization_text": forged_text,
            }
        )
        source = self._decision_file(
            "forged-interactive-waiver-decisions.json",
            decisions,
        )
        waiver_phrase = f"WAIVE INDEPENDENT REVIEW {paths.study_id}"
        verdict_phrase = f"RECORD VERDICT {paths.study_id} VERDICT-0001"

        with self.assertRaisesRegex(
            HumanGateError,
            "confirmation did not exactly match",
        ):
            record_verdict(
                paths,
                source,
                stdin=TTYBuffer(f"{forged_text}\n"),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

        destination = record_verdict(
            paths,
            source,
            stdin=TTYBuffer(f"{waiver_phrase}\n{verdict_phrase}\n"),
            stdout=TTYBuffer(),
        )
        recorded = load_json(destination)

        authorization = recorded["review_basis"]["authorization"]
        self.assertEqual(authorization["source"], "interactive_human_confirmation")
        self.assertEqual(authorization["text"], waiver_phrase)
        self.assertEqual(
            authorization["text_sha256"],
            sha256_json(waiver_phrase),
        )
        self.assertNotEqual(authorization["text"], forged_text)

    def test_legacy_v2_reviewed_basis_replays_a_schema_v1_packet(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze legacy reviewed Verdict scope")
        history = paths.study / "review-history"
        history.mkdir(parents=True)
        legacy_packet = {
            "schema_version": 1,
            "study_id": paths.study_id,
            "legacy_review_scope": "Historical packet predating packet schema v2.",
        }
        packet_source = self.root / ".objects" / "legacy-review-packet.json"
        atomic_write_json(packet_source, legacy_packet)
        packet_digest = sha256_file(packet_source)
        packet_archive = history / f"REVIEW_PACKET-{packet_digest}.json"
        atomic_write_json(packet_archive, legacy_packet, mode=0o444)
        legacy_review = self._review_document(paths, packet_archive)
        review_source = self.root / ".objects" / "legacy-review.json"
        atomic_write_json(review_source, legacy_review)
        review_digest = sha256_file(review_source)
        review_archive = history / f"REVIEW-{review_digest}.json"
        atomic_write_json(review_archive, legacy_review, mode=0o444)
        verdict = self._legacy_verdict(
            paths,
            "VERDICT-0001",
            schema_version=2,
        )
        verdict["review_basis"] = {
            "mode": "reviewed",
            "review": file_record(review_archive, paths.root),
            "review_packet": file_record(packet_archive, paths.root),
        }
        verdict["verdict_sha256"] = record_digest(verdict, "verdict_sha256")
        self._remove_sequence_for_legacy_migration(paths)
        atomic_write_json(paths.verdict, verdict, mode=0o444)
        migrate_legacy_review_verdict_sequence(paths)

        self.assertEqual(errors_only(validate_study(paths)), [])

        packet_archive.chmod(0o644)
        packet_archive.write_text("{}\n", encoding="utf-8")
        messages = [issue.message for issue in errors_only(validate_study(paths))]
        self.assertTrue(
            any("Verdict Review basis is invalid" in message for message in messages),
            messages,
        )

    def test_legacy_migration_rejects_current_review_and_verdict_formats(
        self,
    ) -> None:
        review_paths = self.initialize("SC-0001")
        self.fill_brief(review_paths)
        self.approve(review_paths)
        self.commit_all("freeze current Review migration rejection scope")
        packet_path = create_review_packet(review_paths)
        source = self.root / ".objects" / "current-review-for-migration.json"
        atomic_write_json(
            source,
            self._review_document(review_paths, packet_path),
        )
        with (
            patch(
                "tools.studyctl.review_verdict_sequence.advance_review_verdict_sequence",
                side_effect=WorkflowError("simulated current Review crash"),
            ),
            self.assertRaisesRegex(
                WorkflowError,
                "simulated current Review crash",
            ),
        ):
            import_and_render_review(review_paths, source)
        self._remove_sequence_for_legacy_migration(review_paths)

        with self.assertRaisesRegex(
            ValidationError,
            "only historical schema-v1 Review packets",
        ):
            migrate_legacy_review_verdict_sequence(review_paths)

        verdict_paths = self.initialize("SC-0002")
        self.fill_brief(verdict_paths)
        self.approve(verdict_paths)
        self.commit_all("freeze current Verdict migration rejection scope")
        current_verdict = {
            "schema_version": 3,
            "study_id": verdict_paths.study_id,
            "verdict_id": "VERDICT-0001",
            "verdict_sha256": None,
        }
        current_verdict["verdict_sha256"] = record_digest(
            current_verdict,
            "verdict_sha256",
        )
        self._remove_sequence_for_legacy_migration(verdict_paths)
        atomic_write_json(
            verdict_paths.verdict,
            current_verdict,
            mode=0o444,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "only historical Verdict schema versions 1 and 2",
        ):
            migrate_legacy_review_verdict_sequence(verdict_paths)

    def test_fresh_legacy_verdict_injection_is_not_historical_authority(
        self,
    ) -> None:
        for schema_version, study_id in ((1, "SC-0001"), (2, "SC-0002")):
            with self.subTest(schema_version=schema_version):
                paths = self.initialize(study_id)
                self.fill_brief(paths)
                self.approve(paths)
                self.commit_all(
                    f"freeze fresh schema-v{schema_version} injection scope"
                )
                verdict = self._legacy_verdict(
                    paths,
                    "VERDICT-0001",
                    schema_version=schema_version,
                )
                verdict["verdict_sha256"] = record_digest(
                    verdict,
                    "verdict_sha256",
                )
                atomic_write_json(paths.verdict, verdict, mode=0o444)

                messages = [
                    issue.message for issue in errors_only(validate_study(paths))
                ]

                self.assertTrue(
                    any(
                        "visible Review/Verdict authority does not match the sequence"
                        in message
                        for message in messages
                    ),
                    messages,
                )

    def test_legacy_v1_and_v2_are_readable_but_new_v2_is_rejected(self) -> None:
        legacy_paths = self.initialize()
        self.fill_brief(legacy_paths)
        self.approve(legacy_paths)
        self.commit_all("freeze legacy Verdict scope")
        legacy = self._legacy_verdict(
            legacy_paths,
            "VERDICT-0001",
            schema_version=1,
        )
        legacy["verdict_sha256"] = record_digest(legacy, "verdict_sha256")
        self._remove_sequence_for_legacy_migration(legacy_paths)
        atomic_write_json(legacy_paths.verdict, legacy, mode=0o444)
        migrate_legacy_review_verdict_sequence(legacy_paths)

        self.assertEqual(load_json(legacy_paths.verdict)["schema_version"], 1)
        self.assertEqual(errors_only(validate_study(legacy_paths)), [])

        legacy_v2_paths = self.initialize("SC-0002")
        self.fill_brief(legacy_v2_paths)
        self.approve(legacy_v2_paths)
        self.commit_all("freeze legacy v2 Verdict scope")
        legacy_v2 = self._legacy_verdict(
            legacy_v2_paths,
            "VERDICT-0001",
            schema_version=2,
        )
        legacy_v2["verdict_sha256"] = record_digest(
            legacy_v2, "verdict_sha256"
        )
        self._remove_sequence_for_legacy_migration(legacy_v2_paths)
        atomic_write_json(legacy_v2_paths.verdict, legacy_v2, mode=0o444)
        migrate_legacy_review_verdict_sequence(legacy_v2_paths)

        self.assertEqual(load_json(legacy_v2_paths.verdict)["schema_version"], 2)
        self.assertEqual(errors_only(validate_study(legacy_v2_paths)), [])

        new_paths = self.initialize("SC-0003")
        self.fill_brief(new_paths)
        self.approve(new_paths)
        self.commit_all("freeze current Verdict scope")
        unrecorded_v2 = self._legacy_verdict(
            new_paths,
            "VERDICT-0001",
            schema_version=2,
        )
        unrecorded_v2["confirmation"] = {
            "typed_text": "[FILLED BY STUDYCTL]",
            "confirmed_at": "[FILLED BY STUDYCTL]",
        }
        source = self.root / ".objects" / "new-v2-verdict.json"
        atomic_write_json(source, unrecorded_v2)

        with self.assertRaisesRegex(
            ValidationError,
            "accepts only decision input",
        ):
            record_verdict(
                new_paths,
                source,
                stdin=TTYBuffer(
                    f"RECORD VERDICT {new_paths.study_id} VERDICT-0001\n"
                ),
                stdout=TTYBuffer(),
            )
        self.assertFalse(new_paths.verdict.exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
