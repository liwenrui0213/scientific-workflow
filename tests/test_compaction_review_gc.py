from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from typing import Any
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.gc import garbage_collection_report
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from tools.studyctl.models import StudyPaths, ValidationError, utc_now
from tools.studyctl.rendering import render_status
from tools.studyctl.review import create_review_packet, import_and_render_review
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import validate_study


class _CompactionPlanMixin:
    def write_compaction_plan(
        self,
        paths: StudyPaths,
        archive_work_files: list[str],
        *,
        name: str,
        decisive_evidence: list[dict[str, Any]] | None = None,
        representative_failures: list[str] | None = None,
    ) -> Path:
        compaction_input = prepare_compaction(paths)
        compaction_state = load_json(compaction_input)
        claims = load_json(paths.claims)
        plan = {
            "schema_version": 1,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": archive_work_files,
            "decisive_evidence": decisive_evidence or [],
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "open_questions": list(claims["frontier"]["open_questions"]),
            "next_actions": list(claims["frontier"]["next_actions"]),
            "representative_failures": representative_failures or [],
            "budget_state": compaction_state["budget_totals"],
        }
        destination = paths.work / name
        atomic_write_json(destination, plan)
        return destination


class CompactionTests(_CompactionPlanMixin, WorkflowTestCase):
    def test_prepare_rejects_blocked_host_change_scope(self) -> None:
        paths = self.initialize_approved_with_claim()
        rogue = self.root / "unclassified-host-change.txt"
        rogue.write_text("not authorized by a CHANGESET\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValidationError,
            "host repository change scope is BLOCKED",
        ):
            prepare_compaction(paths)
        self.assertFalse((paths.generated / "COMPACTION_INPUT.json").exists())

    def test_prepare_records_profile_host_scope_and_active_work_bindings(self) -> None:
        paths = self.initialize_approved_with_claim()
        note = paths.active_work / "bound-note.md"
        note.write_text("snapshot me\n", encoding="utf-8")

        state = load_json(prepare_compaction(paths))

        profile_path = self.root / "scientific-workflow" / "repository-profile.json"
        self.assertEqual(
            state["repository_profile"],
            {
                "path": "scientific-workflow/repository-profile.json",
                "sha256": sha256_file(profile_path),
            },
        )
        self.assertEqual(state["host_change_scope"]["outcome"], "PASS")
        self.assertTrue(state["host_change_scope"]["git_available"])
        self.assertEqual(
            state["host_change_scope"]["consequential_paths"],
            {
                "items": [],
                "total_count": 0,
                "selected_count": 0,
                "truncated": False,
                "inventory_sha256": sha256_json([]),
                "selected_bytes": 2,
            },
        )
        self.assertEqual(
            state["host_change_scope"]["fingerprint_sha256"],
            sha256_json([]),
        )
        self.assertEqual(
            state["active_work_inventory_sha256"],
            state["active_work_inventory"]["inventory_sha256"],
        )
        expected_file_record = {
            "path": note.relative_to(paths.root).as_posix(),
            "size": note.stat().st_size,
            "sha256": sha256_file(note),
        }
        self.assertEqual(
            state["active_work_files"],
            {
                "items": [expected_file_record],
                "total_count": 1,
                "selected_count": 1,
                "truncated": False,
                "inventory_sha256": sha256_json([expected_file_record]),
                "selected_bytes": len(
                    json.dumps(
                        [expected_file_record],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
            },
        )
        expected_inventory = [
            {
                "path": note.relative_to(paths.root).as_posix(),
                "kind": "file",
                "mode": note.stat().st_mode & 0o7777,
                "size": note.stat().st_size,
                "sha256": sha256_file(note),
            }
        ]
        self.assertEqual(
            state["active_work_inventory"]["items"],
            expected_inventory,
        )
        self.assertEqual(
            state["active_work_inventory"]["inventory_sha256"],
            sha256_json(expected_inventory),
        )
        self.assertEqual(state["candidate_archive_items"]["items"], [note.name])

    def test_prepare_bounds_large_work_indexes_and_commits_full_inventory(self) -> None:
        paths = self.initialize_approved_with_claim()
        file_count = 1_500
        for index in range(file_count):
            (paths.active_work / f"note-{index:04d}.txt").write_text(
                f"scratch {index}\n",
                encoding="utf-8",
            )

        output = prepare_compaction(paths)
        state = load_json(output)
        work_files = [
            {
                "path": path.relative_to(paths.root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(paths.active_work.glob("*.txt"))
        ]
        work_inventory = [
            {
                "path": record["path"],
                "kind": "file",
                "mode": (paths.root / record["path"]).stat().st_mode & 0o7777,
                "size": record["size"],
                "sha256": record["sha256"],
            }
            for record in work_files
        ]

        for key, expected_hash in (
            ("active_work_files", sha256_json(work_files)),
            ("active_work_inventory", sha256_json(work_inventory)),
            (
                "candidate_archive_items",
                sha256_json([f"note-{index:04d}.txt" for index in range(file_count)]),
            ),
        ):
            with self.subTest(index=key):
                index_record = state[key]
                self.assertEqual(index_record["total_count"], file_count)
                self.assertEqual(index_record["selected_count"], len(index_record["items"]))
                self.assertGreater(index_record["selected_count"], 0)
                self.assertLessEqual(index_record["selected_count"], 64)
                self.assertLessEqual(index_record["selected_bytes"], 8 * 1024)
                self.assertTrue(index_record["truncated"])
                self.assertEqual(index_record["inventory_sha256"], expected_hash)
        self.assertEqual(
            state["active_work_inventory_sha256"],
            sha256_json(work_inventory),
        )
        self.assertLess(output.stat().st_size, 256 * 1024)

    def test_finalize_rejects_change_to_unselected_work_after_bounded_prepare(self) -> None:
        paths = self.initialize_approved_with_claim()
        files: list[Path] = []
        for index in range(160):
            path = paths.active_work / f"note-{index:04d}.txt"
            path.write_text(f"prepared {index}\n", encoding="utf-8")
            files.append(path)
        plan = self.write_compaction_plan(
            paths,
            [],
            name="bounded-active-work-change-plan.json",
        )
        state = load_json(paths.generated / "COMPACTION_INPUT.json")
        selected_paths = {
            item["path"] for item in state["active_work_inventory"]["items"]
        }
        unselected = files[-1]
        self.assertTrue(state["active_work_inventory"]["truncated"])
        self.assertNotIn(unselected.relative_to(paths.root).as_posix(), selected_paths)

        unselected.write_text("changed outside bounded projection\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValidationError,
            "work/active inventory changed after compact-prepare",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_prepare_projects_schema_max_claim_payload_as_bounded_locators(self) -> None:
        paths = self.initialize_approved_with_claim()
        long_statement = "statement-marker-" + "s" * (4096 - len("statement-marker-"))
        long_scope = "scope-marker-" + "c" * (4096 - len("scope-marker-"))
        long_uncertainty = "uncertainty-marker-" + "u" * (
            4096 - len("uncertainty-marker-")
        )
        claims = load_json(paths.claims)
        claims["claims"] = [
            {
                "claim_id": f"CLAIM-{index:04d}",
                "statement": long_statement,
                "scope": long_scope,
                "state": "under_test",
                "evidence_basis": "none",
                "lifecycle": "active",
                "supporting_evidence": [],
                "contradictory_evidence": [],
                "other_evidence": [],
                "uncertainty": long_uncertainty,
                "limitations": ["l" * 1024 for _ in range(32)],
                "updated_at": utc_now(),
            }
            for index in range(1, 33)
        ]
        claims["frontier"] = {
            "summary": "f" * 4096,
            "claim_ids": [f"CLAIM-{index:04d}" for index in range(1, 33)],
            "open_questions": ["q" * 1024 for _ in range(64)],
            "next_actions": ["a" * 1024 for _ in range(64)],
            "human_decisions_required": ["d" * 1024 for _ in range(32)],
        }
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        output = prepare_compaction(paths)
        state = load_json(output)

        self.assertGreater(paths.claims.stat().st_size, 1_000_000)
        self.assertLess(output.stat().st_size, 256 * 1024)
        self.assertEqual(state["claims_source"]["path"], paths.claims.relative_to(paths.root).as_posix())
        self.assertEqual(state["claims_source"]["size"], paths.claims.stat().st_size)
        self.assertEqual(state["claims_source"]["sha256"], sha256_file(paths.claims))
        self.assertEqual(state["claims_source"]["revision"], claims["revision"])
        self.assertEqual(
            state["claims_source"]["claim_inventory_sha256"],
            sha256_json(claims["claims"]),
        )
        claim_index = state["current_claims"]
        self.assertEqual(claim_index["total_count"], 32)
        self.assertEqual(claim_index["selected_count"], len(claim_index["items"]))
        self.assertTrue(claim_index["truncated"])
        self.assertLessEqual(claim_index["selected_bytes"], 8 * 1024)
        first_locator = claim_index["items"][0]
        self.assertEqual(first_locator["claim_id"], "CLAIM-0001")
        self.assertEqual(first_locator["state"], "under_test")
        self.assertEqual(first_locator["sha256"], sha256_json(claims["claims"][0]))
        self.assertTrue(first_locator["statement"]["truncated"])
        serialized = output.read_text(encoding="utf-8")
        self.assertNotIn(long_statement, serialized)
        self.assertNotIn(long_scope, serialized)
        self.assertNotIn(long_uncertainty, serialized)
        self.assertEqual(state["current_frontier"]["open_questions"]["total_count"], 64)
        self.assertTrue(state["current_frontier"]["open_questions"]["truncated"])

    def test_evidence_inventory_binding_stays_constant_size(self) -> None:
        paths = self.initialize_approved_with_claim()
        evidence_count = 500
        evidence_paths: list[Path] = []
        for index in range(1, evidence_count + 1):
            path = paths.evidence / f"EVID-{index:04d}.v0001.json"
            atomic_write_json(path, {"fixture": index})
            evidence_paths.append(path)

        binding = current_evidence_inventory_binding(paths)
        expected_inventory = [
            {
                "path": path.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in evidence_paths
        ]

        self.assertEqual(
            binding,
            {
                "total_count": evidence_count,
                "inventory_sha256": sha256_json(expected_inventory),
            },
        )
        self.assertLess(len(json.dumps(binding).encode("utf-8")), 256)

    def test_finalize_rejects_evidence_added_after_plan_binding(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        plan = self.write_compaction_plan(
            paths,
            [],
            name="evidence-drift-plan.json",
        )
        create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )

        with self.assertRaisesRegex(
            ValidationError,
            "Evidence set changed after compact-prepare",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_finalize_rejects_repository_profile_change_after_prepare(self) -> None:
        paths = self.initialize_approved_with_claim()
        plan = self.write_compaction_plan(paths, [], name="profile-change-plan.json")
        profile_path = self.root / "scientific-workflow" / "repository-profile.json"
        profile = load_json(profile_path)
        profile["vendor_patterns"].append("vendor-cache/**")
        atomic_write_json(profile_path, profile)

        with self.assertRaisesRegex(
            ValidationError,
            "repository profile changed after compact-prepare",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_finalize_rejects_host_scope_change_after_prepare(self) -> None:
        paths = self.initialize_approved_with_claim()
        plan = self.write_compaction_plan(paths, [], name="host-change-plan.json")
        (self.root / "late-host-change.txt").write_text(
            "appeared after prepare\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValidationError, "change scope is BLOCKED"):
            finalize_compaction(paths, plan)
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_finalize_rejects_active_work_inventory_change_after_prepare(self) -> None:
        paths = self.initialize_approved_with_claim()
        note = paths.active_work / "mutable-note.md"
        note.write_text("prepared bytes\n", encoding="utf-8")
        plan = self.write_compaction_plan(
            paths,
            [note.name],
            name="active-work-change-plan.json",
        )
        note.write_text("changed after prepare\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValidationError,
            "work/active inventory changed after compact-prepare",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(note.read_text(encoding="utf-8"), "changed after prepare\n")
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_finalize_rejects_empty_active_work_directory_added_after_prepare(self) -> None:
        paths = self.initialize_approved_with_claim()
        plan = self.write_compaction_plan(
            paths,
            [],
            name="active-work-directory-change-plan.json",
        )
        (paths.active_work / "late-empty-directory").mkdir()

        with self.assertRaisesRegex(
            ValidationError,
            "work/active inventory changed after compact-prepare",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_generated_projection_change_does_not_drift_compaction_binding(self) -> None:
        paths = self.initialize_approved_with_claim()
        plan = self.write_compaction_plan(paths, [], name="generated-change-plan.json")
        status = paths.generated / "STATUS.md"
        status.write_text("non-authoritative generated projection\n", encoding="utf-8")

        checkpoint = load_json(finalize_compaction(paths, plan))

        state = load_json(paths.generated / "COMPACTION_INPUT.json")
        self.assertEqual(
            checkpoint["repository_profile"],
            state["repository_profile"],
        )
        self.assertEqual(
            checkpoint["host_change_scope"]["fingerprint_sha256"],
            state["host_change_scope"]["fingerprint_sha256"],
        )
        self.assertEqual(
            checkpoint["prepared_active_work_inventory_sha256"],
            state["active_work_inventory_sha256"],
        )

    def test_checkpoint_hash_pins_representative_failed_direction(self) -> None:
        paths = self.initialize_approved_with_claim()
        failure = paths.study / "failed-directions" / "unstable-method.md"
        failure.write_text("Representative failed direction.\n", encoding="utf-8")
        failure_ref = failure.relative_to(paths.root).as_posix()
        plan = self.write_compaction_plan(
            paths,
            [],
            name="failed-direction-plan.json",
            representative_failures=[failure_ref],
        )
        checkpoint_path = finalize_compaction(paths, plan)
        checkpoint = load_json(checkpoint_path)
        self.assertEqual(
            checkpoint["representative_failures"],
            [
                {
                    "kind": "failed_direction",
                    "path": failure_ref,
                    "size": failure.stat().st_size,
                    "sha256": sha256_file(failure),
                }
            ],
        )

        failure.write_text("Mutated failed direction.\n", encoding="utf-8")
        errors = [issue.message for issue in validate_study(paths) if issue.level == "ERROR"]
        self.assertIn("representative failed-direction hash/size is stale", errors)

    def test_unassigned_cohorts_are_grouped_by_fingerprint(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifests = [
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(4)"],
                purpose=f"unassigned {precision} Cohort",
                cohort_id=None,
                hardware_class="test-cpu",
                precision=precision,
            )
            for precision in ("float32", "float64")
        ]

        compaction_input = load_json(prepare_compaction(paths))
        counts = {
            item["cohort"]: item["status_counts"]
            for item in compaction_input["run_counts_by_cohort_and_status"]["items"]
        }
        expected_keys = {
            f"FINGERPRINT-{manifest['cohort']['fingerprint_sha256']}"
            for manifest in manifests
        }
        self.assertEqual(set(counts), expected_keys)
        self.assertEqual(
            {key: value["succeeded"] for key, value in counts.items()},
            {key: 1 for key in expected_keys},
        )

    def test_all_formal_files_are_classified_and_active_ones_propagate(self) -> None:
        paths = self.initialize()
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        model = paths.formal / "MODEL.md"
        model.write_text(
            "# Fixture Model\n\nStatus: active\n\nDefines only the test fixture model.\n",
            encoding="utf-8",
        )
        dataset_split = paths.formal / "DATASET_SPLIT.json"
        atomic_write_json(
            dataset_split,
            {"status": "active", "definition": "deterministic fixture only"},
        )
        evaluator = paths.formal / "EVALUATOR.json"
        atomic_write_json(
            evaluator,
            {"status": "active", "principle": "exact deterministic equality"},
        )
        draft_note = paths.formal / "NOTES.md"
        draft_note.write_text("# Draft formal note\n", encoding="utf-8")
        self.approve(paths)

        compaction_input = load_json(prepare_compaction(paths))
        active_paths = {
            item["path"]
            for item in compaction_input["active_formal_artifacts"]["items"]
        }
        stale_paths = {
            item["path"]
            for item in compaction_input["stale_formal_artifacts"]["items"]
        }
        self.assertIn(model.relative_to(paths.root).as_posix(), active_paths)
        self.assertIn(dataset_split.relative_to(paths.root).as_posix(), active_paths)
        self.assertIn(evaluator.relative_to(paths.root).as_posix(), active_paths)
        self.assertIn(draft_note.relative_to(paths.root).as_posix(), stale_paths)

        plan = self.write_compaction_plan(paths, [], name="formal-files-plan.json")
        checkpoint = load_json(finalize_compaction(paths, plan))
        checkpoint_paths = {item["path"] for item in checkpoint["active_formal_artifacts"]}
        self.assertEqual(checkpoint_paths, active_paths)

        packet = load_json(create_review_packet(paths))
        packet_paths = {item["path"] for item in packet["active_formal_artifacts"]}
        self.assertEqual(packet_paths, active_paths)

    def test_compaction_rejects_parent_and_absolute_archive_paths(self) -> None:
        paths = self.initialize_approved_with_claim()
        outside = paths.active_work.parent / "outside.txt"
        outside.write_text("must remain active\n", encoding="utf-8")

        unsafe_paths = ("../outside.txt", str(outside.resolve()))
        for index, unsafe in enumerate(unsafe_paths, start=1):
            with self.subTest(archive_path=unsafe):
                plan = self.write_compaction_plan(
                    paths,
                    [unsafe],
                    name=f"unsafe-plan-{index}.json",
                )
                with self.assertRaisesRegex(ValidationError, "unsafe archive path"):
                    finalize_compaction(paths, plan)
                self.assertEqual(outside.read_text(encoding="utf-8"), "must remain active\n")
                self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])
                self.assertFalse((paths.generated / ".compaction.lock").exists())

    def test_compaction_refuses_authoritatively_referenced_work_file(self) -> None:
        paths = self.initialize_approved_with_claim()
        referenced = paths.active_work / "referenced.txt"
        referenced.write_text("authoritative input\n", encoding="utf-8")
        claims = load_json(paths.claims)
        claims["claims"][0]["limitations"].append("work/active/referenced.txt")
        atomic_write_json(paths.claims, claims)

        plan = self.write_compaction_plan(
            paths,
            ["referenced.txt"],
            name="referenced-plan.json",
        )
        compaction_input = load_json(paths.generated / "COMPACTION_INPUT.json")
        self.assertNotIn(
            "referenced.txt",
            compaction_input["candidate_archive_items"]["items"],
        )

        with self.assertRaisesRegex(
            ValidationError,
            "refusing to archive authoritative referenced work file",
        ):
            finalize_compaction(paths, plan)
        self.assertTrue(referenced.is_file())
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_compaction_rejects_duplicate_normalized_archive_sources(self) -> None:
        paths = self.initialize_approved_with_claim()
        source = paths.active_work / "duplicate.txt"
        source.write_text("archive once only\n", encoding="utf-8")
        plan = self.write_compaction_plan(
            paths,
            ["duplicate.txt", "work/active/duplicate.txt"],
            name="duplicate-plan.json",
        )

        with self.assertRaisesRegex(ValidationError, "repeats archive source"):
            finalize_compaction(paths, plan)
        self.assertEqual(source.read_text(encoding="utf-8"), "archive once only\n")
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_compaction_cannot_label_draft_evidence_as_decisive(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        draft = load_json(draft_path)
        plan = self.write_compaction_plan(
            paths,
            [],
            name="draft-decisive-plan.json",
            decisive_evidence=[
                {
                    "evidence_id": draft["evidence_id"],
                    "version": draft["version"],
                    "sha256": "0" * 64,
                }
            ],
        )

        with self.assertRaisesRegex(
            ValidationError,
            "decisive_evidence contains a missing or stale Evidence reference",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(load_json(draft_path)["status"], "draft")
        self.assertEqual(list(paths.checkpoints.glob("CHECKPOINT-*.json")), [])

    def test_compaction_protects_run_input_and_missing_input_is_reported(self) -> None:
        paths = self.initialize_approved_with_claim()
        source = paths.active_work / "critical-input.txt"
        source.write_text("immutable Run input\n", encoding="utf-8")
        source_relative = source.relative_to(paths.root).as_posix()
        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(4)"],
            purpose="Run with an explicitly registered work input",
            cohort_id="COHORT-001",
            input_paths=[source_relative],
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        evidence_ref = load_json(paths.claims)["claims"][0]["supporting_evidence"][0]

        compaction_input = load_json(prepare_compaction(paths))
        self.assertNotIn(
            source.name,
            compaction_input["candidate_archive_items"]["items"],
        )
        plan = self.write_compaction_plan(
            paths,
            [source.name],
            name="run-input-plan.json",
            decisive_evidence=[evidence_ref],
        )
        with self.assertRaisesRegex(
            ValidationError,
            "refusing to archive authoritative referenced work file",
        ):
            finalize_compaction(paths, plan)
        self.assertEqual(source.read_text(encoding="utf-8"), "immutable Run input\n")

        source.unlink()
        issues = validate_study(paths)
        self.assertTrue(
            any(
                issue.level == "ERROR" and "input is unavailable" in issue.message
                for issue in issues
            )
        )
        status = render_status(paths).read_text(encoding="utf-8")
        self.assertIn("INVALID", status)
        self.assertIn("input is unavailable", status)

    def test_checkpoint_chain_and_status_regeneration_are_deterministic(self) -> None:
        paths = self.initialize_approved_with_claim()
        first_source = paths.active_work / "first" / "note.txt"
        first_source.parent.mkdir(parents=True)
        first_source.write_text("first snapshot\n", encoding="utf-8")
        first_plan = self.write_compaction_plan(
            paths,
            ["first/note.txt"],
            name="first-plan.json",
        )
        first_path = finalize_compaction(paths, first_plan)
        first = load_json(first_path)

        self.assertEqual(first["checkpoint_id"], "CHECKPOINT-000001")
        self.assertIsNone(first["previous_checkpoint"])
        self.assertEqual(first["checkpoint_sha256"], record_digest(first, "checkpoint_sha256"))
        self.assertFalse(first_source.exists())
        first_archive = paths.archived_work / "CHECKPOINT-000001" / "first" / "note.txt"
        self.assertEqual(first_archive.read_text(encoding="utf-8"), "first snapshot\n")
        self.assertEqual(first_archive.stat().st_mode & 0o222, 0)
        self.assertEqual(first["representative_failures"], [])
        self.assertEqual(
            first["archived_work_files"],
            [
                {
                    "source_path": "studies/SC-0001/work/active/first/note.txt",
                    "archived_path": (
                        "studies/SC-0001/work/archived/"
                        "CHECKPOINT-000001/first/note.txt"
                    ),
                    "size": len(b"first snapshot\n"),
                    "sha256": sha256_file(first_archive),
                }
            ],
        )

        second_source = paths.active_work / "second.txt"
        second_source.write_text("second snapshot\n", encoding="utf-8")
        second_plan = self.write_compaction_plan(
            paths,
            ["work/active/second.txt"],
            name="second-plan.json",
        )
        second_path = finalize_compaction(paths, second_plan)
        second = load_json(second_path)

        self.assertEqual(second["checkpoint_id"], "CHECKPOINT-000002")
        self.assertEqual(
            second["previous_checkpoint"],
            {
                "checkpoint_id": first["checkpoint_id"],
                "sha256": first["checkpoint_sha256"],
            },
        )
        self.assertEqual(second["checkpoint_sha256"], record_digest(second, "checkpoint_sha256"))
        self.assertEqual(first_path.stat().st_mode & 0o222, 0)
        self.assertEqual(second_path.stat().st_mode & 0o222, 0)
        checkpoint_errors = [
            issue for issue in validate_study(paths) if issue.level == "ERROR" and "CHECKPOINT" in issue.path
        ]
        self.assertEqual(checkpoint_errors, [])

        status_path = render_status(paths)
        expected_status = status_path.read_bytes()
        status_path.write_text("stale generated projection\n", encoding="utf-8")
        regenerated = render_status(paths).read_bytes()
        self.assertEqual(regenerated, expected_status)
        self.assertIn(b"CHECKPOINT-000002", regenerated)
        self.assertIn(b"projection, not a source of truth", regenerated)


class ReviewTests(_CompactionPlanMixin, WorkflowTestCase):
    @staticmethod
    def review_document(paths: StudyPaths, packet_path: Path) -> dict[str, Any]:
        finding = {
            "finding_id": "FINDING-001",
            "severity": "major",
            "title": "Inspect the implementation-to-Claim mapping",
            "description": "The mapping needs an independent source-level check.",
            "sources": [
                {
                    "kind": "brief",
                    "path": paths.brief.relative_to(paths.root).as_posix(),
                    "line": 1,
                    "note": "approved scientific question",
                },
                {
                    "kind": "file",
                    "path": "tools/example.py",
                    "symbol": "compute_result",
                    "line": 12,
                },
                {"kind": "claim", "claim_id": "CLAIM-0001"},
                {"kind": "run", "run_id": "RUN-000001"},
                {"kind": "evidence", "evidence_id": "EVID-0001"},
                {"kind": "checkpoint", "checkpoint_id": "CHECKPOINT-000001"},
                {"kind": "commit", "commit": "0123456789abcdef"},
            ],
            "recommendation": "Have the human reviewer inspect each cited source.",
        }
        return {
            "schema_version": 1,
            "study_id": paths.study_id,
            "reviewed_at": "2026-07-19T00:00:00Z",
            "reviewer": {"identity": "Independent Test Reviewer"},
            "review_packet_sha256": sha256_file(packet_path),
            "summary": "Independent review found a source-mapping question.",
            "requirement_coverage": [],
            "implementation_findings": [finding],
            "protected_condition_findings": [],
            "cohort_findings": [],
            "reproducibility_findings": [],
            "scientific_claim_findings": [],
            "contradictory_evidence_findings": [],
            "formalization_findings": [],
            "open_questions": ["Does the implementation match the approved scope?"],
            "recommended_human_checks": ["Inspect the cited implementation symbol."],
        }

    @staticmethod
    def recursive_keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(ReviewTests.recursive_keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(ReviewTests.recursive_keys(child) for child in value))
        return set()

    def test_review_packet_contains_reproducible_sources_but_no_favorable_conclusion(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        evidence_ref = load_json(paths.claims)["claims"][0]["supporting_evidence"][0]
        plan = self.write_compaction_plan(
            paths,
            [],
            name="review-checkpoint-plan.json",
            decisive_evidence=[evidence_ref],
        )
        checkpoint_path = finalize_compaction(paths, plan)

        packet_path = create_review_packet(paths, base_ref="main")
        packet = load_json(packet_path)

        self.assertEqual(packet["packet_sha256"], record_digest(packet, "packet_sha256"))
        self.assertEqual(packet["brief"]["path"], paths.brief.relative_to(paths.root).as_posix())
        self.assertEqual(packet["brief"]["approval"]["brief"]["sha256"], sha256_file(paths.brief))
        self.assertEqual(packet["decisive_evidence"], [evidence_ref])
        self.assertEqual(packet["contradictory_evidence"], [])
        trigger_registry = packet["observation_trigger_registry"]
        trigger_registry_path = paths.root / trigger_registry["path"]
        self.assertEqual(trigger_registry["version"], 1)
        self.assertEqual(
            trigger_registry["sha256"],
            load_json(trigger_registry_path)["registry_sha256"],
        )
        self.assertEqual(
            trigger_registry["size"],
            trigger_registry_path.stat().st_size,
        )
        self.assertEqual(packet["evidence"][0]["object"]["record_sha256"], evidence["record_sha256"])
        self.assertEqual(packet["decisive_run_manifests"][0]["run_id"], manifest["run_id"])
        self.assertEqual(
            packet["cohort_fingerprints"][manifest["run_id"]],
            manifest["cohort"]["fingerprint_sha256"],
        )
        run_source = packet["decisive_run_manifests"][0]
        self.assertEqual(
            run_source["path"],
            (paths.runs / manifest["run_id"] / "manifest.json")
            .relative_to(paths.root)
            .as_posix(),
        )
        self.assertNotIn("execution", run_source)
        self.assertEqual(
            packet["reproducibility_commands"][0]["manifest_path"],
            run_source["path"],
        )
        self.assertNotIn("argv", packet["reproducibility_commands"][0])
        self.assertTrue(
            packet["protected_condition_hash_checks"]["critical_run_checks"][0][
                "brief_hash_matches_active"
            ]
        )
        self.assertTrue(
            packet["protected_condition_hash_checks"]["critical_run_checks"][0][
                "approval_hash_matches_active"
            ]
        )
        self.assertEqual(
            packet["latest_checkpoint"]["checkpoint_sha256"],
            load_json(checkpoint_path)["checkpoint_sha256"],
        )
        self.assertTrue(packet["git_diff_metadata"]["available"])
        self.assertIsNone(packet["git_diff_metadata"]["deviation"])
        self.assertNotIn("repository is not a Git worktree", packet["known_deviations"])

        conclusion_keys = {
            "conclusion",
            "verdict",
            "implementation_acceptance",
            "scientific_acceptance",
            "favorable_conclusion",
        }
        self.assertTrue(conclusion_keys.isdisjoint(self.recursive_keys(packet)))
        serialized = json.dumps(packet, sort_keys=True).lower()
        self.assertNotIn("accepted_within_scope", serialized)

    def test_review_render_validates_and_preserves_source_references(self) -> None:
        paths = self.initialize_approved_with_claim()
        packet_path = create_review_packet(paths)
        review = self.review_document(paths, packet_path)
        source = self.root / "independent-review.json"
        atomic_write_json(source, review)

        markdown_path = import_and_render_review(paths, source)
        rendered = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(load_json(paths.generated / "REVIEW.json"), review)
        self.assertIn("**Non-authoritative projection.**", rendered)
        self.assertIn("The human Verdict remains authoritative.", rendered)
        self.assertIn("[MAJOR] FINDING-001", rendered)
        self.assertIn(
            "brief: path=studies/SC-0001/BRIEF.md, line=1, note=approved scientific question",
            rendered,
        )
        self.assertIn("file: path=tools/example.py, symbol=compute_result, line=12", rendered)
        self.assertIn("claim: claim_id=CLAIM-0001", rendered)
        self.assertIn("run: run_id=RUN-000001", rendered)
        self.assertIn("evidence: evidence_id=EVID-0001", rendered)
        self.assertIn("checkpoint: checkpoint_id=CHECKPOINT-000001", rendered)
        self.assertIn("commit: commit=0123456789abcdef", rendered)

    def test_review_render_rejects_schema_invalid_and_stale_reviews(self) -> None:
        paths = self.initialize_approved_with_claim()
        packet_path = create_review_packet(paths)
        review = self.review_document(paths, packet_path)

        invalid = json.loads(json.dumps(review))
        invalid["implementation_findings"][0]["sources"] = []
        invalid_source = self.root / "invalid-review.json"
        atomic_write_json(invalid_source, invalid)
        with self.assertRaisesRegex(ValidationError, "invalid structured review"):
            import_and_render_review(paths, invalid_source)
        self.assertFalse((paths.generated / "REVIEW.json").exists())
        self.assertFalse((paths.generated / "REVIEW.md").exists())

        missing_locator = json.loads(json.dumps(review))
        missing_locator["implementation_findings"][0]["sources"] = [{"kind": "file"}]
        missing_locator_source = self.root / "missing-locator-review.json"
        atomic_write_json(missing_locator_source, missing_locator)
        with self.assertRaisesRegex(ValidationError, "invalid structured review"):
            import_and_render_review(paths, missing_locator_source)

        missing_reviewer = json.loads(json.dumps(review))
        missing_reviewer["reviewer"] = {}
        missing_reviewer_source = self.root / "missing-reviewer.json"
        atomic_write_json(missing_reviewer_source, missing_reviewer)
        with self.assertRaisesRegex(ValidationError, "invalid structured review"):
            import_and_render_review(paths, missing_reviewer_source)

        stale = json.loads(json.dumps(review))
        stale["review_packet_sha256"] = "0" * 64
        stale_source = self.root / "stale-review.json"
        atomic_write_json(stale_source, stale)
        with self.assertRaisesRegex(
            ValidationError,
            "review does not reference the current REVIEW_PACKET.json",
        ):
            import_and_render_review(paths, stale_source)
        self.assertFalse((paths.generated / "REVIEW.json").exists())
        self.assertFalse((paths.generated / "REVIEW.md").exists())


class GarbageCollectionTests(WorkflowTestCase):
    def run_with_outputs(
        self,
        paths: StudyPaths,
        output_paths: list[str],
        *,
        pinned_outputs: list[str] | None = None,
        baseline_outputs: list[str] | None = None,
        unique_anomaly_outputs: list[str] | None = None,
    ) -> dict[str, Any]:
        statements = [
            f"Path({path!r}).parent.mkdir(parents=True, exist_ok=True); "
            f"Path({path!r}).write_text('4\\n', encoding='utf-8')"
            for path in output_paths
        ]
        code = "from pathlib import Path; " + "; ".join(statements)
        return execute_run(
            paths,
            argv=[sys.executable, "-c", code],
            purpose="create GC fixture objects",
            cohort_id="COHORT-001",
            output_paths=output_paths,
            pinned_outputs=pinned_outputs,
            baseline_outputs=baseline_outputs,
            unique_anomaly_outputs=unique_anomaly_outputs,
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def test_gc_is_dry_run_and_classifies_candidates_and_retained_objects(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.initialize_git()
        self.commit_all("initial approved fixture")

        ordinary_outputs = [
            ".objects/candidate.txt",
            ".objects/pinned.txt",
            ".objects/baseline.txt",
            ".objects/anomaly.txt",
        ]
        ordinary_run = self.run_with_outputs(
            paths,
            ordinary_outputs,
            pinned_outputs=[".objects/pinned.txt"],
            baseline_outputs=[".objects/baseline.txt"],
            unique_anomaly_outputs=[".objects/anomaly.txt"],
        )
        self.commit_all("record ordinary GC outputs")

        referenced_run = self.successful_run(paths, output=".objects/referenced.txt")
        evidence = self.finalized_supporting_evidence(paths, [referenced_run])
        self.support_claim(paths, evidence)
        orphan = self.root / ".objects" / "orphan.txt"
        orphan.write_text("not registered by a Run\n", encoding="utf-8")

        report = garbage_collection_report(paths)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["deleted"], [])
        self.assertEqual(
            report["candidates"],
            [
                {
                    "path": ".objects/candidate.txt",
                    "run_ids": [ordinary_run["run_id"]],
                    "size": 2,
                    "sha256": ordinary_run["outputs"][0]["sha256"],
                    "reason": "unreferenced ordinary output with a reproducible Run manifest",
                }
            ],
        )
        retained = {item["path"]: item for item in report["retained"]}
        expected_reasons = {
            ".objects/pinned.txt": "output is pinned",
            ".objects/baseline.txt": "output is a baseline",
            ".objects/anomaly.txt": "output is a unique anomaly",
            ".objects/referenced.txt": "Run is referenced by Evidence, Claim, or Verdict",
            ".objects/orphan.txt": "object has no reproducible Run manifest",
        }
        self.assertEqual(set(retained), set(expected_reasons))
        for path, reason in expected_reasons.items():
            with self.subTest(path=path):
                self.assertEqual(retained[path]["reason"], reason)
                self.assertTrue((self.root / path).is_file())
        self.assertEqual(retained[".objects/orphan.txt"]["run_id"], "unregistered")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            return_code = studyctl_main(["--root", str(self.root), "gc", paths.study_id])
        self.assertEqual(return_code, 2)
        self.assertIn("garbage collection is dry-run only; pass --dry-run", stderr.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            return_code = studyctl_main(
                ["--root", str(self.root), "gc", paths.study_id, "--dry-run"]
            )
        self.assertEqual(return_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        for relative in [*ordinary_outputs, ".objects/referenced.txt", ".objects/orphan.txt"]:
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_output_retention_flags_are_validated_before_execution(self) -> None:
        paths = self.initialize_approved_with_claim()
        marker = self.root / ".objects" / "must-not-run.txt"
        command = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ]

        with self.assertRaisesRegex(ValidationError, "must also be declared with --output"):
            execute_run(
                paths,
                argv=command,
                purpose="invalid pin fixture",
                output_paths=[".objects/declared.txt"],
                pinned_outputs=[".objects/not-declared.txt"],
            )
        with self.assertRaisesRegex(
            ValidationError,
            "cannot be both baseline and unique anomaly",
        ):
            execute_run(
                paths,
                argv=command,
                purpose="conflicting classification fixture",
                output_paths=[".objects/declared.txt"],
                baseline_outputs=[".objects/declared.txt"],
                unique_anomaly_outputs=[".objects/declared.txt"],
            )

        self.assertFalse(marker.exists())
        self.assertEqual(list(paths.runs.iterdir()), [])

    def test_gc_retains_output_when_an_input_changed_during_run(self) -> None:
        paths = self.initialize_approved_with_claim()
        mutable_input = self.root / "inputs" / "mutable.txt"
        mutable_input.parent.mkdir(parents=True)
        mutable_input.write_text("before\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("inputs/mutable.txt\n", encoding="utf-8")
        self.initialize_git()
        self.commit_all("record mutable-input fixture")

        output = ".objects/changing-input-output.txt"
        code = (
            "from pathlib import Path; "
            "Path('inputs/mutable.txt').write_text('after\\n', encoding='utf-8'); "
            f"Path({output!r}).write_text('4\\n', encoding='utf-8')"
        )
        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", code],
            purpose="exercise changed-during-Run input retention",
            cohort_id="COHORT-001",
            input_paths=["inputs/mutable.txt"],
            output_paths=[output],
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        self.assertTrue(manifest["inputs"][0]["changed_during_run"])

        report = garbage_collection_report(paths)
        self.assertEqual(report["candidates"], [])
        retained = {item["path"]: item for item in report["retained"]}
        self.assertEqual(
            retained[output]["reason"],
            "input changed during Run: inputs/mutable.txt",
        )
        self.assertTrue((self.root / output).is_file())

    def test_tracked_code_change_blocks_evidence_and_gc_deletion(self) -> None:
        paths = self.initialize_approved_with_claim()
        tracked_code = self.root / "tracked_code.py"
        tracked_code.write_text("VALUE = 4\n", encoding="utf-8")
        self.initialize_git()
        self.commit_all("record tracked-code fixture")

        output = ".objects/code-change-output.txt"
        code = (
            "from pathlib import Path; "
            "Path('tracked_code.py').write_text('VALUE = 5\\n', encoding='utf-8'); "
            f"Path({output!r}).write_text('4\\n', encoding='utf-8')"
        )
        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", code],
            purpose="tracked-code mutation fixture",
            cohort_id="COHORT-001",
            output_paths=[output],
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        self.assertTrue(manifest["code_state"]["changed_during_run"])
        self.assertNotEqual(
            manifest["code_state"]["before"],
            manifest["code_state"]["after"],
        )

        report = garbage_collection_report(paths)
        self.assertEqual(report["candidates"], [])
        retained = {item["path"]: item for item in report["retained"]}
        self.assertEqual(retained[output]["reason"], "tracked code changed during Run")

        with self.assertRaisesRegex(
            ValidationError,
            "tracked code changed",
        ):
            self.finalized_supporting_evidence(paths, [manifest])
        warnings = [
            issue.message for issue in validate_study(paths) if issue.level == "WARNING"
        ]
        self.assertIn("tracked code changed during Run", warnings)


if __name__ == "__main__":
    unittest.main()
