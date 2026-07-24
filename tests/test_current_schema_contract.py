from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.graph_records import (
    create_control_graph_draft,
    create_experiment_intent_draft,
    finalize_control_graph,
    finalize_experiment_intent,
)
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
)
from tools.studyctl.models import (
    CHECKPOINT_SCHEMA_VERSION,
    CLAIMS_SCHEMA_VERSION,
    COMPACTION_PLAN_SCHEMA_VERSION,
    CONTROL_GRAPH_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    EXPERIMENT_INTENT_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    StudyPaths,
)
from tools.studyctl.observation import create_observation_draft
from tools.studyctl.validation import object_schema_issues, validate_study


class CurrentSchemaContractTests(WorkflowTestCase):
    @staticmethod
    def _error_messages(paths: StudyPaths) -> list[str]:
        return [
            issue.message
            for issue in validate_study(paths)
            if issue.level == "ERROR"
        ]

    def _write_compaction_plan(self, paths: StudyPaths) -> Path:
        compaction_input = prepare_compaction(paths)
        prepared = load_json(compaction_input)
        claims = load_json(paths.claims)
        plan = {
            "schema_version": COMPACTION_PLAN_SCHEMA_VERSION,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": [],
            "decisive_evidence": [],
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "representative_failures": [],
            "budget_state": prepared["budget_totals"],
        }
        path = paths.work / "current-schema-compaction-plan.json"
        atomic_write_json(path, plan)
        return path

    def test_new_artifacts_emit_and_validate_against_current_schemas(self) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint_path = finalize_compaction(
            paths,
            self._write_compaction_plan(paths),
        )
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
        intent_draft_path = create_experiment_intent_draft(
            paths,
            "INTENT-0001",
            evidence_gap_id="GAP-0001",
            evidence_gap="The current-schema fixture lacks an exact observation.",
            objective="Record and assess the exact fixture value.",
            requested_observations=["fixture_value"],
            evidence_requirements=["One provenance-bound exact value."],
            claim_id="CLAIM-0001",
        )
        intent_draft = load_json(intent_draft_path)
        intent_draft["assessment_semantics"] = {
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
        intent_draft["scope"] = "The deterministic current-schema fixture."
        atomic_write_json(intent_draft_path, intent_draft)
        intent_path = finalize_experiment_intent(paths, intent_draft_path)
        plan_draft_path = create_control_graph_draft(
            paths,
            "CG-0001",
            intent_id="INTENT-0001",
            intent_version=1,
        )
        plan_draft = load_json(plan_draft_path)
        plan_draft["nodes"] = [
            {
                "node_id": "run_fixture",
                "kind": "task",
                "purpose": "Run the deterministic schema fixture.",
                "command": [sys.executable, "-c", "print(4)"],
                "loop_contract": None,
            }
        ]
        plan_draft["completion"]["required_node_ids"] = ["run_fixture"]
        atomic_write_json(plan_draft_path, plan_draft)
        control_graph_path = finalize_control_graph(paths, plan_draft_path)

        artifacts = (
            ("claims", paths.claims, CLAIMS_SCHEMA_VERSION),
            (
                "observation",
                observation_path,
                OBSERVATION_SCHEMA_VERSION,
            ),
            ("evidence", evidence_path, EVIDENCE_SCHEMA_VERSION),
            ("checkpoint", checkpoint_path, CHECKPOINT_SCHEMA_VERSION),
            (
                "experiment_intent",
                intent_path,
                EXPERIMENT_INTENT_SCHEMA_VERSION,
            ),
            ("control_graph", control_graph_path, CONTROL_GRAPH_SCHEMA_VERSION),
        )
        for name, path, expected_version in artifacts:
            with self.subTest(schema=name):
                value = load_json(path)
                self.assertEqual(value["schema_version"], expected_version)
                self.assertEqual(
                    object_schema_issues(self.root, name, path, value),
                    [],
                )

    def test_missing_malformed_and_unknown_versions_fail_closed(self) -> None:
        path = self.root / "unsupported.json"
        current_versions = {
            "claims": CLAIMS_SCHEMA_VERSION,
            "observation": OBSERVATION_SCHEMA_VERSION,
            "evidence": EVIDENCE_SCHEMA_VERSION,
            "checkpoint": CHECKPOINT_SCHEMA_VERSION,
            "experiment_intent": EXPERIMENT_INTENT_SCHEMA_VERSION,
            "control_graph": CONTROL_GRAPH_SCHEMA_VERSION,
        }
        for name, current_version in current_versions.items():
            for raw_version in (None, True, 0, current_version + 1):
                with self.subTest(schema=name, schema_version=raw_version):
                    value = (
                        {}
                        if raw_version is None
                        else {"schema_version": raw_version}
                    )
                    issues = object_schema_issues(self.root, name, path, value)
                    self.assertTrue(issues)
                    self.assertTrue(
                        all(issue.level == "ERROR" for issue in issues),
                        [issue.render() for issue in issues],
                    )
                    self.assertTrue(
                        any(
                            "schema_version" in issue.message
                            for issue in issues
                        ),
                        [issue.render() for issue in issues],
                    )

    def test_checkpoint_requires_execution_context_bindings(self) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint_path = finalize_compaction(
            paths,
            self._write_compaction_plan(paths),
        )
        checkpoint = load_json(checkpoint_path)

        for field in (
            "repository_profile",
            "host_change_scope",
            "prepared_active_work_inventory_sha256",
        ):
            with self.subTest(field=field):
                malformed = copy.deepcopy(checkpoint)
                malformed.pop(field)
                messages = [
                    issue.message
                    for issue in object_schema_issues(
                        self.root,
                        "checkpoint",
                        checkpoint_path,
                        malformed,
                    )
                ]
                self.assertTrue(
                    any(
                        f"missing required property '{field}'" in message
                        for message in messages
                    ),
                    messages,
                )

    def test_checkpoint_replays_current_graph_sequence_file_binding(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        checkpoint_path = finalize_compaction(
            paths,
            self._write_compaction_plan(paths),
        )
        original = load_json(checkpoint_path)
        mutations = (
            ("path", "studies/SC-0001/forged-sequence.json",
             "canonical Study authority"),
            ("size", original["graph_record_sequence"]["size"] + 1,
             "file binding differs"),
            ("file_sha256", "0" * 64, "file binding differs"),
        )

        for field, value, expected_message in mutations:
            with self.subTest(field=field):
                malformed = copy.deepcopy(original)
                malformed["graph_record_sequence"][field] = value
                malformed["checkpoint_sha256"] = record_digest(
                    malformed,
                    "checkpoint_sha256",
                )
                atomic_write_json(checkpoint_path, malformed, mode=0o444)
                messages = self._error_messages(paths)
                atomic_write_json(checkpoint_path, original, mode=0o444)
                self.assertTrue(
                    any(expected_message in message for message in messages),
                    messages,
                )

    def test_supersession_chain_must_end_at_active_claim(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.add_proposed_claim(paths, "CLAIM-0002", lifecycle="active")
        self.add_proposed_claim(paths, "CLAIM-0003", lifecycle="active")
        claims = load_json(paths.claims)
        first, second, third = claims["claims"]
        first["lifecycle"] = "superseded"
        first["superseded_by"] = "CLAIM-0002"
        second["lifecycle"] = "superseded"
        second["superseded_by"] = "CLAIM-0003"
        claims["frontier"]["claim_ids"] = ["CLAIM-0003"]
        atomic_write_json(paths.claims, claims)

        self.assertEqual(self._error_messages(paths), [])

        third["lifecycle"] = "retired"
        claims["frontier"]["claim_ids"] = []
        atomic_write_json(paths.claims, claims)
        errors = self._error_messages(paths)

        self.assertTrue(
            any(
                "Claim supersession chain from CLAIM-0001 must end at an active Claim; "
                "CLAIM-0003 has lifecycle 'retired'" in message
                for message in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "Claim supersession chain from CLAIM-0002 must end at an active Claim; "
                "CLAIM-0003 has lifecycle 'retired'" in message
                for message in errors
            ),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
