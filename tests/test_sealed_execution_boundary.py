from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.studyctl.hashing import file_record
from tools.studyctl.models import ValidationError
from tools.studyctl.run_registry import _capsule_command, _capsule_environment
from tools.studyctl.validation import sealed_run_evidence_eligible


class SealedExecutionBoundaryTests(unittest.TestCase):
    @staticmethod
    def _seatbelt_usable() -> bool:
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            return False
        probe = subprocess.run(
            [str(sandbox), "-p", "(version 1) (allow default)", "--", "/usr/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return probe.returncode == 0

    def test_capsule_environment_drops_undeclared_path_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "HELDOUT_PATH": "/tmp/heldout.json",
                    "DATABASE_URL": "sqlite:////tmp/private.db",
                    "PATH": "/usr/bin:/bin",
                    "PYTHONHASHSEED": "7",
                },
                clear=True,
            ):
                environment = _capsule_environment(home)

        self.assertNotIn("HELDOUT_PATH", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertEqual(environment["PYTHONHASHSEED"], "7")
        self.assertEqual(environment["HOME"], str(home))
        self.assertEqual(environment["TMPDIR"], str(home / "tmp"))

    def test_capsule_policy_is_read_only_except_declared_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            source.mkdir()
            script = root / "evaluate.py"
            declared_input = root / "declared.json"
            hidden_input = root / "hidden.json"
            output = root / "objects" / "result.json"
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            script.write_text("print('ok')\n", encoding="utf-8")
            declared_input.write_text("{}\n", encoding="utf-8")
            hidden_input.write_text("{}\n", encoding="utf-8")
            input_snapshot = file_record(declared_input, root)

            with patch("platform.system", return_value="Darwin"), patch(
                "shutil.which",
                side_effect=lambda value: (
                    "/usr/bin/sandbox-exec"
                    if value == "sandbox-exec"
                    else sys.executable
                ),
            ):
                wrapped, environment, boundary = _capsule_command(
                    root=root,
                    configured_cwd=root,
                    profile={"source_roots": ["src"]},
                    command=[sys.executable, str(script)],
                    inputs=[(declared_input, input_snapshot)],
                    output_paths=[output.relative_to(root).as_posix()],
                    capsule_home=capsule_home,
                )

            policy = (capsule_home / "sandbox.sb").read_text(encoding="utf-8")

        self.assertEqual(wrapped[:3], ["/usr/bin/sandbox-exec", "-f", str(capsule_home / "sandbox.sb")])
        self.assertEqual(wrapped[-2:], [sys.executable, str(script)])
        self.assertIn("(deny default)", policy)
        self.assertIn(str(declared_input), policy)
        self.assertNotIn(str(hidden_input), policy)
        resolved_output = output.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
        self.assertIn(f"file-write* (literal \"{resolved_output}\")", policy)
        self.assertNotIn(f"file-write* (subpath \"{resolved_root}\")", policy)
        self.assertNotIn("HELDOUT_PATH", environment)
        self.assertEqual(boundary["mode"], "sealed")
        self.assertFalse(boundary["repository_write_access"])

    def test_missing_isolation_backend_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            with patch("platform.system", return_value="Linux"), patch(
                "shutil.which", return_value=None
            ):
                with self.assertRaisesRegex(
                    ValidationError, "requires a supported isolation backend"
                ):
                    _capsule_command(
                        root=root,
                        configured_cwd=root,
                        profile={"source_roots": []},
                        command=[sys.executable, "-c", "print(1)"],
                        inputs=[],
                        output_paths=[],
                        capsule_home=capsule_home,
                    )

    def test_endpoint_only_manifest_is_not_evidence_eligible(self) -> None:
        manifest = {
            "schema_version": 4,
            "status": "succeeded",
            "change_scope": {
                "before": {"outcome": "PASS", "violations": []},
                "after": {"outcome": "PASS", "violations": []},
            },
            "inputs": [],
            "outputs": [],
            "formalization": {"artifacts_unchanged_during_run": True},
        }

        self.assertFalse(sealed_run_evidence_eligible(manifest))

    def test_seatbelt_rejects_runtime_code_replacement_when_available(self) -> None:
        if not self._seatbelt_usable():
            self.skipTest("macOS Seatbelt cannot be nested in this test environment")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            source.mkdir()
            approved = source / "algorithm.py"
            approved.write_text("RESULT = 'approved'\n", encoding="utf-8")
            output = root / "result.txt"
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            attack = (
                "from pathlib import Path; "
                f"p=Path({str(approved)!r}); "
                "p.write_text(\"RESULT = 'unapproved'\\n\"); "
                f"Path({str(output)!r}).write_text('made with changed code')"
            )
            wrapped, environment, _ = _capsule_command(
                root=root,
                configured_cwd=root,
                profile={"source_roots": ["src"]},
                command=[sys.executable, "-c", attack],
                inputs=[],
                output_paths=[output.relative_to(root).as_posix()],
                capsule_home=capsule_home,
            )

            completed = subprocess.run(
                wrapped,
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(approved.read_text(encoding="utf-8"), "RESULT = 'approved'\n")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
