from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys

from tests.helpers import TTYBuffer, WorkflowTestCase
from tools.studyctl.approval import record_verdict
from tools.studyctl.compaction import (
    budget_totals,
    current_evidence_hashes,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.git_state import git_state
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.models import StudyPaths
from tools.studyctl.rendering import render_status
from tools.studyctl.review import create_review_packet
from tools.studyctl.validation import errors_only, run_index, validate_study


class DemonstrationLifecycleTests(WorkflowTestCase):
    """A non-scientific fixture exercising the complete V1 authority chain."""

    def compaction_plan(
        self,
        paths: StudyPaths,
        evidence_ref: dict[str, object],
    ) -> Path:
        compaction_input = prepare_compaction(paths)
        claims = load_json(paths.claims)
        destination = self.root / "demonstration-compaction-plan.json"
        atomic_write_json(
            destination,
            {
                "schema_version": 1,
                "study_id": paths.study_id,
                "compaction_input_sha256": sha256_file(compaction_input),
                "claims_sha256": sha256_file(paths.claims),
                "evidence_sha256": current_evidence_hashes(paths),
                "archive_work_files": ["initial-idea.md"],
                "decisive_evidence": [evidence_ref],
                "contradictory_evidence": [],
                "frontier": claims["frontier"],
                "open_questions": claims["frontier"]["open_questions"],
                "next_actions": claims["frontier"]["next_actions"],
                "representative_failures": [],
                "budget_state": budget_totals(run_index(paths)),
            },
        )
        return destination

    def verdict_source(
        self,
        paths: StudyPaths,
        checkpoint: dict[str, object],
        evidence_ref: dict[str, object],
    ) -> Path:
        claims = load_json(paths.claims)
        claim = claims["claims"][0]
        destination = self.root / "demonstration-human-verdict.json"
        atomic_write_json(
            destination,
            {
                "schema_version": 1,
                "study_id": paths.study_id,
                "verdict_id": "VERDICT-0001",
                "created_at": "2026-07-19T00:00:00Z",
                "reviewer": {
                    "identity": "Independent fixture reviewer",
                    "source": "human-authored demonstration fixture",
                },
                "judged_scope": {
                    "commit": git_state(paths.root)["commit"],
                    "brief_sha256": sha256_file(paths.brief),
                    "checkpoint": {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "sha256": checkpoint["checkpoint_sha256"],
                    },
                    "claims": [
                        {
                            "claim_id": claim["claim_id"],
                            "sha256": sha256_json(claim),
                        }
                    ],
                    "evidence": [evidence_ref],
                },
                "implementation_verdict": {
                    "decision": "accepted",
                    "rationale": "The fixture completed every deterministic workflow transition.",
                    "conditions": [],
                },
                "scientific_verdict": {
                    "decision": "requires_more_evidence",
                    "rationale": "A software fixture is not scientific evidence beyond its test scope.",
                    "scope": "Only the non-scientific deterministic demonstration is judged.",
                    "conditions": [],
                },
                "confirmation": {
                    "typed_text": "[FILLED BY STUDYCTL]",
                    "confirmed_at": "[FILLED BY STUDYCTL]",
                },
                "verdict_sha256": None,
            },
        )
        return destination

    def test_complete_non_scientific_example_lifecycle(self) -> None:
        # 1-2. Initialize, draft, and procedurally approve the exact Brief bytes.
        paths = self.initialize()
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        self.approve(paths)

        # 3. Keep exploratory material explicitly outside the authority chain.
        note = paths.active_work / "initial-idea.md"
        note.write_text("Provisional fixture note; not Evidence.\n", encoding="utf-8")

        # 4-6. Record execution, finalize explicit Evidence, and update the Claim.
        manifest = self.successful_run(paths, output=".objects/demonstration-result.txt")
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        evidence_ref = load_json(paths.claims)["claims"][0]["supporting_evidence"][0]

        # 7. Compact semantic state and archive only the explicitly listed work note.
        checkpoint_path = finalize_compaction(
            paths,
            self.compaction_plan(paths, evidence_ref),
        )
        checkpoint = load_json(checkpoint_path)
        archived_note = paths.archived_work / checkpoint["checkpoint_id"] / note.name
        self.assertEqual(
            archived_note.read_text(encoding="utf-8"),
            "Provisional fixture note; not Evidence.\n",
        )
        self.assertFalse(note.exists())

        # 8. Regenerate deterministic projections for status and independent review.
        status_path = render_status(paths)
        packet_path = create_review_packet(paths)
        self.assertIn("CLAIM-0001", status_path.read_text(encoding="utf-8"))
        self.assertEqual(load_json(packet_path)["decisive_evidence"], [evidence_ref])

        # 9. Validate and record separate implementation/scientific human decisions.
        verdict_id = "VERDICT-0001"
        verdict_stdout = TTYBuffer()
        verdict_path = record_verdict(
            paths,
            self.verdict_source(paths, checkpoint, evidence_ref),
            stdin=TTYBuffer(f"RECORD VERDICT {paths.study_id} {verdict_id}\n"),
            stdout=verdict_stdout,
        )
        verdict = load_json(verdict_path)
        confirmation_display = verdict_stdout.getvalue()
        self.assertIn(f"Judged Brief SHA-256: {sha256_file(paths.brief)}", confirmation_display)
        self.assertIn("Judged commit:", confirmation_display)
        self.assertIn("CLAIM-0001", confirmation_display)
        self.assertIn("EVID-0001", confirmation_display)
        self.assertEqual(verdict["implementation_verdict"]["decision"], "accepted")
        self.assertEqual(
            verdict["scientific_verdict"]["decision"],
            "requires_more_evidence",
        )
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_documented_cli_run_order_preserves_exact_argv(self) -> None:
        paths = self.initialize_approved_with_claim()
        command = [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1:])",
            "--literal-flag",
            "value with spaces",
        ]
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            return_code = studyctl_main(
                [
                    "--root",
                    str(self.root),
                    "run",
                    paths.study_id,
                    "--purpose",
                    "documented CLI order fixture",
                    "--cohort",
                    "COHORT-001",
                    "--",
                    *command,
                ]
            )

        self.assertEqual(return_code, 0)
        result = load_json(paths.runs / "RUN-000001" / "manifest.json")
        self.assertEqual(result["execution"]["argv"], command)
        self.assertIn('"run_id": "RUN-000001"', stdout.getvalue())

    def test_cross_study_ids_are_rejected_for_run_evidence_and_checkpoint(self) -> None:
        paths = self.initialize_approved_with_claim()
        note = paths.active_work / "initial-idea.md"
        note.write_text("identity-check fixture\n", encoding="utf-8")
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        evidence_ref = load_json(paths.claims)["claims"][0]["supporting_evidence"][0]
        checkpoint_path = finalize_compaction(
            paths,
            self.compaction_plan(paths, evidence_ref),
        )

        records = (
            (
                paths.runs / manifest["run_id"] / "manifest.json",
                "integrity.manifest_sha256",
                "Run study_id does not match Study directory",
            ),
            (
                paths.evidence / "EVID-0001.v0001.json",
                "record_sha256",
                "Evidence study_id does not match Study directory",
            ),
            (
                checkpoint_path,
                "checkpoint_sha256",
                "Checkpoint study_id does not match Study directory",
            ),
        )
        for path, digest_field, expected_message in records:
            with self.subTest(path=path):
                original = load_json(path)
                forged = copy.deepcopy(original)
                forged["study_id"] = "SC-9999"
                if digest_field == "integrity.manifest_sha256":
                    forged["integrity"]["manifest_sha256"] = nested_record_digest(
                        forged,
                        "integrity",
                        "manifest_sha256",
                    )
                else:
                    forged[digest_field] = record_digest(forged, digest_field)
                atomic_write_json(path, forged, mode=0o444)

                messages = [issue.message for issue in errors_only(validate_study(paths))]
                self.assertIn(expected_message, messages)
                atomic_write_json(path, original, mode=0o444)

        self.assertEqual(errors_only(validate_study(paths)), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
