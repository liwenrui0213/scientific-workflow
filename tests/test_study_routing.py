from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

from tests.helpers import REPOSITORY_ROOT, TTYBuffer, WorkflowTestCase, completed_process
from tools.studyctl.approval import record_verdict
from tools.studyctl.approval import begin_brief_revision
from tools.studyctl.git_state import git_state
from tools.studyctl.hashing import atomic_write_json, load_json, sha256_file
from tools.studyctl.models import StudyPaths, WorkflowError, utc_now
from tools.studyctl.study_routing import classify_study_dirs, resolve_study


class StudyRoutingTests(WorkflowTestCase):
    def completed_draft(self, study_id: str) -> StudyPaths:
        paths = self.initialize(study_id)
        self.fill_brief(paths)
        self.add_proposed_claim(paths)
        return paths

    def approved_study(self, study_id: str) -> StudyPaths:
        paths = self.completed_draft(study_id)
        self.approve(paths)
        return paths

    def cli_resolve(self, study_id: str | None = None) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-B",
            "-m",
            "tools.studyctl",
            "--root",
            str(self.root),
            "resolve-study",
        ]
        if study_id is not None:
            argv.append(study_id)
        return completed_process(
            argv,
            REPOSITORY_ROOT,
        )

    def study_tree_snapshot(self) -> dict[str, bytes | None]:
        root = self.root / "studies"
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")
        }

    def record_empty_scope_verdict(self, paths: StudyPaths) -> None:
        verdict_id = "VERDICT-0001"
        source = {
            "schema_version": 1,
            "study_id": paths.study_id,
            "verdict_id": verdict_id,
            "created_at": utc_now(),
            "reviewer": {
                "identity": "Independent Routing Test Reviewer",
                "source": "human_authored_test_fixture",
            },
            "judged_scope": {
                "commit": git_state(paths.root)["commit"],
                "brief_sha256": sha256_file(paths.brief),
                "checkpoint": None,
                "claims": [],
                "evidence": [],
            },
            "implementation_verdict": {
                "decision": "accepted",
                "rationale": "The routing fixture satisfies its implementation scope.",
                "conditions": [],
            },
            "scientific_verdict": {
                "decision": "requires_more_evidence",
                "rationale": "The fixture does not establish a scientific conclusion.",
                "scope": "Only the deterministic routing fixture is judged.",
                "conditions": [],
            },
            "confirmation": {
                "typed_text": "[FILLED BY STUDYCTL]",
                "confirmed_at": "[FILLED BY STUDYCTL]",
            },
            "verdict_sha256": None,
        }
        source_path = self.root / "routing-verdict-source.json"
        atomic_write_json(source_path, source)
        phrase = f"RECORD VERDICT {paths.study_id} {verdict_id}"
        record_verdict(
            paths,
            source_path,
            stdin=TTYBuffer(phrase + "\n"),
            stdout=TTYBuffer(),
        )

    def test_unique_approved_study_resolves_to_scientific_study(self) -> None:
        paths = self.approved_study("SC-2001")

        candidates = classify_study_dirs(self.root)
        selected = resolve_study(self.root)
        result = self.cli_resolve()

        self.assertEqual([(item.study_id, item.phase) for item in candidates], [("SC-2001", "approved")])
        self.assertEqual(selected.study_id, paths.study_id)
        self.assertEqual(selected.skill, "scientific-study")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "study_id": "SC-2001",
                "phase": "approved",
                "skill": "scientific-study",
            },
        )
        self.assertEqual(result.stderr, "")

    def test_unique_unapproved_draft_reuses_id_with_start_skill(self) -> None:
        paths = self.completed_draft("SC-2002")
        before = sorted(item.name for item in paths.study.parent.iterdir())

        selected = resolve_study(self.root)
        result = self.cli_resolve()

        self.assertEqual(selected.study_id, "SC-2002")
        self.assertEqual(selected.phase, "draft")
        self.assertEqual(selected.skill, "start-scientific-study")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["study_id"], "SC-2002")
        self.assertFalse(paths.brief_approval.exists())
        self.assertEqual(sorted(item.name for item in paths.study.parent.iterdir()), before)

    def test_initialized_placeholder_draft_routes_to_start_without_reinitializing(self) -> None:
        paths = self.initialize("SC-2010")
        before = self.study_tree_snapshot()

        selected = resolve_study(self.root)
        result = self.cli_resolve(paths.study_id)

        self.assertEqual((selected.study_id, selected.phase), ("SC-2010", "draft"))
        self.assertIn("editable Brief draft", selected.detail)
        self.assertEqual(selected.skill, "start-scientific-study")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["skill"], "start-scientific-study")
        self.assertEqual(self.study_tree_snapshot(), before)

    def test_new_brief_version_remains_resumable_as_same_draft(self) -> None:
        paths = self.approved_study("SC-2011")
        begin_brief_revision(paths)
        before = self.study_tree_snapshot()

        selected = resolve_study(self.root)
        result = self.cli_resolve(paths.study_id)

        self.assertEqual((selected.study_id, selected.phase), ("SC-2011", "draft"))
        self.assertEqual(selected.skill, "start-scientific-study")
        self.assertIn("editable Brief draft", selected.detail)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["study_id"], "SC-2011")
        self.assertEqual(self.study_tree_snapshot(), before)

    def test_valid_verdict_does_not_remove_approved_study_from_resolution(self) -> None:
        paths = self.approved_study("SC-2003")
        self.record_empty_scope_verdict(paths)

        selected = resolve_study(self.root)

        self.assertTrue(paths.verdict.is_file())
        self.assertEqual((selected.study_id, selected.phase), ("SC-2003", "approved"))

    def test_legacy_claims_require_same_study_migration_not_replacement(self) -> None:
        paths = self.approved_study("SC-2008")
        claims = load_json(paths.claims)
        claims["schema_version"] = 1
        atomic_write_json(paths.claims, claims)
        before = self.study_tree_snapshot()

        candidate = classify_study_dirs(self.root)[0]
        self.assertEqual((candidate.study_id, candidate.phase), ("SC-2008", "invalid"))
        self.assertIn("historical-validation-only", candidate.detail)
        with self.assertRaisesRegex(WorkflowError, "migrate it.*before resuming"):
            resolve_study(self.root)

        result = self.cli_resolve()
        self.assertEqual(result.returncode, 2)
        self.assertIn("repair this Study", result.stderr)
        self.assertNotIn("start-scientific-study", result.stderr)
        self.assertEqual(self.study_tree_snapshot(), before)

    def test_zero_candidates_fails_without_allocating_a_study(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "candidates: none"):
            resolve_study(self.root)

        result = self.cli_resolve()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Ask the user once", result.stderr)
        self.assertIn("do not initialize", result.stderr)
        self.assertFalse((self.root / "studies").exists())

    def test_multiple_candidates_fail_with_concise_sorted_summary_and_no_write(self) -> None:
        approved = self.approved_study("SC-2005")
        self.completed_draft("SC-2004")
        before = self.study_tree_snapshot()

        with self.assertRaisesRegex(WorkflowError, r"SC-2004\[draft\].*SC-2005\[approved\]"):
            resolve_study(self.root)
        result = self.cli_resolve()

        self.assertEqual(result.returncode, 2)
        self.assertIn("Ask the user once to name the intended Study", result.stderr)
        self.assertNotIn("SC-2006", result.stderr)
        self.assertEqual(self.study_tree_snapshot(), before)

        selected = resolve_study(self.root, approved.study_id)
        explicit_result = self.cli_resolve(approved.study_id)
        self.assertEqual((selected.study_id, selected.phase), ("SC-2005", "approved"))
        self.assertEqual(explicit_result.returncode, 0, explicit_result.stderr)
        self.assertEqual(json.loads(explicit_result.stdout)["study_id"], "SC-2005")
        self.assertEqual(self.study_tree_snapshot(), before)

    def test_missing_explicit_id_fails_without_falling_back_to_init(self) -> None:
        existing = self.approved_study("SC-2007")
        before = self.study_tree_snapshot()

        with self.assertRaisesRegex(WorkflowError, "requested Study does not exist: SC-9999"):
            resolve_study(self.root, "SC-9999")
        result = self.cli_resolve("SC-9999")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("do not initialize a replacement Study", result.stderr)
        self.assertTrue(existing.study.is_dir())
        self.assertEqual(self.study_tree_snapshot(), before)

    def test_invalid_or_unsafe_unique_candidate_fails_closed(self) -> None:
        invalid = self.initialize("SC-2006")
        invalid.claims.unlink()
        before_invalid_resolution = self.study_tree_snapshot()
        candidate = classify_study_dirs(self.root)[0]
        self.assertEqual(candidate.phase, "invalid")
        self.assertIn("CLAIMS.json", candidate.detail)
        with self.assertRaisesRegex(WorkflowError, r"SC-2006\[invalid\]"):
            resolve_study(self.root)
        self.assertTrue(invalid.study.is_dir())
        self.assertEqual(self.study_tree_snapshot(), before_invalid_resolution)

        # A second fixture would make routing ambiguous, so replace the invalid
        # directory with a link and verify that the scan never follows it.
        shutil.rmtree(invalid.study)
        target = self.root / "unsafe-routing-target"
        target.mkdir()
        invalid.study.symlink_to(target, target_is_directory=True)
        before_unsafe_resolution = self.study_tree_snapshot()

        unsafe = classify_study_dirs(self.root)[0]
        self.assertEqual((unsafe.study_id, unsafe.phase), ("SC-2006", "invalid"))
        self.assertIn("symbolic link", unsafe.detail)
        with self.assertRaisesRegex(WorkflowError, "do not initialize a new Study"):
            resolve_study(self.root)
        self.assertEqual(self.study_tree_snapshot(), before_unsafe_resolution)

    def test_missing_malformed_and_non_utf8_briefs_are_invalid_not_drafts(self) -> None:
        cases = {
            "missing": None,
            "malformed": b"garbage\n",
            "non-utf8": b"\xff\xfe\xfd",
        }
        for offset, (label, payload) in enumerate(cases.items(), start=1):
            with self.subTest(case=label):
                study_id = f"SC-{2100 + offset}"
                paths = self.initialize(study_id)
                if payload is None:
                    paths.brief.unlink()
                else:
                    paths.brief.write_bytes(payload)
                before = self.study_tree_snapshot()

                candidate = resolve_candidate = classify_study_dirs(self.root)[-1]
                self.assertEqual(resolve_candidate.study_id, study_id)
                self.assertEqual(candidate.phase, "invalid")
                result = self.cli_resolve(study_id)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("invalid", result.stderr)
                self.assertEqual(self.study_tree_snapshot(), before)

    def test_unicode_content_is_valid_but_unicode_lookalike_id_is_not_selected(self) -> None:
        paths = self.completed_draft("SC-2201")
        text = paths.brief.read_text(encoding="utf-8")
        paths.brief.write_text(
            text.replace(
                "Does the deterministic fixture produce the integer four?",
                "确定性计算是否产生整数四？",
            ),
            encoding="utf-8",
        )
        self.approve(paths)
        lookalike = paths.study.parent / "SC-２２０２"
        lookalike.mkdir()
        before = self.study_tree_snapshot()

        selected = resolve_study(self.root)
        explicit = self.cli_resolve("SC-２２０２")

        self.assertEqual((selected.study_id, selected.phase), ("SC-2201", "approved"))
        self.assertEqual(explicit.returncode, 2)
        self.assertIn("invalid study ID", explicit.stderr)
        self.assertEqual(self.study_tree_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
