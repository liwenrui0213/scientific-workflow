from __future__ import annotations

from pathlib import Path
import sys
import unittest

from tests.helpers import WorkflowTestCase
from tools.studyctl.confirmation import (
    create_confirmation_draft,
    finalize_confirmation,
)
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import atomic_write_json, load_json
from tools.studyctl.models import utc_now
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import errors_only, validate_study


class ClaimPromotionContractTests(WorkflowTestCase):
    method = "Compare the recorded exact integer result with four."
    output_path = ".objects/confirmatory-result.txt"

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
                "acceptance_criteria": [
                    "The recorded exact integer equals four."
                ],
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

    def _command(self, *, exit_code: int) -> list[str]:
        return [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"p = Path({self.output_path!r}); "
                "p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text('4\\n', encoding='utf-8'); "
                f"raise SystemExit({exit_code})"
            ),
        ]

    def _make_confirmation(
        self,
        *,
        argv: list[str],
        acceptable_terminal_statuses: list[str],
    ) -> dict[str, object]:
        draft_path = create_confirmation_draft(
            self.paths,
            "CONF-0001",
            ["CLAIM-0001"],
        )
        draft = load_json(draft_path)
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
            "decision_rule": (
                "Support exactly when the recorded value equals four."
            ),
            "stopping_rule": "Execute the frozen slot exactly once.",
            "exclusion_rule": "Exclude only Runs that are Evidence-ineligible.",
        }
        draft["run_slots"] = [
            {
                "slot_id": "SLOT-001",
                "candidate_id": "CAND-001",
                "argv": argv,
                "seed": None,
                "hardware_class": "test-cpu",
                "precision": "exact-integer",
                "cohort_fields": {},
                "input_paths": [],
                "outcome_contract": {
                    "acceptable_terminal_statuses":
                        acceptable_terminal_statuses,
                    "observation_required": False,
                    "required_output_paths": [self.output_path],
                },
            }
        ]
        atomic_write_json(draft_path, draft)
        finalized = load_json(finalize_confirmation(self.paths, draft_path))
        self.assertEqual(finalized["schema_version"], 3)
        self.assertEqual(finalized["status"], "finalized")
        return finalized

    def _confirmatory_run(self, argv: list[str]) -> dict[str, object]:
        return execute_run(
            self.paths,
            argv=argv,
            purpose="confirmatory Claim-promotion fixture",
            epistemic_mode="confirmatory",
            confirmation_id="CONF-0001",
            confirmation_slot="SLOT-001",
            output_paths=[self.output_path],
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def _finalize_evidence(
        self,
        *,
        evidence_id: str,
        run: dict[str, object],
        assessment: str,
        role: str,
    ) -> dict[str, object]:
        draft_path = create_evidence_draft(
            self.paths,
            evidence_id,
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        draft = load_json(draft_path)
        draft["addresses"]["question"] = (
            "Does the deterministic result equal four?"
        )
        draft["runs"][0]["role"] = role
        if draft["evidence_basis"]["mode"] == "exploratory":
            draft["analysis"]["method"] = self.method
        draft["result"] = {
            "value": 4 if assessment == "supports" else 5,
            "comparison": "equal" if assessment == "supports" else "not_equal",
        }
        draft["scope"] = "the exact deterministic fixture"
        draft["uncertainty"] = "No sampling uncertainty."
        draft["limitations"] = ["No broader generalization is claimed."]
        draft["inference"] = {
            "observation_to_claim": (
                "The recorded exact-integer result is compared with the Claim's "
                "required value under the frozen evaluator."
            ),
            "auxiliary_assumptions": [
                "The immutable Run accurately identifies the executed command."
            ],
            "competing_explanations": [
                "A fixture defect could explain the recorded result."
            ],
            "falsification_conditions": [
                "A hash-stable rerun with a different value overturns this assessment."
            ],
        }
        draft["assessment"] = assessment
        atomic_write_json(draft_path, draft)
        return load_json(finalize_evidence(self.paths, draft_path))

    def _supporting_confirmation(
        self,
        *,
        exit_code: int = 0,
        acceptable_terminal_statuses: list[str] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        argv = self._command(exit_code=exit_code)
        self._make_confirmation(
            argv=argv,
            acceptable_terminal_statuses=(
                acceptable_terminal_statuses
                if acceptable_terminal_statuses is not None
                else ["succeeded"]
            ),
        )
        run = self._confirmatory_run(argv)
        evidence = self._finalize_evidence(
            evidence_id="EVID-0001",
            run=run,
            assessment="supports",
            role="supporting",
        )
        self.support_claim(self.paths, evidence)
        return run, evidence

    def _contradictory_evidence(self) -> dict[str, object]:
        run = execute_run(
            self.paths,
            argv=[sys.executable, "-c", "print(5)"],
            purpose="contradictory exploratory fixture",
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        return self._finalize_evidence(
            evidence_id="EVID-0002",
            run=run,
            assessment="contradicts",
            role="contradictory",
        )

    def _claim_messages(self) -> list[str]:
        return [
            issue.message
            for issue in errors_only(validate_study(self.paths))
            if "CLAIM-0001" in issue.message
        ]

    def _add_contradiction(
        self,
        contradictory: dict[str, object],
        *,
        disposition: dict[str, str] | None = None,
    ) -> None:
        claims = load_json(self.paths.claims)
        claim = claims["claims"][0]
        claim["contradictory_evidence"] = [
            {
                "evidence_id": contradictory["evidence_id"],
                "version": contradictory["version"],
                "sha256": contradictory["record_sha256"],
            }
        ]
        if disposition is None:
            claim.pop("conflict_disposition", None)
        else:
            claim["conflict_disposition"] = disposition
        claim["updated_at"] = utc_now()
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(self.paths.claims, claims)

    def test_failed_supporting_run_requires_predeclared_acceptable_status(
        self,
    ) -> None:
        run, evidence = self._supporting_confirmation(
            exit_code=1,
            acceptable_terminal_statuses=["succeeded"],
        )

        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["change_scope"]["evidence_eligible"])
        self.assertEqual(evidence["assessment"], "supports")
        messages = self._claim_messages()
        self.assertTrue(
            any("outcome contract" in message for message in messages),
            messages,
        )

    def test_predeclared_failed_outcome_can_support_failure_claim(self) -> None:
        run, evidence = self._supporting_confirmation(
            exit_code=1,
            acceptable_terminal_statuses=["failed"],
        )

        self.assertEqual(run["status"], "failed")
        self.assertEqual(evidence["assessment"], "supports")
        self.assertEqual(self._claim_messages(), [])

    def test_numerically_supported_requires_nonempty_bounded_scope(self) -> None:
        self._supporting_confirmation()

        for invalid_scope in (None, "   "):
            with self.subTest(scope=invalid_scope):
                claims = load_json(self.paths.claims)
                claims["claims"][0]["scope"] = invalid_scope
                claims["claims"][0]["updated_at"] = utc_now()
                claims["revision"] += 1
                claims["updated_at"] = utc_now()
                atomic_write_json(self.paths.claims, claims)

                messages = self._claim_messages()
                self.assertTrue(
                    any(
                        "numerically_supported requires an explicit bounded scope"
                        in message
                        for message in messages
                    ),
                    messages,
                )

                claims = load_json(self.paths.claims)
                claims["claims"][0]["scope"] = (
                    "the fixture command and recorded environment"
                )
                claims["claims"][0]["updated_at"] = utc_now()
                claims["revision"] += 1
                claims["updated_at"] = utc_now()
                atomic_write_json(self.paths.claims, claims)

    def test_contradictory_evidence_requires_resolved_synthesis(self) -> None:
        self._supporting_confirmation()
        contradictory = self._contradictory_evidence()

        invalid_dispositions: list[dict[str, str] | None] = [
            None,
            {
                "status": "unresolved",
                "synthesis": "The conflict remains unresolved.",
            },
            {"status": "resolved", "synthesis": "   "},
        ]
        for disposition in invalid_dispositions:
            with self.subTest(disposition=disposition):
                self._add_contradiction(
                    contradictory,
                    disposition=disposition,
                )

                messages = self._claim_messages()
                self.assertTrue(
                    any(
                        "conflict_disposition" in message
                        and "synthesis" in message
                        for message in messages
                    ),
                    messages,
                )

    def test_resolved_contradiction_allows_numerically_supported_claim(
        self,
    ) -> None:
        self._supporting_confirmation()
        contradictory = self._contradictory_evidence()
        self._add_contradiction(
            contradictory,
            disposition={
                "status": "resolved",
                "synthesis": (
                    "The contradictory exploratory Run used a different recorded "
                    "result and does not overturn the frozen confirmatory result "
                    "within the exact deterministic scope."
                ),
            },
        )

        self.assertEqual(self._claim_messages(), [])


if __name__ == "__main__":
    unittest.main()
