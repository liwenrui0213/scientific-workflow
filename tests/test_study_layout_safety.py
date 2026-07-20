from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from tests.helpers import REPOSITORY_ROOT, WorkflowTestCase, completed_process
from tools.studyctl.hashing import atomic_write_json, load_json
from tools.studyctl.models import ValidationError, WorkflowError, study_paths
from tools.studyctl.validation import validate_study
from tools.studyctl.workspace import repository_profile_path


class StudyLayoutSafetyTests(WorkflowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._outside = tempfile.TemporaryDirectory()
        self.outside = Path(self._outside.name)

    def tearDown(self) -> None:
        self._outside.cleanup()
        super().tearDown()

    def write_profile_study_root(self, relative: str) -> None:
        path = repository_profile_path(self.root)
        profile = load_json(path)
        self.assertIsInstance(profile, dict)
        profile["study_root"] = relative
        atomic_write_json(path, profile)

    def test_missing_nested_study_path_is_allowed_only_for_initialization(self) -> None:
        self.write_profile_study_root("research/state/studies")

        paths = study_paths(self.root, self.study_id, must_exist=False)

        self.assertEqual(
            paths.study,
            self.root.resolve() / "research" / "state" / "studies" / self.study_id,
        )
        self.assertFalse(paths.study.exists())
        paths.assert_safe_layout(must_exist=False)
        with self.assertRaisesRegex(WorkflowError, "study does not exist"):
            paths.assert_safe_layout(must_exist=True)

        initialized = self.initialize()
        self.assertTrue(initialized.study.is_dir())
        initialized.assert_safe_layout(must_exist=True)

    def test_configured_study_root_cannot_traverse_an_external_symlink(self) -> None:
        linked_root = self.root / "linked-studies"
        linked_root.symlink_to(self.outside, target_is_directory=True)
        self.write_profile_study_root("linked-studies")

        with self.assertRaisesRegex(ValidationError, "symbolic link"):
            study_paths(self.root, self.study_id, must_exist=False)

    def test_study_directory_itself_cannot_be_a_symlink(self) -> None:
        paths = self.initialize()
        study_directory = paths.study
        shutil.rmtree(study_directory)
        external_study = self.outside / self.study_id
        external_study.mkdir()
        study_directory.symlink_to(external_study, target_is_directory=True)

        with self.assertRaisesRegex(ValidationError, "symbolic link"):
            study_paths(self.root, self.study_id)

    def test_managed_directory_symlinks_are_rejected(self) -> None:
        paths = self.initialize()
        directory_names = ("runs", "generated", "evidence", "formal", "work")

        for name in directory_names:
            with self.subTest(directory=name):
                managed = paths.study / name
                shutil.rmtree(managed)
                target = self.outside / f"target-{name}"
                target.mkdir()
                managed.symlink_to(target, target_is_directory=True)

                with self.assertRaisesRegex(ValidationError, "symbolic link"):
                    study_paths(self.root, self.study_id)

                managed.unlink()
                managed.mkdir()
                if name == "work":
                    (managed / "active").mkdir()
                    (managed / "archived").mkdir()

    def test_nested_managed_entries_cannot_escape_through_symlinks(self) -> None:
        paths = self.initialize()
        external_file = self.outside / "external.txt"
        external_file.write_text("outside\n", encoding="utf-8")
        external_directory = self.outside / "external-directory"
        external_directory.mkdir()
        cases = (
            ("run directory", paths.runs / "RUN-000001", external_directory, True),
            ("generated projection", paths.generated / "STATUS.md", external_file, False),
            ("Evidence record", paths.evidence / "EVID-9999.v0001.json", external_file, False),
            ("formal artifact", paths.formal / "METHOD.md", external_file, False),
            ("work note", paths.active_work / "notes.md", external_file, False),
        )

        for label, link, target, is_directory in cases:
            with self.subTest(path=label):
                original = link.read_bytes() if link.is_file() else None
                if original is not None:
                    link.unlink()
                link.symlink_to(target, target_is_directory=is_directory)

                with self.assertRaisesRegex(ValidationError, "symbolic link"):
                    study_paths(self.root, self.study_id)

                link.unlink()
                if original is not None:
                    link.write_bytes(original)

    def test_authoritative_file_symlink_is_rejected(self) -> None:
        paths = self.initialize()
        external_file = self.outside / "forged.json"
        external_file.write_text("{}\n", encoding="utf-8")
        claims_path = paths.claims
        original = claims_path.read_bytes()
        claims_path.unlink()
        claims_path.symlink_to(external_file)

        with self.assertRaisesRegex(ValidationError, "symbolic link"):
            study_paths(self.root, self.study_id)

        claims_path.unlink()
        claims_path.write_bytes(original)
        self.assertEqual(study_paths(self.root, self.study_id).claims.read_bytes(), original)

    def test_validation_rechecks_layout_after_paths_were_constructed(self) -> None:
        paths = self.initialize()
        external_file = self.outside / "projection.md"
        external_file.write_text("forged projection\n", encoding="utf-8")
        unsafe_projection = paths.generated / "UNSAFE.md"
        unsafe_projection.symlink_to(external_file)

        issues = validate_study(paths)

        errors = [issue for issue in issues if issue.level == "ERROR"]
        self.assertTrue(errors)
        self.assertTrue(
            any("symbolic link" in issue.message for issue in errors),
            [issue.render() for issue in errors],
        )

    def test_cli_fails_closed_before_reading_symlinked_run_tree(self) -> None:
        paths = self.initialize()
        shutil.rmtree(paths.runs)
        paths.runs.symlink_to(self.outside, target_is_directory=True)

        result = completed_process(
            [
                sys.executable,
                "-B",
                "-m",
                "tools.studyctl",
                "--root",
                str(self.root),
                "validate",
                self.study_id,
            ],
            REPOSITORY_ROOT,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_managed_directory_must_not_be_a_regular_file(self) -> None:
        paths = self.initialize()
        shutil.rmtree(paths.formal)
        paths.formal.write_text("not a directory\n", encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "not a directory"):
            study_paths(self.root, self.study_id)


if __name__ == "__main__":
    unittest.main()
