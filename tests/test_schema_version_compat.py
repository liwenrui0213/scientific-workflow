from __future__ import annotations

import copy
from pathlib import Path
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import compaction_pressure, write_active_selector
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.hashing import atomic_write_json, load_json, record_digest, sha256_file
from tools.studyctl.models import (
    CHECKPOINT_SCHEMA_VERSION,
    CLAIMS_SCHEMA_VERSION,
    StudyPaths,
    utc_now,
    ValidationError,
)
from tools.studyctl.rendering import render_status
from tools.studyctl.validation import (
    _checkpoint_issues,
    object_schema_issues,
    validate_study,
)


class SchemaVersionCompatibilityTests(WorkflowTestCase):
    @staticmethod
    def _claim(number: int, *, long_text: bool = False) -> dict[str, object]:
        text = "x" * 5000 if long_text else f"Legacy Claim {number}."
        return {
            "claim_id": f"CLAIM-{number:04d}",
            "statement": text,
            "scope": text,
            "state": "proposed",
            "supporting_evidence": [],
            "contradictory_evidence": [],
            "other_evidence": [],
            "uncertainty": None,
            "limitations": [],
            "updated_at": utc_now(),
        }

    def _claims_document(
        self,
        paths: StudyPaths,
        *,
        schema_version: int,
        claim_count: int,
    ) -> dict[str, object]:
        claims = [self._claim(number) for number in range(1, claim_count + 1)]
        return {
            "schema_version": schema_version,
            "study_id": paths.study_id,
            "revision": 1,
            "updated_at": utc_now(),
            "claims": claims,
            "frontier": {
                "summary": "Legacy unbounded Frontier.",
                "claim_ids": [claim["claim_id"] for claim in claims],
                "open_questions": [],
                "next_actions": [],
                "human_decisions_required": [],
            },
            "formalization_debt": [],
        }

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
            "schema_version": 1,
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
        path = paths.work / "schema-version-compaction-plan.json"
        atomic_write_json(path, plan)
        return path

    def test_frozen_v1_claims_accept_legacy_state_beyond_v2_bounds(self) -> None:
        paths = self.initialize_approved_with_claim()
        legacy = self._claims_document(paths, schema_version=1, claim_count=257)
        atomic_write_json(paths.claims, legacy)

        self.assertEqual(self._error_messages(paths), [])

        bounded = copy.deepcopy(legacy)
        bounded["schema_version"] = CLAIMS_SCHEMA_VERSION
        issues = object_schema_issues(
            self.root,
            "claims",
            paths.claims,
            bounded,
        )
        self.assertTrue(
            any("$.claims: expected at most 256 item(s)" in issue.message for issue in issues),
            [issue.render() for issue in issues],
        )
        self.assertTrue(
            any(
                "$.frontier.claim_ids: expected at most 64 item(s)" in issue.message
                for issue in issues
            ),
            [issue.render() for issue in issues],
        )

    def test_v1_claims_are_auditable_but_cannot_become_active_context(self) -> None:
        paths = self.initialize_approved_with_claim()
        legacy = self._claims_document(paths, schema_version=1, claim_count=257)
        legacy["claims"][0]["statement"] = (
            "LEGACY-CONTENT-MUST-NOT-ENTER-STATUS-" + "x" * 200_000
        )
        atomic_write_json(paths.claims, legacy)

        issues = validate_study(paths)
        self.assertFalse(any(issue.level == "ERROR" for issue in issues), issues)
        self.assertTrue(
            any(
                issue.level == "WARNING"
                and "historical-validation-only" in issue.message
                for issue in issues
            ),
            [issue.render() for issue in issues],
        )
        for operation in (
            lambda: write_active_selector(paths),
            lambda: compaction_pressure(paths),
            lambda: prepare_compaction(paths),
        ):
            with self.assertRaisesRegex(
                ValidationError,
                "requires bounded CLAIMS.json schema_version 2",
            ):
                operation()

        status_path = render_status(paths)
        status = status_path.read_text(encoding="utf-8")
        self.assertLess(status_path.stat().st_size, 4_096)
        self.assertIn("Active Context Migration Required", status)
        self.assertNotIn("LEGACY-CONTENT-MUST-NOT-ENTER-STATUS", status)

    def test_v1_checkpoint_uses_v1_claim_and_frontier_schema(self) -> None:
        paths = self.initialize_approved_with_claim()
        snapshots = [
            self._claim(number, long_text=number == 1)
            for number in range(1, 66)
        ]
        checkpoint = {
            "schema_version": 1,
            "study_id": paths.study_id,
            "checkpoint_id": "CHECKPOINT-000001",
            "created_at": utc_now(),
            "brief": {"sha256": "a" * 64, "approval_sha256": "b" * 64},
            "active_formal_artifacts": [],
            "claims_file_sha256": "c" * 64,
            "claims_snapshot": snapshots,
            "frontier": {
                "summary": "f" * 5000,
                "claim_ids": [claim["claim_id"] for claim in snapshots],
                "open_questions": ["q" * 5000],
                "next_actions": [],
                "human_decisions_required": [],
            },
            "decisive_evidence": [],
            "contradictory_evidence": [],
            "open_questions": ["q" * 5000],
            "next_actions": [],
            "budget_state": {},
            "formalization_debt": [],
            "representative_failures": [],
            "archived_work_files": [],
            "previous_checkpoint": None,
            "compaction_plan_sha256": "d" * 64,
            "checkpoint_sha256": "",
        }
        checkpoint["checkpoint_sha256"] = record_digest(
            checkpoint, "checkpoint_sha256"
        )
        path = paths.checkpoints / "CHECKPOINT-000001.json"
        atomic_write_json(path, checkpoint)

        issues = _checkpoint_issues(paths, {})

        self.assertEqual(
            [issue.render() for issue in issues],
            [],
        )

    def test_new_study_and_new_checkpoint_emit_bounded_v2(self) -> None:
        paths = self.initialize_approved_with_claim()
        self.assertEqual(load_json(paths.claims)["schema_version"], CLAIMS_SCHEMA_VERSION)

        checkpoint_path = finalize_compaction(
            paths,
            self._write_compaction_plan(paths),
        )
        checkpoint = load_json(checkpoint_path)

        self.assertEqual(
            checkpoint["schema_version"], CHECKPOINT_SCHEMA_VERSION
        )
        self.assertIn("inactive_claim_refs", checkpoint)
        self.assertIn("active_context_watermarks", checkpoint)
        self.assertEqual(
            object_schema_issues(
                self.root,
                "checkpoint",
                checkpoint_path,
                checkpoint,
            ),
            [],
        )

    def test_unsupported_or_missing_versions_fail_closed(self) -> None:
        path = self.root / "unsupported.json"
        for name in ("claims", "checkpoint"):
            for raw_version in (None, True, 0, 3):
                with self.subTest(schema=name, schema_version=raw_version):
                    value = {} if raw_version is None else {"schema_version": raw_version}
                    issues = object_schema_issues(self.root, name, path, value)
                    self.assertEqual(len(issues), 1)
                    self.assertEqual(issues[0].level, "ERROR")
                    self.assertIn(
                        f"unsupported {name} schema_version",
                        issues[0].message,
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
