from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import math
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.formalization import artifact_ready
from tools.studyctl.graph_records import (
    active_control_graph,
    activate_control_graph,
    control_graph_lifecycle,
    create_control_graph_draft,
    create_experiment_intent_draft,
    deactivate_control_graph,
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
from tools.studyctl.run_registry import execute_run
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
        draft["assessment_semantics"] = {
            "aggregation": "all_required",
            "criteria": [
                {
                    "criterion_id": "CRIT-001",
                    "observation": "fixture_value",
                    "operator": "eq",
                    "target": 4,
                    "unit": None,
                    "on_pass": "supports",
                    "on_fail": "contradicts",
                }
            ],
            "default_outcome": "inconclusive",
        }
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
        self.assertEqual(selector["graph_records"]["sequence"]["high_water_mark"], 3)
        self.assertEqual(
            selector["graph_records"]["sequence"]["path"],
            f"studies/{paths.study_id}/GRAPH_RECORDS.sequence.json",
        )

        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(2 + 2)"],
            purpose="explicitly PLAN-bound fixture",
            control_node_id="run_fixture",
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        plan_snapshots = [
            item
            for item in manifest["formal_artifacts"]
            if item.get("kind") == "PLAN"
        ]
        self.assertEqual(len(plan_snapshots), 1)
        self.assertEqual(plan_snapshots[0]["sha256"], sha256_file(active_path))
        # Executing and validating a control graph cannot promote a Claim.
        self.assertEqual(load_json(paths.claims)["claims"][0]["state"], "proposed")

    def test_plan_activation_and_deactivation_are_append_only(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)
        self._final_plan(paths, graph_id="CG-0002")

        self.assertEqual(
            control_graph_lifecycle(paths, "CG-0001", 1),
            {"state": "never_activated", "last_event": None},
        )
        self.assertEqual(
            control_graph_lifecycle(paths, "CG-0002", 1),
            {"state": "never_activated", "last_event": None},
        )

        activate_control_graph(paths, "CG-0001", 1)
        active_state = control_graph_lifecycle(paths, "CG-0001", 1)
        self.assertEqual(active_state["state"], "active")
        activation_path = paths.root / active_state["last_event"]["path"]
        activation = load_json(activation_path)
        self.assertEqual(activation["action"], "activated")
        self.assertEqual(activation["prior_state"], "never_activated")
        self.assertIsNone(activation["previous_event"])
        self.assertEqual(stat.S_IMODE(activation_path.stat().st_mode), 0o444)

        deactivation_path = deactivate_control_graph(
            paths,
            reason="The prospective control graph completed its useful role.",
        )
        inactive_state = control_graph_lifecycle(paths, "CG-0001", 1)
        deactivation = load_json(deactivation_path)
        self.assertEqual(inactive_state["state"], "inactive")
        self.assertEqual(deactivation["action"], "deactivated")
        self.assertEqual(deactivation["prior_state"], "active")
        self.assertEqual(
            deactivation["previous_event"]["sha256"],
            activation["record_sha256"],
        )
        self.assertFalse((paths.formal / "PLAN.json").exists())
        self.assertTrue(activation_path.is_file())
        self.assertEqual(
            load_json(paths.graph_record_sequence)["high_water_mark"],
            5,
        )
        self.assertEqual(
            control_graph_lifecycle(paths, "CG-0002", 1),
            {"state": "never_activated", "last_event": None},
        )
        self.assertEqual(
            [
                issue.render()
                for issue in validate_study(paths)
                if issue.level == "ERROR"
            ],
            [],
        )

    def test_plan_deactivation_rejects_never_active_state(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)

        with self.assertRaisesRegex(
            ValidationError, "never been activated"
        ):
            deactivate_control_graph(
                paths, reason="There is no active graph to retire."
            )

        self.assertEqual(
            control_graph_lifecycle(paths, "CG-0001", 1)["state"],
            "never_activated",
        )

    def test_plan_lifecycle_event_deletion_is_detected(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)
        activate_control_graph(paths, "CG-0001", 1)
        state = control_graph_lifecycle(paths, "CG-0001", 1)
        event_path = paths.root / state["last_event"]["path"]
        payload = event_path.read_bytes()

        event_path.unlink()

        messages = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("monotone sequence count" in message for message in messages),
            messages,
        )

        event_path.write_bytes(payload)
        event_path.chmod(0o444)
        self.assertEqual(
            [
                issue.render()
                for issue in validate_study(paths)
                if issue.level == "ERROR"
            ],
            [],
        )

    def test_stale_active_plan_can_be_explicitly_deactivated(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._final_intent(paths)
        self._final_plan(paths)
        activate_control_graph(paths, "CG-0001", 1)
        self._final_intent(
            paths,
            objective="Supersede the Intent while preserving PLAN history.",
        )

        with self.assertRaisesRegex(ValidationError, "superseded"):
            active_control_graph(paths)
        validation_messages = [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]
        self.assertTrue(
            any("superseded" in message for message in validation_messages),
            validation_messages,
        )
        invalid_stdout = io.StringIO()
        invalid_stderr = io.StringIO()
        with redirect_stdout(invalid_stdout), redirect_stderr(invalid_stderr):
            self.assertEqual(
                studyctl_main(
                    ["--root", str(self.root), "context", paths.study_id]
                ),
                2,
            )
        self.assertEqual(invalid_stdout.getvalue(), "")
        self.assertIn("superseded", invalid_stderr.getvalue())

        event_path = deactivate_control_graph(
            paths,
            reason="The realized Intent was superseded.",
        )

        self.assertEqual(load_json(event_path)["action"], "deactivated")
        self.assertEqual(
            control_graph_lifecycle(paths, "CG-0001", 1)["state"],
            "inactive",
        )
        self.assertFalse((paths.formal / "PLAN.json").exists())
        self.assertEqual(
            [
                issue.render()
                for issue in validate_study(paths)
                if issue.level == "ERROR"
            ],
            [],
        )
        valid_stdout = io.StringIO()
        valid_stderr = io.StringIO()
        with redirect_stdout(valid_stdout), redirect_stderr(valid_stderr):
            self.assertEqual(
                studyctl_main(
                    ["--root", str(self.root), "context", paths.study_id]
                ),
                0,
            )
        self.assertEqual(valid_stderr.getvalue(), "")
        selector = load_json(Path(valid_stdout.getvalue().strip()))
        graph = selector["graph_records"]["control_graphs"]["items"][0]
        self.assertEqual(graph["lifecycle"]["state"], "inactive")

    def test_corrupt_materialized_plan_does_not_block_lifecycle_deactivation(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        intent_path = self._final_intent(paths)
        graph_path = self._final_plan(paths)
        intent = load_json(intent_path)
        graph = load_json(graph_path)
        activate_control_graph(paths, "CG-0001", 1)
        plan_path = paths.formal / "PLAN.json"
        plan_path.chmod(0o644)
        plan_path.write_text("{not-json}\n", encoding="utf-8")

        event_path = deactivate_control_graph(
            paths,
            reason=(
                "Retire the sealed active graph after detecting a corrupted "
                "materialized PLAN pointer."
            ),
        )

        self.assertFalse(plan_path.exists())
        event = load_json(event_path)
        self.assertEqual(event["action"], "deactivated")
        self.assertEqual(event["plan_ref"]["sha256"], graph["record_sha256"])
        self.assertEqual(
            event["intent_ref"]["sha256"], intent["record_sha256"]
        )
        self.assertIsNone(active_control_graph(paths))
        self.assertEqual(
            [
                issue.render()
                for issue in validate_study(paths)
                if issue.level == "ERROR"
            ],
            [],
        )

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
        draft["assessment_semantics"] = {
            "aggregation": "all_required",
            "criteria": [
                {
                    "criterion_id": "CRIT-001",
                    "observation": "fixture_value",
                    "operator": "eq",
                    "target": 4,
                    "unit": None,
                    "on_pass": "supports",
                    "on_fail": "contradicts",
                }
            ],
            "default_outcome": "inconclusive",
        }
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

    def test_minimal_intent_can_finalize_without_premature_claim_semantics(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        draft_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The exploratory behavior has not been observed.",
            objective="Observe the behavior without pre-committing a Claim assessment.",
            requested_observations=["exploratory_behavior"],
        )

        finalized = load_json(finalize_experiment_intent(paths, draft_path))

        self.assertEqual(finalized["evidence_requirements"], [])
        self.assertIsNone(finalized["assessment_semantics"])
        self.assertIsNone(finalized["scope"])
        self.assertIsNone(finalized["addresses"]["target_claim"])
        self.assertEqual(finalized["status"], "finalized")

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
        draft["assessment_semantics"] = {
            "aggregation": "all_required",
            "criteria": [
                {
                    "criterion_id": "CRIT-001",
                    "observation": "unrequested_metric",
                    "operator": "eq",
                    "target": 4,
                    "unit": None,
                    "on_pass": "supports",
                    "on_fail": "contradicts",
                }
            ],
            "default_outcome": "inconclusive",
        }
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
        draft["assessment_semantics"] = {
            "aggregation": "all_required",
            "criteria": [
                {
                    "criterion_id": "CRIT-001",
                    "observation": "fixture_value",
                    "operator": "gte",
                    "target": "four",
                    "unit": None,
                    "on_pass": "supports",
                    "on_fail": "contradicts",
                }
            ],
            "default_outcome": "inconclusive",
        }
        draft["scope"] = "The exact deterministic fixture and approved Brief."
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(
            ValidationError, "(?i)ordering.*numeric"
        ):
            finalize_experiment_intent(paths, draft_path)
        self.assertFalse(
            (paths.experiment_intents / "INTENT-0001.v0001.json").exists()
        )

    def test_control_graph_accepts_agent_defined_cycle_but_rejects_nonfinite_budget(
        self,
    ) -> None:
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
                "kind": "agent.research_step",
                "purpose": "Generate one implementation or diagnosis candidate.",
                "specification": {"strategy": "agent_defined"},
            },
            {
                "node_id": "second",
                "kind": "diagnostic_probe",
                "purpose": "Evaluate whether the candidate reduces uncertainty.",
                "specification": {"measure": "information_gain"},
            },
        ]
        draft["edges"] = [
            {
                "from": "first",
                "to": "second",
                "condition": "candidate_available",
            },
            {
                "from": "second",
                "to": "first",
                "relation": "revisits",
                "condition": {
                    "when": "important uncertainty remains",
                    "policy": "agent_defined",
                },
            },
        ]
        draft["completion"]["required_node_ids"] = ["second"]
        atomic_write_json(draft_path, draft)

        final_path = finalize_control_graph(paths, draft_path)

        self.assertTrue(final_path.is_file())
        self.assertEqual(load_json(final_path)["nodes"][0]["kind"], "agent.research_step")
        self.assertEqual(
            [issue for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

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

    def test_generic_plan_does_not_capture_or_block_an_ordinary_run(self) -> None:
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
        manifest = self.successful_run(paths)
        self.assertIsNone(manifest["control_binding"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertFalse(
            any(
                item.get("kind") == "PLAN"
                for item in manifest["formal_artifacts"]
            )
        )


if __name__ == "__main__":
    unittest.main()
