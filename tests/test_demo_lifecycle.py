from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys

from tests.helpers import TTYBuffer, WorkflowTestCase
from tools.studyctl.approval import begin_brief_revision, record_verdict
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
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
from tools.studyctl.models import StudyPaths, ValidationError, utc_now
from tools.studyctl.rendering import render_status
from tools.studyctl.review import create_review_packet, import_and_render_review
from tools.studyctl.validation import errors_only, validate_study


class DemonstrationLifecycleTests(WorkflowTestCase):
    """A non-scientific fixture exercising the complete V2 authority chain."""

    def compaction_plan(
        self,
        paths: StudyPaths,
        evidence_ref: dict[str, object] | None,
    ) -> Path:
        compaction_input = prepare_compaction(paths)
        prepared = load_json(compaction_input)
        claims = load_json(paths.claims)
        destination = paths.work / "demonstration-compaction-plan.json"
        atomic_write_json(
            destination,
            {
                "schema_version": 2,
                "study_id": paths.study_id,
                "compaction_input_sha256": sha256_file(compaction_input),
                "claims_sha256": sha256_file(paths.claims),
                "evidence_inventory": current_evidence_inventory_binding(paths),
                "archive_work_files": (
                    ["initial-idea.md"]
                    if (paths.active_work / "initial-idea.md").is_file()
                    else []
                ),
                "decisive_evidence": [] if evidence_ref is None else [evidence_ref],
                "contradictory_evidence": [],
                "frontier": claims["frontier"],
                "representative_failures": [],
                "budget_state": prepared["budget_totals"],
            },
        )
        return destination

    def verdict_decisions(
        self,
        paths: StudyPaths,
    ) -> Path:
        destination = self.root / ".objects" / "demonstration-human-verdict.json"
        atomic_write_json(
            destination,
            {
                "input_version": 1,
                "claim_ids": ["CLAIM-0001"],
                "implementation_verdict": {
                    "decision": "accepted",
                    "rationale": "The fixture completed every deterministic workflow transition.",
                },
                "scientific_verdict": {
                    "decision": "requires_more_evidence",
                    "rationale": "A software fixture is not scientific evidence beyond its test scope.",
                    "scope": "Only the non-scientific deterministic demonstration is judged.",
                },
            },
        )
        return destination

    @staticmethod
    def independent_review(
        paths: StudyPaths,
        packet_path: Path,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "study_id": paths.study_id,
            "reviewed_at": "2026-07-24T00:00:00Z",
            "reviewer": {
                "identity": "Independent Demonstration Reviewer",
                "source": "fresh read-only test session",
            },
            "review_packet_sha256": sha256_file(packet_path),
            "summary": "The exact deterministic demonstration packet was reviewed.",
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
                "Keep the scientific Verdict limited to this non-scientific fixture."
            ],
        }

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

        # 8. Freeze and independently review the exact deterministic state.
        status_path = render_status(paths)
        self.commit_all("freeze the state submitted for independent Review")
        packet_path = create_review_packet(paths)
        packet = load_json(packet_path)
        review_source = self.root / ".objects" / "demonstration-review.json"
        atomic_write_json(
            review_source,
            self.independent_review(paths, packet_path),
        )
        import_and_render_review(paths, review_source)
        self.assertIn("CLAIM-0001", status_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["decisive_evidence"], [evidence_ref])

        # 9. Validate and record separate implementation/scientific human decisions.
        verdict_id = "VERDICT-0001"
        verdict_stdout = TTYBuffer()
        decisions_path = self.verdict_decisions(paths)
        verdict_path = record_verdict(
            paths,
            decisions_path,
            stdin=TTYBuffer(f"RECORD VERDICT {paths.study_id} {verdict_id}\n"),
            stdout=verdict_stdout,
        )
        verdict = load_json(verdict_path)
        confirmation_display = verdict_stdout.getvalue()
        self.assertIn(f"Judged Brief SHA-256: {sha256_file(paths.brief)}", confirmation_display)
        self.assertIn("Judged commit:", confirmation_display)
        self.assertIn("CLAIM-0001", confirmation_display)
        self.assertIn("EVID-0001", confirmation_display)
        self.assertEqual(verdict["verdict_id"], verdict_id)
        self.assertRegex(verdict["created_at"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertEqual(
            verdict["reviewer"],
            {
                "identity": "Test Reviewer <reviewer@example.test>",
                "source": "git_config",
            },
        )
        self.assertEqual(
            verdict["judged_scope"],
            {
                "commit": git_state(paths.root)["commit"],
                "brief_sha256": sha256_file(paths.brief),
                "checkpoint": {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "sha256": checkpoint["checkpoint_sha256"],
                },
                "claims": [
                    {
                        "claim_id": "CLAIM-0001",
                        "sha256": sha256_json(checkpoint["claims_snapshot"][0]),
                    }
                ],
                "evidence": [evidence_ref],
                "active_context": packet["review_scope"]["active_context"],
            },
        )
        self.assertEqual(verdict["review_basis"]["mode"], "reviewed")
        self.assertEqual(
            verdict["review_basis"]["review_packet"]["sha256"],
            sha256_file(packet_path),
        )
        self.assertEqual(verdict["implementation_verdict"]["decision"], "accepted")
        self.assertEqual(
            verdict["scientific_verdict"]["decision"],
            "requires_more_evidence",
        )
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_agent_initiated_verdict_cli_requires_and_records_explicit_authorization(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint = load_json(
            finalize_compaction(paths, self.compaction_plan(paths, None))
        )
        self.commit_all("freeze the Agent-initiated Verdict scope")
        decision_path = self.root / ".objects" / "agent-verdict-decisions.json"
        instruction = (
            "Record the implementation and scientific Verdict below for SC-0001, "
            "and explicitly waive independent Review for this deterministic fixture, "
            "without asking me to assemble its mechanical hashes."
        )
        waiver_text = (
            "I explicitly waive independent Review for this deterministic "
            "Agent-initiated Verdict fixture."
        )
        valid_input = {
            "input_version": 2,
            "authorization": {
                "source": "explicit_user_instruction",
                "instruction": instruction,
            },
            "claim_ids": ["CLAIM-0001"],
            "implementation_verdict": {
                "decision": "accepted",
                "rationale": "The reviewed deterministic implementation is acceptable.",
            },
            "scientific_verdict": {
                "decision": "requires_more_evidence",
                "rationale": "The fixture does not establish a scientific conclusion.",
                "scope": "Only the deterministic fixture is judged.",
            },
            "review_waiver": {
                "reason": (
                    "This test exercises explicit Agent-initiated Verdict "
                    "authorization without commissioning an independent Review."
                ),
                "source": "explicit_user_instruction",
                "authorization_text": waiver_text,
            },
        }

        def invoke(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
            atomic_write_json(decision_path, payload)
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.studyctl",
                    "--root",
                    str(self.root),
                    "verdict",
                    paths.study_id,
                    "--agent-initiated",
                    "--file",
                    str(decision_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        missing_authorization = copy.deepcopy(valid_input)
        missing_authorization.pop("authorization")
        malformed_authorization = copy.deepcopy(valid_input)
        malformed_authorization["authorization"] = {
            "source": "agent_inference",
            "instruction": instruction,
        }
        for label, payload, expected_error in (
            (
                "missing authorization",
                missing_authorization,
                "plus authorization",
            ),
            (
                "malformed authorization",
                malformed_authorization,
                "explicit_user_instruction",
            ),
        ):
            with self.subTest(label=label):
                result = invoke(payload)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(paths.verdict.exists())

        result = invoke(valid_input)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        verdict = load_json(paths.verdict)
        self.assertEqual(
            verdict["authorization"],
            {
                "mode": "agent_initiated",
                "source": "explicit_user_instruction",
                "assurance": "cooperative",
                "instruction": instruction,
                "instruction_sha256": sha256_json(instruction),
            },
        )
        self.assertEqual(
            verdict["confirmation"],
            {
                "mode": "agent_initiated",
                "recorded_at": verdict["confirmation"]["recorded_at"],
            },
        )
        self.assertRegex(
            verdict["confirmation"]["recorded_at"],
            r"^\d{4}-\d{2}-\d{2}T.*Z$",
        )
        self.assertNotIn("typed_text", verdict["confirmation"])
        self.assertNotIn("confirmed_at", verdict["confirmation"])
        self.assertEqual(
            verdict["judged_scope"]["checkpoint"],
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "sha256": checkpoint["checkpoint_sha256"],
            },
        )
        self.assertEqual(
            verdict["judged_scope"]["claims"],
            [
                {
                    "claim_id": "CLAIM-0001",
                    "sha256": sha256_json(checkpoint["claims_snapshot"][0]),
                }
            ],
        )
        self.assertEqual(
            verdict["confirmation"]["mode"],
            "agent_initiated",
        )
        self.assertEqual(verdict["review_basis"]["mode"], "waived")
        self.assertEqual(
            verdict["review_basis"]["authorization"]["text_sha256"],
            sha256_json(waiver_text),
        )
        self.assertEqual(
            verdict["verdict_sha256"],
            record_digest(verdict, "verdict_sha256"),
        )
        self.assertEqual(paths.verdict.stat().st_mode & 0o777, 0o444)
        self.assertIn("Authorization: explicit user instruction", result.stdout)
        self.assertIn(str(paths.verdict), result.stdout)
        self.assertEqual(errors_only(validate_study(paths)), [])

    def test_auto_scoped_verdict_rejects_a_checkpoint_stale_to_claims(self) -> None:
        paths = self.initialize_approved_with_claim()
        finalize_compaction(paths, self.compaction_plan(paths, None))
        decisions_path = self.verdict_decisions(paths)

        claims = load_json(paths.claims)
        claims["frontier"]["summary"] = "The Claim changed after the Checkpoint."
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        self.commit_all("freeze Claims state that is stale to the Checkpoint")

        with self.assertRaisesRegex(
            ValidationError,
            "latest Checkpoint does not bind the current CLAIMS.json; compact before Verdict",
        ):
            record_verdict(
                paths,
                decisions_path,
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_auto_scoped_verdict_rejects_a_checkpoint_from_an_old_brief(self) -> None:
        paths = self.initialize_approved_with_claim()
        finalize_compaction(paths, self.compaction_plan(paths, None))
        begin_brief_revision(paths)
        revised = paths.brief.read_text(encoding="utf-8").replace(
            "[REPLACE: Review and update every affected section for Brief version 2.]",
            "This revision preserves the fixture Claim under a new human authority version.",
        )
        paths.brief.write_text(revised, encoding="utf-8")
        self.approve(paths)

        with self.assertRaisesRegex(
            ValidationError,
            "latest Checkpoint does not bind the current Brief approval; compact before Verdict",
        ):
            record_verdict(
                paths,
                self.verdict_decisions(paths),
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_auto_scoped_verdict_rejects_a_checkpoint_from_an_old_approval(self) -> None:
        paths = self.initialize_approved_with_claim()
        finalize_compaction(paths, self.compaction_plan(paths, None))
        atomic_write_json(
            paths.formal / "EVALUATOR.json",
            {"status": "active", "metric": "exact integer equality"},
        )
        self.approve(paths)

        with self.assertRaisesRegex(
            ValidationError,
            "latest Checkpoint does not bind the current Brief approval; compact before Verdict",
        ):
            record_verdict(
                paths,
                self.verdict_decisions(paths),
                stdin=TTYBuffer(),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_complete_verdict_record_is_historical_read_only(self) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint = load_json(
            finalize_compaction(paths, self.compaction_plan(paths, None))
        )
        atomic_write_json(
            paths.formal / "EVALUATOR.json",
            {"status": "active", "metric": "exact integer equality"},
        )
        self.approve(paths)
        verdict_id = "VERDICT-0001"
        source_path = self.root / ".objects" / "legacy-old-approval-verdict.json"
        atomic_write_json(
            source_path,
            {
                "schema_version": 2,
                "study_id": paths.study_id,
                "verdict_id": verdict_id,
                "created_at": utc_now(),
                "reviewer": {"identity": "Test Reviewer", "source": "test"},
                "review_basis": {
                    "mode": "waived",
                    "reason": (
                        "No imported independent Review was available for this "
                        "manual boundary fixture."
                    ),
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
                            "claim_id": "CLAIM-0001",
                            "sha256": sha256_json(checkpoint["claims_snapshot"][0]),
                        }
                    ],
                    "evidence": [],
                },
                "implementation_verdict": {
                    "decision": "accepted",
                    "rationale": "This legacy input tests authority binding.",
                    "conditions": [],
                },
                "scientific_verdict": {
                    "decision": "requires_more_evidence",
                    "rationale": "The stale approval must not be judged.",
                    "scope": "Only the deterministic fixture.",
                    "conditions": [],
                },
                "confirmation": {
                    "typed_text": "[FILLED BY STUDYCTL]",
                    "confirmed_at": "[FILLED BY STUDYCTL]",
                },
                "verdict_sha256": None,
            },
        )

        with self.assertRaisesRegex(
            ValidationError,
            "accepts only decision input",
        ):
            record_verdict(
                paths,
                source_path,
                stdin=TTYBuffer(f"RECORD VERDICT {paths.study_id} {verdict_id}\n"),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_auto_scoped_verdict_rejects_a_dirty_worktree(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        self.commit_all("freeze clean Verdict fixture")
        (self.root / "unreviewed-change.txt").write_text(
            "This byte sequence is not represented by HEAD.\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "Verdict requires a clean scientific worktree",
        ):
            record_verdict(paths, stdin=TTYBuffer(), stdout=TTYBuffer())

        self.assertFalse(paths.verdict.exists())

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
