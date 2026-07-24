from __future__ import annotations

from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import patch

from tests.helpers import WorkflowTestCase
from tools.studyctl.active_context import (
    ACTIVE_CONTEXT_FILENAME,
    COMPACTION_DUE_FILENAME,
    refresh_active_projection,
    write_active_selector,
)
from tools.studyctl.cli import main
from tools.studyctl.hashing import atomic_write_json, load_json
from tools.studyctl.models import StudyPaths, utc_now
from tools.studyctl.rendering import render_status


class ContextFailClosedTests(WorkflowTestCase):
    def invalidate_claim_with_missing_evidence(self, paths: StudyPaths) -> None:
        claims = load_json(paths.claims)
        claim = claims["claims"][0]
        claim["state"] = "partially_supported"
        claim["evidence_basis"] = "exploratory"
        claim["supporting_evidence"] = [
            {
                "evidence_id": "EVID-9999",
                "version": 1,
                "sha256": "0" * 64,
            }
        ]
        claim["uncertainty"] = "The referenced Evidence is intentionally absent."
        claim["updated_at"] = utc_now()
        claims["revision"] += 1
        claims["updated_at"] = utc_now()
        atomic_write_json(paths.claims, claims)

    def test_context_writes_projection_for_valid_study(self) -> None:
        paths = self.initialize_approved_with_claim()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(self.root),
                    "context",
                    paths.study_id,
                ]
            )

        selector_path = paths.generated / ACTIVE_CONTEXT_FILENAME
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().strip(), str(selector_path))
        self.assertEqual(load_json(selector_path)["study_id"], paths.study_id)

    def test_context_invalid_study_without_prior_projection_creates_nothing(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        selector_path = paths.generated / ACTIVE_CONTEXT_FILENAME
        if selector_path.exists():
            selector_path.unlink()
        self.invalidate_claim_with_missing_evidence(paths)
        self.assertFalse(selector_path.exists())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(self.root),
                    "context",
                    paths.study_id,
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("references missing Evidence", stderr.getvalue())
        self.assertFalse(selector_path.exists())

    def test_context_rejects_invalid_study_without_replacing_last_valid_projection(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        selector_path = write_active_selector(paths)
        valid_projection = selector_path.read_bytes()

        (paths.evidence / "EVID-0001.v0001.json").unlink()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(self.root),
                    "context",
                    paths.study_id,
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Study validation failed", stderr.getvalue())
        self.assertIn("references missing Evidence", stderr.getvalue())
        self.assertEqual(
            (paths.generated / ACTIVE_CONTEXT_FILENAME).read_bytes(),
            valid_projection,
        )

    def test_status_preserves_both_prior_projections_when_authority_is_invalid(
        self,
    ) -> None:
        paths = self.initialize_approved_with_claim()
        manifest = self.successful_run(paths)
        evidence = self.finalized_supporting_evidence(paths, [manifest])
        self.support_claim(paths, evidence)
        selector_path, advisory_path = refresh_active_projection(paths)
        valid_selector = selector_path.read_bytes()
        valid_advisory = advisory_path.read_bytes()

        (paths.evidence / "EVID-0001.v0001.json").unlink()

        status_path = render_status(paths)
        status = status_path.read_text(encoding="utf-8")

        self.assertIn(
            "**INVALID — scientific Evidence/Claim summaries below are not trusted",
            status,
        )
        self.assertIn(
            "ACTIVE_CONTEXT.json and COMPACTION_DUE.json were left unchanged",
            status,
        )
        self.assertEqual(
            (paths.generated / ACTIVE_CONTEXT_FILENAME).read_bytes(),
            valid_selector,
        )
        self.assertEqual(
            (paths.generated / COMPACTION_DUE_FILENAME).read_bytes(),
            valid_advisory,
        )

    def test_status_does_not_create_projections_from_invalid_authority(self) -> None:
        paths = self.initialize_approved_with_claim()
        selector_path = paths.generated / ACTIVE_CONTEXT_FILENAME
        advisory_path = paths.generated / COMPACTION_DUE_FILENAME
        selector_path.unlink(missing_ok=True)
        advisory_path.unlink(missing_ok=True)
        self.invalidate_claim_with_missing_evidence(paths)

        status = render_status(paths).read_text(encoding="utf-8")

        self.assertIn("**INVALID", status)
        self.assertFalse(selector_path.exists())
        self.assertFalse(advisory_path.exists())

    def test_context_lock_spans_validation_and_projection_refresh(self) -> None:
        paths = self.initialize_approved_with_claim()
        events: list[str] = []

        @contextmanager
        def recording_lock(locked_paths: StudyPaths):
            self.assertEqual(locked_paths, paths)
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        def recording_validate(validated_paths: StudyPaths) -> None:
            self.assertEqual(validated_paths, paths)
            self.assertEqual(events, ["lock-enter"])
            events.append("validate")

        def recording_refresh(refreshed_paths: StudyPaths):
            self.assertEqual(refreshed_paths, paths)
            self.assertEqual(events, ["lock-enter", "validate"])
            events.append("refresh")
            return (
                paths.generated / ACTIVE_CONTEXT_FILENAME,
                paths.generated / COMPACTION_DUE_FILENAME,
            )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "tools.studyctl.locking.study_authority_lock",
                recording_lock,
            ),
            patch(
                "tools.studyctl.cli.assert_valid_study",
                recording_validate,
            ),
            patch(
                "tools.studyctl.active_context.refresh_active_projection",
                recording_refresh,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "--root",
                    str(self.root),
                    "context",
                    paths.study_id,
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(events, ["lock-enter", "validate", "refresh", "lock-exit"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            stdout.getvalue().strip(),
            str(paths.generated / ACTIVE_CONTEXT_FILENAME),
        )

    def test_status_rendering_runs_inside_the_authority_lock(self) -> None:
        paths = self.initialize_approved_with_claim()
        events: list[str] = []
        expected = paths.generated / "STATUS.md"

        @contextmanager
        def recording_lock(locked_paths: StudyPaths):
            self.assertEqual(locked_paths, paths)
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        def recording_render(locked_paths: StudyPaths):
            self.assertEqual(locked_paths, paths)
            self.assertEqual(events, ["lock-enter"])
            events.append("render")
            return expected

        with (
            patch(
                "tools.studyctl.rendering.study_authority_lock",
                recording_lock,
            ),
            patch(
                "tools.studyctl.rendering._render_status_under_authority",
                recording_render,
            ),
        ):
            result = render_status(paths)

        self.assertEqual(result, expected)
        self.assertEqual(events, ["lock-enter", "render", "lock-exit"])


if __name__ == "__main__":
    unittest.main()
