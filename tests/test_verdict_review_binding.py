from __future__ import annotations

import copy
from pathlib import Path

from tests.helpers import TTYBuffer, WorkflowTestCase
from tools.studyctl.approval import record_verdict
from tools.studyctl.git_state import git_state
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
)
from tools.studyctl.models import ValidationError, errors_only, utc_now
from tools.studyctl.review import create_review_packet, import_and_render_review
from tools.studyctl.validation import validate_study


class VerdictReviewBindingTests(WorkflowTestCase):
    @staticmethod
    def _review_document(paths: object, packet_path: Path) -> dict[str, object]:
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
    def _verdict_responses(paths: object, verdict_id: str) -> str:
        return "\n".join(
            (
                "accepted",
                "The implementation satisfies the reviewed workflow invariants.",
                "requires_more_evidence",
                "The fixture does not establish a scientific conclusion.",
                "Only the deterministic workflow fixture is judged.",
                f"RECORD VERDICT {paths.study_id} {verdict_id}",
                "",
            )
        )

    @staticmethod
    def _legacy_verdict(paths: object, verdict_id: str) -> dict[str, object]:
        verdict: dict[str, object] = {
            "schema_version": 1,
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
        return verdict

    def _import_review(self, paths: object) -> dict[str, object]:
        packet_path = create_review_packet(paths)
        review = self._review_document(paths, packet_path)
        source = self.root / ".objects" / "independent-review.json"
        atomic_write_json(source, review)
        import_and_render_review(paths, source)
        return review

    def _record_reviewed_verdict(self) -> tuple[object, dict[str, object]]:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        review = self._import_review(paths)
        self.commit_all("freeze independent Review basis")
        stdout = TTYBuffer()
        destination = record_verdict(
            paths,
            stdin=TTYBuffer(self._verdict_responses(paths, "VERDICT-0001")),
            stdout=stdout,
        )
        recorded = load_json(destination)
        self.assertEqual(recorded["schema_version"], 2)
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
        return paths, recorded

    def test_verdict_v2_binds_immutable_review_and_packet_archives(self) -> None:
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

    def test_verdict_without_review_records_explicit_waiver(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze no-Review Verdict scope")
        stdout = TTYBuffer()

        destination = record_verdict(
            paths,
            stdin=TTYBuffer(self._verdict_responses(paths, "VERDICT-0001")),
            stdout=stdout,
        )
        recorded = load_json(destination)

        self.assertEqual(recorded["schema_version"], 2)
        self.assertIn("review_basis", recorded)
        self.assertEqual(set(recorded["review_basis"]), {"mode", "reason"})
        self.assertEqual(recorded["review_basis"]["mode"], "waived")
        self.assertGreater(len(recorded["review_basis"]["reason"].strip()), 0)
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

    def test_legacy_v1_is_readable_but_new_v1_verdict_is_rejected(self) -> None:
        legacy_paths = self.initialize()
        self.fill_brief(legacy_paths)
        self.approve(legacy_paths)
        self.commit_all("freeze legacy Verdict scope")
        legacy = self._legacy_verdict(legacy_paths, "VERDICT-0001")
        legacy["verdict_sha256"] = record_digest(legacy, "verdict_sha256")
        atomic_write_json(legacy_paths.verdict, legacy, mode=0o444)

        self.assertEqual(load_json(legacy_paths.verdict)["schema_version"], 1)
        self.assertEqual(errors_only(validate_study(legacy_paths)), [])

        new_paths = self.initialize("SC-0002")
        self.fill_brief(new_paths)
        self.approve(new_paths)
        self.commit_all("freeze current Verdict scope")
        unrecorded_v1 = self._legacy_verdict(new_paths, "VERDICT-0001")
        unrecorded_v1["confirmation"] = {
            "typed_text": "[FILLED BY STUDYCTL]",
            "confirmed_at": "[FILLED BY STUDYCTL]",
        }
        source = self.root / ".objects" / "new-v1-verdict.json"
        atomic_write_json(source, unrecorded_v1)

        with self.assertRaisesRegex(
            ValidationError,
            "new Verdicts must use the current schema_version",
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
