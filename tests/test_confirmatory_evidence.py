from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.cli import main as studyctl_main
from tools.studyctl.confirmation import (
    abandon_confirmation_campaign,
    create_confirmation_draft,
    discard_stale_confirmation_draft,
    finalize_confirmation,
    load_confirmation_campaign_abandonment,
    recover_confirmation_sequence,
    validate_confirmation_run,
)
from tools.studyctl.confirmation_sequence import (
    confirmation_authority_inventory,
    empty_confirmation_sequence,
    load_confirmation_sequence,
    migrate_legacy_confirmation_sequence,
    require_consistent_confirmation_authority,
    write_confirmation_sequence,
)
from tools.studyctl.evidence import (
    create_evidence_draft,
    effective_evidence_mode,
    finalize_evidence,
)
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_json,
)
from tools.studyctl.models import ValidationError, WorkflowError, utc_now
from tools.studyctl.observation import (
    create_observation_draft,
    finalize_observation,
)
from tools.studyctl.review import _confirmation_source_index
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import errors_only, run_index, validate_study


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
        restart_rationale: str = (
            "Start a new frozen campaign because the explicitly abandoned "
            "predecessor can no longer admit confirmatory execution."
        ),
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
        if draft["campaign"].get("predecessor_campaign") is not None:
            draft["campaign"]["restart_rationale"] = restart_rationale
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

    def abandonment_decision(
        self,
        campaign_id: str,
        *,
        rationale: str = (
            "The entire frozen campaign is no longer an appropriate basis for "
            "new confirmatory execution."
        ),
    ) -> dict[str, object]:
        return {
            "input_version": 1,
            "campaign_id": campaign_id,
            "rationale": rationale,
            "authorization": {
                "source": "explicit_user_instruction",
                "instruction": (
                    "Abandon this whole Confirmation campaign and preserve its "
                    "records, slots, Runs, and Evidence as immutable history."
                ),
            },
        }

    def rewrite_confirmation_as_legacy_v3(
        self,
        confirmation_id: str,
    ) -> Path:
        record_path = self.paths.confirmations / f"{confirmation_id}.json"
        record = load_json(record_path)
        self.assertIsInstance(record, dict)
        record["schema_version"] = 3
        record["campaign"].pop("predecessor_campaign")
        record["campaign"].pop("restart_rationale")
        record["record_sha256"] = None
        record["record_sha256"] = record_digest(
            record,
            "record_sha256",
        )
        atomic_write_json(record_path, record, mode=0o444)
        return record_path

    def populate_evidence(self, draft_path: Path) -> dict[str, object]:
        draft = load_json(draft_path)
        self.assertIsInstance(draft, dict)
        if (
            draft["evidence_basis"]["mode"] in {"confirmatory", "mixed"}
            and draft.get("observation_ref") is None
        ):
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

    def test_whole_campaign_abandonment_preserves_history_and_starts_linked_restart(
        self,
    ) -> None:
        first = self.make_confirmation(
            "CONF-0001",
            ["SLOT-001", "SLOT-002"],
        )
        first_path = self.paths.confirmations / "CONF-0001.json"
        first_bytes = first_path.read_bytes()
        campaign_id = str(first["campaign"]["campaign_id"])

        abandonment_path = abandon_confirmation_campaign(
            self.paths,
            "CONF-0001",
            self.abandonment_decision(campaign_id),
        )
        abandonment = load_json(abandonment_path)

        self.assertEqual(abandonment["scope"], "whole_campaign")
        self.assertEqual(abandonment["status"], "abandoned")
        self.assertEqual(
            abandonment["confirmation_records"],
            [
                {
                    "confirmation_id": "CONF-0001",
                    "sha256": first["record_sha256"],
                    "sequence": 1,
                }
            ],
        )
        self.assertEqual(
            abandonment["planned_slot_ids"],
            ["CONF-0001/SLOT-001", "CONF-0001/SLOT-002"],
        )
        self.assertEqual(
            abandonment["authorization"]["instruction_sha256"],
            sha256_json(abandonment["authorization"]["instruction"]),
        )
        self.assertEqual(abandonment_path.stat().st_mode & 0o222, 0)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(
            load_confirmation_campaign_abandonment(self.paths, campaign_id),
            abandonment,
        )
        confirmation_index = build_active_selector(self.paths)["confirmations"]
        self.assertEqual(
            confirmation_index["pending_finalized"]["total_count"],
            0,
        )
        self.assertEqual(
            confirmation_index["awaiting_evidence"]["total_count"],
            0,
        )
        history_item = confirmation_index["history"]["items"][0]
        self.assertEqual(history_item["campaign_status"], "abandoned")
        self.assertEqual(history_item["pending_slot_count"], 0)
        self.assertEqual(history_item["unconsumed_slot_count"], 2)
        self.assertEqual(
            history_item["abandonment"]["path"],
            abandonment_path.relative_to(self.paths.root).as_posix(),
        )
        self.assertEqual(
            history_item["abandonment"]["record_sha256"],
            abandonment["record_sha256"],
        )
        review_sources, review_inventory = _confirmation_source_index(
            self.paths,
            run_index(self.paths),
        )
        self.assertEqual(review_sources[0]["campaign_status"], "abandoned")
        self.assertEqual(
            review_sources[0]["abandonment"]["record_sha256"],
            abandonment["record_sha256"],
        )
        self.assertEqual(
            review_inventory["total_campaign_abandonment_count"],
            1,
        )
        self.assertFalse(
            review_inventory["campaign_abandonments_truncated"]
        )
        self.assertEqual(
            review_inventory["campaign_abandonments"][0]["path"],
            abandonment_path.relative_to(self.paths.root).as_posix(),
        )
        self.assertEqual(
            len(
                review_inventory[
                    "campaign_abandonments_inventory_sha256"
                ]
            ),
            64,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "campaign .* is abandoned; no new confirmatory Run",
        ):
            self.confirmatory_run("CONF-0001", "SLOT-001")

        with self.assertRaisesRegex(
            ValidationError,
            "campaign restart rationale must be non-empty",
        ):
            self.make_confirmation(
                "CONF-0002",
                ["SLOT-001"],
                restart_rationale="",
            )
        restart_draft_path = (
            self.paths.active_work / "CONF-0002.confirmation.draft.json"
        )
        restart_draft = load_json(restart_draft_path)
        restart_draft["campaign"]["restart_rationale"] = (
            "Start a new frozen campaign after the explicitly authorized "
            "whole-campaign abandonment."
        )
        atomic_write_json(restart_draft_path, restart_draft)
        second = load_json(
            finalize_confirmation(self.paths, restart_draft_path)
        )
        self.assertNotEqual(
            second["campaign"]["campaign_id"],
            first["campaign"]["campaign_id"],
        )
        self.assertEqual(second["campaign"]["sequence"], 1)
        self.assertEqual(second["campaign"]["predecessor"], None)
        self.assertEqual(
            second["campaign"]["predecessor_campaign"],
            {
                "campaign_id": campaign_id,
                "abandonment_sha256": abandonment["record_sha256"],
            },
        )
        self.assertTrue(second["campaign"]["restart_rationale"].strip())
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(errors_only(validate_study(self.paths)), [])

    def test_campaign_abandonment_requires_complete_explicit_authorization(
        self,
    ) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        valid = self.abandonment_decision(campaign_id)

        missing_authorization = dict(valid)
        missing_authorization.pop("authorization")
        unauthorized = dict(valid)
        unauthorized["authorization"] = {
            "source": "agent_inference",
            "instruction": "The Agent inferred that this campaign should be abandoned.",
        }
        missing_instruction = dict(valid)
        missing_instruction["authorization"] = {
            "source": "explicit_user_instruction",
            "instruction": "",
        }
        mismatched_campaign = dict(valid)
        mismatched_campaign["campaign_id"] = "CAMP-" + ("0" * 64)

        for label, decision, message in (
            (
                "missing authorization",
                missing_authorization,
                "must contain exactly input_version=1",
            ),
            (
                "unauthorized source",
                unauthorized,
                "requires an explicit user instruction",
            ),
            (
                "missing instruction",
                missing_instruction,
                "authorization instruction must be non-empty",
            ),
            (
                "mismatched campaign",
                mismatched_campaign,
                "does not match the selected Confirmation",
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValidationError, message):
                    abandon_confirmation_campaign(
                        self.paths,
                        "CONF-0001",
                        decision,
                    )
                self.assertIsNone(
                    load_confirmation_campaign_abandonment(
                        self.paths, campaign_id
                    )
                )

    def test_confirmation_abandon_cli_dispatches_decision_file(self) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        decision_path = self.root / ".objects" / "confirmation-abandon.json"
        atomic_write_json(
            decision_path,
            self.abandonment_decision(campaign_id),
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = studyctl_main(
                [
                    "--root",
                    str(self.root),
                    "confirmation-abandon",
                    self.paths.study_id,
                    "CONF-0001",
                    "--file",
                    str(decision_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        artifact_path = Path(stdout.getvalue().strip())
        self.assertEqual(
            artifact_path,
            self.paths.confirmations / f"{campaign_id}.abandonment.json",
        )
        self.assertEqual(
            load_json(artifact_path)["campaign_id"],
            campaign_id,
        )

    def test_abandonment_validation_rejects_tampered_authorization_hash(
        self,
    ) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        artifact_path = abandon_confirmation_campaign(
            self.paths,
            "CONF-0001",
            self.abandonment_decision(campaign_id),
        )
        os.chmod(artifact_path, 0o644)
        artifact = load_json(artifact_path)
        artifact["authorization"]["instruction"] = (
            "A different instruction that was never authorized."
        )
        artifact["record_sha256"] = record_digest(
            artifact, "record_sha256"
        )
        atomic_write_json(artifact_path, artifact, mode=0o444)

        messages = [
            issue.message for issue in errors_only(validate_study(self.paths))
        ]
        self.assertTrue(
            any(
                "authorization instruction hash is invalid" in message
                for message in messages
            ),
            messages,
        )

    def test_selective_slot_abandonment_is_prohibited(self) -> None:
        confirmation = self.make_confirmation(
            "CONF-0001",
            ["SLOT-001", "SLOT-002"],
        )
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        partial = self.abandonment_decision(campaign_id)
        partial["slot_ids"] = ["CONF-0001/SLOT-001"]

        with self.assertRaisesRegex(
            ValidationError,
            "selective Confirmation record or slot abandonment is prohibited",
        ):
            abandon_confirmation_campaign(
                self.paths,
                "CONF-0001",
                partial,
            )
        self.assertIsNone(
            load_confirmation_campaign_abandonment(self.paths, campaign_id)
        )
        run = self.confirmatory_run("CONF-0001", "SLOT-001")
        self.assertIn(
            run["status"],
            {"succeeded", "failed", "interrupted", "incomplete"},
        )

    def test_abandoned_campaign_loses_support_strength_but_keeps_negative_history(
        self,
    ) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        run = self.confirmatory_run("CONF-0001", "SLOT-001")
        original_draft = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        self.populate_evidence(original_draft)
        original = load_json(finalize_evidence(self.paths, original_draft))
        self.support_claim(self.paths, original)
        self.assertEqual(
            load_json(self.paths.claims)["claims"][0]["state"],
            "numerically_supported",
        )

        campaign_id = str(confirmation["campaign"]["campaign_id"])
        abandon_confirmation_campaign(
            self.paths,
            "CONF-0001",
            self.abandonment_decision(campaign_id),
        )
        messages = [
            issue.message for issue in errors_only(validate_study(self.paths))
        ]
        self.assertTrue(
            any(
                "numerically_supported requires fresh held-out or not-applicable "
                "confirmatory Evidence" in message
                for message in messages
            ),
            messages,
        )
        self.assertFalse(
            any(
                "Evidence evidence_basis does not match" in message
                for message in messages
            ),
            messages,
        )

        supporting_draft = create_evidence_draft(
            self.paths,
            "EVID-0002",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        supporting = load_json(supporting_draft)
        supporting["observation_ref"] = original["observation_ref"]
        atomic_write_json(supporting_draft, supporting)
        self.populate_evidence(supporting_draft)
        with self.assertRaisesRegex(
            ValidationError,
            "abandoned Confirmation campaign .* cannot form a supporting "
            "confirmatory Evidence basis",
        ):
            finalize_evidence(self.paths, supporting_draft)
        self.assertEqual(load_json(supporting_draft)["status"], "draft")

        negative_draft = create_evidence_draft(
            self.paths,
            "EVID-0003",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        negative = load_json(negative_draft)
        negative["observation_ref"] = original["observation_ref"]
        atomic_write_json(negative_draft, negative)
        negative = self.populate_evidence(negative_draft)
        negative["runs"][0]["role"] = "contradictory"
        negative["assessment"] = "contradicts"
        atomic_write_json(negative_draft, negative)
        finalized_negative = load_json(
            finalize_evidence(self.paths, negative_draft)
        )
        self.assertEqual(finalized_negative["assessment"], "contradicts")
        self.assertEqual(
            finalized_negative["runs"][0]["role"],
            "contradictory",
        )

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

    def test_confirmation_authority_sequence_detects_deleted_abandonment(
        self,
    ) -> None:
        initial = load_confirmation_sequence(self.paths)
        self.assertIsNotNone(initial)
        assert initial is not None
        self.assertEqual(initial["high_water_mark"], 0)

        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        after_confirmation = load_confirmation_sequence(self.paths)
        self.assertIsNotNone(after_confirmation)
        assert after_confirmation is not None
        self.assertEqual(after_confirmation["high_water_mark"], 1)

        abandonment_path = abandon_confirmation_campaign(
            self.paths,
            "CONF-0001",
            self.abandonment_decision(campaign_id),
        )
        abandonment = load_json(abandonment_path)
        after_abandonment = load_confirmation_sequence(self.paths)
        self.assertIsNotNone(after_abandonment)
        assert after_abandonment is not None
        self.assertEqual(after_abandonment["high_water_mark"], 2)

        abandonment_path.unlink()
        messages = [
            issue.message for issue in errors_only(validate_study(self.paths))
        ]
        self.assertTrue(
            any(
                "does not match the sequence count" in message
                for message in messages
            ),
            messages,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "does not match the sequence count",
        ):
            load_confirmation_campaign_abandonment(
                self.paths,
                campaign_id,
            )

        atomic_write_json(
            abandonment_path,
            abandonment,
            overwrite=False,
            mode=0o444,
        )
        self.assertEqual(errors_only(validate_study(self.paths)), [])

    def test_explicit_legacy_confirmation_migration_binds_complete_history(
        self,
    ) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        self.rewrite_confirmation_as_legacy_v3("CONF-0001")
        self.paths.confirmation_sequence.unlink()

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = studyctl_main(
                [
                    "--root",
                    str(self.root),
                    "migrate-confirmation-sequence",
                    self.paths.study_id,
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            Path(stdout.getvalue().strip()),
            self.paths.confirmation_sequence,
        )
        sequence = require_consistent_confirmation_authority(self.paths)
        self.assertEqual(sequence["high_water_mark"], 1)
        self.assertEqual(
            sequence["inventory_sha256"],
            sha256_json(confirmation_authority_inventory(self.paths)),
        )
        self.assertEqual(errors_only(validate_study(self.paths)), [])
        with self.assertRaisesRegex(
            WorkflowError,
            "already exists",
        ):
            migrate_legacy_confirmation_sequence(self.paths)

    def test_recovery_rejects_legacy_record_that_requires_migration(
        self,
    ) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        self.rewrite_confirmation_as_legacy_v3("CONF-0001")
        write_confirmation_sequence(
            self.paths,
            empty_confirmation_sequence(self.paths),
        )

        with self.assertRaisesRegex(
            ValidationError,
            "accepts only a current schema-v4 record",
        ):
            recover_confirmation_sequence(self.paths)

        sequence = load_confirmation_sequence(self.paths)
        self.assertIsNotNone(sequence)
        assert sequence is not None
        self.assertEqual(sequence["high_water_mark"], 0)

    def test_legacy_confirmation_migration_rejects_empty_history(
        self,
    ) -> None:
        self.paths.confirmation_sequence.unlink()

        with self.assertRaisesRegex(
            ValidationError,
            "non-empty finalized history",
        ):
            migrate_legacy_confirmation_sequence(self.paths)

        self.assertFalse(self.paths.confirmation_sequence.exists())

    def test_legacy_confirmation_migration_rejects_current_v4_record(
        self,
    ) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        self.paths.confirmation_sequence.unlink()

        with self.assertRaisesRegex(
            ValidationError,
            "only schema v2/v3",
        ):
            migrate_legacy_confirmation_sequence(self.paths)

        self.assertFalse(self.paths.confirmation_sequence.exists())

    def test_legacy_confirmation_migration_rejects_forged_study_binding(
        self,
    ) -> None:
        self.make_confirmation(
            "CONF-0001",
            ["SLOT-001"],
        )
        legacy_path = self.rewrite_confirmation_as_legacy_v3("CONF-0001")
        forged = load_json(legacy_path)
        forged["study_id"] = "SC-9999"
        forged["record_sha256"] = None
        forged["record_sha256"] = record_digest(
            forged,
            "record_sha256",
        )
        atomic_write_json(legacy_path, forged, mode=0o444)
        self.paths.confirmation_sequence.unlink()

        with self.assertRaisesRegex(
            ValidationError,
            "study_id does not match",
        ):
            migrate_legacy_confirmation_sequence(self.paths)

        self.assertFalse(self.paths.confirmation_sequence.exists())

    def test_legacy_confirmation_migration_rejects_stale_record_digest(
        self,
    ) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        legacy_path = self.rewrite_confirmation_as_legacy_v3("CONF-0001")
        stale = load_json(legacy_path)
        stale["analysis_plan"]["method"] = (
            "Tampered method that is not covered by record_sha256."
        )
        atomic_write_json(legacy_path, stale, mode=0o444)
        self.paths.confirmation_sequence.unlink()

        with self.assertRaisesRegex(
            ValidationError,
            "record_sha256 does not match",
        ):
            migrate_legacy_confirmation_sequence(self.paths)

        self.assertFalse(self.paths.confirmation_sequence.exists())

    def test_legacy_confirmation_migration_rejects_writable_record(
        self,
    ) -> None:
        self.make_confirmation("CONF-0001", ["SLOT-001"])
        legacy_path = self.rewrite_confirmation_as_legacy_v3("CONF-0001")
        os.chmod(legacy_path, 0o644)
        self.paths.confirmation_sequence.unlink()

        with self.assertRaisesRegex(
            ValidationError,
            "sealed read-only",
        ):
            migrate_legacy_confirmation_sequence(self.paths)

        self.assertFalse(self.paths.confirmation_sequence.exists())

    def test_broken_abandonment_symlink_is_invalid_not_absent(self) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        abandonment_path = (
            self.paths.confirmations
            / f"{campaign_id}.abandonment.json"
        )
        abandonment_path.symlink_to(
            self.paths.confirmations / "missing-abandonment-target.json"
        )

        with self.assertRaisesRegex(
            ValidationError,
            "regular, non-linked file",
        ):
            load_confirmation_campaign_abandonment(
                self.paths,
                campaign_id,
            )
        messages = [
            issue.message for issue in errors_only(validate_study(self.paths))
        ]
        self.assertTrue(
            any("must not use symbolic links" in message for message in messages),
            messages,
        )

    def test_interrupted_abandonment_has_one_forward_recovery(self) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        with patch(
            "tools.studyctl.confirmation.advance_confirmation_sequence",
            side_effect=RuntimeError("simulated sequence interruption"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated sequence interruption",
            ):
                abandon_confirmation_campaign(
                    self.paths,
                    "CONF-0001",
                    self.abandonment_decision(campaign_id),
                )

        abandonment_path = (
            self.paths.confirmations
            / f"{campaign_id}.abandonment.json"
        )
        self.assertTrue(abandonment_path.is_file())
        with self.assertRaisesRegex(
            ValidationError,
            "left unindexed",
        ):
            load_confirmation_campaign_abandonment(
                self.paths,
                campaign_id,
            )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = studyctl_main(
                [
                    "--root",
                    str(self.root),
                    "recover-confirmation-sequence",
                    self.paths.study_id,
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            Path(stdout.getvalue().strip()),
            self.paths.confirmation_sequence,
        )
        self.assertEqual(
            load_confirmation_campaign_abandonment(
                self.paths,
                campaign_id,
            )["campaign_id"],
            campaign_id,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "exactly one unindexed record",
        ):
            recover_confirmation_sequence(self.paths)

    def test_interrupted_confirmation_finalization_has_forward_recovery(
        self,
    ) -> None:
        with patch(
            "tools.studyctl.confirmation.advance_confirmation_sequence",
            side_effect=RuntimeError("simulated confirmation interruption"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated confirmation interruption",
            ):
                self.make_confirmation("CONF-0001", ["SLOT-001"])

        record_path = self.paths.confirmations / "CONF-0001.json"
        self.assertTrue(record_path.is_file())
        with self.assertRaisesRegex(
            ValidationError,
            "left unindexed",
        ):
            create_confirmation_draft(
                self.paths,
                "CONF-0002",
                ["CLAIM-0001"],
            )
        temporary = (
            self.paths.study
            / ".CONFIRMATIONS.sequence.json.crash.tmp"
        )
        temporary.write_text("unfinished\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ValidationError,
            "refuses unfinished sequence temporary files",
        ):
            recover_confirmation_sequence(self.paths)
        sequence = load_confirmation_sequence(self.paths)
        self.assertIsNotNone(sequence)
        assert sequence is not None
        self.assertEqual(sequence["high_water_mark"], 0)
        temporary.unlink()

        recover_confirmation_sequence(self.paths)
        self.assertEqual(
            load_json(record_path)["confirmation_id"],
            "CONF-0001",
        )
        self.assertEqual(errors_only(validate_study(self.paths)), [])

    def test_stale_pre_abandonment_draft_requires_explicit_disposition(
        self,
    ) -> None:
        confirmation = self.make_confirmation("CONF-0001", ["SLOT-001"])
        campaign_id = str(confirmation["campaign"]["campaign_id"])
        stale_path = create_confirmation_draft(
            self.paths,
            "CONF-0002",
            ["CLAIM-0001"],
        )
        with self.assertRaisesRegex(
            ValidationError,
            "resumable Confirmation draft cannot be discarded",
        ):
            discard_stale_confirmation_draft(
                self.paths,
                "CONF-0002",
                reason="This reason must not permit discarding active work.",
            )
        abandon_confirmation_campaign(
            self.paths,
            "CONF-0001",
            self.abandonment_decision(campaign_id),
        )

        selector = build_active_selector(self.paths)["confirmations"]
        self.assertEqual(selector["drafts"]["total_count"], 0)
        self.assertEqual(selector["stale_drafts"]["total_count"], 1)
        self.assertEqual(
            selector["stale_drafts"]["items"][0]["confirmation_id"],
            "CONF-0002",
        )
        self.assertEqual(
            selector["stale_drafts"]["items"][0]["draft_state"],
            "stale",
        )
        with self.assertRaisesRegex(
            WorkflowError,
            "active Confirmation draft already targets the same exact "
            "Claim-version set",
        ):
            create_confirmation_draft(
                self.paths,
                "CONF-0003",
                ["CLAIM-0001"],
            )

        disposition = discard_stale_confirmation_draft(
            self.paths,
            "CONF-0002",
            reason=(
                "The campaign lifecycle advanced through an authorized whole-"
                "campaign abandonment."
            ),
        )
        self.assertFalse(stale_path.exists())
        self.assertTrue(disposition.is_file())
        disposition_value = load_json(disposition)
        self.assertEqual(disposition_value["classification"], "stale")
        restarted = create_confirmation_draft(
            self.paths,
            "CONF-0003",
            ["CLAIM-0001"],
        )
        restarted_value = load_json(restarted)
        self.assertEqual(
            restarted_value["campaign"]["predecessor_campaign"]["campaign_id"],
            campaign_id,
        )

    def test_new_run_and_supporting_evidence_require_complete_active_chain(
        self,
    ) -> None:
        first = self.make_confirmation("CONF-0001", ["SLOT-001"])
        predecessor_campaign_id = str(first["campaign"]["campaign_id"])
        abandonment_path = abandon_confirmation_campaign(
            self.paths,
            "CONF-0001",
            self.abandonment_decision(predecessor_campaign_id),
        )
        second = self.make_confirmation(
            "CONF-0002",
            ["SLOT-001", "SLOT-002"],
        )
        run = self.confirmatory_run("CONF-0002", "SLOT-001")
        evidence_draft = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        self.populate_evidence(evidence_draft)

        # Simulate a locally recomputed visible inventory after deleting the
        # predecessor decision. The active-tail replay must still reject the
        # orphan successor even though its own immutable bytes are untouched.
        abandonment_path.unlink()
        sequence = load_confirmation_sequence(self.paths)
        self.assertIsNotNone(sequence)
        assert sequence is not None
        visible = confirmation_authority_inventory(self.paths)
        sequence["high_water_mark"] = len(visible)
        sequence["inventory_sha256"] = sha256_json(visible)
        write_confirmation_sequence(self.paths, sequence)

        validate_confirmation_run(self.paths, second, run)
        with self.assertRaisesRegex(
            ValidationError,
            "references a missing predecessor abandonment",
        ):
            self.confirmatory_run("CONF-0002", "SLOT-002")
        with self.assertRaisesRegex(
            ValidationError,
            "references a missing predecessor abandonment",
        ):
            finalize_evidence(self.paths, evidence_draft)
        self.assertEqual(load_json(evidence_draft)["status"], "draft")


if __name__ == "__main__":
    unittest.main()
