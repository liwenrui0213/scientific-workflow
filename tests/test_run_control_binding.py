from __future__ import annotations

import copy
import sys
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.graph_records import (
    activate_control_graph,
    create_control_graph_draft,
    create_experiment_intent_draft,
    deactivate_control_graph,
    finalize_control_graph,
    finalize_experiment_intent,
)
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    sha256_json,
)
from tools.studyctl.models import StudyPaths, ValidationError
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import (
    errors_only,
    object_schema_issues,
    run_dependency_integrity_issues,
    sealed_run_evidence_eligible,
    validate_study,
)


class RunControlBindingTests(WorkflowTestCase):
    def _finalize_intent(
        self,
        paths: StudyPaths,
        *,
        intent_id: str = "INTENT-0001",
    ) -> dict[str, object]:
        intent_draft_path = create_experiment_intent_draft(
            paths,
            intent_id,
            evidence_gap_id="GAP-0001",
            evidence_gap="No exact recorded observation tests the current Claim.",
            objective="Record the deterministic fixture result.",
            requested_observations=["fixture_value"],
            evidence_requirements=["A provenance-bound exact fixture value."],
            claim_id="CLAIM-0001",
        )
        intent_draft = load_json(intent_draft_path)
        intent_draft["scope"] = "The exact deterministic fixture."
        atomic_write_json(intent_draft_path, intent_draft)
        return load_json(
            finalize_experiment_intent(paths, intent_draft_path)
        )

    def _activate_plan(
        self,
        paths: StudyPaths,
        *,
        declare_command: bool = True,
    ) -> dict[str, object]:
        self._finalize_intent(paths)

        graph_draft_path = create_control_graph_draft(
            paths,
            "CG-0001",
            intent_id="INTENT-0001",
            intent_version=1,
            executor="external",
        )
        graph_draft = load_json(graph_draft_path)
        node = {
            "node_id": "run_fixture",
            "kind": "task",
            "purpose": "Run the deterministic fixture.",
            "loop_contract": None,
            "metadata": {"owner": "external-agent"},
        }
        if declare_command:
            node["command"] = [sys.executable, "-c", "print(2 + 2)"]
        graph_draft["nodes"] = [node]
        graph_draft["edges"] = []
        graph_draft["completion"]["required_node_ids"] = ["run_fixture"]
        atomic_write_json(graph_draft_path, graph_draft)
        finalize_control_graph(paths, graph_draft_path)
        active_path = activate_control_graph(paths, "CG-0001", 1)
        return load_json(active_path)

    def _bound_run(
        self,
        paths: StudyPaths,
        *,
        control_node_id: str,
    ) -> dict[str, object]:
        return execute_run(
            paths,
            argv=[sys.executable, "-c", "print(2 + 2)"],
            purpose="control-binding fixture",
            control_node_id=control_node_id,
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def test_cli_plan_node_binds_exact_active_graph_intent_and_node(self) -> None:
        paths = self.initialize_approved_with_claim()
        active = self._activate_plan(paths)

        exit_code = studyctl_main(
            [
                "--root",
                str(self.root),
                "run",
                paths.study_id,
                "--purpose",
                "CLI control-binding fixture",
                "--plan-node",
                "run_fixture",
                "--hardware-class",
                "test-cpu",
                "--precision",
                "exact-integer",
                "--",
                sys.executable,
                "-c",
                "print(2 + 2)",
            ]
        )

        self.assertEqual(exit_code, 0)
        manifest_path = paths.runs / "RUN-000001" / "manifest.json"
        manifest = load_json(manifest_path)
        node = active["nodes"][0]
        self.assertEqual(manifest["schema_version"], 5)
        self.assertEqual(
            manifest["control_binding"],
            {
                "control_graph_id": active["control_graph_id"],
                "version": active["version"],
                "sha256": active["record_sha256"],
                "intent_ref": active["realizes_intent"],
                "node_id": "run_fixture",
                "node_spec_sha256": sha256_json(node),
            },
        )
        self.assertEqual(
            manifest["intent_binding"], active["realizes_intent"]
        )
        self.assertEqual(
            [
                item["kind"]
                for item in manifest["formal_artifacts"]
                if item.get("kind") == "PLAN"
            ],
            ["PLAN"],
        )
        self.assertEqual(
            object_schema_issues(
                self.root, "run", manifest_path, manifest
            ),
            [],
        )

    def test_unknown_plan_node_is_rejected_before_run_registration(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)

        with self.assertRaisesRegex(
            ValidationError, "Control Graph node does not exist"
        ):
            self._bound_run(paths, control_node_id="missing_node")

        self.assertEqual(list(paths.runs.glob("RUN-*")), [])
        ledger = load_json(paths.study / "RUNS.ledger.json")
        self.assertEqual(ledger["high_water_mark"], 0)
        self.assertEqual(ledger["runs"], {})

    def test_declared_node_command_mismatch_is_rejected_before_registration(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)

        with self.assertRaisesRegex(
            ValidationError, "argv does not exactly match"
        ):
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(5)"],
                purpose="mismatched control-binding fixture",
                control_node_id="run_fixture",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        ledger = load_json(paths.study / "RUNS.ledger.json")
        self.assertEqual(ledger["high_water_mark"], 0)
        self.assertEqual(ledger["runs"], {})

    def test_opaque_node_without_command_accepts_explicit_run_argv(self) -> None:
        paths = self.initialize_approved_with_claim()
        active = self._activate_plan(paths, declare_command=False)

        manifest = execute_run(
            paths,
            argv=[sys.executable, "-c", "print(99)"],
            purpose="opaque external-node fixture",
            control_node_id="run_fixture",
            hardware_class="test-cpu",
            precision="exact-integer",
        )

        self.assertEqual(
            manifest["control_binding"]["node_spec_sha256"],
            sha256_json(active["nodes"][0]),
        )

    def test_plan_node_requires_current_active_plan(self) -> None:
        paths = self.initialize_approved_with_claim()

        with self.assertRaisesRegex(
            ValidationError, "requires an active formal/PLAN.json"
        ):
            self._bound_run(paths, control_node_id="run_fixture")

    def test_active_plan_does_not_implicitly_bind_unplanned_run(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)

        manifest = self.successful_run(paths)

        self.assertEqual(manifest["schema_version"], 5)
        self.assertIsNone(manifest["intent_binding"])
        self.assertIsNone(manifest["control_binding"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertFalse(
            any(
                item.get("kind") == "PLAN"
                for item in manifest["formal_artifacts"]
            )
        )

    def test_ordinary_run_and_evidence_never_consult_ambient_plan(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)

        with patch(
            "tools.studyctl.graph_records.active_control_graph",
            side_effect=AssertionError(
                "ordinary scientific growth must not consult ambient PLAN"
            ),
        ):
            manifest = self.successful_run(paths)
            evidence_path = create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [manifest["run_id"]],
            )
            selector = build_active_selector(paths)

        self.assertTrue(evidence_path.is_file())
        self.assertFalse(
            any(
                item.get("kind") == "PLAN"
                for item in manifest["formal_artifacts"]
            )
        )
        self.assertNotIn(
            "PLAN",
            {
                item["kind"]
                for item in selector["active_formal_artifacts"]["sources"]
            },
        )
        graph = selector["graph_records"]["control_graphs"]["items"][0]
        self.assertEqual(graph["lifecycle"]["state"], "active")

    def test_malformed_ambient_plan_does_not_block_ordinary_growth(self) -> None:
        paths = self.initialize_approved_with_claim()
        plan_path = paths.formal / "PLAN.json"
        plan_path.write_text("{not-json", encoding="utf-8")

        manifest = self.successful_run(paths)
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )

        self.assertTrue(evidence_path.is_file())
        self.assertIsNone(manifest["control_binding"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertFalse(
            any(
                item.get("kind") == "PLAN"
                for item in manifest["formal_artifacts"]
            )
        )

    def test_plan_symlink_is_ignored_only_by_ordinary_run(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        plan_path = paths.formal / "PLAN.json"
        graph_path = paths.control_graphs / "CG-0001.v0001.json"
        plan_path.unlink()
        plan_path.symlink_to(graph_path)

        manifest = self.successful_run(paths)

        self.assertIsNone(manifest["control_binding"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertFalse(
            any(
                item.get("kind") == "PLAN"
                for item in manifest["formal_artifacts"]
            )
        )
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        self.assertTrue(evidence_path.is_file())
        with self.assertRaisesRegex(
            ValidationError, "regular non-symlink file"
        ):
            self._bound_run(paths, control_node_id="run_fixture")
        self.assertEqual(
            sorted(path.name for path in paths.runs.glob("RUN-*")),
            ["RUN-000001"],
        )

    def test_plan_directory_is_ignored_only_by_ordinary_growth(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        plan_path = paths.formal / "PLAN.json"
        plan_path.unlink()
        plan_path.mkdir()
        (plan_path / "child.json").write_text(
            '{"status":"active","must_not_be_read":"ambient PLAN child"}\n',
            encoding="utf-8",
        )

        manifest = self.successful_run(paths)
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )

        self.assertTrue(evidence_path.is_file())
        self.assertIsNone(manifest["control_binding"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertFalse(
            any(
                item.get("path", "").startswith("formal/PLAN.json")
                for item in manifest["formal_artifacts"]
            )
        )
        with self.assertRaisesRegex(
            ValidationError, "regular non-symlink file"
        ):
            self._bound_run(paths, control_node_id="run_fixture")
        self.assertEqual(
            sorted(path.name for path in paths.runs.glob("RUN-*")),
            ["RUN-000001"],
        )

    def test_stale_active_plan_does_not_block_or_taint_ordinary_run(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        self._finalize_intent(paths)

        manifest = self.successful_run(paths)

        self.assertIsNone(manifest["intent_binding"])
        self.assertIsNone(manifest["control_binding"])
        self.assertTrue(manifest["change_scope"]["evidence_eligible"])
        self.assertTrue(
            manifest["formalization"]["artifacts_unchanged_during_run"]
        )
        self.assertFalse(
            any(
                item.get("kind") == "PLAN"
                for item in manifest["formal_artifacts"]
            )
        )
        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        self.assertTrue(evidence_path.is_file())

        with self.assertRaisesRegex(ValidationError, "superseded"):
            self._bound_run(paths, control_node_id="run_fixture")

    def test_deactivated_plan_cannot_bind_an_explicit_run(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        deactivate_control_graph(
            paths,
            reason="The graph is no longer selected for execution.",
        )

        with self.assertRaisesRegex(
            ValidationError, "requires an active formal/PLAN.json"
        ):
            self._bound_run(paths, control_node_id="run_fixture")

        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_cli_intent_binding_does_not_require_a_plan(self) -> None:
        paths = self.initialize_approved_with_claim()
        intent = self._finalize_intent(paths)

        exit_code = studyctl_main(
            [
                "--root",
                str(self.root),
                "run",
                paths.study_id,
                "--purpose",
                "CLI Intent-only fixture",
                "--intent",
                "INTENT-0001",
                "--intent-version",
                "1",
                "--hardware-class",
                "test-cpu",
                "--precision",
                "exact-integer",
                "--",
                sys.executable,
                "-c",
                "print(2 + 2)",
            ]
        )

        self.assertEqual(exit_code, 0)
        manifest = load_json(
            paths.runs / "RUN-000001" / "manifest.json"
        )
        self.assertEqual(
            manifest["intent_binding"],
            {
                "intent_id": intent["intent_id"],
                "version": intent["version"],
                "sha256": intent["record_sha256"],
            },
        )
        self.assertIsNone(manifest["control_binding"])

    def test_intent_binding_requires_id_and_version_together(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._finalize_intent(paths)

        with self.assertRaisesRegex(
            ValidationError,
            "intent_id and intent_version must be provided together",
        ):
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(2 + 2)"],
                purpose="incomplete Intent binding",
                intent_id="INTENT-0001",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        self.assertEqual(list(paths.runs.glob("RUN-*")), [])

    def test_explicit_intent_must_match_selected_plan(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        self._finalize_intent(paths, intent_id="INTENT-0002")

        with self.assertRaisesRegex(
            ValidationError,
            "does not match the selected Control Graph",
        ):
            execute_run(
                paths,
                argv=[sys.executable, "-c", "print(2 + 2)"],
                purpose="mismatched Intent and PLAN",
                intent_id="INTENT-0002",
                intent_version=1,
                control_node_id="run_fixture",
                hardware_class="test-cpu",
                precision="exact-integer",
            )

        ledger = load_json(paths.study / "RUNS.ledger.json")
        self.assertEqual(ledger["high_water_mark"], 0)

    def test_validation_and_evidence_replay_bound_graph_digest(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        manifest = self._bound_run(paths, control_node_id="run_fixture")
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        forged = copy.deepcopy(manifest)
        forged["control_binding"]["sha256"] = "0" * 64
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, forged, mode=0o444)

        messages = [
            issue.message for issue in errors_only(validate_study(paths))
        ]
        self.assertTrue(
            any("control_binding graph digest" in message for message in messages),
            messages,
        )
        evidence_integrity_messages = [
            issue.message
            for issue in errors_only(
                run_dependency_integrity_issues(
                    paths,
                    forged,
                    for_evidence=True,
                )
            )
        ]
        self.assertTrue(
            any(
                "control_binding graph digest" in message
                for message in evidence_integrity_messages
            ),
            evidence_integrity_messages,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "Run ledger entry|control_binding graph digest",
        ):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [manifest["run_id"]],
            )

    def test_validation_replays_bound_node_digest(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        manifest = self._bound_run(paths, control_node_id="run_fixture")
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        forged = copy.deepcopy(manifest)
        forged["control_binding"]["node_spec_sha256"] = "0" * 64
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, forged, mode=0o444)

        messages = [
            issue.message for issue in errors_only(validate_study(paths))
        ]
        self.assertTrue(
            any("control_binding node digest" in message for message in messages),
            messages,
        )

    def test_validation_replays_bound_node_command(self) -> None:
        paths = self.initialize_approved_with_claim()
        self._activate_plan(paths)
        manifest = self._bound_run(paths, control_node_id="run_fixture")
        manifest_path = paths.runs / manifest["run_id"] / "manifest.json"
        forged = copy.deepcopy(manifest)
        forged["execution"]["argv"] = [
            sys.executable,
            "-c",
            "print(5)",
        ]
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, forged, mode=0o444)

        messages = [
            issue.message for issue in errors_only(validate_study(paths))
        ]
        self.assertTrue(
            any("bound Control Graph node command" in message for message in messages),
            messages,
        )

    def test_frozen_v4_run_projects_only_a_null_control_binding(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        manifest_path = (
            paths.runs / str(manifest["run_id"]) / "manifest.json"
        )
        legacy = copy.deepcopy(manifest)
        legacy["schema_version"] = 4
        legacy.pop("intent_binding")
        legacy.pop("control_binding")
        legacy["integrity"]["manifest_sha256"] = nested_record_digest(
            legacy,
            "integrity",
            "manifest_sha256",
        )

        self.assertEqual(
            errors_only(
                object_schema_issues(
                    self.root, "run", manifest_path, legacy
                )
            ),
            [],
        )
        self.assertTrue(sealed_run_evidence_eligible(legacy))

        forged = copy.deepcopy(legacy)
        forged["control_binding"] = None
        messages = [
            issue.message
            for issue in errors_only(
                object_schema_issues(
                    self.root, "run", manifest_path, forged
                )
            )
        ]
        self.assertIn(
            "$: additional property is not allowed: 'control_binding'",
            messages,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
