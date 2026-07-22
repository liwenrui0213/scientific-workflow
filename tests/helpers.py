from __future__ import annotations

import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from tools.studyctl.approval import approve_brief
from tools.studyctl.budget import replace_brief_hard_budget
from tools.studyctl.cli import initialize_study
from tools.studyctl.evidence import create_evidence_draft, finalize_evidence
from tools.studyctl.hashing import atomic_write_json, load_json, sha256_file
from tools.studyctl.models import StudyPaths, study_paths, utc_now
from tools.studyctl.run_registry import execute_run


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def completed_process(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


class WorkflowTestCase(unittest.TestCase):
    study_id = "SC-0001"

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        shutil.copytree(
            REPOSITORY_ROOT / "scientific-workflow",
            self.root / "scientific-workflow",
        )
        (self.root / ".objects").mkdir()
        (self.root / ".objects" / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
        self.initialize_git()
        self.commit_all("initialize workflow fixture")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def initialize(self, study_id: str | None = None) -> StudyPaths:
        selected = study_id or self.study_id
        initialize_study(self.root, selected, "Deterministic fixture")
        return study_paths(self.root, selected)

    def fill_brief(self, paths: StudyPaths) -> None:
        replacements = {
            "[REPLACE: State the precise scientific question and define all nonstandard symbols.]":
                "Does the deterministic fixture produce the integer four?",
            "[REPLACE: List the claims the study is intended to test, including scope.]":
                "CLAIM-0001 states that the fixture result equals four in this exact scope.",
            "[REPLACE: State what this Study intentionally will not establish, optimize, or change.]":
                "Do not generalize beyond the deterministic fixture or optimize its implementation.",
            "[REPLACE: Record only assumptions explicitly supplied by the human. Write \"None stated\" if none were supplied.]":
                "The human supplied exact integer arithmetic as the intended fixture semantics.",
            "[REPLACE: Record material assumptions inferred by the Agent and label them unconfirmed. Write \"None\" if no confirmation is needed.]":
                "None",
            "[REPLACE: List unresolved questions and distinguish decisions required before approval from non-blocking scientific uncertainty. Write \"None\" if there are no open questions.]":
                "None",
            "[REPLACE: State evaluator principles, dataset split, acceptance criteria, baselines, precision, and any conditions that must not change silently.]":
                "Use exact integer equality, no dataset split, the recorded Python baseline, and fixed precision.",
            "[REPLACE: Specify required comparisons, uncertainty reporting, contradictory checks, and reproducibility expectations.]":
                "Require a deterministic Run, explicit scope and limitations, and a contradictory-evidence check.",
            "[REPLACE: State advisory allocation or calendar guidance only; do not duplicate hard numeric limits in prose.]":
                "No additional advisory allocation or calendar guidance.",
            "[REPLACE: State which events require human attention or a new Brief version.]":
                "Escalate evaluator, data split, acceptance criterion, budget, or Claim-scope changes.",
        }
        text = paths.brief.read_text(encoding="utf-8")
        for old, new in replacements.items():
            self.assertIn(old, text)
            text = text.replace(old, new)
        text = replace_brief_hard_budget(
            text,
            gpu_hours=0,
            cpu_hours=1,
            storage_gb=1,
        )
        paths.brief.write_text(text, encoding="utf-8")

    def set_hard_budget(
        self,
        paths: StudyPaths,
        *,
        gpu_hours: float | int | None,
        cpu_hours: float | int | None,
        storage_gb: float | int | None,
    ) -> None:
        text = replace_brief_hard_budget(
            paths.brief.read_text(encoding="utf-8"),
            gpu_hours=gpu_hours,
            cpu_hours=cpu_hours,
            storage_gb=storage_gb,
        )
        paths.brief.write_text(text, encoding="utf-8")

    def add_proposed_claim(
        self,
        paths: StudyPaths,
        claim_id: str = "CLAIM-0001",
        *,
        lifecycle: str | None = None,
    ) -> dict[str, Any]:
        claims = load_json(paths.claims)
        claim = {
            "claim_id": claim_id,
            "statement": "The deterministic result equals four.",
            "scope": "the fixture command and recorded environment",
            "state": "proposed",
            "evidence_basis": "none",
            "supporting_evidence": [],
            "contradictory_evidence": [],
            "other_evidence": [],
            "uncertainty": None,
            "limitations": [],
            "updated_at": utc_now(),
        }
        claim["lifecycle"] = lifecycle or "active"
        claims["claims"].append(claim)
        claims["frontier"]["claim_ids"].append(claim_id)
        claims["frontier"]["summary"] = "Test the deterministic fixture Claim."
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)
        return claim

    def approve(self, paths: StudyPaths) -> Path:
        brief_hash = sha256_file(paths.brief)
        stdin = TTYBuffer(f"APPROVE {paths.study_id} {brief_hash}\n")
        stdout = TTYBuffer()
        return approve_brief(paths, stdin=stdin, stdout=stdout)

    def initialize_approved_with_claim(self) -> StudyPaths:
        paths = self.initialize()
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        self.approve(paths)
        return paths

    def successful_run(
        self,
        paths: StudyPaths,
        *,
        output: str | None = None,
        cohort_id: str | None = "COHORT-001",
        cohort_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        outputs = [] if output is None else [output]
        code = "print(2 + 2)" if output is None else (
            "from pathlib import Path; Path(" + repr(output) + ").parent.mkdir(parents=True, exist_ok=True); "
            "Path(" + repr(output) + ").write_text(\"4\\n\", encoding=\"utf-8\"); print(4)"
        )
        return execute_run(
            paths,
            argv=[sys.executable, "-c", code],
            purpose="deterministic fixture",
            cohort_id=cohort_id,
            output_paths=outputs,
            hardware_class="test-cpu",
            precision="exact-integer",
            cohort_fields=cohort_fields,
        )

    def finalized_supporting_evidence(
        self,
        paths: StudyPaths,
        manifests: list[dict[str, Any]],
        *,
        evidence_id: str = "EVID-0001",
        compatibility_justification: str | None = None,
        assessment: str = "supports",
    ) -> dict[str, Any]:
        draft_path = create_evidence_draft(
            paths,
            evidence_id,
            ["CLAIM-0001"],
            [manifest["run_id"] for manifest in manifests],
        )
        item = load_json(draft_path)
        item["addresses"]["question"] = "Does the deterministic result equal four?"
        for run in item["runs"]:
            run["role"] = "supporting"
        item["analysis"]["method"] = "Compare the recorded deterministic integer result with four."
        item["result"] = {"value": 4, "comparison": "equal"}
        item["scope"] = "the exact fixture command and recorded Cohort fields"
        item["uncertainty"] = "No sampling uncertainty; execution provenance remains in the Run."
        item["limitations"] = ["This fixture does not establish a broader scientific result."]
        self.fill_evidence_inference(item)
        item["assessment"] = assessment
        if compatibility_justification is not None:
            item["analysis"]["comparison"]["compatibility_justification"] = compatibility_justification
        atomic_write_json(draft_path, item)
        finalize_evidence(paths, draft_path)
        return load_json(draft_path)

    def fill_evidence_inference(self, item: dict[str, Any]) -> None:
        item["inference"] = {
            "observation_to_claim": (
                "The exact recorded value four satisfies the addressed Claim within "
                "the explicitly stated deterministic fixture scope."
            ),
            "auxiliary_assumptions": [
                "The immutable Run record accurately identifies the executed exact-integer computation."
            ],
            "competing_explanations": [
                "A fixture or provenance error could produce the value four without supporting a broader Claim."
            ],
            "falsification_conditions": [
                "A hash-stable rerun yielding a value other than four would overturn this assessment."
            ],
        }

    def support_claim(self, paths: StudyPaths, evidence: dict[str, Any]) -> None:
        claims = load_json(paths.claims)
        claim = claims["claims"][0]
        evidence_basis = evidence.get("evidence_basis", {})
        basis = evidence_basis.get("mode", "exploratory")
        held_out = evidence_basis.get("held_out", {})
        strong_confirmation = basis in {"confirmatory", "mixed"} and (
            (
                held_out.get("status") == "held_out"
                and held_out.get("freshness") == "fresh"
            )
            or (
                held_out.get("status") == "not_applicable"
                and held_out.get("freshness") == "not_applicable"
            )
        )
        claim["state"] = (
            "numerically_supported" if strong_confirmation else "partially_supported"
        )
        claim["evidence_basis"] = basis
        claim["supporting_evidence"] = [
            {
                "evidence_id": evidence["evidence_id"],
                "version": evidence["version"],
                "sha256": evidence["record_sha256"],
            }
        ]
        claim["uncertainty"] = "Limited to the deterministic fixture scope."
        claim["updated_at"] = utc_now()
        claims["frontier"]["summary"] = "The fixture Claim is numerically supported; review scope."
        claims["frontier"]["next_actions"] = ["Run independent review."]
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

    def initialize_git(self) -> None:
        if not (self.root / ".git").exists():
            result = completed_process(["git", "init", "-b", "main"], self.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        for key, value in (("user.name", "Test Reviewer"), ("user.email", "reviewer@example.test")):
            result = completed_process(["git", "config", key, value], self.root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def commit_all(self, message: str) -> None:
        result = completed_process(["git", "add", "."], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = completed_process(["git", "commit", "-m", message], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
