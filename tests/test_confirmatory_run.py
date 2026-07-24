from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase, completed_process
from tools.studyctl.cli import build_parser
from tools.studyctl.confirmation import (
    create_confirmation_draft,
    finalize_confirmation,
)
from tools.studyctl.hashing import atomic_write_json, load_json
from tools.studyctl.models import StudyPaths, ValidationError
from tools.studyctl.run_registry import (
    confirmation_binding,
    effective_run_mode,
    execute_run,
)


class ConfirmatoryRunTests(WorkflowTestCase):
    confirmation_id = "CONF-0001"
    slot_id = "SLOT-001"
    seed = 7
    hardware_class = "test-cpu"
    precision = "exact-integer"

    def test_cli_preserves_seed_scalar_type_for_exact_slot_matching(self) -> None:
        parser = build_parser()
        integer = parser.parse_args(
            ["run", "SC-0001", "--purpose", "p", "--seed", "17", "--", "echo"]
        )
        numeric_text = parser.parse_args(
            [
                "run",
                "SC-0001",
                "--purpose",
                "p",
                "--seed",
                '"17"',
                "--",
                "echo",
            ]
        )
        symbolic = parser.parse_args(
            ["run", "SC-0001", "--purpose", "p", "--seed", "seed-a", "--", "echo"]
        )

        self.assertEqual(integer.seed, 17)
        self.assertIsInstance(integer.seed, int)
        self.assertEqual(numeric_text.seed, "17")
        self.assertEqual(symbolic.seed, "seed-a")

    def _commit_candidate(self, candidate: Path, source: str = "VALUE = 4\n") -> None:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(source, encoding="utf-8")
        relative = candidate.relative_to(self.root).as_posix()
        result = completed_process(["git", "add", relative], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = completed_process(
            ["git", "commit", "-m", "freeze confirmatory candidate"],
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _write_formal_artifacts(self, paths: StudyPaths) -> tuple[Path, Path]:
        protocol_path = paths.formal / "PROTOCOL.json"
        protocol = load_json(
            self.root / "scientific-workflow" / "templates" / "PROTOCOL.json"
        )
        protocol.update(
            {
                "status": "finalized",
                "purpose": "Freeze one deterministic exact-integer comparison.",
                "acceptance_criteria": ["The command exits with status zero."],
                "compute_budget": {
                    "estimated_gpu_hours": 0,
                    "estimated_cpu_hours": 0,
                },
                "seeds": [self.seed],
                "cohort_fields": {},
            }
        )
        atomic_write_json(protocol_path, protocol)

        evaluator_path = paths.formal / "EVALUATOR.json"
        atomic_write_json(
            evaluator_path,
            {
                "schema_version": 1,
                "status": "active",
                "metric": "exact integer equality",
                "decision_rule": "The observed value must equal four.",
            },
        )
        return protocol_path, evaluator_path

    def _external_held_out_path(self) -> Path:
        path = self.root.parent / f"{self.root.name}-held-out.txt"
        path.write_text("unseen fixture\n", encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def _finalized_confirmation(
        self,
        argv: list[str],
        *,
        held_out: bool = False,
    ) -> tuple[StudyPaths, Path, Path, Path, Path | None]:
        paths = self.initialize()
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        candidate = self.root / "tests" / "confirmation_candidate.py"
        self._commit_candidate(candidate)
        protocol_path, evaluator_path = self._write_formal_artifacts(paths)
        self.approve(paths)

        held_out_path = self._external_held_out_path() if held_out else None
        draft_path = create_confirmation_draft(
            paths,
            self.confirmation_id,
            ["CLAIM-0001"],
        )
        draft = load_json(draft_path)
        draft["candidates"][0].update(
            {
                "description": "Committed deterministic fixture candidate.",
                "paths": [candidate.relative_to(self.root).as_posix()],
            }
        )
        draft["analysis_plan"].update(
            {
                "method": "Run the frozen command once and compare exactly.",
                "primary_outcomes": ["process exit status"],
                "decision_rule": "The process exit status is zero.",
                "stopping_rule": "Stop after the single registered slot.",
                "exclusion_rule": "No result-dependent exclusion is permitted.",
            }
        )
        if held_out_path is None:
            draft["held_out"].update(
                {
                    "status": "not_applicable",
                    "description": (
                        "This exact deterministic fixture has no sampled data or "
                        "condition that can be held out."
                    ),
                }
            )
        input_paths: list[str] = []
        if held_out_path is not None:
            raw_held_out = str(held_out_path)
            draft["held_out"].update(
                {
                    "status": "held_out",
                    "description": "External bytes not used by any earlier Run.",
                    "paths": [raw_held_out],
                }
            )
            input_paths = [raw_held_out]
        draft["run_slots"][0].update(
            {
                "candidate_id": draft["candidates"][0]["candidate_id"],
                "argv": argv,
                "seed": self.seed,
                "hardware_class": self.hardware_class,
                "precision": self.precision,
                "cohort_fields": {},
                "input_paths": input_paths,
            }
        )
        atomic_write_json(draft_path, draft)
        finalized_path = finalize_confirmation(paths, draft_path)
        finalized = load_json(finalized_path)
        self.assertEqual(finalized["status"], "finalized")
        self.assertEqual(finalized["run_slots"][0]["slot_id"], self.slot_id)
        return paths, candidate, protocol_path, evaluator_path, held_out_path

    def _execute_confirmatory(
        self,
        paths: StudyPaths,
        argv: list[str],
        *,
        input_paths: list[str] | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        options: dict[str, object] = {
            "argv": argv,
            "purpose": "registered deterministic confirmation",
            "seed": self.seed,
            "hardware_class": self.hardware_class,
            "precision": self.precision,
            "cohort_fields": None,
            "input_paths": input_paths,
            "epistemic_mode": "confirmatory",
            "confirmation_id": self.confirmation_id,
            "confirmation_slot": self.slot_id,
        }
        options.update(overrides)
        return execute_run(paths, **options)  # type: ignore[arg-type]

    def test_default_run_is_exploratory_and_legacy_versions_cannot_be_upgraded(self) -> None:
        paths = self.initialize_approved_with_claim()

        manifest = self.successful_run(paths)

        self.assertEqual(manifest["schema_version"], 5)
        self.assertEqual(
            manifest["epistemic_role"],
            {
                "mode": "exploratory",
                "confirmation_id": None,
                "confirmation_sha256": None,
                "slot_id": None,
            },
        )
        self.assertEqual(effective_run_mode(manifest), "exploratory")
        self.assertIsNone(confirmation_binding(manifest))

        missing_role = copy.deepcopy(manifest)
        missing_role.pop("epistemic_role")
        self.assertEqual(effective_run_mode(missing_role), "exploratory")
        self.assertIsNone(confirmation_binding(missing_role))

        decorated_v3 = copy.deepcopy(manifest)
        decorated_v3["schema_version"] = 3
        decorated_v3["epistemic_role"] = {
            "mode": "confirmatory",
            "confirmation_id": self.confirmation_id,
            "confirmation_sha256": "a" * 64,
            "slot_id": self.slot_id,
        }
        self.assertEqual(effective_run_mode(decorated_v3), "exploratory")
        self.assertIsNone(confirmation_binding(decorated_v3))

    def test_confirmatory_mode_requires_valid_record_and_identifiers_before_launch(self) -> None:
        paths = self.initialize_approved_with_claim()
        argv = [sys.executable, "-c", "print(4)"]

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as popen:
            with self.assertRaisesRegex(ValidationError, "[Cc]onfirmation"):
                self._execute_confirmatory(paths, argv)

        execution_calls = [
            call for call in popen.call_args_list if call.args and call.args[0] == argv
        ]
        self.assertEqual(execution_calls, [])
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

        for confirmation_id, slot_id in (
            ("bad", self.slot_id),
            (self.confirmation_id, "bad"),
        ):
            with self.subTest(confirmation_id=confirmation_id, slot_id=slot_id):
                with self.assertRaisesRegex(ValidationError, "invalid"):
                    execute_run(
                        paths,
                        argv=argv,
                        purpose="reject invalid preregistration identifiers",
                        epistemic_mode="confirmatory",
                        confirmation_id=confirmation_id,
                        confirmation_slot=slot_id,
                    )
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

    def test_running_manifest_contains_frozen_binding_before_process_launch(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, _, _, _, _ = self._finalized_confirmation(argv)
        observed_roles: list[dict[str, object]] = []
        real_popen = subprocess.Popen

        def observe_launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            if args and args[0] == argv:
                manifests = sorted(paths.runs.glob("RUN-*/manifest.json"))
                self.assertEqual(len(manifests), 1)
                running = load_json(manifests[0])
                self.assertEqual(running["status"], "running")
                observed_roles.append(copy.deepcopy(running["epistemic_role"]))
            return real_popen(*args, **kwargs)  # type: ignore[arg-type,return-value]

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            side_effect=observe_launch,
        ):
            manifest = self._execute_confirmatory(paths, argv)

        finalized = load_json(paths.confirmations / f"{self.confirmation_id}.json")
        expected_role = {
            "mode": "confirmatory",
            "confirmation_id": self.confirmation_id,
            "confirmation_sha256": finalized["record_sha256"],
            "slot_id": self.slot_id,
        }
        self.assertEqual(observed_roles, [expected_role])
        self.assertEqual(manifest["epistemic_role"], expected_role)
        self.assertFalse(
            any(
                "/confirmations/" in str(record.get("path", ""))
                for record in manifest["formal_artifacts"]
            ),
            manifest["formal_artifacts"],
        )
        self.assertEqual(effective_run_mode(manifest), "confirmatory")
        self.assertEqual(
            confirmation_binding(manifest),
            {key: expected_role[key] for key in (
                "confirmation_id",
                "confirmation_sha256",
                "slot_id",
            )},
        )

    def test_failed_run_consumes_confirmation_slot(self) -> None:
        argv = [sys.executable, "-c", "raise SystemExit(9)"]
        paths, _, _, _, _ = self._finalized_confirmation(argv)

        first = self._execute_confirmatory(paths, argv)
        self.assertEqual(first["status"], "failed")

        with patch(
            "tools.studyctl.run_registry.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as popen:
            with self.assertRaisesRegex(ValidationError, "already consumed"):
                self._execute_confirmatory(paths, argv)

        repeated_execution_calls = [
            call for call in popen.call_args_list if call.args and call.args[0] == argv
        ]
        self.assertEqual(repeated_execution_calls, [])
        manifests = sorted(paths.runs.glob("RUN-*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(load_json(manifests[0])["status"], "failed")

    def test_running_run_consumes_confirmation_slot_concurrently(self) -> None:
        argv = [sys.executable, "-c", "import time; time.sleep(1); print(4)"]
        paths, _, _, _, _ = self._finalized_confirmation(argv)

        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(self._execute_confirmatory, paths, argv)
            running_seen = False
            for _ in range(200):
                manifests = sorted(paths.runs.glob("RUN-*/manifest.json"))
                if manifests and load_json(manifests[0]).get("status") == "running":
                    running_seen = True
                    break
                time.sleep(0.01)
            self.assertTrue(running_seen, "first Run never published its running Manifest")
            with self.assertRaisesRegex(ValidationError, "already consumed"):
                self._execute_confirmatory(paths, argv)
            first = future.result(timeout=10)

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(len(list(paths.runs.glob("RUN-*/manifest.json"))), 1)

    def test_slot_rejects_changed_claim(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, _, _, _, _ = self._finalized_confirmation(argv)
        claims = load_json(paths.claims)
        claims["claims"][0]["statement"] = "The result equals five."
        atomic_write_json(paths.claims, claims)

        with self.assertRaisesRegex(ValidationError, "Claim|claim"):
            self._execute_confirmatory(paths, argv)
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

    def test_slot_rejects_changed_protocol(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, _, protocol_path, _, _ = self._finalized_confirmation(argv)
        protocol = load_json(protocol_path)
        protocol["purpose"] = "Changed after confirmation was frozen."
        atomic_write_json(protocol_path, protocol)

        with self.assertRaisesRegex(ValidationError, "PROTOCOL|protocol"):
            self._execute_confirmatory(paths, argv)
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

    def test_slot_rejects_changed_evaluator_even_after_brief_reapproval(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, _, _, evaluator_path, _ = self._finalized_confirmation(argv)
        evaluator = load_json(evaluator_path)
        evaluator["metric"] = "changed post-registration metric"
        atomic_write_json(evaluator_path, evaluator)
        self.approve(paths)

        with self.assertRaisesRegex(ValidationError, "EVALUATOR|evaluator"):
            self._execute_confirmatory(paths, argv)
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

    def test_slot_rejects_changed_candidate(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, candidate, _, _, _ = self._finalized_confirmation(argv)
        candidate.write_text("VALUE = 5\n", encoding="utf-8")
        relative = candidate.relative_to(self.root).as_posix()
        result = completed_process(["git", "add", relative], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = completed_process(
            ["git", "commit", "-m", "change candidate after confirmation"],
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        with self.assertRaisesRegex(ValidationError, "candidate|Candidate|code state"):
            self._execute_confirmatory(paths, argv)
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

    def test_slot_rejects_changed_argv_and_execution_conditions(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, _, _, _, _ = self._finalized_confirmation(argv)
        extra_input = self._external_held_out_path()
        cases = (
            ("argv", {"argv": [sys.executable, "-c", "print(5)"]}),
            ("seed", {"seed": self.seed + 1}),
            ("hardware", {"hardware_class": "different-cpu"}),
            ("precision", {"precision": "float32"}),
            ("cohort", {"cohort_fields": ['variant="changed"']}),
            ("inputs", {"input_paths": [str(extra_input)]}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                options: dict[str, object] = {
                    "argv": argv,
                    "input_paths": None,
                }
                options.update(changes)
                with self.assertRaisesRegex(ValidationError, "slot|does not match"):
                    self._execute_confirmatory(paths, **options)  # type: ignore[arg-type]
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])

    def test_slot_rejects_changed_held_out_bytes(self) -> None:
        argv = [sys.executable, "-c", "print(4)"]
        paths, _, _, _, held_out_path = self._finalized_confirmation(
            argv,
            held_out=True,
        )
        self.assertIsNotNone(held_out_path)
        assert held_out_path is not None
        held_out_path.write_text("revealed and changed\n", encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "held-out|held_out|hash"):
            self._execute_confirmatory(
                paths,
                argv,
                input_paths=[str(held_out_path)],
            )
        self.assertEqual(list(paths.runs.glob("RUN-*/manifest.json")), [])


if __name__ == "__main__":
    unittest.main()
