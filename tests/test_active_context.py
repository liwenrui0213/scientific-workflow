from __future__ import annotations

import copy
from pathlib import Path
import shutil
import sys
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import active_claims, compaction_pressure
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.hashing import atomic_write_json, load_json, sha256_file
from tools.studyctl.models import StudyPaths, ValidationError, utc_now
from tools.studyctl.rendering import render_status
from tools.studyctl.review import create_review_packet
from tools.studyctl.run_ledger import (
    empty_ledger,
    load_ledger,
    mark_registration_aborted,
    reserve_run_id,
    write_ledger,
)
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import validate_study


class ActiveContextTests(WorkflowTestCase):
    def set_pressure_thresholds(
        self, updates: dict[str, tuple[int, int]]
    ) -> None:
        policy_path = self.root / "scientific-workflow" / "policy.json"
        policy = load_json(policy_path)
        pressure = policy["active_context"]["compaction_pressure"]
        for name, (soft, hard) in updates.items():
            pressure[name] = {"soft": soft, "hard": hard}
        atomic_write_json(policy_path, policy)
        self.commit_all("configure active-context thresholds")

    def write_compaction_plan(
        self,
        paths: StudyPaths,
        *,
        name: str,
        archive_work_files: list[str] | None = None,
    ) -> Path:
        compaction_input = prepare_compaction(paths)
        state = load_json(compaction_input)
        claims = load_json(paths.claims)
        plan = {
            "schema_version": 2,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": archive_work_files or [],
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
    def errors(paths: StudyPaths) -> list[str]:
        return [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]

    def test_active_claim_and_string_size_boundaries(self) -> None:
        paths = self.initialize_approved_with_claim()
        claims = load_json(paths.claims)

        self.assertEqual(claims["claims"][0]["lifecycle"], "active")
        self.assertEqual(
            [item["claim_id"] for item in active_claims(claims)],
            ["CLAIM-0001"],
        )
        self.assertEqual(self.errors(paths), [])

        claims["claims"][0]["statement"] = "s" * 4096
        claims["frontier"]["open_questions"] = ["q" * 1024]
        atomic_write_json(paths.claims, claims)
        self.assertEqual(self.errors(paths), [])

        claims["claims"][0]["statement"] += "s"
        claims["frontier"]["open_questions"][0] += "q"
        atomic_write_json(paths.claims, claims)
        errors = self.errors(paths)
        self.assertTrue(
            any("$.claims[0].statement: string is longer than 4096" in item for item in errors),
            errors,
        )
        self.assertTrue(
            any(
                "$.frontier.open_questions[0]: string is longer than 1024" in item
                for item in errors
            ),
            errors,
        )

    def test_frontier_claim_boundary_and_lifecycle_semantics(self) -> None:
        paths = self.initialize_approved_with_claim()
        claims = load_json(paths.claims)
        prototype = claims["claims"][0]
        for number in range(2, 65):
            claim = copy.deepcopy(prototype)
            claim["claim_id"] = f"CLAIM-{number:04d}"
            claim["statement"] = f"Bounded active Claim {number}."
            claim["updated_at"] = utc_now()
            claims["claims"].append(claim)
            claims["frontier"]["claim_ids"].append(claim["claim_id"])
        atomic_write_json(paths.claims, claims)

        self.assertEqual(self.errors(paths), [])
        pressure = compaction_pressure(paths)
        active_metric = next(
            item for item in pressure["metrics"] if item["name"] == "active_claims"
        )
        self.assertEqual(active_metric["observed"], 64)
        self.assertEqual(active_metric["level"], "hard")

        extra = copy.deepcopy(prototype)
        extra["claim_id"] = "CLAIM-0065"
        extra["statement"] = "This Claim crosses the structural Frontier bound."
        claims["claims"].append(extra)
        claims["frontier"]["claim_ids"].append(extra["claim_id"])
        claims["claims"][0]["lifecycle"] = "retired"
        atomic_write_json(paths.claims, claims)
        errors = self.errors(paths)
        self.assertTrue(
            any("$.frontier.claim_ids: expected at most 64 item(s)" in item for item in errors),
            errors,
        )
        self.assertIn(
            "Frontier Claim CLAIM-0001 must have active lifecycle",
            errors,
        )

    def test_terminal_claim_pressure_can_fall_only_after_checkpoint_seal(self) -> None:
        self.set_pressure_thresholds(
            {
                "authoritative_claims": (2, 4),
                "terminal_claims": (1, 2),
            }
        )
        paths = self.initialize_approved_with_claim()
        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="active")
        self.add_proposed_claim(paths, "CLAIM-0003", lifecycle="active")
        claims = load_json(paths.claims)
        for claim in claims["claims"][1:]:
            claim["lifecycle"] = "retired"
        claims["frontier"]["claim_ids"] = ["CLAIM-0001"]
        atomic_write_json(paths.claims, claims)

        pressure = compaction_pressure(paths)
        terminal = next(
            item for item in pressure["metrics"] if item["name"] == "terminal_claims"
        )
        self.assertEqual(terminal["observed"], 2)
        self.assertEqual(terminal["level"], "hard")

        plan = self.write_compaction_plan(paths, name="terminal-claims.json")
        checkpoint = load_json(finalize_compaction(paths, plan))
        self.assertEqual(
            [item["claim_id"] for item in checkpoint["inactive_claim_refs"]],
            ["CLAIM-0002", "CLAIM-0003"],
        )
        first_record = self.root / checkpoint["inactive_claim_refs"][0]["record_path"]
        self.assertTrue(first_record.is_file())
        self.assertEqual(
            load_json(first_record)["statement"],
            "The deterministic result equals four.",
        )

        claims["claims"] = claims["claims"][:1]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        self.assertEqual(self.errors(paths), [])
        after = compaction_pressure(paths)
        observations = {item["name"]: item for item in after["metrics"]}
        self.assertEqual(observations["authoritative_claims"]["observed"], 1)
        self.assertEqual(observations["terminal_claims"]["observed"], 0)
        self.assertEqual(after["level"], "normal")

        first_record.unlink()
        errors = self.errors(paths)
        self.assertTrue(
            any("Checkpoint Claim record is missing" in item for item in errors),
            errors,
        )

    def test_pressure_status_reports_exact_soft_and_hard_boundaries(self) -> None:
        self.set_pressure_thresholds({"active_work_files": (1, 2)})
        paths = self.initialize_approved_with_claim()

        (paths.active_work / "one.txt").write_text("one\n", encoding="utf-8")
        soft = compaction_pressure(paths)
        self.assertEqual(soft["level"], "soft")
        self.assertFalse(soft["growth_blocked"])
        soft_status = render_status(paths).read_text(encoding="utf-8")
        self.assertIn("Pressure level: **SOFT**", soft_status)
        self.assertIn("plan semantic compaction before a hard growth gate", soft_status)

        (paths.active_work / "two.txt").write_text("two\n", encoding="utf-8")
        hard = compaction_pressure(paths)
        metric = next(
            item for item in hard["metrics"] if item["name"] == "active_work_files"
        )
        self.assertEqual(
            metric,
            {
                "name": "active_work_files",
                "observed": 2,
                "soft": 1,
                "hard": 2,
                "level": "hard",
            },
        )
        hard_status = render_status(paths).read_text(encoding="utf-8")
        self.assertIn("Pressure level: **HARD**", hard_status)
        self.assertIn("next Run, new Evidence, and review", hard_status)

    def test_growth_gates_and_checkpoint_watermark_recovery(self) -> None:
        self.set_pressure_thresholds(
            {
                "runs_since_checkpoint": (1, 2),
                "evidence_records_since_checkpoint": (1, 2),
                "active_work_files": (1, 2),
            }
        )
        paths = self.initialize_approved_with_claim()
        manifests = [self.successful_run(paths) for _ in range(2)]
        marker = self.root / ".objects" / "must-not-run.txt"

        with self.assertRaisesRegex(
            ValidationError, "hard threshold blocks the next Run"
        ):
            execute_run(
                paths,
                argv=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                ],
                purpose="must be blocked by compaction pressure",
            )
        self.assertFalse(marker.exists())
        self.assertEqual(len(list(paths.runs.glob("RUN-*/manifest.json"))), 2)

        first_plan = self.write_compaction_plan(paths, name="run-pressure.json")
        first_checkpoint = load_json(finalize_compaction(paths, first_plan))
        self.assertEqual(
            first_checkpoint["active_context_watermarks"],
            {
                "run_count": 2,
                "observation_record_count": 0,
                "evidence_record_count": 0,
            },
        )
        self.assertEqual(compaction_pressure(paths)["level"], "normal")
        third_manifest = self.successful_run(paths)
        self.assertEqual(third_manifest["run_id"], "RUN-000003")

        for number, manifest in enumerate(manifests, start=1):
            create_evidence_draft(
                paths,
                f"EVID-{number:04d}",
                ["CLAIM-0001"],
                [manifest["run_id"]],
            )
        with self.assertRaisesRegex(
            ValidationError, "hard threshold blocks new Evidence"
        ):
            create_evidence_draft(
                paths,
                "EVID-0003",
                ["CLAIM-0001"],
                [third_manifest["run_id"]],
            )
        self.assertFalse((paths.evidence / "EVID-0003.v0001.json").exists())

        second_plan = self.write_compaction_plan(paths, name="evidence-pressure.json")
        second_checkpoint = load_json(finalize_compaction(paths, second_plan))
        self.assertEqual(
            second_checkpoint["active_context_watermarks"],
            {
                "run_count": 3,
                "observation_record_count": 0,
                "evidence_record_count": 2,
            },
        )

        for name in ("one.txt", "two.txt"):
            (paths.active_work / name).write_text(name + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ValidationError, "hard threshold blocks scientific review"
        ):
            create_review_packet(paths)

        # Compaction itself remains available at hard pressure and archives
        # only the explicitly selected work files.
        third_plan = self.write_compaction_plan(
            paths,
            name="work-pressure.json",
            archive_work_files=["one.txt", "two.txt"],
        )
        finalize_compaction(paths, third_plan)
        self.assertEqual(compaction_pressure(paths)["level"], "normal")
        self.commit_all("freeze post-compaction independent Review scope")
        packet = load_json(create_review_packet(paths))
        self.assertEqual(packet["study_id"], paths.study_id)
        self.assertEqual(packet["evidence"], [])
        self.assertEqual(
            packet["evidence_inventory"]["total_record_count"], 2
        )
        self.assertEqual(
            packet["evidence_inventory"]["active_referenced_record_count"], 0
        )

    def test_checkpoint_is_active_only_and_compaction_loads_latest_ref(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="retired")
        claims = load_json(paths.claims)
        claims["frontier"]["claim_ids"].remove("CLAIM-0002")
        atomic_write_json(paths.claims, claims)

        first_plan = self.write_compaction_plan(paths, name="first.json")
        first = load_json(finalize_compaction(paths, first_plan))
        self.assertEqual(
            [claim["claim_id"] for claim in first["claims_snapshot"]],
            ["CLAIM-0001"],
        )
        self.assertEqual(
            first["inactive_claim_refs"][0]["claim_id"],
            "CLAIM-0002",
        )
        self.assertEqual(first["inactive_claim_refs"][0]["lifecycle"], "retired")

        second_plan = self.write_compaction_plan(paths, name="second.json")
        second = load_json(finalize_compaction(paths, second_plan))
        state = load_json(prepare_compaction(paths))
        self.assertEqual(
            state["previous_checkpoints"],
            [
                {
                    "checkpoint_id": second["checkpoint_id"],
                    "sha256": second["checkpoint_sha256"],
                }
            ],
        )
        self.assertNotIn("frontier", state["previous_checkpoints"][0])
        self.assertEqual(
            [claim["claim_id"] for claim in state["current_claims"]["items"]],
            ["CLAIM-0001"],
        )
        self.assertEqual(state["claim_inventory"]["total_count"], 2)

        claims = load_json(paths.claims)
        retired = next(
            claim for claim in claims["claims"] if claim["claim_id"] == "CLAIM-0002"
        )
        retired["lifecycle"] = "active"
        claims["frontier"]["claim_ids"].append("CLAIM-0002")
        atomic_write_json(paths.claims, claims)
        errors = self.errors(paths)
        self.assertTrue(
            any(
                "Claim CLAIM-0002 lifecycle regressed or changed from retired to active"
                in item
                for item in errors
            ),
            errors,
        )

    def test_supersession_rules_and_non_frontier_evidence_rejection(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="active")
        claims = load_json(paths.claims)
        first, second = claims["claims"]
        first["lifecycle"] = "superseded"
        first["superseded_by"] = "CLAIM-0002"
        claims["frontier"]["claim_ids"] = ["CLAIM-0002"]
        atomic_write_json(paths.claims, claims)
        self.assertEqual(self.errors(paths), [])

        manifest = self.successful_run(paths)
        with self.assertRaisesRegex(
            ValidationError, "only active Frontier Claim"
        ):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [manifest["run_id"]],
            )

        claims = load_json(paths.claims)
        claims["claims"][1]["lifecycle"] = "superseded"
        claims["claims"][1]["superseded_by"] = "CLAIM-0001"
        claims["frontier"]["claim_ids"] = []
        atomic_write_json(paths.claims, claims)
        errors = self.errors(paths)
        self.assertTrue(
            any("Claim supersession cycle includes" in item for item in errors),
            errors,
        )

    def test_invalid_pressure_policy_fails_closed(self) -> None:
        paths = self.initialize_approved_with_claim()
        policy_path = self.root / "scientific-workflow" / "policy.json"
        policy = load_json(policy_path)
        policy["active_context"]["compaction_pressure"]["active_claims"] = {
            "soft": 2,
            "hard": 2,
        }
        atomic_write_json(policy_path, policy)

        with self.assertRaisesRegex(
            ValidationError, "requires integers 0 <= soft < hard"
        ):
            compaction_pressure(paths)

    def test_aborted_run_reservation_remains_in_pressure_high_water_mark(self) -> None:
        paths = self.initialize_approved_with_claim()
        ledger = load_ledger(paths)
        self.assertIsNotNone(ledger)
        assert ledger is not None
        ledger, run_id = reserve_run_id(
            paths,
            ledger,
            {"gpu_hours": 0.0, "cpu_hours": 0.0, "storage_gb": 0.0},
        )
        mark_registration_aborted(paths, ledger, run_id)

        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])
        metric = next(
            item
            for item in compaction_pressure(paths)["metrics"]
            if item["name"] == "runs_since_checkpoint"
        )
        self.assertEqual(metric["observed"], 1)

        plan = self.write_compaction_plan(paths, name="aborted-run-pressure.json")
        checkpoint = load_json(finalize_compaction(paths, plan))
        self.assertEqual(
            checkpoint["active_context_watermarks"]["run_count"],
            1,
        )
        self.assertEqual(
            next(
                item
                for item in compaction_pressure(paths)["metrics"]
                if item["name"] == "runs_since_checkpoint"
            )["observed"],
            0,
        )

    def test_run_ledger_cannot_roll_back_below_checkpoint_watermark(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.successful_run(paths)
        plan = self.write_compaction_plan(paths, name="run-watermark.json")
        checkpoint = load_json(finalize_compaction(paths, plan))
        self.assertEqual(
            checkpoint["active_context_watermarks"]["run_count"],
            1,
        )

        shutil.rmtree(paths.runs / "RUN-000001")
        write_ledger(paths, empty_ledger(paths))

        errors = self.errors(paths)
        self.assertTrue(
            any(
                "Run ledger high_water_mark is below Checkpoint" in message
                for message in errors
            ),
            errors,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "below the latest Checkpoint watermark",
        ):
            compaction_pressure(paths)


if __name__ == "__main__":
    unittest.main()
