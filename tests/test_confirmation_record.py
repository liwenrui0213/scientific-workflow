from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import time
import unittest
from typing import Callable

from tests.helpers import WorkflowTestCase, completed_process
from tools.studyctl.active_context import build_active_selector
from tools.studyctl.compaction import prepare_compaction
from tools.studyctl.confirmation import (
    create_confirmation_draft,
    finalize_confirmation,
    load_final_confirmation,
    validate_confirmation_run,
)
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
)
from tools.studyctl.models import StudyPaths, ValidationError
from tools.studyctl.rendering import active_formal_artifacts, render_status
from tools.studyctl.run_ledger import load_ledger, write_ledger
from tools.studyctl.run_registry import execute_run
from tools.studyctl.validation import errors_only, validate_study


class ConfirmationRecordTests(WorkflowTestCase):
    argv = [sys.executable, "-c", "print(4)"]
    held_out_name = "held-out-confirmation.txt"

    def setUp(self) -> None:
        super().setUp()
        self.paths = self.initialize()
        self.fill_brief(self.paths)
        self.add_proposed_claim(self.paths)
        self.candidate = self.root / "src" / "frozen_candidate.py"
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        self.candidate.write_text("VALUE = 4\n", encoding="utf-8")
        relative = self.candidate.relative_to(self.root).as_posix()
        result = completed_process(["git", "add", relative], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = completed_process(
            ["git", "commit", "-m", "add confirmation fixture candidate"],
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        atomic_write_json(
            self.paths.formal / "PROTOCOL.json",
            {
                "schema_version": 1,
                "status": "active",
                "purpose": "Freeze one exact deterministic confirmation.",
                "hypotheses": ["The deterministic result equals four."],
                "inputs": [],
                "evaluator": "formal/EVALUATOR.json",
                "dataset_split": None,
                "baseline": None,
                "acceptance_criteria": ["The process exits successfully."],
                "compute_budget": {
                    "estimated_gpu_hours": 0,
                    "estimated_cpu_hours": 0,
                },
                "seeds": [],
                "cohort_fields": {},
                "known_deviations": [],
            },
        )
        atomic_write_json(
            self.paths.formal / "EVALUATOR.json",
            {
                "schema_version": 1,
                "status": "active",
                "metric": "exact integer equality",
            },
        )
        self.approve(self.paths)

    def _held_out(self) -> Path:
        path = self.root / ".objects" / self.held_out_name
        path.write_text("unseen fixture\n", encoding="utf-8")
        return path

    def _finalize(
        self,
        *,
        confirmation_id: str = "CONF-0001",
        held_out: Path | None = None,
        before_finalize: Callable[[Path], None] | None = None,
    ) -> dict[str, object]:
        draft_path = create_confirmation_draft(
            self.paths,
            confirmation_id,
            ["CLAIM-0001"],
        )
        draft = load_json(draft_path)
        draft["candidates"][0].update(
            {
                "description": "The committed deterministic candidate.",
                "paths": [self.candidate.relative_to(self.root).as_posix()],
            }
        )
        draft["analysis_plan"].update(
            {
                "method": "Compare the exact result with four.",
                "primary_outcomes": ["process exit status"],
                "decision_rule": "Support only when the process exits successfully.",
                "stopping_rule": "Stop after the single frozen slot.",
                "exclusion_rule": "No result-dependent exclusion is allowed.",
            }
        )
        if held_out is None:
            draft["held_out"].update(
                {
                    "status": "not_applicable",
                    "description": (
                        "This exact deterministic fixture has no sampled data or "
                        "condition that can be held out."
                    ),
                }
            )
        inputs: list[str] = []
        if held_out is not None:
            inputs = [held_out.relative_to(self.root).as_posix()]
            draft["held_out"].update(
                {
                    "status": "held_out",
                    "description": "Workflow-observed fresh held-out bytes.",
                    "paths": list(inputs),
                }
            )
        draft["run_slots"][0].update(
            {
                "argv": list(self.argv),
                "seed": None,
                "hardware_class": "test-cpu",
                "precision": "exact-integer",
                "cohort_fields": {},
                "input_paths": list(inputs),
            }
        )
        atomic_write_json(draft_path, draft)
        if before_finalize is not None:
            before_finalize(draft_path)
        record_path = finalize_confirmation(self.paths, draft_path)
        record = load_json(record_path)
        self.assertIsInstance(record, dict)
        return record

    def _confirmatory_run(self, held_out: Path | None = None) -> dict[str, object]:
        return execute_run(
            self.paths,
            argv=list(self.argv),
            purpose="new post-registration confirmatory execution",
            epistemic_mode="confirmatory",
            confirmation_id="CONF-0001",
            confirmation_slot="SLOT-001",
            input_paths=[] if held_out is None else [held_out],
            hardware_class="test-cpu",
            precision="exact-integer",
        )

    def test_confirmation_requires_approved_brief_and_freezes_read_only_record(self) -> None:
        unapproved = self.initialize("SC-0002")
        self.fill_brief(unapproved)
        self.add_proposed_claim(unapproved)
        with self.assertRaisesRegex(ValidationError, "human-approved Brief"):
            create_confirmation_draft(unapproved, "CONF-0002", ["CLAIM-0001"])

        record = self._finalize()
        record_path = self.paths.confirmations / "CONF-0001.json"

        self.assertEqual(record["record_sha256"], record_digest(record, "record_sha256"))
        self.assertEqual(record_path.stat().st_mode & 0o222, 0)
        self.assertEqual(record_path.stat().st_nlink, 1)
        self.assertEqual(
            record["held_out"]["workflow_observed_run_high_water_mark"],
            0,
        )
        self.assertEqual(errors_only(validate_study(self.paths)), [])

        os.chmod(record_path, 0o644)
        messages = "\n".join(
            issue.render() for issue in errors_only(validate_study(self.paths))
        )
        self.assertIn("must be read-only", messages)
        os.chmod(record_path, 0o444)

    def test_default_draft_cannot_accidentally_claim_held_out_not_applicable(self) -> None:
        draft_path = create_confirmation_draft(
            self.paths,
            "CONF-0001",
            ["CLAIM-0001"],
        )
        draft = load_json(draft_path)
        self.assertEqual(draft["held_out"]["status"], "not_held_out")
        self.assertEqual(draft["held_out"]["description"], "")
        draft["candidates"][0].update(
            {
                "description": "The committed deterministic candidate.",
                "paths": [self.candidate.relative_to(self.root).as_posix()],
            }
        )
        draft["analysis_plan"].update(
            {
                "method": "Compare the exact result with four.",
                "primary_outcomes": ["process exit status"],
                "decision_rule": "Support only on a zero exit status.",
                "stopping_rule": "Stop after one frozen slot.",
                "exclusion_rule": "No result-dependent exclusion is allowed.",
            }
        )
        draft["run_slots"][0].update(
            {
                "argv": list(self.argv),
                "hardware_class": "test-cpu",
                "precision": "exact-integer",
            }
        )
        atomic_write_json(draft_path, draft)

        with self.assertRaisesRegex(ValidationError, "held-out description"):
            finalize_confirmation(self.paths, draft_path)

    def test_finalized_confirmation_rejects_null_frozen_at_after_reseal(self) -> None:
        self._finalize()
        record_path = self.paths.confirmations / "CONF-0001.json"
        os.chmod(record_path, 0o644)
        record = load_json(record_path)
        record["frozen_at"] = None
        record["record_sha256"] = record_digest(record, "record_sha256")
        atomic_write_json(record_path, record, mode=0o444)

        messages = "\n".join(
            issue.render() for issue in errors_only(validate_study(self.paths))
        )
        self.assertIn("frozen_at", messages)

    def test_resume_projections_expose_drafts_and_pending_confirmation_slots(self) -> None:
        self._finalize()
        create_confirmation_draft(
            self.paths,
            "CONF-0002",
            ["CLAIM-0001"],
        )

        selector = build_active_selector(self.paths)
        confirmations = selector["confirmations"]
        self.assertEqual(
            [item["confirmation_id"] for item in confirmations["drafts"]["items"]],
            ["CONF-0002"],
        )
        self.assertEqual(confirmations["history"]["total_count"], 1)
        self.assertEqual(confirmations["history"]["pending_count"], 1)
        self.assertEqual(confirmations["history"]["awaiting_evidence_count"], 0)
        self.assertEqual(confirmations["history"]["completed_count"], 0)
        pending = confirmations["pending_finalized"]["items"]
        self.assertEqual([item["confirmation_id"] for item in pending], ["CONF-0001"])
        self.assertEqual(pending[0]["pending_slot_ids"]["items"], ["SLOT-001"])
        self.assertEqual(pending[0]["pending_slot_ids"]["total_count"], 1)
        self.assertFalse(pending[0]["pending_slot_ids"]["truncated"])
        self.assertNotIn(
            "formal/confirmations/CONF-0001.json",
            {item["path"] for item in active_formal_artifacts(self.paths)},
        )

        status_path = render_status(self.paths)
        status = status_path.read_text(encoding="utf-8")
        self.assertIn("## Pending Confirmation Work", status)
        self.assertIn("Draft `CONF-0002`", status)
        self.assertIn("`CONF-0001`: 1 pending slot(s)", status)
        prepared = load_json(prepare_compaction(self.paths))
        self.assertEqual(prepared["confirmations"], confirmations)

        run = self._confirmatory_run()
        resumed = build_active_selector(self.paths)["confirmations"]
        self.assertEqual(resumed["pending_finalized"]["total_count"], 0)
        self.assertEqual(resumed["awaiting_evidence"]["total_count"], 1)
        self.assertEqual(
            resumed["awaiting_evidence"]["items"][0]["confirmation_id"],
            "CONF-0001",
        )
        self.assertEqual(resumed["history"]["pending_count"], 0)
        self.assertEqual(resumed["history"]["awaiting_evidence_count"], 1)
        self.assertEqual(resumed["history"]["completed_count"], 0)
        self.assertEqual(resumed["history"]["items"][0]["consumed_slot_count"], 1)
        resumed_status = render_status(self.paths).read_text(encoding="utf-8")
        self.assertIn(
            "`CONF-0001` consumed all planned slots and now awaits a confirmatory Evidence draft.",
            resumed_status,
        )

        evidence_draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        draft_projection = build_active_selector(self.paths)["confirmations"]
        self.assertEqual(draft_projection["awaiting_evidence"]["total_count"], 1)
        self.assertEqual(draft_projection["history"]["completed_count"], 0)
        draft_locator = draft_projection["awaiting_evidence"]["items"][0][
            "evidence_drafts"
        ]
        self.assertEqual(draft_locator["total_count"], 1)
        self.assertEqual(draft_locator["items"][0]["evidence_id"], "EVID-0001")
        evidence_draft = load_json(evidence_draft_path)
        evidence_draft["addresses"]["question"] = "Does the frozen candidate return four?"
        evidence_draft["runs"][0]["role"] = "supporting"
        evidence_draft["result"] = {"value": 4}
        evidence_draft["scope"] = "the single frozen deterministic slot"
        evidence_draft["uncertainty"] = "No sampling uncertainty."
        evidence_draft["limitations"] = ["Only one deterministic candidate is tested."]
        evidence_draft["assessment"] = "supports"
        atomic_write_json(evidence_draft_path, evidence_draft)
        finalize_evidence(self.paths, evidence_draft_path)

        represented = build_active_selector(self.paths)["confirmations"]
        self.assertEqual(represented["awaiting_evidence"]["total_count"], 0)
        self.assertEqual(represented["history"]["completed_count"], 1)

        second_draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        self.assertEqual(second_draft_path.name, "EVID-0001.v0002.json")
        revision = build_active_selector(self.paths)["confirmations"]
        self.assertEqual(revision["history"]["completed_count"], 1)
        self.assertEqual(revision["awaiting_evidence"]["total_count"], 1)
        revision_locator = revision["awaiting_evidence"]["items"][0][
            "evidence_drafts"
        ]
        self.assertEqual(revision_locator["total_count"], 1)
        self.assertEqual(revision_locator["items"][0]["evidence_id"], "EVID-0001")
        self.assertEqual(revision_locator["items"][0]["version"], 2)
        revision_status = render_status(self.paths).read_text(encoding="utf-8")
        self.assertIn(
            "`CONF-0001` has confirmatory Evidence draft(s) to resume: "
            "`EVID-0001` v2",
            revision_status,
        )

    def test_running_confirmation_projection_exposes_slot_and_run_locator(
        self,
    ) -> None:
        self.argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(1); print(4)",
        ]
        self._finalize()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._confirmatory_run)
            manifest_path: Path | None = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                manifests = sorted(self.paths.runs.glob("RUN-*/manifest.json"))
                if manifests and load_json(manifests[0]).get("status") == "running":
                    manifest_path = manifests[0]
                    break
                if future.done():
                    break
                time.sleep(0.01)
            self.assertIsNotNone(
                manifest_path,
                "confirmatory Run never published its running Manifest",
            )
            assert manifest_path is not None

            confirmations = build_active_selector(self.paths)["confirmations"]
            self.assertEqual(confirmations["in_progress"]["total_count"], 1)
            record = confirmations["in_progress"]["items"][0]
            self.assertEqual(record["confirmation_id"], "CONF-0001")
            self.assertEqual(record["running_slot_count"], 1)
            running = record["running_slots"]
            self.assertEqual(running["total_count"], 1)
            self.assertEqual(running["selected_count"], 1)
            self.assertFalse(running["truncated"])
            self.assertEqual(len(running["inventory_sha256"]), 64)
            locator = running["items"][0]
            self.assertEqual(locator["slot_id"], "SLOT-001")
            self.assertEqual(locator["run_id"], manifest_path.parent.name)
            self.assertEqual(locator["status"], "running")
            self.assertEqual(
                locator["path"],
                manifest_path.relative_to(self.paths.root).as_posix(),
            )
            self.assertEqual(locator["size"], manifest_path.stat().st_size)
            self.assertEqual(locator["sha256"], sha256_file(manifest_path))

            status = render_status(self.paths).read_text(encoding="utf-8")
            self.assertIn("`SLOT-001` as `RUN-000001`", status)
            result = future.result(timeout=10)

        self.assertEqual(result["status"], "succeeded")

    def test_confirmation_resume_index_is_bounded_and_commits_full_history(self) -> None:
        def expand_slots(draft_path: Path) -> None:
            draft = load_json(draft_path)
            base_slot = draft["run_slots"][0]
            draft["run_slots"] = [
                {**base_slot, "slot_id": f"SLOT-{number:03d}"}
                for number in range(1, 21)
            ]
            atomic_write_json(draft_path, draft)

        self._finalize(
            confirmation_id="CONF-0001",
            before_finalize=expand_slots,
        )
        for number in range(2, 11):
            self._finalize(confirmation_id=f"CONF-{number:04d}")

        confirmations = build_active_selector(self.paths)["confirmations"]
        pending = confirmations["pending_finalized"]
        history = confirmations["history"]

        self.assertEqual(pending["total_count"], 10)
        self.assertEqual(pending["selected_count"], 8)
        self.assertTrue(pending["truncated"])
        self.assertEqual(history["total_count"], 10)
        self.assertEqual(history["selected_count"], 8)
        self.assertTrue(history["truncated"])
        self.assertEqual(len(pending["inventory_sha256"]), 64)
        self.assertEqual(len(history["inventory_sha256"]), 64)
        self.assertEqual(
            [item["confirmation_id"] for item in pending["items"]],
            [f"CONF-{number:04d}" for number in range(1, 9)],
        )
        first_slots = pending["items"][0]["pending_slot_ids"]
        self.assertEqual(first_slots["total_count"], 20)
        self.assertEqual(first_slots["selected_count"], 16)
        self.assertTrue(first_slots["truncated"])
        self.assertEqual(
            first_slots["items"],
            [f"SLOT-{number:03d}" for number in range(1, 17)],
        )

    def test_post_freeze_exploratory_use_invalidates_fresh_held_out_slot(self) -> None:
        held_out = self._held_out()
        record = self._finalize(held_out=held_out)
        self.assertEqual(record["held_out"]["freshness"], "fresh")

        exploratory = execute_run(
            self.paths,
            argv=list(self.argv),
            purpose="an exploratory look at data frozen as held out",
            input_paths=[held_out],
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        self.assertEqual(exploratory["epistemic_role"]["mode"], "exploratory")

        with self.assertRaisesRegex(
            ValidationError,
            "held-out inputs were used after Confirmation freeze",
        ):
            self._confirmatory_run(held_out)
        self.assertEqual(len(list(self.paths.runs.glob("RUN-*/manifest.json"))), 1)

    def test_confirmation_freeze_fails_closed_when_run_history_is_hidden(self) -> None:
        held_out = self._held_out()
        exploratory = execute_run(
            self.paths,
            argv=list(self.argv),
            purpose="prior exploratory use that must remain visible",
            input_paths=[held_out],
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        manifest_path = (
            self.paths.runs / str(exploratory["run_id"]) / "manifest.json"
        )
        hidden_path = manifest_path.with_name("manifest.hidden.json")

        def hide_history(_: Path) -> None:
            manifest_path.rename(hidden_path)

        try:
            with self.assertRaisesRegex(
                ValidationError,
                "Run registry structure is invalid|missing Run Manifest",
            ):
                self._finalize(held_out=held_out, before_finalize=hide_history)
        finally:
            if hidden_path.exists():
                hidden_path.rename(manifest_path)
        self.assertFalse((self.paths.confirmations / "CONF-0001.json").exists())

    def test_historical_confirmatory_run_survives_later_workspace_evolution(self) -> None:
        self._finalize()
        run = self._confirmatory_run()

        self.candidate.write_text("VALUE = 5\n", encoding="utf-8")
        protocol = load_json(self.paths.formal / "PROTOCOL.json")
        protocol["purpose"] = "A later protocol version for future work."
        atomic_write_json(self.paths.formal / "PROTOCOL.json", protocol)

        record = load_final_confirmation(self.paths, "CONF-0001")
        validate_confirmation_run(self.paths, record, run)
        draft_path = create_evidence_draft(
            self.paths,
            "EVID-0001",
            ["CLAIM-0001"],
            [str(run["run_id"])],
        )
        draft = load_json(draft_path)
        draft["addresses"]["question"] = "Does the frozen candidate return four?"
        draft["runs"][0]["role"] = "supporting"
        draft["result"] = {"value": 4}
        draft["scope"] = "the frozen candidate and registered exact protocol"
        draft["uncertainty"] = "No sampling uncertainty."
        draft["limitations"] = ["Later candidate versions are outside this Evidence."]
        draft["assessment"] = "supports"
        atomic_write_json(draft_path, draft)

        finalized = load_json(finalize_evidence(self.paths, draft_path))
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(finalized["evidence_basis"]["mode"], "confirmatory")

    def test_immutable_run_must_match_frozen_slot(self) -> None:
        self._finalize()
        run = self._confirmatory_run()
        record = load_final_confirmation(self.paths, "CONF-0001")
        changed = dict(run)
        changed["execution"] = dict(run["execution"])
        changed["execution"]["argv"] = [sys.executable, "-c", "print(5)"]

        with self.assertRaisesRegex(ValidationError, "argv"):
            validate_confirmation_run(self.paths, record, changed)

    def test_pre_freeze_exploratory_run_cannot_be_resealed_as_confirmatory(self) -> None:
        exploratory = execute_run(
            self.paths,
            argv=list(self.argv),
            purpose="exploration that predates the confirmation boundary",
            hardware_class="test-cpu",
            precision="exact-integer",
        )
        record = self._finalize()
        self.assertEqual(
            record["held_out"]["workflow_observed_run_high_water_mark"],
            1,
        )

        run_id = str(exploratory["run_id"])
        manifest_path = self.paths.runs / run_id / "manifest.json"
        forged = load_json(manifest_path)
        forged["epistemic_role"] = {
            "mode": "confirmatory",
            "confirmation_id": "CONF-0001",
            "confirmation_sha256": record["record_sha256"],
            "slot_id": "SLOT-001",
        }
        forged["integrity"]["manifest_sha256"] = nested_record_digest(
            forged,
            "integrity",
            "manifest_sha256",
        )
        atomic_write_json(manifest_path, forged, mode=0o444)
        ledger = load_ledger(self.paths)
        self.assertIsNotNone(ledger)
        assert ledger is not None
        ledger["runs"][run_id]["manifest_sha256"] = sha256_file(manifest_path)
        write_ledger(self.paths, ledger)

        messages = "\n".join(
            issue.render() for issue in errors_only(validate_study(self.paths))
        )
        self.assertIn("predates its Confirmation Record", messages)
        with self.assertRaisesRegex(
            ValidationError,
            "predates its Confirmation Record",
        ):
            create_evidence_draft(
                self.paths,
                "EVID-0001",
                ["CLAIM-0001"],
                [run_id],
            )


if __name__ == "__main__":
    unittest.main()
