from __future__ import annotations

import sys
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.confirmation import claim_spec_sha256
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.graph_records import (
    activate_control_graph,
    create_control_graph_draft,
    create_experiment_intent_draft,
    finalize_control_graph,
    finalize_experiment_intent,
)
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
)
from tools.studyctl.models import StudyPaths, ValidationError
from tools.studyctl.observation import (
    create_observation_draft,
    finalize_observation,
)
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import errors_only, validate_study


class CognitiveControlChainTests(WorkflowTestCase):
    def intent_only_run(self, paths: StudyPaths) -> tuple[dict, dict]:
        intent_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The current Claim lacks an exact fixture observation.",
            objective="Produce one provenance-bound fixture observation.",
            requested_observations=["fixture_value"],
            evidence_requirements=[],
            claim_id="CLAIM-0001",
        )
        finalized_intent = load_json(
            finalize_experiment_intent(paths, intent_path)
        )
        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(2 + 2)"],
            purpose="Intent-only cognitive fixture",
            intent_id="INTENT-0001",
            intent_version=1,
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        return finalized_intent, manifest

    def bound_run(self, paths: StudyPaths) -> tuple[dict, dict]:
        intent_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The current Claim lacks an exact fixture observation.",
            objective="Produce one provenance-bound fixture observation.",
            requested_observations=["fixture_value"],
            evidence_requirements=[],
            claim_id="CLAIM-0001",
        )
        finalized_intent = load_json(
            finalize_experiment_intent(paths, intent_path)
        )

        graph_path = create_control_graph_draft(
            paths,
            "CG-0001",
            intent_id="INTENT-0001",
            intent_version=1,
            executor="external",
        )
        graph = load_json(graph_path)
        graph["nodes"] = [
            {
                "node_id": "agent_selected_step",
                "kind": "agent_selected_method",
                "purpose": "Produce the requested observation.",
                "command": [sys.executable, "-c", "print(2 + 2)"],
                "loop_contract": None,
                "metadata": {"topology_selected_by": "agent"},
            }
        ]
        graph["edges"] = []
        graph["completion"]["required_node_ids"] = ["agent_selected_step"]
        atomic_write_json(graph_path, graph)
        finalize_control_graph(paths, graph_path)
        activate_control_graph(paths, "CG-0001", 1)

        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(2 + 2)"],
            purpose="cognitive-control binding fixture",
            control_node_id="agent_selected_step",
            cohort_id="COHORT-001",
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        return finalized_intent, manifest

    def test_observation_and_evidence_inherit_exact_run_intent_binding(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        intent, manifest = self.bound_run(paths)
        expected_ref = {
            "intent_id": intent["intent_id"],
            "version": intent["version"],
            "sha256": intent["record_sha256"],
        }

        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["independent_review"],
        )
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        observation = load_json(observation_path)
        evidence = load_json(evidence_path)
        claim = load_json(paths.claims)["claims"][0]

        self.assertEqual(observation["intent_refs"], [expected_ref])
        self.assertEqual(evidence["intent_refs"], [expected_ref])
        self.assertEqual(
            evidence["addresses"]["claim_spec_sha256"],
            claim_spec_sha256(claim),
        )

    def test_intent_only_run_propagates_to_observation_and_evidence(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        intent, manifest = self.intent_only_run(paths)
        expected_ref = {
            "intent_id": intent["intent_id"],
            "version": intent["version"],
            "sha256": intent["record_sha256"],
        }

        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["independent_review"],
        )
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )

        self.assertIsNone(manifest["control_binding"])
        self.assertEqual(manifest["intent_binding"], expected_ref)
        self.assertEqual(
            load_json(observation_path)["intent_refs"], [expected_ref]
        )
        self.assertEqual(
            load_json(evidence_path)["intent_refs"], [expected_ref]
        )

    def test_unplanned_exploration_remains_valid_and_explicit(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)

        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["independent_review"],
        )
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )

        self.assertIsNone(manifest["control_binding"])
        self.assertIsNone(manifest["intent_binding"])
        self.assertEqual(load_json(observation_path)["intent_refs"], [])
        self.assertEqual(load_json(evidence_path)["intent_refs"], [])

    def test_evidence_rejects_rewritten_run_intent_binding(self) -> None:
        paths = self.initialize_approved_with_claim()
        _, manifest = self.bound_run(paths)
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        evidence = load_json(evidence_path)
        evidence["intent_refs"][0]["sha256"] = "0" * 64
        atomic_write_json(evidence_path, evidence)

        with self.assertRaisesRegex(
            ValidationError,
            "intent_refs do not exactly match",
        ):
            finalize_evidence(paths, evidence_path)

    def test_observation_rejects_forged_independent_intent_digest(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        _, manifest = self.intent_only_run(paths)
        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["independent_review"],
        )
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        forged = load_json(manifest_path)
        forged["intent_binding"]["sha256"] = "0" * 64
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, forged, mode=0o444)
        observation = load_json(observation_path)
        observation["runs"][0]["manifest_sha256"] = forged["integrity"][
            "manifest_sha256"
        ]
        atomic_write_json(observation_path, observation)

        with self.assertRaisesRegex(
            ValidationError,
            "intent_binding digest",
        ):
            finalize_observation(paths, observation_path)

    def test_observation_rejects_semantically_forged_run_control_binding(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        _, manifest = self.bound_run(paths)
        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["independent_review"],
        )
        manifest_path = (
            paths.runs / manifest["run_id"] / "manifest.json"
        )
        forged = load_json(manifest_path)
        forged["control_binding"]["node_spec_sha256"] = "0" * 64
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, forged, mode=0o444)
        observation = load_json(observation_path)
        observation["runs"][0]["manifest_sha256"] = forged["integrity"][
            "manifest_sha256"
        ]
        atomic_write_json(observation_path, observation)

        with self.assertRaisesRegex(
            ValidationError,
            "control_binding node digest",
        ):
            finalize_observation(paths, observation_path)

    def test_evidence_rejects_silent_claim_scope_drift(self) -> None:
        paths = self.initialize_approved_with_claim()
        _, manifest = self.bound_run(paths)
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        claims = load_json(paths.claims)
        claims["claims"][0]["scope"] = "A silently broadened scope."
        atomic_write_json(paths.claims, claims)

        with self.assertRaisesRegex(
            ValidationError,
            "Claim specification changed",
        ):
            finalize_evidence(paths, evidence_path)

    def test_finalized_unlinked_evidence_detects_later_claim_scope_drift(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        self.finalized_supporting_evidence(paths, [manifest])
        claims = load_json(paths.claims)
        claims["claims"][0]["scope"] = "A later, silently broadened scope."
        atomic_write_json(paths.claims, claims)

        messages = [
            issue.message for issue in errors_only(validate_study(paths))
        ]
        self.assertTrue(
            any("Claim specification changed" in message for message in messages),
            messages,
        )


if __name__ == "__main__":
    unittest.main()
