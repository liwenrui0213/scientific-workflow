from __future__ import annotations

import sys
from pathlib import Path
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import (
    ACTIVE_CONTEXT_OCCURRENCE_ITEM_LIMIT,
    build_occurrence_locator,
    write_active_selector,
)
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.hashing import atomic_write_json, load_json, sha256_file
from tools.studyctl.models import StudyPaths, ValidationError
from tools.studyctl.run_registry import execute_run


class OccurrenceLocatorTests(WorkflowTestCase):
    def failed_run(self, paths: StudyPaths) -> dict[str, object]:
        return execute_run(
            paths,
            argv=[sys.executable, "-c", "raise SystemExit(7)"],
            purpose="deterministic failed attempt",
            output_paths=[],
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def missing_output_run(self, paths: StudyPaths) -> dict[str, object]:
        return execute_run(
            paths,
            argv=[sys.executable, "-c", "print(4)"],
            purpose="deterministic missing-output attempt",
            output_paths=[".objects/not-produced.txt"],
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    @staticmethod
    def occurrence_binding(locator: dict[str, object]) -> dict[str, object]:
        return {
            "total_count": locator["total_count"],
            "inventory_sha256": locator["inventory_sha256"],
        }

    def compaction_plan(
        self,
        paths: StudyPaths,
        *,
        occurrence_inventory: dict[str, object],
        name: str,
    ) -> Path:
        prepared_path = paths.generated / "COMPACTION_INPUT.json"
        prepared = load_json(prepared_path)
        claims = load_json(paths.claims)
        plan = {
            "schema_version": 2,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(prepared_path),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "occurrence_inventory": occurrence_inventory,
            "archive_work_files": [],
            "decisive_evidence": [],
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "representative_failures": [],
            "budget_state": prepared["budget_totals"],
        }
        path = paths.work / name
        atomic_write_json(path, plan)
        return path

    def test_locator_exposes_only_occurrence_facts_and_exact_sources(self) -> None:
        paths = self.initialize_approved_with_claim()
        failed = self.failed_run(paths)
        missing = self.missing_output_run(paths)
        ordinary = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(
            paths,
            [ordinary],
        )

        locator = build_occurrence_locator(paths)

        self.assertEqual(locator["total_count"], 3)
        runs = {
            item["run_id"]: item
            for item in locator["run_occurrences"]["items"]
        }
        self.assertEqual(
            runs[failed["run_id"]]["facts"],
            ["run_status_failed"],
        )
        self.assertEqual(
            runs[missing["run_id"]]["facts"],
            [
                "missing_declared_output",
                "evidence_ineligible_attempt",
            ],
        )
        self.assertEqual(
            runs[missing["run_id"]]["missing_declared_output_count"],
            1,
        )
        self.assertNotIn(ordinary["run_id"], runs)
        attention_evidence = locator[
            "finalized_undispositioned_evidence"
        ]["items"]
        self.assertEqual(
            [
                (item["evidence_id"], item["version"], item["fact"])
                for item in attention_evidence
            ],
            [
                (
                    evidence["evidence_id"],
                    evidence["version"],
                    "finalized_undispositioned_evidence",
                )
            ],
        )
        self.assertEqual(
            attention_evidence[0]["claim_ids"],
            ["CLAIM-0001"],
        )
        for item in [*runs.values(), *attention_evidence]:
            source = paths.root / item["source"]["path"]
            self.assertTrue(source.is_file())
            self.assertEqual(item["source"]["size"], source.stat().st_size)
            self.assertEqual(item["source"]["sha256"], sha256_file(source))
        self.assertEqual(
            locator["authority"]["run_ledger"]["high_water_mark"],
            3,
        )
        self.assertEqual(
            locator["authority"]["evidence_sequence"]["finalized_count"],
            1,
        )
        self.assertEqual(locator["assurance"], "derived_occurrence_facts_only")

    def test_locator_is_bounded_and_commits_to_complete_inventory(self) -> None:
        paths = self.initialize_approved_with_claim()
        for _ in range(ACTIVE_CONTEXT_OCCURRENCE_ITEM_LIMIT + 1):
            self.failed_run(paths)

        first = build_occurrence_locator(paths)
        second = build_occurrence_locator(paths)
        run_index = first["run_occurrences"]

        self.assertEqual(
            run_index["total_count"],
            ACTIVE_CONTEXT_OCCURRENCE_ITEM_LIMIT + 1,
        )
        self.assertEqual(
            run_index["selected_count"],
            ACTIVE_CONTEXT_OCCURRENCE_ITEM_LIMIT,
        )
        self.assertTrue(run_index["truncated"])
        self.assertEqual(
            run_index["items"][0]["run_id"],
            f"RUN-{ACTIVE_CONTEXT_OCCURRENCE_ITEM_LIMIT + 1:06d}",
        )
        self.assertEqual(first["inventory_sha256"], second["inventory_sha256"])
        self.assertEqual(
            run_index["inventory_sha256"],
            second["run_occurrences"]["inventory_sha256"],
        )

    def test_empty_representative_failures_cannot_hide_occurrence_history(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        failure = self.failed_run(paths)
        prepared_path = prepare_compaction(paths)
        prepared = load_json(prepared_path)
        plan = self.compaction_plan(
            paths,
            occurrence_inventory=self.occurrence_binding(
                prepared["occurrences"]
            ),
            name="occurrence-plan.json",
        )

        checkpoint_path = finalize_compaction(paths, plan)
        checkpoint = load_json(checkpoint_path)
        self.assertEqual(checkpoint["representative_failures"], [])

        selector = load_json(write_active_selector(paths))
        items = selector["occurrences"]["run_occurrences"]["items"]
        self.assertEqual(
            [item["run_id"] for item in items],
            [failure["run_id"]],
        )
        self.assertEqual(selector["occurrences"]["total_count"], 1)

    def test_finalize_rejects_stale_occurrence_inventory_binding(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.failed_run(paths)
        prepare_compaction(paths)
        plan = self.compaction_plan(
            paths,
            occurrence_inventory={
                "total_count": 1,
                "inventory_sha256": "f" * 64,
            },
            name="stale-occurrence-plan.json",
        )

        with self.assertRaisesRegex(
            ValidationError,
            "occurrence_inventory does not match",
        ):
            finalize_compaction(paths, plan)


if __name__ == "__main__":
    unittest.main()
