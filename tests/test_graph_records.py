from __future__ import annotations

import math
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.formalization import artifact_ready
from tools.studyctl.graph_records import (
    activate_control_graph,
    create_control_graph_draft,
    create_experiment_intent_draft,
    finalize_control_graph,
    finalize_experiment_intent,
    recover_graph_record_sequence,
)
from tools.studyctl.hashing import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    load_json,
    sha256_file,
)
from tools.studyctl.models import ValidationError
from tools.studyctl.validation import validate_study


class GraphRecordTests(WorkflowTestCase):
    def _final_intent(
        self,
        paths,
        *,
        intent_id: str = "INTENT-0001",
        objective: str = "Determine whether the exact fixture result equals four.",
    ) -> Path:
        draft_path = create_experiment_intent_draft(
            paths,
            intent_id,
            evidence_gap_id="GAP-0001",
            evidence_gap="No exact recorded observation tests the current Claim.",
            objective=objective,
            requested_observations=["fixture_value"],
            evidence_requirements=["A provenance-bound exact fixture value."],
            claim_id="CLAIM-0001",
        )
        draft = load_json(draft_path)
        draft["assessment_semantics"]["criteria"] = [
            {
                "criterion_id": "CRIT-001",
                "observation": "fixture_value",
                "operator": "eq",
                "target": 4,
                "unit": None,
                "on_pass": "supports",
                "on_fail": "contradicts",
            }
        ]
        draft["scope"] = "The exact deterministic fixture and approved Brief."
        atomic_write_json(draft_path, draft)
        return finalize_experiment_intent(paths, draft_path)

    def _final_plan(
        self,
        paths,
        *,
        graph_id: str = "CG-0001",
        intent_version: int = 1,
        single_node: bool = False,
    ) -> Path:
        draft_path = create_control_graph_draft(
            paths,
            graph_id,
            intent_id="INTENT-0001",
            intent_version=intent_version,
            executor="studyctl",
            cpu_hours=0.25,
            parallel_workers=1,
        )
        draft = load_json(draft_path)
        run_node = {
            "node_id": "run_fixture",
            "kind": "task",
            "purpose": "Run the deterministic fixture.",
            "command": [sys.executable, "-c", "print(2 + 2)"],
            "loop_contract": None,
        }
        if single_node:
            draft["nodes"] = [run_node]
            draft["edges"] = []
            draft["completion"]["required_node_ids"] = ["run_fixture"]
        else:
            validate_node = {
                "node_id": "validate_value",
                "kind": "validator",
                "purpose": "Validate the exact fixture value.",
                "command": [sys.executable, "-c", "assert 2 + 2 == 4"],
                "loop_contract": None,
            }
            draft["nodes"] = [run_node, validate_node]
            draft["edges"] = [
                {
                    "from": "run_fixture",
                    "to": "validate_value",
                    "condition": "on_success",
                }
            ]
            draft["completion"]["required_node_ids"] = ["validate_value"]
        atomic_write_json(draft_path, draft)
        return finalize_control_graph(paths, draft_path)

    def test_intent_plan_activation_and_run_snapshot_are_exact(self) -> None:
        paths = self.initialize_approved_with_claim()
        intent_path = self._final_intent(paths)
        plan_path = self._final_plan(paths)

        active_path = activate_control_graph(paths, "CG-0001", 1)

        self.assertEqual(active_path.read_bytes(), plan_path.read_bytes())
        self.assertEqual(stat.S_IMODE(intent_path.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(plan_path.stat().st_mode), 0o444)
        self.assertTrue(artifact_ready(paths, "PLAN"))
        self.assertEqual(
            [issue.render() for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

        selector = build_active_selector(paths)
        intents = selector["graph_records"]["experiment_intents"]
        plans = selector["graph_records"]["control_graphs"]
        self.assertEqual(intents["total_count"], 1)
        self.assertEqual(plans["total_count"], 1)
        self.assertEqual(
            plans["items"][0]["realizes_intent"]["sha256"],
            intents["items"][0]["sha256"],
        )
        self.assertEqual(selector["graph_records"]["sequence"]["high_water_mark"], 2)
        self.assertEqual(
            selector["graph_records"]["sequence"]["path"],
            f"studies/{paths.study_id}/GRAPH_RECORDS.sequence.json",
        )

        manifest = self.successful_run(paths)
        plan_snapshots = [
            item
            for item in manifest["formal_artifacts"]
            if item.get("kind") == "PLAN"
        ]
        self.assertEqual(len(plan_snapshots), 1)
        self.assertEqual(plan_snapshots[0]["sha256"], sha256_file(active_path))
        # Executing and validating a control graph cannot promote a Claim.
        self.assertEqual(load_json(paths.claims)["claims"][0]["state"], "proposed")

    def test_native_graph_sequence_detects_tail_and_whole_family_deletion(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        first = self._final_intent(paths)
        second = self._final_intent(
            paths,
            objective="Create a second version whose tail must remain visible.",
        )
        sequence = load_json(paths.graph_record_sequence)
        self.assertEqual(sequence["high_water_mark"], 2)

        second_bytes = second.read_bytes()
        second.unlink()
        with self.assertRaisesRegex(ValidationError, "monotone sequence count"):
            build_active_selector(paths)
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("monotone sequence count" in message for message in errors),
            errors,
        )

        second.write_bytes(second_bytes)
        second.chmod(0o444)
        first_bytes = first.read_bytes()
        first.unlink()
        second.unlink()
        with self.assertRaisesRegex(ValidationError, "monotone sequence count"):
            create_experiment_intent_draft(
                paths,
                "INTENT-0001",
                evidence_gap_id="GAP-0001",
                evidence_gap="A deleted family must not be recreated.",
                objective="This operation must fail closed.",
                requested_observations=["fixture_value"],
                evidence_requirements=["An exact value."],
                claim_id="CLAIM-0001",
            )
        first.write_bytes(first_bytes)
        first.chmod(0o444)
        second.write_bytes(second_bytes)
        second.chmod(0o444)
        self.assertEqual(
            [issue for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

    def test_missing_graph_sequence_fails_closed_without_legacy_reconstruction(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        paths.graph_record_sequence.unlink()

        with self.assertRaisesRegex(ValidationError, "sequence is missing"):
            build_active_selector(paths)
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("sequence is missing" in message for message in errors),
            errors,
        )

    def test_graph_sequence_binds_exact_finalized_file_bytes(self) -> None:
        paths = self.initialize_approved_with_claim()
        intent_path = self._final_intent(paths)
        original_hash = sha256_file(intent_path)
        value = load_json(intent_path)
        atomic_write_bytes(
            intent_path,
            canonical_json_bytes(value),
            mode=0o444,
            require_parent_fsync=True,
        )
        self.assertNotEqual(sha256_file(intent_path), original_hash)
        self.assertEqual(
            value["record_sha256"],
            load_json(intent_path)["record_sha256"],
        )

        with self.assertRaisesRegex(ValidationError, "sequence inventory"):
            build_active_selector(paths)

    def test_interrupted_sequence_advance_requires_forward_only_recovery(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        draft_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="No exact value has been recorded.",
            objective="Record the exact deterministic value.",
            requested_observations=["fixture_value"],
            evidence_requirements=["A provenance-bound exact value."],
            claim_id="CLAIM-0001",
        )
        draft = load_json(draft_path)
        draft["assessment_semantics"]["criteria"] = [
            {
                "criterion_id": "CRIT-001",
                "observation": "fixture_value",
                "operator": "eq",
                "target": 4,
                "unit": None,
                "on_pass": "supports",
                "on_fail": "contradicts",
            }
        ]
        draft["scope"] = "The exact deterministic fixture and approved Brief."
        atomic_write_json(draft_path, draft)

        with patch(
            "tools.studyctl.graph_record_sequence.write_graph_record_sequence",
            side_effect=OSError("simulated sequence publication failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated"):
                finalize_experiment_intent(paths, draft_path)

        final_path = paths.experiment_intents / "INTENT-0001.v0001.json"
        self.assertTrue(final_path.is_file())
        self.assertEqual(
            load_json(paths.graph_record_sequence)["high_water_mark"],
            0,
        )
        with self.assertRaisesRegex(ValidationError, "monotone sequence count"):
            build_active_selector(paths)

        recover_graph_record_sequence(paths)
        self.assertEqual(
            load_json(paths.graph_record_sequence)["high_water_mark"],
            1,
        )
        self.assertEqual(
            [issue for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

    def test_single_node_graph_is_valid_boundary_case(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        plan_path = self._final_plan(paths, single_node=True)

        plan = load_json(plan_path)
        self.assertEqual(plan["edges"], [])
        self.assertEqual(plan["completion"]["required_node_ids"], ["run_fixture"])
        self.assertEqual(
            [issue.render() for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

    def test_new_intent_version_makes_old_active_plan_stale(self) -> None:
        paths = self.initialize_approved_with_claim()
        first_intent = self._final_intent(paths)
        self._final_plan(paths)
        activate_control_graph(paths, "CG-0001", 1)

        second_intent = self._final_intent(
            paths,
            objective="Replicate the exact fixture result under the same approved scope.",
        )
        second = load_json(second_intent)
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            second["previous_ref"]["sha256"],
            load_json(first_intent)["record_sha256"],
        )
        intent_index = build_active_selector(paths)["graph_records"][
            "experiment_intents"
        ]
        self.assertEqual(intent_index["total_count"], 2)
        self.assertEqual(intent_index["current_count"], 1)
        self.assertEqual(intent_index["items"][0]["version"], 2)
        self.assertFalse(artifact_ready(paths, "PLAN"))
        with self.assertRaisesRegex(ValidationError, "superseded"):
            activate_control_graph(paths, "CG-0001", 1)
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("superseded" in message for message in errors),
            errors,
        )

    def test_target_claim_leaving_frontier_makes_active_plan_stale(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)
        activate_control_graph(paths, "CG-0001", 1)

        claims = load_json(paths.claims)
        claims["frontier"]["claim_ids"] = []
        atomic_write_json(paths.claims, claims)

        self.assertFalse(artifact_ready(paths, "PLAN"))
        with self.assertRaisesRegex(ValidationError, "active Frontier Claim"):
            activate_control_graph(paths, "CG-0001", 1)
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("active Frontier Claim" in message for message in errors),
            errors,
        )

    def test_missing_prior_version_blocks_projection_and_new_lineage(self) -> None:
        paths = self.initialize_approved_with_claim()
        first_intent = self._final_intent(paths)
        self._final_intent(
            paths,
            objective="Create a second exact Intent version for lineage testing.",
        )
        first_intent.unlink()

        with self.assertRaisesRegex(ValidationError, "not contiguous from v1"):
            build_active_selector(paths)
        (
            paths.active_work
            / "INTENT-0001.v0001.experiment-intent.draft.json"
        ).unlink()
        with self.assertRaisesRegex(ValidationError, "not contiguous from v1"):
            create_experiment_intent_draft(
                paths,
                "INTENT-0001",
                evidence_gap_id="GAP-0001",
                evidence_gap="The prior version is missing.",
                objective="This draft must not be created.",
                requested_observations=["fixture_value"],
                evidence_requirements=["An exact value."],
                claim_id="CLAIM-0001",
            )

    def test_intent_rejects_criterion_for_unrequested_observation(self) -> None:
        paths = self.initialize_approved_with_claim()
        draft_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The exact value has not been recorded.",
            objective="Record the exact value.",
            requested_observations=["fixture_value"],
            evidence_requirements=["An exact recorded value."],
            claim_id="CLAIM-0001",
        )
        draft = load_json(draft_path)
        draft["assessment_semantics"]["criteria"] = [
            {
                "criterion_id": "CRIT-001",
                "observation": "unrequested_metric",
                "operator": "eq",
                "target": 4,
                "unit": None,
                "on_pass": "supports",
                "on_fail": "contradicts",
            }
        ]
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(
            ValidationError, "must name a requested observation"
        ):
            finalize_experiment_intent(paths, draft_path)
        self.assertFalse(
            (paths.experiment_intents / "INTENT-0001.v0001.json").exists()
        )

    def test_intent_finalization_rejects_linked_draft_sources(self) -> None:
        paths = self.initialize_approved_with_claim()
        draft_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The exact value has not been recorded.",
            objective="Record the exact value.",
            requested_observations=["fixture_value"],
            evidence_requirements=["An exact recorded value."],
            claim_id="CLAIM-0001",
        )
        payload_path = draft_path.with_name("intent-payload.json")
        draft_path.rename(payload_path)
        draft_path.symlink_to(payload_path.name)

        with self.assertRaisesRegex(ValidationError, "symbolic links"):
            finalize_experiment_intent(paths, draft_path)

        draft_path.unlink()
        draft_path.hardlink_to(payload_path)
        with self.assertRaisesRegex(ValidationError, "must not be hard-linked"):
            finalize_experiment_intent(paths, draft_path)

    def test_intent_rejects_nonnumeric_ordering_target(self) -> None:
        paths = self.initialize_approved_with_claim()
        draft_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The exact value has not been ordered against a threshold.",
            objective="Compare the exact value with a numeric threshold.",
            requested_observations=["fixture_value"],
            evidence_requirements=["An exact recorded value."],
            claim_id="CLAIM-0001",
        )
        draft = load_json(draft_path)
        draft["assessment_semantics"]["criteria"] = [
            {
                "criterion_id": "CRIT-001",
                "observation": "fixture_value",
                "operator": "gte",
                "target": "four",
                "unit": None,
                "on_pass": "supports",
                "on_fail": "contradicts",
            }
        ]
        draft["scope"] = "The exact deterministic fixture and approved Brief."
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(
            ValidationError, "(?i)ordering.*numeric"
        ):
            finalize_experiment_intent(paths, draft_path)
        self.assertFalse(
            (paths.experiment_intents / "INTENT-0001.v0001.json").exists()
        )

    def test_control_graph_rejects_cycle_and_nonfinite_budget(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        with self.assertRaisesRegex(ValidationError, "finite non-negative"):
            create_control_graph_draft(
                paths,
                "CG-0002",
                intent_id="INTENT-0001",
                intent_version=1,
                cpu_hours=math.nan,
            )

        draft_path = create_control_graph_draft(
            paths,
            "CG-0001",
            intent_id="INTENT-0001",
            intent_version=1,
        )
        draft = load_json(draft_path)
        draft["nodes"] = [
            {
                "node_id": "first",
                "kind": "task",
                "purpose": "First task.",
                "command": [sys.executable, "-c", "print(1)"],
                "loop_contract": None,
            },
            {
                "node_id": "second",
                "kind": "validator",
                "purpose": "Second task.",
                "command": [sys.executable, "-c", "print(2)"],
                "loop_contract": None,
            },
        ]
        draft["edges"] = [
            {"from": "first", "to": "second", "condition": "on_success"},
            {"from": "second", "to": "first", "condition": "on_failure"},
        ]
        draft["completion"]["required_node_ids"] = ["second"]
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(ValidationError, "contain a cycle"):
            finalize_control_graph(paths, draft_path)
        self.assertFalse((paths.control_graphs / "CG-0001.v0001.json").exists())

    def test_control_graph_rejects_whitespace_command_item(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        draft_path = create_control_graph_draft(
            paths,
            "CG-0001",
            intent_id="INTENT-0001",
            intent_version=1,
        )
        draft = load_json(draft_path)
        draft["nodes"] = [
            {
                "node_id": "run_fixture",
                "kind": "task",
                "purpose": "Run the deterministic fixture.",
                "command": [sys.executable, "-c", "   "],
                "loop_contract": None,
            }
        ]
        draft["completion"]["required_node_ids"] = ["run_fixture"]
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(
            ValidationError, "(?i)command.*non-empty"
        ):
            finalize_control_graph(paths, draft_path)
        self.assertFalse((paths.control_graphs / "CG-0001.v0001.json").exists())

    def test_active_plan_tamper_fails_closed(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)
        active_path = activate_control_graph(paths, "CG-0001", 1)
        active = load_json(active_path)
        active["executor"]["parameters"]["partition"] = "changed-after-activation"
        atomic_write_json(active_path, active, mode=0o444)

        self.assertFalse(artifact_ready(paths, "PLAN"))
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("does not exactly materialize" in message for message in errors),
            errors,
        )

    def test_active_plan_must_be_sealed_regular_single_link_file(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        finalized_plan = self._final_plan(paths)
        active_path = activate_control_graph(paths, "CG-0001", 1)

        active_path.unlink()
        active_path.symlink_to(finalized_plan)
        self.assertFalse(artifact_ready(paths, "PLAN"))

        active_path.unlink()
        active_path = activate_control_graph(paths, "CG-0001", 1)
        active_path.chmod(0o644)
        self.assertFalse(artifact_ready(paths, "PLAN"))

        active_path.unlink()
        active_path.hardlink_to(finalized_plan)
        self.assertFalse(artifact_ready(paths, "PLAN"))

    def test_active_control_graph_missing_record_type_fails_closed(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)
        active_path = activate_control_graph(paths, "CG-0001", 1)
        active = load_json(active_path)
        del active["record_type"]
        atomic_write_json(active_path, active, mode=0o444)

        self.assertFalse(artifact_ready(paths, "PLAN"))
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("record_type" in message for message in errors),
            errors,
        )

    def test_generic_plan_is_rejected_before_any_control_graph_exists(self) -> None:
        paths = self.initialize_approved_with_claim()
        generic_plan = {
            "schema_version": 1,
            "status": "active",
            "purpose": "Unstructured parallel orchestration plan.",
            "workers": ["worker-a", "worker-b"],
        }
        atomic_write_json(paths.formal / "PLAN.json", generic_plan, mode=0o444)

        self.assertFalse(artifact_ready(paths, "PLAN"))
        errors = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("activated ControlGraphSpec" in message for message in errors),
            errors,
        )
        with self.assertRaisesRegex(ValidationError, "activated ControlGraphSpec"):
            self.successful_run(paths)


if __name__ == "__main__":
    unittest.main()
