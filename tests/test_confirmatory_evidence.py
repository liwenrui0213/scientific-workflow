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
    ) -> dict[str, object]:
        draft_path = create_confirmation_draft(
            self.paths,
            confirmation_id,
            ["CLAIM-0001"],
        )
        draft = load_json(draft_path)
        self.assertIsInstance(draft, dict)
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
        draft["addresses"]["question"] = "Does the deterministic result equal four?"
        for run_ref in draft["runs"]:
            run_ref["role"] = "supporting"
        if draft["evidence_basis"]["mode"] == "exploratory":
            draft["analysis"]["method"] = self.method
        draft["result"] = {"value": 4, "comparison": "equal"}
        draft["scope"] = "the exact deterministic fixture"
        draft["uncertainty"] = "No sampling uncertainty."
        draft["limitations"] = ["No broader generalization is claimed."]
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
        self.assertEqual(effective_evidence_mode({"status": "finalized"}), "exploratory")

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

    def test_missing_legacy_claim_basis_is_conservative_and_migratable(self) -> None:
        run = self.successful_run(self.paths)
        evidence = self.finalized_supporting_evidence(self.paths, [run])
        self.support_claim(self.paths, evidence)
        claims = load_json(self.paths.claims)
        del claims["claims"][0]["evidence_basis"]
        atomic_write_json(self.paths.claims, claims)

        issues = validate_study(self.paths)
        messages = [issue.message for issue in issues]
        self.assertFalse(any(issue.level == "ERROR" for issue in issues), messages)
        self.assertTrue(
            any(
                issue.level == "WARNING"
                and "conservative effective basis is 'exploratory'" in issue.message
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
        self.assertEqual(basis["planned_slot_ids"], ["SLOT-001"])
        self.assertEqual(basis["included_slot_ids"], ["SLOT-001"])
        self.assertEqual(basis["missing_slot_ids"], [])
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
        self.assertEqual(basis["included_slot_ids"], ["SLOT-001"])

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
            ["SLOT-002"],
        )
        self.populate_evidence(draft_path)

        with self.assertRaisesRegex(
            ValidationError,
            "cannot finalize with missing Confirmation slots: SLOT-002",
        ):
            finalize_evidence(self.paths, draft_path)
        self.assertEqual(load_json(draft_path)["status"], "draft")

    def test_confirmatory_runs_from_multiple_registrations_are_rejected(self) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        first = self.confirmatory_run("CONF-0001", "SLOT-001")
        self.make_confirmation("CONF-0002", ["SLOT-001"])
        second = self.confirmatory_run("CONF-0002", "SLOT-001")

        with self.assertRaisesRegex(
            ValidationError,
            "multiple Confirmation registrations",
        ):
            create_evidence_draft(
                self.paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [str(first["run_id"]), str(second["run_id"])],
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
            "Claim statement or scope changed after Confirmation",
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
