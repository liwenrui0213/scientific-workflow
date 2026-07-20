from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import io
import math
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests.helpers import TTYBuffer, WorkflowTestCase
from tools.studyctl.approval import approve_brief, record_verdict
from tools.studyctl.cli import initialize_study
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.formalization import check_formalization
from tools.studyctl.git_state import git_state
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from tools.studyctl.models import (
    HumanGateError,
    ValidationError,
    WorkflowError,
    study_paths,
    utc_now,
)
from tools.studyctl.rendering import render_status
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import (
    brief_approval_issues,
    errors_only,
    validate_study,
)


class CoreWorkflowTests(WorkflowTestCase):
    def error_messages(self, paths: object) -> list[str]:
        return [issue.message for issue in errors_only(validate_study(paths))]

    def set_supporting_reference(
        self,
        paths: object,
        *,
        evidence_id: str,
        version: int,
        digest: str,
    ) -> None:
        claims = load_json(paths.claims)
        claim = claims["claims"][0]
        claim["state"] = "numerically_supported"
        claim["supporting_evidence"] = [
            {
                "evidence_id": evidence_id,
                "version": version,
                "sha256": digest,
            }
        ]
        claim["uncertainty"] = "Limited to the deterministic fixture scope."
        claim["updated_at"] = utc_now()
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

    def valid_verdict_source(self, paths: object, verdict_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "study_id": paths.study_id,
            "verdict_id": verdict_id,
            "created_at": utc_now(),
            "reviewer": {
                "identity": "Independent Test Reviewer",
                "source": "human_authored_test_fixture",
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
                "rationale": "The implementation satisfies the tested workflow invariants.",
                "conditions": [],
            },
            "scientific_verdict": {
                "decision": "requires_more_evidence",
                "rationale": "The fixture establishes workflow behavior, not a scientific conclusion.",
                "scope": "Only the deterministic workflow fixture is judged.",
                "conditions": [],
            },
            "confirmation": {
                "typed_text": "[FILLED BY STUDYCTL]",
                "confirmed_at": "[FILLED BY STUDYCTL]",
            },
            "verdict_sha256": None,
        }

    def test_init_creates_draft_study_without_false_approval_or_verdict(self) -> None:
        study_id = "SC-1200"
        created = initialize_study(self.root, study_id, "  Core workflow fixture  ")
        paths = study_paths(self.root, study_id)

        self.assertEqual(created, paths.study)
        for directory in (
            paths.formal,
            paths.active_work,
            paths.archived_work,
            paths.runs,
            paths.evidence,
            paths.checkpoints,
            paths.generated,
        ):
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir(), f"missing initialized directory: {directory}")
        brief = paths.brief.read_text(encoding="utf-8")
        self.assertIn("# Scientific Brief: SC-1200 — Core workflow fixture", brief)
        for heading in (
            "## Non-Goals",
            "## Human-Supplied Assumptions",
            "## Agent-Inferred Assumptions Requiring Confirmation",
            "## Open Questions at Authorization",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, brief)
        claims = load_json(paths.claims)
        self.assertEqual(claims["study_id"], study_id)
        self.assertEqual(claims["revision"], 1)
        self.assertEqual(claims["claims"], [])
        self.assertFalse(paths.brief_approval.exists())
        self.assertEqual(list(paths.study.glob("VERDICT*.json")), [])
        status = (paths.generated / "STATUS.md").read_text(encoding="utf-8")
        self.assertIn("Approval: **missing or stale**", status)
        self.assertNotIn("Approval: **current**", status)

    def test_completed_intake_draft_is_distinguished_from_invalid_state(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.add_proposed_claim(paths)

        status = render_status(paths).read_text(encoding="utf-8")

        self.assertIn("DRAFT — structurally valid, awaiting human Brief approval", status)
        self.assertNotIn("INVALID —", status)
        self.assertIn("Approve or restore the active Brief.", status)

    def test_unresolved_intake_placeholder_blocks_brief_approval(self) -> None:
        paths = self.initialize()
        brief_hash = sha256_file(paths.brief)

        with self.assertRaisesRegex(ValidationError, "replacement placeholders"):
            approve_brief(
                paths,
                stdin=TTYBuffer(f"APPROVE {paths.study_id} {brief_hash}\n"),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.brief_approval.exists())

    def test_init_rejects_invalid_id_and_blank_title_without_partial_study(self) -> None:
        with self.assertRaisesRegex(ValidationError, "invalid study ID"):
            initialize_study(self.root, "SC-123", "Invalid identifier")
        self.assertFalse((self.root / "studies" / "SC-123").exists())

        with self.assertRaisesRegex(ValidationError, "title must not be empty"):
            initialize_study(self.root, "SC-1201", " \t\n")
        self.assertFalse((self.root / "studies" / "SC-1201").exists())

    def test_init_rejects_duplicate_without_overwriting_original(self) -> None:
        study_id = "SC-1202"
        initialize_study(self.root, study_id, "Original title")
        paths = study_paths(self.root, study_id)
        original_brief = paths.brief.read_bytes()
        original_claims = paths.claims.read_bytes()

        with self.assertRaisesRegex(
            WorkflowError,
            "refusing to overwrite existing study: SC-1202",
        ):
            initialize_study(self.root, study_id, "Replacement title")

        self.assertEqual(paths.brief.read_bytes(), original_brief)
        self.assertEqual(paths.claims.read_bytes(), original_claims)
        self.assertNotIn("Replacement title", paths.brief.read_text(encoding="utf-8"))

    def test_approve_brief_rejects_non_tty_without_recording_approval(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        brief_hash = sha256_file(paths.brief)
        phrase = f"APPROVE {paths.study_id} {brief_hash}\n"

        with self.assertRaisesRegex(HumanGateError, "requires an interactive TTY"):
            approve_brief(paths, stdin=io.StringIO(phrase), stdout=io.StringIO())

        self.assertFalse(paths.brief_approval.exists())
        self.assertEqual(sha256_file(paths.brief), brief_hash)

    def test_verdict_rejects_non_tty_then_records_valid_tty_confirmation(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        verdict_id = "VERDICT-0001"
        source_path = self.root / "verdict-source.json"
        atomic_write_json(source_path, self.valid_verdict_source(paths, verdict_id))
        phrase = f"RECORD VERDICT {paths.study_id} {verdict_id}"

        with self.assertRaisesRegex(HumanGateError, "requires an interactive TTY"):
            record_verdict(
                paths,
                source_path,
                stdin=io.StringIO(phrase + "\n"),
                stdout=io.StringIO(),
            )
        self.assertFalse(paths.verdict.exists())

        destination = record_verdict(
            paths,
            source_path,
            stdin=TTYBuffer(phrase + "\n"),
            stdout=TTYBuffer(),
        )
        recorded = load_json(destination)
        self.assertEqual(destination, paths.verdict)
        self.assertEqual(recorded["confirmation"]["typed_text"], phrase)
        self.assertNotIn("[FILLED BY STUDYCTL]", recorded["confirmation"]["confirmed_at"])
        self.assertEqual(
            recorded["verdict_sha256"],
            record_digest(recorded, "verdict_sha256"),
        )
        self.assertEqual(destination.stat().st_mode & 0o777, 0o444)
        self.assertIsNone(load_json(source_path)["verdict_sha256"])

    def test_verdict_claim_scope_requires_a_hash_pinned_reference(self) -> None:
        paths = self.initialize_approved_with_claim()
        verdict_id = "VERDICT-0001"
        source = self.valid_verdict_source(paths, verdict_id)
        source["judged_scope"]["claims"] = [{"claim_id": "CLAIM-0001"}]
        source_path = self.root / "unhashed-claim-verdict.json"
        atomic_write_json(source_path, source)

        with self.assertRaisesRegex(
            ValidationError,
            "Verdict source does not match the Verdict schema",
        ):
            record_verdict(
                paths,
                source_path,
                stdin=TTYBuffer(f"RECORD VERDICT {paths.study_id} {verdict_id}\n"),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

        claim = load_json(paths.claims)["claims"][0]
        source["judged_scope"]["claims"] = [
            {"claim_id": claim["claim_id"], "sha256": sha256_json(claim)}
        ]
        atomic_write_json(source_path, source)
        with self.assertRaisesRegex(
            ValidationError,
            "Claim references require the latest Checkpoint snapshot",
        ):
            record_verdict(
                paths,
                source_path,
                stdin=TTYBuffer(f"RECORD VERDICT {paths.study_id} {verdict_id}\n"),
                stdout=TTYBuffer(),
            )

        self.assertFalse(paths.verdict.exists())

    def test_validation_rechecks_recorded_verdict_evidence_targets(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        evidence_ref = {
            "evidence_id": evidence["evidence_id"],
            "version": evidence["version"],
            "sha256": evidence["record_sha256"],
        }
        verdict_id = "VERDICT-0001"
        source = self.valid_verdict_source(paths, verdict_id)
        source["judged_scope"]["evidence"] = [evidence_ref]
        source_path = self.root / "evidence-scoped-verdict.json"
        atomic_write_json(source_path, source)
        verdict_path = record_verdict(
            paths,
            source_path,
            stdin=TTYBuffer(f"RECORD VERDICT {paths.study_id} {verdict_id}\n"),
            stdout=TTYBuffer(),
        )
        original = load_json(verdict_path)
        self.assertEqual(self.error_messages(paths), [])

        forged = copy.deepcopy(original)
        forged["judged_scope"]["evidence"][0]["sha256"] = "0" * 64
        forged["verdict_sha256"] = record_digest(forged, "verdict_sha256")
        atomic_write_json(verdict_path, forged, mode=0o444)
        self.assertIn(
            "Verdict Evidence hash is stale ('EVID-0001', 1)",
            self.error_messages(paths),
        )

        atomic_write_json(verdict_path, original, mode=0o444)
        (paths.evidence / "EVID-0001.v0001.json").unlink()
        self.assertIn(
            "Verdict references missing Evidence ('EVID-0001', 1)",
            self.error_messages(paths),
        )

    def test_brief_byte_change_makes_approval_hash_stale(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        approval = load_json(paths.brief_approval)
        approved_hash = approval["brief"]["sha256"]
        original = paths.brief.read_bytes()
        altered = b"!" + original[1:]

        paths.brief.write_bytes(altered)

        self.assertEqual(len(altered), len(original))
        self.assertNotEqual(sha256_file(paths.brief), approved_hash)
        messages = [issue.message for issue in brief_approval_issues(paths)]
        self.assertIn("Brief changed after approval; approval is stale", messages)

    def test_brief_approval_validation_rejects_malformed_identity_and_scope(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        original = load_json(paths.brief_approval)
        cases = (
            (
                lambda item: item.__setitem__("reviewer", {}),
                "missing required property 'identity'",
            ),
            (
                lambda item: item["brief"].__setitem__("path", "wrong/BRIEF.md"),
                "approval Brief path does not match active Brief",
            ),
            (
                lambda item: item.__setitem__("repository", {}),
                "missing required property 'available'",
            ),
        )
        for mutate, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                forged = copy.deepcopy(original)
                mutate(forged)
                forged["approval_sha256"] = record_digest(forged, "approval_sha256")
                atomic_write_json(paths.brief_approval, forged, mode=0o444)
                messages = [issue.message for issue in brief_approval_issues(paths)]
                self.assertTrue(
                    any(expected_message in message for message in messages),
                    messages,
                )
                atomic_write_json(paths.brief_approval, original, mode=0o444)

        self.assertEqual(brief_approval_issues(paths), [])

    def test_concurrent_run_executions_allocate_unique_run_ids(self) -> None:
        paths = self.initialize_approved_with_claim()
        worker_count = 8
        barrier = threading.Barrier(worker_count)

        def launch(index: int) -> dict[str, object]:
            barrier.wait(timeout=10)
            return execute_run(
                paths,
                argv=[sys.executable, "-c", f"print({index})"],
                purpose=f"concurrent allocation fixture {index}",
                cohort_id="COHORT-001",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(launch, index) for index in range(worker_count)]
            manifests = [future.result(timeout=30) for future in futures]

        run_ids = [str(manifest["run_id"]) for manifest in manifests]
        self.assertEqual(len(run_ids), worker_count)
        self.assertEqual(len(set(run_ids)), worker_count)
        self.assertEqual(
            set(run_ids),
            {f"RUN-{index:06d}" for index in range(1, worker_count + 1)},
        )
        for manifest in manifests:
            manifest_path = paths.runs / str(manifest["run_id"]) / "manifest.json"
            with self.subTest(run_id=manifest["run_id"]):
                self.assertTrue(manifest_path.is_file())
                self.assertEqual(load_json(manifest_path)["status"], "succeeded")
        self.assertEqual(self.error_messages(paths), [])

    def test_run_manifest_is_written_once_in_terminal_form_without_temp_files(self) -> None:
        paths = self.initialize_approved_with_claim()
        observed_manifest_writes: list[dict[str, object]] = []
        real_atomic_write_json = atomic_write_json

        def observe_write(path: Path, value: object, **kwargs: object) -> None:
            if path.name == "manifest.json":
                observed_manifest_writes.append(
                    {
                        "existed_before": path.exists(),
                        "status": value["status"],
                        "sealed_at": value["integrity"]["sealed_at"],
                        "manifest_sha256": value["integrity"]["manifest_sha256"],
                        "overwrite": kwargs.get("overwrite"),
                    }
                )
            real_atomic_write_json(path, value, **kwargs)

        with patch("tools.studyctl.run_registry.atomic_write_json", side_effect=observe_write):
            manifest = self.successful_run(paths)

        run_directory = paths.runs / manifest["run_id"]
        disk_manifest = load_json(run_directory / "manifest.json")
        self.assertEqual(
            observed_manifest_writes,
            [
                {
                    "existed_before": False,
                    "status": "succeeded",
                    "sealed_at": manifest["integrity"]["sealed_at"],
                    "manifest_sha256": manifest["integrity"]["manifest_sha256"],
                    "overwrite": False,
                }
            ],
        )
        self.assertEqual(disk_manifest, manifest)
        self.assertEqual(
            {path.name for path in run_directory.iterdir()},
            {"manifest.json", "stdout.log", "stderr.log"},
        )
        temporary_files = [
            path for path in run_directory.rglob("*") if path.name.endswith(".tmp")
        ]
        self.assertEqual(temporary_files, [])

    def test_completed_manifest_rejects_overwrite_and_retains_valid_hash(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        original_bytes = manifest_path.read_bytes()
        original_file_hash = sha256_file(manifest_path)
        disk_manifest = load_json(manifest_path)
        self.assertEqual(
            disk_manifest["integrity"]["manifest_sha256"],
            nested_record_digest(disk_manifest, "integrity", "manifest_sha256"),
        )

        replacement = copy.deepcopy(disk_manifest)
        replacement["purpose"] = "unauthorized replacement"
        replacement["integrity"]["manifest_sha256"] = nested_record_digest(
            replacement,
            "integrity",
            "manifest_sha256",
        )
        with self.assertRaisesRegex(WorkflowError, "refusing to overwrite existing file"):
            atomic_write_json(manifest_path, replacement, overwrite=False, mode=0o444)

        self.assertEqual(manifest_path.read_bytes(), original_bytes)
        self.assertEqual(sha256_file(manifest_path), original_file_hash)
        self.assertEqual(load_json(manifest_path), disk_manifest)
        self.assertEqual(
            [path for path in manifest_path.parent.iterdir() if path.name.endswith(".tmp")],
            [],
        )

    def test_run_schema_requires_git_and_environment_provenance(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        original = load_json(manifest_path)

        for field in ("git", "environment"):
            with self.subTest(field=field):
                forged = copy.deepcopy(original)
                forged[field] = {}
                forged["integrity"]["manifest_sha256"] = nested_record_digest(
                    forged,
                    "integrity",
                    "manifest_sha256",
                )
                atomic_write_json(manifest_path, forged, mode=0o444)
                messages = self.error_messages(paths)
                self.assertTrue(
                    any("missing required property" in message for message in messages),
                    messages,
                )
                atomic_write_json(manifest_path, original, mode=0o444)

        self.assertEqual(self.error_messages(paths), [])

    def test_run_passes_shell_metacharacters_as_one_literal_argv_item(self) -> None:
        paths = self.initialize_approved_with_claim()
        marker = self.root / "shell-side-effect-must-not-exist"
        literal = f"literal; touch {marker}; $(touch {marker}) | cat > {marker} &"
        argv = [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            literal,
        ]

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as popen:
            manifest = execute_run(
                paths,
                argv=argv,
                purpose="literal argv metacharacter fixture",
                cohort_id="COHORT-001",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        stdout_path = Path(manifest["logs"]["stdout"]["path"])
        stderr_path = Path(manifest["logs"]["stderr"]["path"])
        if not stdout_path.is_absolute():
            stdout_path = self.root / stdout_path
        if not stderr_path.is_absolute():
            stderr_path = self.root / stderr_path
        self.assertEqual(manifest["execution"]["argv"], argv)
        execution_calls = [
            call for call in popen.call_args_list if call.args and call.args[0] == argv
        ]
        self.assertEqual(len(execution_calls), 1)
        self.assertIs(execution_calls[0].kwargs["shell"], False)
        self.assertEqual(stdout_path.read_text(encoding="utf-8"), literal + "\n")
        self.assertEqual(stderr_path.read_text(encoding="utf-8"), "")
        self.assertFalse(marker.exists())

    def test_evidence_draft_rejects_missing_run(self) -> None:
        paths = self.initialize_approved_with_claim()

        with self.assertRaisesRegex(ValidationError, "RUN-999999"):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                ["RUN-999999"],
            )

        self.assertEqual(list(paths.evidence.glob("EVID-*.json")), [])
        self.assertEqual(list(paths.evidence.glob(".*.lock")), [])

    def test_evidence_finalization_requires_a_fresh_approved_brief(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        paths.brief.write_text(
            paths.brief.read_text(encoding="utf-8") + "\nUnauthorized scope change.\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "fresh approved Brief is required before Evidence finalization",
        ):
            finalize_evidence(paths, draft_path)

        self.assertEqual(load_json(draft_path)["status"], "draft")

    def test_claim_validation_rejects_missing_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.assertEqual(self.error_messages(paths), [])
        self.set_supporting_reference(
            paths,
            evidence_id="EVID-9999",
            version=1,
            digest="0" * 64,
        )

        messages = self.error_messages(paths)
        self.assertIn(
            "Claim CLAIM-0001 references missing Evidence ('EVID-9999', 1)",
            messages,
        )

    def test_claim_validation_rejects_draft_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        run = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [run["run_id"]],
        )
        draft = load_json(draft_path)
        self.assertEqual(self.error_messages(paths), [])
        self.set_supporting_reference(
            paths,
            evidence_id=draft["evidence_id"],
            version=draft["version"],
            digest="0" * 64,
        )

        messages = self.error_messages(paths)
        key = "('EVID-0001', 1)"
        self.assertIn(f"Claim CLAIM-0001 references draft Evidence {key}", messages)
        self.assertIn(f"Claim CLAIM-0001 has stale Evidence hash {key}", messages)

    def test_claim_validation_rejects_stale_finalized_evidence_hash(self) -> None:
        paths = self.initialize_approved_with_claim()
        run = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [run])
        self.support_claim(paths, evidence)
        self.assertEqual(self.error_messages(paths), [])
        self.set_supporting_reference(
            paths,
            evidence_id=evidence["evidence_id"],
            version=evidence["version"],
            digest="0" * 64,
        )

        messages = self.error_messages(paths)
        self.assertIn(
            "Claim CLAIM-0001 has stale Evidence hash ('EVID-0001', 1)",
            messages,
        )
        self.assertFalse(any("references draft Evidence" in message for message in messages))

    def test_claim_evidence_roles_must_match_final_assessments(self) -> None:
        cases = (
            (
                "SC-1204",
                "inconclusive",
                "supporting_evidence",
                "numerically_supported",
                "as supporting",
            ),
            (
                "SC-1205",
                "supports",
                "contradictory_evidence",
                "contradicted",
                "as contradictory",
            ),
        )
        for study_id, assessment, role, state, expected in cases:
            with self.subTest(assessment=assessment, role=role):
                paths = self.initialize(study_id)
                self.fill_brief(paths)
                self.add_proposed_claim(paths)
                self.approve(paths)
                manifest = self.successful_run(paths)
                evidence = self.finalized_supporting_evidence(
                    paths,
                    [manifest],
                    assessment=assessment,
                )
                reference = {
                    "evidence_id": evidence["evidence_id"],
                    "version": evidence["version"],
                    "sha256": evidence["record_sha256"],
                }
                claims = load_json(paths.claims)
                claim = claims["claims"][0]
                claim["state"] = state
                claim[role] = [reference]
                claim["uncertainty"] = "Limited to this role-consistency fixture."
                claim["updated_at"] = utc_now()
                claims["revision"] += 1
                claims["updated_at"] = utc_now()
                atomic_write_json(paths.claims, claims)

                messages = self.error_messages(paths)
                self.assertTrue(any(expected in message for message in messages), messages)

    def test_validation_rechecks_related_evidence_targets(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        first = self.finalized_supporting_evidence(
            paths,
            [manifest],
            evidence_id="EVID-0001",
        )
        second_path = create_evidence_draft(
            paths,
            "EVID-0002",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        second = load_json(second_path)
        second["addresses"]["question"] = "Does the related fixture preserve its source?"
        second["runs"][0]["role"] = "context"
        second["analysis"]["method"] = "Reference the finalized source Evidence explicitly."
        second["result"] = {"relationship": "recorded"}
        second["scope"] = "Only the related-Evidence validation fixture."
        second["uncertainty"] = "No sampling uncertainty is asserted."
        second["limitations"] = ["This is not a scientific result."]
        second["assessment"] = "inconclusive"
        second["related_evidence"]["supporting"] = [
            {
                "evidence_id": first["evidence_id"],
                "version": first["version"],
                "sha256": first["record_sha256"],
            }
        ]
        atomic_write_json(second_path, second)
        finalize_evidence(paths, second_path)
        self.assertEqual(self.error_messages(paths), [])

        (paths.evidence / "EVID-0001.v0001.json").unlink()
        messages = self.error_messages(paths)
        self.assertIn(
            "missing related Evidence reference: ('EVID-0001', 1)",
            messages,
        )

    def test_incompatible_cohorts_require_explicit_justification(self) -> None:
        paths = self.initialize_approved_with_claim()
        first = self.successful_run(
            paths,
            cohort_id="COHORT-001",
            cohort_fields=['variant="alpha"'],
        )
        second = self.successful_run(
            paths,
            cohort_id="COHORT-002",
            cohort_fields=['variant="beta"'],
        )
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [first["run_id"], second["run_id"]],
        )
        draft = load_json(draft_path)
        comparison = draft["analysis"]["comparison"]
        self.assertEqual(comparison["mode"], "compatible_cohorts")
        self.assertEqual(comparison["changed_fields"], ["variant"])
        self.assertEqual(len(comparison["cohort_fingerprints"]), 2)
        self.assertIsNone(comparison["compatibility_justification"])
        self.assertIn(
            "incompatible Cohorts lack compatibility justification",
            self.error_messages(paths),
        )

        draft["addresses"]["question"] = "Do both recorded variants equal four?"
        for run_reference in draft["runs"]:
            run_reference["role"] = "supporting"
        draft["analysis"]["method"] = "Compare each deterministic result with four."
        draft["result"] = {"alpha": 4, "beta": 4}
        draft["scope"] = "Only the two explicitly recorded Cohorts."
        draft["uncertainty"] = "No sampling uncertainty in this deterministic fixture."
        draft["assessment"] = "supports"
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(ValidationError, "compatibility_justification"):
            finalize_evidence(paths, draft_path)
        unchanged = load_json(draft_path)
        self.assertEqual(unchanged["status"], "draft")
        self.assertIsNone(unchanged["record_sha256"])
        self.assertFalse((paths.evidence / ".EVID-0001.lock").exists())
        self.assertEqual(
            [path for path in paths.evidence.iterdir() if path.name.endswith(".tmp")],
            [],
        )

        unchanged["analysis"]["comparison"]["compatibility_justification"] = (
            "Both variants use exact integer equality; results are reported separately."
        )
        unchanged["analysis"]["comparison"]["changed_fields"] = ["fabricated-field"]
        atomic_write_json(draft_path, unchanged)
        with self.assertRaisesRegex(
            ValidationError,
            "changed_fields does not exactly match the Run Cohorts",
        ):
            finalize_evidence(paths, draft_path)

        corrected = load_json(draft_path)
        corrected["analysis"]["comparison"]["changed_fields"] = ["variant"]
        atomic_write_json(draft_path, corrected)
        finalized_path = finalize_evidence(paths, draft_path)
        self.assertEqual(load_json(finalized_path)["status"], "finalized")

    def test_gpu_formalization_threshold_and_invalid_estimates(self) -> None:
        paths = self.initialize()

        below = check_formalization(paths, {"estimated_gpu_hours": 9.999})
        self.assertEqual(below.outcome, "PASS")
        self.assertEqual(below.requirements, [])

        at_threshold = check_formalization(paths, {"estimated_gpu_hours": 10.0})
        self.assertEqual(at_threshold.outcome, "BLOCKED")
        self.assertEqual(
            at_threshold.requirements,
            [
                {
                    "level": "required_before_expensive_run",
                    "artifact": "PROTOCOL",
                    "reason": "estimated GPU use 10 h meets the 10 h threshold",
                }
            ],
        )

        approved_paths = self.initialize("SC-1203")
        self.fill_brief(approved_paths)
        self.add_proposed_claim(approved_paths)
        self.approve(approved_paths)
        with self.assertRaisesRegex(ValidationError, "formalization gate blocked Run"):
            execute_run(
                approved_paths,
                argv=[sys.executable, "-c", "print(4)"],
                purpose="must not execute without an active Protocol",
                estimated_gpu_hours=10.0,
            )
        self.assertEqual(list(approved_paths.runs.iterdir()), [])

        for invalid in (-0.001, math.nan):
            with self.subTest(estimated_gpu_hours=invalid):
                with self.assertRaisesRegex(
                    ValidationError,
                    "estimated GPU hours must be finite and non-negative",
                ):
                    check_formalization(paths, {"estimated_gpu_hours": invalid})

    def test_scientific_critical_run_binds_method_gate_at_evidence(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(4)"],
            purpose="scientific-critical declaration fixture",
            cohort_id="COHORT-001",
            scientific_critical=True,
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        self.assertEqual(manifest["formalization"]["outcome"], "ADVISORY")
        self.assertEqual(
            [item["artifact"] for item in manifest["formalization"]["requirements"]],
            ["METHOD"],
        )

        with self.assertRaisesRegex(
            ValidationError,
            "formalization gate blocked Evidence finalization",
        ):
            self.finalized_supporting_evidence(paths, [manifest])
        draft_path = paths.evidence / "EVID-0001.v0001.json"
        self.assertEqual(load_json(draft_path)["status"], "draft")

        (paths.formal / "METHOD.md").write_text(
            "# Fixture Method\n\n"
            "Status: active\n\n"
            "## Scientific Mapping\n\n"
            "The exact integer computation maps the fixture statement to a recorded result.\n\n"
            "## Algorithm\n\n"
            "Execute the deterministic command once, retain provenance, and compare with four.\n",
            encoding="utf-8",
        )
        finalized = finalize_evidence(paths, draft_path)
        self.assertEqual(load_json(finalized)["status"], "finalized")

    def test_evaluator_and_data_split_changes_each_require_reapproval(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.approve(paths)
        changes = (
            (
                paths.formal / "EVALUATOR.json",
                {"status": "active", "metric": "exact integer equality"},
                "changes_evaluator",
            ),
            (
                paths.formal / "DATASET_SPLIT.json",
                {"status": "active", "split": "deterministic fixture only"},
                "changes_dataset_split",
            ),
        )

        for artifact_path, contents, option_name in changes:
            with self.subTest(option=option_name):
                atomic_write_json(artifact_path, contents)
                stale_messages = [
                    issue.message for issue in brief_approval_issues(paths)
                ]
                self.assertIn(
                    "protected evaluator, data split, or acceptance criteria changed after approval",
                    stale_messages,
                )
                blocked = check_formalization(paths, {option_name: True})
                self.assertEqual(blocked.outcome, "BLOCKED")
                self.assertEqual(
                    [item["artifact"] for item in blocked.requirements],
                    ["BRIEF"],
                )
                self.assertIn(
                    "require a new procedural human Brief approval",
                    blocked.requirements[0]["reason"],
                )

                previous_approval_hash = sha256_file(paths.brief_approval)
                self.approve(paths)
                self.assertNotEqual(sha256_file(paths.brief_approval), previous_approval_hash)
                self.assertEqual(brief_approval_issues(paths), [])
                approved = check_formalization(paths, {option_name: True})
                self.assertEqual(approved.outcome, "PASS")
                self.assertEqual(approved.requirements, [])

        archives = list(
            (paths.study / "brief-history").glob("BRIEF.approval.v0001.r*.json")
        )
        self.assertEqual(len(archives), 2)

    def test_protected_artifacts_require_an_active_evaluator_before_approval(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        atomic_write_json(
            paths.formal / "DATASET_SPLIT.json",
            {"status": "active", "split": "fixture-only"},
        )
        brief_hash = sha256_file(paths.brief)
        phrase = TTYBuffer(f"APPROVE {paths.study_id} {brief_hash}\n")

        with self.assertRaisesRegex(
            ValidationError,
            "require an active formal/EVALUATOR.json",
        ):
            approve_brief(paths, stdin=phrase, stdout=TTYBuffer())
        self.assertFalse(paths.brief_approval.exists())

        atomic_write_json(
            paths.formal / "EVALUATOR.json",
            {"status": "active", "principle": "exact equality"},
        )
        self.approve(paths)
        self.assertEqual(brief_approval_issues(paths), [])


if __name__ == "__main__":
    unittest.main()
