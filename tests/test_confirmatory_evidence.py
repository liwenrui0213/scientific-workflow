from __future__ import annotations

from pathlib import Path
import sys
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.confirmation import (
    create_confirmation_draft,
    finalize_confirmation,
)
from tools.studyctl.evidence import (
    create_evidence_draft,
    effective_evidence_mode,
    finalize_evidence,
)
from tools.studyctl.hashing import atomic_write_json, load_json
from tools.studyctl.models import ValidationError, utc_now
from tools.studyctl.observation import (
    create_observation_draft,
    finalize_observation,
)
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import errors_only, validate_study


class ConfirmatoryEvidenceTests(WorkflowTestCase):
    argv = [sys.executable, "-c", "print(4)"]
    method = "Compare the recorded exact integer result with four."

    def setUp(self) -> None:
        super().setUp()
        self.paths = self.initialize()
        self.fill_brief(self.paths)
        self.add_proposed_claim(self.paths)
        atomic_write_json(
            self.paths.formal / "PROTOCOL.json",
            {
                "schema_version": 1,
                "status": "active",
                "purpose": "Confirm the deterministic fixture result.",
                "hypotheses": ["The exact result equals four."],
                "inputs": [],
                "evaluator": "formal/EVALUATOR.json",
                "dataset_split": None,
                "baseline": None,
                "acceptance_criteria": ["The recorded exact integer equals four."],
                "compute_budget": {
                    "estimated_gpu_hours": 0,
                    "estimated_cpu_hours": 0.01,
                },
                "seeds": [],
                "cohort_fields": {},
                "known_deviations": [],
            },
        )
        atomic_write_json(
            self.paths.formal / "EVALUATOR.json",
            {"status": "active", "metric": "exact integer equality"},
        )
        self.approve(self.paths)

    def make_confirmation(
        self,
        confirmation_id: str,
        slot_ids: list[str],
        *,
        continuation_kind: str = "replication",
        invalidity_reason: str | None = None,
    ) -> dict[str, object]:
        draft_path = create_confirmation_draft(
            self.paths,
            confirmation_id,
            ["CLAIM-0001"],
        )
        draft = load_json(draft_path)
        self.assertIsInstance(draft, dict)
        if draft["campaign"]["sequence"] > 1:
            draft["campaign"]["continuation_kind"] = continuation_kind
            draft["campaign"]["rationale"] = (
                "Continue the same exact Claim-version campaign with full "
                "disclosure of the preceding Confirmation."
            )
            draft["campaign"]["changes"] = [
                "A new immutable Confirmation and new Run slots are registered."
            ]
            if continuation_kind == "corrective_supersession":
                draft["campaign"]["supersedes"] = draft["campaign"]["predecessor"]
                draft["campaign"]["invalidity_reason"] = invalidity_reason
        draft["candidates"] = [
            {
                "candidate_id": "CAND-001",
                "description": "The deterministic fixture candidate.",
                "paths": ["scientific-workflow/repository-profile.json"],
                "bindings": [],
                "code_state": None,
            }
        ]
        draft["held_out"] = {
            "status": "not_applicable",
            "description": "No held-out data apply to exact integer arithmetic.",
            "paths": [],
            "bindings": [],
            "workflow_observed_prior_run_count": 0,
            "freshness": "not_applicable",
        }
        draft["analysis_plan"] = {
            "method": self.method,
            "primary_outcomes": ["The recorded integer value."],
            "decision_rule": "Support exactly when the recorded value equals four.",
            "stopping_rule": "Execute each frozen slot exactly once.",
            "exclusion_rule": "Exclude only Runs that are Evidence-ineligible.",
        }
        draft["run_slots"] = [
            {
                "slot_id": slot_id,
                "candidate_id": "CAND-001",
                "argv": self.argv,
                "seed": None,
                "hardware_class": "test-cpu",
                "precision": "exact-integer",
                "cohort_fields": {},
                "input_paths": [],
                "outcome_contract": {
                    "acceptable_terminal_statuses": ["succeeded"],
                    "observation_required": True,
                    "required_output_paths": [],
                },
            }
            for slot_id in slot_ids
        ]
        atomic_write_json(draft_path, draft)
        finalized_path = finalize_confirmation(self.paths, draft_path)
        finalized = load_json(finalized_path)
        self.assertIsInstance(finalized, dict)
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(
            [slot["slot_id"] for slot in finalized["run_slots"]],
            slot_ids,
        )
        return finalized

    def confirmatory_run(
        self,
        confirmation_id: str,
        slot_id: str,
        *,
        missing_output: str | None = None,
    ) -> dict[str, object]:
        return execute_run(
            self.paths,
            argv=list(self.argv),
            purpose=f"confirmatory fixture {confirmation_id}/{slot_id}",
            epistemic_mode="confirmatory",
            confirmation_id=confirmation_id,
            confirmation_slot=slot_id,
            output_paths=[] if missing_output is None else [missing_output],
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def populate_evidence(self, draft_path: Path) -> dict[str, object]:
        draft = load_json(draft_path)
        self.assertIsInstance(draft, dict)
        if draft["evidence_basis"]["mode"] in {"confirmatory", "mixed"}:
            observation_id = draft["evidence_id"].replace("EVID-", "OBS-")
            observation_path = create_observation_draft(
                self.paths,
                observation_id,
                [run_ref["run_id"] for run_ref in draft["runs"]],
                ["confirmatory_use"],
            )
            observation = load_json(observation_path)
            observation["promotion"]["rationale"] = (
                "The confirmatory outcome contract requires a reusable formal Observation."
            )
            observation["analysis"].update(
                {
                    "method": self.method,
                    "inclusion_rule": "Include every Run disclosed by this Evidence.",
                    "exclusion_rule": "Exclude no listed Run.",
                    "aggregation_rule": "Report the exact deterministic fixture result.",
                }
            )
            if len(observation["cohorts"]) > 1:
                observation["analysis"]["cohort_compatibility_justification"] = (
                    "The fixture compares the same exact-integer outcome across "
                    "the explicitly recorded exploratory and confirmatory Cohorts."
                )
            observation["results"]["primary"] = {
                "value": 4,
                "comparison": "equal",
            }
            observation["uncertainty"] = {
                "statistical": "No sampling uncertainty.",
                "numerical": "Exact integer arithmetic.",
                "measurement": "No external measurement.",
            }
            observation["scope"] = "The exact deterministic fixture Runs."
            observation["analysis_assumptions"] = [
                "Each immutable Run accurately records its deterministic outcome."
            ]
            observation["limitations"] = [
                "The fixture does not establish a broader scientific result."
            ]
            atomic_write_json(observation_path, observation)
            finalized_observation = load_json(
                finalize_observation(self.paths, observation_path)
            )
            draft["observation_ref"] = {
                "observation_id": finalized_observation["observation_id"],
                "version": finalized_observation["version"],
                "sha256": finalized_observation["record_sha256"],
            }
        draft["addresses"]["question"] = "Does the deterministic result equal four?"
        for run_ref in draft["runs"]:
            run_ref["role"] = "supporting"
        if draft["evidence_basis"]["mode"] == "exploratory":
            draft["analysis"]["method"] = self.method
        draft["result"] = {"value": 4, "comparison": "equal"}
        draft["scope"] = "the exact deterministic fixture"
        draft["uncertainty"] = "No sampling uncertainty."
        draft["limitations"] = ["No broader generalization is claimed."]
        self.fill_evidence_inference(draft)
        draft["assessment"] = "supports"
        atomic_write_json(draft_path, draft)
        return draft

    def test_exploratory_evidence_remains_the_default(self) -> None:
        run = self.successful_run(self.paths)

        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        draft = load_json(draft_path)

        self.assertEqual(effective_evidence_mode(draft), "exploratory")
        self.assertEqual(
            draft["evidence_basis"],
            {
                "mode": "exploratory",
                "exploratory_run_ids": [run["run_id"]],
                "confirmatory_run_ids": [],
                "confirmation": None,
                "confirmation_campaign": None,
                "planned_slot_ids": [],
                "included_slot_ids": [],
                "missing_slot_ids": [],
                "excluded_confirmatory_runs": [],
                "held_out": {
                    "status": "not_held_out",
                    "freshness": "unknown",
                    "workflow_observed_prior_run_count": 0,
                },
            },
        )
        self.populate_evidence(draft_path)
        finalized = load_json(finalize_evidence(self.paths, draft_path))
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(effective_evidence_mode(finalized), "exploratory")
        with self.assertRaisesRegex(ValidationError, "evidence_basis"):
            effective_evidence_mode({"status": "finalized"})

        self.support_claim(self.paths, finalized)
        claims = load_json(self.paths.claims)
        self.assertEqual(claims["claims"][0]["state"], "partially_supported")
        relevant = [
            issue.render()
            for issue in errors_only(validate_study(self.paths))
            if "CLAIM-0001" in issue.message
        ]
        self.assertEqual(relevant, [])

        claims["claims"][0]["scope"] = None
        atomic_write_json(self.paths.claims, claims)
        messages = "\n".join(
            issue.render() for issue in errors_only(validate_study(self.paths))
        )
        self.assertIn("partially_supported requires an explicit bounded scope", messages)
        claims["claims"][0]["scope"] = "the exact deterministic fixture"

        claims["claims"][0]["state"] = "numerically_supported"
        atomic_write_json(self.paths.claims, claims)
        messages = "\n".join(
            issue.render() for issue in errors_only(validate_study(self.paths))
        )
        self.assertIn(
            "numerically_supported requires fresh held-out or not-applicable confirmatory Evidence",
            messages,
        )

    def test_missing_claim_basis_is_rejected(self) -> None:
        run = self.successful_run(self.paths)
        evidence = self.finalized_supporting_evidence(self.paths, [run])
        self.support_claim(self.paths, evidence)
        claims = load_json(self.paths.claims)
        del claims["claims"][0]["evidence_basis"]
        atomic_write_json(self.paths.claims, claims)

        issues = validate_study(self.paths)
        messages = [issue.message for issue in issues]
        self.assertTrue(
            any(
                issue.level == "ERROR"
                and "evidence_basis" in issue.message
                for issue in issues
            ),
            messages,
        )

    def test_complete_confirmatory_evidence_finalizes(self) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        run = self.confirmatory_run("CONF-0001", "SLOT-001")

        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        draft = load_json(draft_path)
        basis = draft["evidence_basis"]

        self.assertEqual(basis["mode"], "confirmatory")
        self.assertEqual(basis["exploratory_run_ids"], [])
        self.assertEqual(basis["confirmatory_run_ids"], [run["run_id"]])
        self.assertEqual(
            basis["confirmation"],
            {
                "confirmation_id": "CONF-0001",
                "sha256": confirmation["record_sha256"],
            },
        )
        self.assertEqual(
            basis["planned_slot_ids"], ["CONF-0001/SLOT-001"]
        )
        self.assertEqual(
            basis["included_slot_ids"], ["CONF-0001/SLOT-001"]
        )
        self.assertEqual(basis["missing_slot_ids"], [])
        campaign = basis["confirmation_campaign"]
        self.assertEqual(
            [item["confirmation_id"] for item in campaign["confirmations"]],
            ["CONF-0001"],
        )
        self.assertEqual(campaign["confirmations"][0]["sequence"], 1)
        self.assertEqual(len(draft["analysis"]["registered_plans"]), 1)
        self.assertEqual(draft["analysis"]["method"], self.method)

        self.populate_evidence(draft_path)
        finalized = load_json(finalize_evidence(self.paths, draft_path))
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(effective_evidence_mode(finalized), "confirmatory")

        self.support_claim(self.paths, finalized)
        claims = load_json(self.paths.claims)
        self.assertEqual(claims["claims"][0]["state"], "numerically_supported")
        relevant = [
            issue.render()
            for issue in errors_only(validate_study(self.paths))
            if "CLAIM-0001" in issue.message
        ]
        self.assertEqual(relevant, [])

    def test_mixed_evidence_distinguishes_both_run_sets(self) -> None:
        exploratory = self.successful_run(self.paths)
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        confirmatory = self.confirmatory_run("CONF-0001", "SLOT-001")

        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(exploratory["run_id"]), str(confirmatory["run_id"])],
        )
        basis = load_json(draft_path)["evidence_basis"]

        self.assertEqual(basis["mode"], "mixed")
        self.assertEqual(basis["exploratory_run_ids"], [exploratory["run_id"]])
        self.assertEqual(basis["confirmatory_run_ids"], [confirmatory["run_id"]])
        self.assertEqual(
            basis["included_slot_ids"], ["CONF-0001/SLOT-001"]
        )

        self.populate_evidence(draft_path)
        finalized = load_json(finalize_evidence(self.paths, draft_path))
        self.assertEqual(effective_evidence_mode(finalized), "mixed")

        self.support_claim(self.paths, finalized)
        claims = load_json(self.paths.claims)
        self.assertEqual(claims["claims"][0]["state"], "numerically_supported")
        self.assertEqual(claims["claims"][0]["evidence_basis"], "mixed")
        relevant = [
            issue.render()
            for issue in errors_only(validate_study(self.paths))
            if "CLAIM-0001" in issue.message
        ]
        self.assertEqual(relevant, [])

    def test_omitting_an_eligible_successful_slot_is_rejected(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001", "SLOT-002"])
        first = self.confirmatory_run("CONF-0001", "SLOT-001")
        second = self.confirmatory_run("CONF-0001", "SLOT-002")

        with self.assertRaisesRegex(
            ValidationError,
            "must include every Evidence-eligible terminal Run.*" + str(second["run_id"]),
        ):
            create_evidence_draft(
                self.paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [str(first["run_id"])],
            )
        self.assertEqual(list(self.paths.evidence.glob("EVID-*.json")), [])

    def test_missing_planned_slot_blocks_finalization(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001", "SLOT-002"])
        first = self.confirmatory_run("CONF-0001", "SLOT-001")
        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(first["run_id"])],
        )
        self.assertEqual(
            load_json(draft_path)["evidence_basis"]["missing_slot_ids"],
            ["CONF-0001/SLOT-002"],
        )
        self.populate_evidence(draft_path)

        with self.assertRaisesRegex(
            ValidationError,
            "cannot finalize with missing Confirmation slots: CONF-0001/SLOT-002",
        ):
            finalize_evidence(self.paths, draft_path)
        self.assertEqual(load_json(draft_path)["status"], "draft")

    def test_campaign_rejects_latest_only_reporting_and_accepts_full_history(self) -> None:
        self.argv = [sys.executable, "-c", "raise SystemExit(1)"]
        first_confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        first = self.confirmatory_run("CONF-0001", "SLOT-001")
        self.assertIn(
            first["status"], {"succeeded", "failed", "interrupted", "incomplete"}
        )

        self.argv = [sys.executable, "-c", "print(4)"]
        second_confirmation = self.make_confirmation(
            "CONF-0002",
            ["SLOT-001"],
            continuation_kind="corrective_supersession",
            invalidity_reason=(
                "The first registered command intentionally represented the "
                "reviewer's failed-attempt fixture."
            ),
        )
        second = self.confirmatory_run("CONF-0002", "SLOT-001")
        self.assertIn(
            second["status"], {"succeeded", "failed", "interrupted", "incomplete"}
        )

        self.assertEqual(
            second_confirmation["campaign"]["campaign_id"],
            first_confirmation["campaign"]["campaign_id"],
        )
        self.assertEqual(second_confirmation["campaign"]["sequence"], 2)
        self.assertEqual(
            second_confirmation["campaign"]["supersedes"],
            {
                "confirmation_id": "CONF-0001",
                "sha256": first_confirmation["record_sha256"],
            },
        )
        with self.assertRaisesRegex(
            ValidationError,
            "must include every Evidence-eligible terminal Run.*"
            + str(first["run_id"]),
        ):
            create_evidence_draft(
                self.paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [str(second["run_id"])],
            )

        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(first["run_id"]), str(second["run_id"])],
        )
        draft = self.populate_evidence(draft_path)
        draft["runs"][0]["role"] = "failed_attempt"
        atomic_write_json(draft_path, draft)
        finalized = load_json(finalize_evidence(self.paths, draft_path))
        campaign = finalized["evidence_basis"]["confirmation_campaign"]
        self.assertEqual(
            [item["confirmation_id"] for item in campaign["confirmations"]],
            ["CONF-0001", "CONF-0002"],
        )
        self.assertEqual(
            finalized["evidence_basis"]["planned_slot_ids"],
            ["CONF-0001/SLOT-001", "CONF-0002/SLOT-001"],
        )
        self.assertEqual(
            finalized["evidence_basis"]["confirmatory_run_ids"],
            [first["run_id"], second["run_id"]],
        )
        self.assertEqual(len(finalized["analysis"]["registered_plans"]), 2)

    def test_new_confirmation_is_blocked_while_prior_slots_are_unfinished(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])

        with self.assertRaisesRegex(
            ValidationError,
            "prior campaign slots are unfinished: CONF-0001/SLOT-001",
        ):
            self.make_confirmation("CONF-0002", ["SLOT-001"])
        self.assertFalse(
            (self.paths.confirmations / "CONF-0002.json").exists()
        )

    def test_corrective_supersession_requires_an_invalidity_reason(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        self.confirmatory_run("CONF-0001", "SLOT-001")

        with self.assertRaisesRegex(
            ValidationError,
            "predecessor invalidity reason",
        ):
            self.make_confirmation(
                "CONF-0002",
                ["SLOT-001"],
                continuation_kind="corrective_supersession",
                invalidity_reason=None,
            )

    def test_old_evidence_remains_immutable_but_cannot_support_an_extended_campaign(
        self,
    ) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        first = self.confirmatory_run("CONF-0001", "SLOT-001")
        first_draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(first["run_id"])],
        )
        self.populate_evidence(first_draft_path)
        first_evidence = load_json(
            finalize_evidence(self.paths, first_draft_path)
        )
        self.support_claim(self.paths, first_evidence)
        self.assertEqual(
            load_json(self.paths.claims)["claims"][0]["state"],
            "numerically_supported",
        )

        self.make_confirmation("CONF-0002", ["SLOT-001"])
        issues = errors_only(validate_study(self.paths))
        messages = [issue.message for issue in issues]

        self.assertFalse(
            any("Evidence evidence_basis does not match" in message for message in messages),
            messages,
        )
        self.assertTrue(
            any(
                "numerically_supported requires fresh held-out or not-applicable "
                "confirmatory Evidence" in message
                for message in messages
            ),
            messages,
        )

    def test_every_frozen_analysis_field_is_enforced(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        run = self.confirmatory_run("CONF-0001", "SLOT-001")
        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        self.populate_evidence(draft_path)
        mutations = {
            "method": "Choose a favorable post-hoc analysis.",
            "primary_outcomes": ["Only the most favorable observed outcome."],
            "decision_rule": "Support whenever any observed result looks favorable.",
            "stopping_rule": "Stop as soon as a favorable result appears.",
            "exclusion_rule": "Exclude unfavorable results after inspection.",
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                draft = load_json(draft_path)
                original = draft["analysis"][field]
                draft["analysis"][field] = replacement
                atomic_write_json(draft_path, draft)

                with self.assertRaisesRegex(
                    ValidationError,
                    rf"analysis fields must exactly match.*{field}",
                ):
                    finalize_evidence(self.paths, draft_path)
                self.assertEqual(load_json(draft_path)["status"], "draft")

                draft = load_json(draft_path)
                draft["analysis"][field] = original
                atomic_write_json(draft_path, draft)

    def test_changed_claim_statement_is_rejected(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        run = self.confirmatory_run("CONF-0001", "SLOT-001")
        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        self.populate_evidence(draft_path)
        claims = load_json(self.paths.claims)
        claims["claims"][0]["statement"] = "The deterministic result exceeds four."
        claims["claims"][0]["updated_at"] = utc_now()
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(self.paths.claims, claims)

        with self.assertRaisesRegex(
            ValidationError,
            "Claim specification changed after the draft was created",
        ):
            finalize_evidence(self.paths, draft_path)
        self.assertEqual(load_json(draft_path)["status"], "draft")

    def test_unreported_ineligible_terminal_run_is_rejected(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001", "SLOT-002"])
        included = self.confirmatory_run("CONF-0001", "SLOT-001")
        excluded = self.confirmatory_run(
            "CONF-0001",
            "SLOT-002",
            missing_output=".objects/never-created.txt",
        )
        self.assertFalse(excluded["change_scope"]["evidence_eligible"])
        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(included["run_id"])],
        )
        draft = self.populate_evidence(draft_path)
        exclusions = draft["evidence_basis"]["excluded_confirmatory_runs"]
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]["run_id"], excluded["run_id"])
        self.assertEqual(
            exclusions[0]["slot_id"], "CONF-0001/SLOT-002"
        )
        self.assertTrue(exclusions[0]["reason"].strip())
        draft["evidence_basis"]["excluded_confirmatory_runs"] = []
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(
            ValidationError,
            "differing fields: excluded_confirmatory_runs",
        ):
            finalize_evidence(self.paths, draft_path)
        self.assertEqual(load_json(draft_path)["status"], "draft")


if __name__ == "__main__":
    unittest.main()
