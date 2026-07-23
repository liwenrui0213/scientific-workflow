from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
import unittest
from typing import Any

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.cli import build_parser, dispatch
from tools.studyctl.compaction import (
    current_evidence_inventory_binding,
    finalize_compaction,
    prepare_compaction,
)
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
)
from tools.studyctl.models import SCHEMA_VERSION, ValidationError
from tools.studyctl.observation import (
    create_observation_draft,
    finalize_observation,
)
from tools.studyctl.observation_triggers import load_current_registry
from tools.studyctl.validation import validate_study


class ObservationRecordTests(WorkflowTestCase):
    def write_trigger_registry_extension(
        self,
        *,
        trigger_id: str = "cross_checkpoint_reanalysis",
        assessment: str = "endorsed",
        adoption_decision: str = "adopted",
        kind: str = "semantic",
        validator: str | None = None,
    ) -> dict[str, Any]:
        current = load_current_registry(self.root)
        extension = {
            "confirmatory_allowed": False,
            "description": (
                "The same bounded analysis must be re-examined across several "
                "Checkpoints without changing its recorded meaning."
            ),
            "governance": {
                "origin": "reviewed_extension",
                "proposal": {
                    "why_existing_triggers_are_insufficient": (
                        "Checkpoint reuse alone does not identify repeated "
                        "cross-Checkpoint reanalysis as the reason for promotion."
                    ),
                    "expected_benefit": (
                        "Reviewers can distinguish stable reuse from a new "
                        "analysis performed after each Checkpoint."
                    ),
                    "abuse_risks": [
                        "The condition could be asserted to formalize trivial repetition."
                    ],
                },
                "independent_review": {
                    "assessment": assessment,
                    "rationale": (
                        "The proposed condition has a distinct, reviewable meaning."
                    ),
                    "reviewer_independence_statement": (
                        "The reviewer did not implement the proposed extension."
                    ),
                },
                "human_adoption": {
                    "decision": adoption_decision,
                    "rationale": (
                        "Adopt the narrowly scoped semantic condition."
                    ),
                    "human_authorization_statement": (
                        "The human explicitly selected this registry extension."
                    ),
                },
            },
            "id": trigger_id,
            "kind": kind,
            "validator": validator,
        }
        updated = copy.deepcopy(current)
        updated["registry_version"] = int(current["registry_version"]) + 1
        updated["previous_registry_sha256"] = current["registry_sha256"]
        updated["triggers"].append(extension)
        updated["triggers"].sort(key=lambda item: item["id"])
        updated["registry_sha256"] = record_digest(
            updated, "registry_sha256"
        )
        destination = (
            self.root
            / "scientific-workflow"
            / "observation-trigger-registries"
            / f"v{updated['registry_version']:04d}.json"
        )
        atomic_write_json(destination, updated, overwrite=False)
        return updated

    def fill_observation(
        self,
        draft: dict[str, Any],
        *,
        primary: dict[str, Any] | None = None,
    ) -> None:
        draft["promotion"]["rationale"] = (
            "The analysis is promoted for reuse, aggregation, and independent review."
        )
        draft["analysis"].update(
            {
                "method": "Aggregate exact integer outputs by Run.",
                "inclusion_rule": "Include every listed successful source Run.",
                "exclusion_rule": "Retain excluded Runs with an explicit rationale.",
                "aggregation_rule": "Report every value and their exact arithmetic mean.",
            }
        )
        draft["results"]["primary"] = primary or {
            "values": [4],
            "arithmetic_mean": 4,
        }
        draft["results"]["distribution"] = {
            "sample_size": len(draft["runs"]),
            "minimum": 4,
            "maximum": 4,
        }
        draft["uncertainty"] = {
            "statistical": "No sampling model is asserted.",
            "numerical": "Exact integer arithmetic.",
            "measurement": "No external measurement.",
        }
        draft["scope"] = "The listed immutable fixture Runs and their Cohort."
        draft["analysis_assumptions"] = [
            "Each included Run records the exact integer output it produced."
        ]
        draft["limitations"] = [
            "The fixture does not generalize beyond the recorded Runs."
        ]

    def fill_evidence(self, draft: dict[str, Any]) -> None:
        draft["addresses"]["question"] = (
            "Does the deterministic fixture result equal four?"
        )
        for run_ref in draft["runs"]:
            run_ref["role"] = "supporting"
        draft["analysis"]["method"] = (
            "Use the exact promoted Observation and interpret it for CLAIM-0001."
        )
        draft["result"] = {
            "observation": "Every included exact integer result equals four."
        }
        draft["scope"] = "The exact fixture Runs bound by the Observation."
        draft["uncertainty"] = (
            "No sampling uncertainty; provenance uncertainty is bounded by hashes."
        )
        draft["limitations"] = [
            "This does not establish a broader scientific generalization."
        ]
        self.fill_evidence_inference(draft)
        draft["assessment"] = "supports"

    def test_inline_observation_remains_the_default_boundary(self) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)

        draft_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"]],
        )
        draft = load_json(draft_path)

        self.assertIsNone(draft["observation_ref"])
        self.assertEqual(list(paths.observations.glob("OBS-*.json")), [])
        with self.assertRaisesRegex(
            ValidationError,
            "exactly one Claim",
        ):
            create_evidence_draft(
                paths,
                "EVID-0002",
                ["CLAIM-0001", "CLAIM-0002"],
                [manifest["run_id"]],
            )

    def test_active_trigger_registry_is_discoverable_from_cli(self) -> None:
        paths = self.initialize_approved_with_claim()
        args = build_parser().parse_args(
            [
                "--root",
                str(self.root),
                "observation-trigger-list",
                paths.study_id,
            ]
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = dispatch(args)
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["registry_version"], 1)
        self.assertEqual(
            payload["registry_sha256"],
            load_current_registry(self.root)["registry_sha256"],
        )
        self.assertIn(
            {
                "id": "multiple_runs",
                "kind": "structural",
                "confirmatory_allowed": True,
                "description": (
                    "The promoted analysis aggregates or compares at least "
                    "two immutable Runs."
                ),
                "origin": "builtin",
            },
            payload["triggers"],
        )

    def test_promoted_observation_is_hash_bound_and_bounded_in_context(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifests = [self.successful_run(paths) for _ in range(2)]
        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"] for manifest in manifests],
            ["multiple_runs", "multi_claim_reuse"],
        )
        observation = load_json(observation_path)
        registry = load_current_registry(self.root)
        self.assertEqual(
            observation["promotion"]["registry"],
            {
                "version": registry["registry_version"],
                "sha256": registry["registry_sha256"],
            },
        )
        self.fill_observation(
            observation,
            primary={"values": [4, 4], "arithmetic_mean": 4},
        )
        atomic_write_json(observation_path, observation)
        finalize_observation(paths, observation_path)
        finalized_observation = load_json(observation_path)

        self.assertEqual(finalized_observation["status"], "finalized")
        self.assertEqual(
            finalized_observation["record_sha256"],
            record_digest(finalized_observation, "record_sha256"),
        )
        self.assertEqual(len(finalized_observation["runs"]), 2)

        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifest["run_id"] for manifest in manifests],
            observation_id="OBS-0001",
            observation_version=1,
        )
        evidence = load_json(evidence_path)
        self.fill_evidence(evidence)
        atomic_write_json(evidence_path, evidence)
        finalize_evidence(paths, evidence_path)
        finalized_evidence = load_json(evidence_path)
        self.support_claim(paths, finalized_evidence)

        selector = build_active_selector(paths)
        locator_index = selector["decisive_observations"]
        self.assertEqual(locator_index["total_count"], 1)
        locator = locator_index["items"][0]
        self.assertEqual(locator["observation_id"], "OBS-0001")
        self.assertEqual(locator["record_sha256"], finalized_observation["record_sha256"])
        self.assertEqual(
            locator["promotion_registry"],
            finalized_observation["promotion"]["registry"],
        )
        self.assertEqual(
            locator["promotion_triggers"],
            ["multiple_runs", "multi_claim_reuse"],
        )
        self.assertEqual(locator["run_count"], 2)
        self.assertEqual(locator["addressed_by"], ["EVID-0001"])
        self.assertEqual(
            [issue.render() for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

        compaction_input = prepare_compaction(paths)
        prepared = load_json(compaction_input)
        claims = load_json(paths.claims)
        evidence_ref = claims["claims"][0]["supporting_evidence"][0]
        plan = {
            "schema_version": SCHEMA_VERSION,
            "study_id": paths.study_id,
            "compaction_input_sha256": sha256_file(compaction_input),
            "claims_sha256": sha256_file(paths.claims),
            "evidence_inventory": current_evidence_inventory_binding(paths),
            "archive_work_files": [],
            "decisive_evidence": [evidence_ref],
            "contradictory_evidence": [],
            "frontier": claims["frontier"],
            "open_questions": list(claims["frontier"]["open_questions"]),
            "next_actions": list(claims["frontier"]["next_actions"]),
            "representative_failures": [],
            "budget_state": prepared["budget_totals"],
        }
        plan_path = paths.work / "observation-compaction-plan.json"
        atomic_write_json(plan_path, plan)
        checkpoint = load_json(finalize_compaction(paths, plan_path))
        self.assertEqual(
            checkpoint["decisive_observations"],
            [finalized_evidence["observation_ref"]],
        )
        self.assertEqual(
            checkpoint["active_context_watermarks"][
                "observation_record_count"
            ],
            1,
        )

    def test_excluded_run_stale_ref_and_duplicate_analysis_are_rejected(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifests = [self.successful_run(paths) for _ in range(2)]
        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"] for manifest in manifests],
            ["multiple_runs", "complex_analysis"],
        )
        observation = load_json(observation_path)
        observation["runs"][1]["disposition"] = "excluded"
        observation["runs"][1]["rationale"] = (
            "Excluded by the declared analysis rule but retained for audit."
        )
        self.fill_observation(observation)
        atomic_write_json(observation_path, observation)
        finalize_observation(paths, observation_path)

        with self.assertRaisesRegex(
            ValidationError,
            "included or anomaly Runs",
        ):
            create_evidence_draft(
                paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [manifest["run_id"] for manifest in manifests],
                observation_id="OBS-0001",
                observation_version=1,
            )

        evidence_path = create_evidence_draft(
            paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [manifests[0]["run_id"]],
            observation_id="OBS-0001",
            observation_version=1,
        )
        evidence = load_json(evidence_path)
        self.fill_evidence(evidence)
        evidence["observation_ref"]["sha256"] = "0" * 64
        atomic_write_json(evidence_path, evidence)
        with self.assertRaisesRegex(
            ValidationError,
            "Observation reference is stale",
        ):
            finalize_evidence(paths, evidence_path)

        duplicate_path = create_observation_draft(
            paths,
            "OBS-0002",
            [manifest["run_id"] for manifest in manifests],
            ["multiple_runs", "context_deduplication"],
        )
        duplicate = load_json(duplicate_path)
        duplicate["runs"][1]["disposition"] = "excluded"
        duplicate["runs"][1]["rationale"] = (
            "Excluded by the declared analysis rule but retained for audit."
        )
        self.fill_observation(duplicate)
        atomic_write_json(duplicate_path, duplicate)
        with self.assertRaisesRegex(
            ValidationError,
            "duplicate Observation analysis fingerprint",
        ):
            finalize_observation(paths, duplicate_path)

    def test_reviewed_semantic_trigger_extension_is_versioned_and_bound(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        registry = self.write_trigger_registry_extension()

        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["cross_checkpoint_reanalysis"],
        )
        observation = load_json(observation_path)
        self.assertEqual(
            observation["promotion"]["registry"],
            {
                "version": 2,
                "sha256": registry["registry_sha256"],
            },
        )
        self.fill_observation(observation)
        atomic_write_json(observation_path, observation)
        finalize_observation(paths, observation_path)

        finalized = load_json(observation_path)
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(
            finalized["promotion"]["triggers"],
            ["cross_checkpoint_reanalysis"],
        )
        self.assertEqual(
            [issue.message for issue in validate_study(paths) if issue.level == "ERROR"],
            [],
        )

    def test_unregistered_empty_and_unreviewed_triggers_fail_closed(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)

        with self.assertRaisesRegex(
            ValidationError,
            "unsupported Observation promotion trigger",
        ):
            create_observation_draft(
                paths,
                "OBS-0001",
                [manifest["run_id"]],
                ["reviewer_likes_this_reason"],
            )
        with self.assertRaisesRegex(
            ValidationError,
            "at least one registered promotion trigger",
        ):
            create_observation_draft(
                paths,
                "OBS-0001",
                [manifest["run_id"]],
                [],
            )

        self.write_trigger_registry_extension(assessment="rejected")
        with self.assertRaisesRegex(
            ValidationError,
            "requires an endorsed independent review",
        ):
            load_current_registry(self.root)

    def test_registry_is_append_only_and_observation_binding_is_exact(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        observation_path = create_observation_draft(
            paths,
            "OBS-0001",
            [manifest["run_id"]],
            ["independent_review"],
        )
        observation = load_json(observation_path)
        self.fill_observation(observation)
        observation["promotion"]["registry"]["sha256"] = "0" * 64
        atomic_write_json(observation_path, observation)
        with self.assertRaisesRegex(
            ValidationError,
            "registry reference is stale",
        ):
            finalize_observation(paths, observation_path)

        current = load_current_registry(self.root)
        changed = copy.deepcopy(current)
        changed["registry_version"] = 2
        changed["previous_registry_sha256"] = current["registry_sha256"]
        changed["triggers"][0]["description"] = (
            "A retroactively changed meaning that must be rejected."
        )
        changed["registry_sha256"] = record_digest(
            changed, "registry_sha256"
        )
        destination = (
            self.root
            / "scientific-workflow"
            / "observation-trigger-registries"
            / "v0002.json"
        )
        atomic_write_json(destination, changed, overwrite=False)
        with self.assertRaisesRegex(
            ValidationError,
            "must not reinterpret existing triggers",
        ):
            load_current_registry(self.root)

    def test_extension_requires_explicit_human_adoption(self) -> None:
        self.write_trigger_registry_extension(
            adoption_decision="deferred"
        )
        with self.assertRaisesRegex(
            ValidationError,
            "requires an explicit human adoption decision",
        ):
            load_current_registry(self.root)

    def test_structural_extension_requires_deterministic_validator(
        self,
    ) -> None:
        self.write_trigger_registry_extension(
            kind="structural",
            validator=None,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "requires a supported deterministic validator",
        ):
            load_current_registry(self.root)


if __name__ == "__main__":
    unittest.main()
