from __future__ import annotations

import sys
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

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
from tools.studyctl.models import StudyPaths, ValidationError, utc_now
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

    def test_running_attempt_is_only_marked_in_progress(self) -> None:
        paths = self.initialize_approved_with_claim()
        real_popen = subprocess.Popen
        observed: list[dict[str, object]] = []

        def observe_running(*args: object, **kwargs: object):
            manifests = sorted(paths.runs.glob("RUN-*/manifest.json"))
            if manifests and not observed:
                running = load_json(manifests[0])
                if running.get("status") == "running":
                    locator = build_occurrence_locator(paths)
                    items = locator["run_occurrences"]["items"]
                    self.assertEqual(len(items), 1)
                    observed.append(items[0])
            return real_popen(*args, **kwargs)

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            side_effect=observe_running,
        ):
            terminal = execute_run(
                paths,
                argv=[sys.executable, "-c", "print(4)"],
                purpose="running occurrence boundary fixture",
                output_paths=[".objects/not-yet-produced.txt"],
                cohort_id="COHORT-001",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["status"], "running")
        self.assertEqual(observed[0]["facts"], ["in_progress"])
        self.assertEqual(
            observed[0]["missing_declared_output_count"], 0
        )
        self.assertNotIn(
            "evidence_ineligible_attempt", observed[0]["facts"]
        )
        self.assertEqual(terminal["status"], "succeeded")
        terminal_item = build_occurrence_locator(paths)[
            "run_occurrences"
        ]["items"][0]
        self.assertEqual(
            terminal_item["facts"],
            [
                "missing_declared_output",
                "evidence_ineligible_attempt",
            ],
        )

    def test_archived_claim_disposition_remains_visible_after_prune(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        self.add_proposed_claim(
            paths, "CLAIM-0002", lifecycle="active"
        )
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(
            paths,
            [manifest],
            claim_id="CLAIM-0002",
        )

        claims = load_json(paths.claims)
        archived_claim = next(
            claim
            for claim in claims["claims"]
            if claim["claim_id"] == "CLAIM-0002"
        )
        archived_claim["lifecycle"] = "retired"
        archived_claim["state"] = "partially_supported"
        archived_claim["evidence_basis"] = "exploratory"
        archived_claim["supporting_evidence"] = [
            {
                "evidence_id": evidence["evidence_id"],
                "version": evidence["version"],
                "sha256": evidence["record_sha256"],
            }
        ]
        archived_claim["uncertainty"] = (
            "Limited to the deterministic fixture scope."
        )
        archived_claim["updated_at"] = utc_now()
        claims["frontier"]["claim_ids"] = ["CLAIM-0001"]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        prepared_path = prepare_compaction(paths)
        prepared = load_json(prepared_path)
        plan = self.compaction_plan(
            paths,
            occurrence_inventory=self.occurrence_binding(
                prepared["occurrences"]
            ),
            name="archive-disposition-plan.json",
        )
        finalize_compaction(paths, plan)

        claims = load_json(paths.claims)
        claims["claims"] = [
            claim
            for claim in claims["claims"]
            if claim["claim_id"] != "CLAIM-0002"
        ]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

        locator = build_occurrence_locator(paths)
        self.assertEqual(
            locator["finalized_undispositioned_evidence"]["items"],
            [],
        )
        archived_sources = locator["authority"][
            "archived_claim_dispositions"
        ]
        self.assertEqual(archived_sources["total_count"], 1)
        source_item = archived_sources["items"][0]
        self.assertEqual(source_item["claim_id"], "CLAIM-0002")
        self.assertEqual(source_item["dispositioned_evidence_count"], 1)
        source_path = paths.root / source_item["source"]["path"]
        self.assertTrue(source_path.is_file())
        self.assertEqual(
            source_item["source"]["sha256"], sha256_file(source_path)
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
