from __future__ import annotations

from pathlib import Path
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.evidence import create_evidence_draft
from tools.studyctl.hashing import atomic_write_json, load_json, sha256_file
from tools.studyctl.models import (
    CHECKPOINT_SCHEMA_VERSION,
    CLAIMS_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    SCHEMA_VERSION,
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
            "schema_version": SCHEMA_VERSION,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": [],
            "decisive_evidence": [],
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "open_questions": list(claims["frontier"]["open_questions"]),
            "next_actions": list(claims["frontier"]["next_actions"]),
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

        artifacts = (
            ("claims", paths.claims, CLAIMS_SCHEMA_VERSION),
            (
                "observation",
                observation_path,
                OBSERVATION_SCHEMA_VERSION,
            ),
            ("evidence", evidence_path, EVIDENCE_SCHEMA_VERSION),
            ("checkpoint", checkpoint_path, CHECKPOINT_SCHEMA_VERSION),
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
