from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.studyctl.hashing import file_record, sha256_json
from tools.studyctl.models import ValidationError
from tools.studyctl.execution_backends import _bubblewrap_device_paths
from tools.studyctl.run_registry import (
    _capsule_environment,
    _capsule_plan,
)
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

    def test_linux_gpu_devices_follow_explicit_visibility_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            device_root = Path(temporary)
            for name in ("nvidia0", "nvidia1", "nvidiactl", "nvidia-uvm"):
                (device_root / name).touch()
            (device_root / "dri").mkdir()
            (device_root / "kfd").touch()

            cpu_only = _bubblewrap_device_paths({}, device_root)
            cuda_one = _bubblewrap_device_paths(
                {"CUDA_VISIBLE_DEVICES": "1"}, device_root
            )
            rocm = _bubblewrap_device_paths(
                {"ROCR_VISIBLE_DEVICES": "0"}, device_root
            )

        self.assertEqual(cpu_only, [])
        self.assertIn((device_root / "nvidia1").absolute(), cuda_one)
        self.assertIn((device_root / "nvidiactl").absolute(), cuda_one)
        self.assertNotIn((device_root / "nvidia0").absolute(), cuda_one)
        self.assertEqual(
            rocm,
            sorted(
                [
                    (device_root / "dri").absolute(),
                    (device_root / "kfd").absolute(),
                ],
                key=str,
            ),
        )

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
            ), patch(
                "tools.studyctl.execution_backends._probe"
            ):
                plan = _capsule_plan(
                    root=root,
                    configured_cwd=root,
                    profile={
                        "source_roots": ["src"],
                        "object_root": "objects",
                        "execution": {
                            "backend_preference": [
                                "linux-bubblewrap",
                                "macos-seatbelt",
                            ],
                            "trusted_read_only_paths": [],
                        },
                    },
                    command=[sys.executable, str(script)],
                    inputs=[(declared_input, input_snapshot)],
                    output_paths=[output.relative_to(root).as_posix()],
                    capsule_home=capsule_home,
                )
                wrapped = plan.argv
                environment = plan.environment
                boundary = plan.boundary

            policy = (capsule_home / "sandbox.sb").read_text(encoding="utf-8")

        self.assertEqual(
            wrapped[:3],
            [
                "/usr/bin/sandbox-exec",
                "-f",
                str(capsule_home.resolve() / "sandbox.sb"),
            ],
        )
        self.assertEqual(
            wrapped[-2:], [str(Path(sys.executable).resolve()), str(script)]
        )
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
                    _capsule_plan(
                        root=root,
                        configured_cwd=root,
                        profile={
                            "source_roots": [],
                            "object_root": "objects",
                            "execution": {
                                "backend_preference": [
                                    "linux-bubblewrap",
                                    "macos-seatbelt",
                                ],
                                "trusted_read_only_paths": [],
                            },
                        },
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

    def test_linux_bubblewrap_uses_private_copy_out_and_no_host_root_bind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            source.mkdir()
            script = source / "evaluate.py"
            declared_input = root / "declared.json"
            hidden_input = root / "hidden.json"
            output = root / "objects" / "result.json"
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            script.write_text("print('ok')\n", encoding="utf-8")
            declared_input.write_text('{"visible": true}\n', encoding="utf-8")
            hidden_input.write_text('{"heldout": true}\n', encoding="utf-8")
            input_snapshot = file_record(declared_input, root)

            def executable(value: str) -> str | None:
                if value in {"bwrap", "bubblewrap"}:
                    return "/usr/bin/bwrap"
                if value == "true":
                    return "/usr/bin/true"
                return sys.executable

            with patch("platform.system", return_value="Linux"), patch(
                "shutil.which", side_effect=executable
            ), patch(
                "tools.studyctl.execution_backends._probe"
            ), patch(
                "tools.studyctl.execution_backends._tool_version",
                return_value="bubblewrap 0.10.0",
            ):
                plan = _capsule_plan(
                    root=root,
                    configured_cwd=root,
                    profile={
                        "source_roots": ["src"],
                        "object_root": "objects",
                        "execution": {
                            "backend_preference": ["linux-bubblewrap"],
                            "trusted_read_only_paths": [],
                        },
                    },
                    command=[sys.executable, str(script)],
                    inputs=[(declared_input, input_snapshot)],
                    output_paths=[output.relative_to(root).as_posix()],
                    capsule_home=capsule_home,
                )

            joined = "\n".join(plan.argv)
            self.assertEqual(plan.boundary["backend"], "linux-bubblewrap")
            self.assertEqual(plan.boundary["output_staging"], "private-copy-out")
            self.assertEqual(
                plan.boundary["environment_sha256"],
                sha256_json(plan.boundary["environment_variables"]),
            )
            self.assertIn("--unshare-net", plan.argv)
            self.assertIn("--unshare-pid", plan.argv)
            self.assertNotIn("--share-net", plan.argv)
            self.assertIn("--clearenv", plan.argv)
            self.assertNotIn("--ro-bind\n/\n/", joined)
            self.assertIn(str(declared_input), joined)
            self.assertNotIn(str(hidden_input), joined)
            self.assertEqual(len(plan.output_mappings), 1)
            staged = plan.output_mappings[0].staged
            staged.write_text('{"result": 1}\n', encoding="utf-8")
            plan.materialize_outputs()

            self.assertEqual(output.read_text(encoding="utf-8"), '{"result": 1}\n')
            self.assertEqual(plan.output_mappings[0].destination, output)

    def test_linux_boundary_requires_registered_policy_and_environment_binding(
        self,
    ) -> None:
        change_state = {
            "outcome": "PASS",
            "git": {"available": True},
            "changed_paths": [],
        }
        effective_environment = {
            "HOME": "${CAPSULE_HOME}",
            "TMPDIR": "${CAPSULE_HOME}/tmp",
        }
        manifest = {
            "schema_version": 4,
            "status": "succeeded",
            "change_scope": {
                "before": change_state,
                "after": change_state,
            },
            "execution_boundary": {
                "mode": "sealed",
                "backend": "linux-bubblewrap",
                "backend_version": "bubblewrap 0.9.0",
                "policy_format": "bubblewrap-mount-policy-v1",
                "policy_sha256": "a" * 64,
                "output_staging": "private-copy-out",
                "environment_allowlist": ["HOME", "TMPDIR"],
                "environment_variables": effective_environment,
                "environment_sha256": sha256_json(effective_environment),
                "read_only_paths": [],
                "writable_paths": [],
                "device_paths": [],
                "declared_inputs_only": True,
                "repository_write_access": False,
                "declared_outputs_only": True,
                "network_access": False,
            },
            "inputs": [],
            "outputs": [],
            "formalization": {"artifacts_unchanged_during_run": True},
        }

        self.assertTrue(sealed_run_evidence_eligible(manifest))
        manifest["execution_boundary"]["policy_format"] = "seatbelt-profile-v1"
        self.assertFalse(sealed_run_evidence_eligible(manifest))
        manifest["execution_boundary"][
            "policy_format"
        ] = "bubblewrap-mount-policy-v1"
        manifest["execution_boundary"]["environment_sha256"] = "b" * 64
        self.assertFalse(sealed_run_evidence_eligible(manifest))

    def test_seatbelt_boundary_requires_complete_policy_and_environment_binding(
        self,
    ) -> None:
        change_state = {
            "outcome": "PASS",
            "git": {"available": True},
            "changed_paths": [],
        }
        effective_environment = {
            "HOME": "${CAPSULE_HOME}",
            "TMPDIR": "${CAPSULE_HOME}/tmp",
        }
        manifest = {
            "schema_version": 5,
            "status": "succeeded",
            "change_scope": {
                "before": change_state,
                "after": change_state,
            },
            "execution_boundary": {
                "mode": "sealed",
                "backend": "macos-seatbelt",
                "backend_version": "15.0",
                "policy_format": "seatbelt-profile-v1",
                "policy_sha256": "a" * 64,
                "output_staging": "direct",
                "environment_allowlist": ["HOME", "TMPDIR"],
                "environment_variables": effective_environment,
                "environment_sha256": sha256_json(effective_environment),
                "read_only_paths": ["/usr/bin/python3"],
                "writable_paths": [],
                "device_paths": [],
                "declared_inputs_only": True,
                "repository_write_access": False,
                "declared_outputs_only": True,
                "network_access": False,
            },
            "inputs": [],
            "outputs": [],
            "formalization": {"artifacts_unchanged_during_run": True},
        }

        self.assertTrue(sealed_run_evidence_eligible(manifest))
        for required_field in (
            "backend_version",
            "policy_format",
            "output_staging",
            "environment_allowlist",
            "environment_variables",
            "environment_sha256",
            "read_only_paths",
            "writable_paths",
            "device_paths",
        ):
            with self.subTest(required_field=required_field):
                forged = copy.deepcopy(manifest)
                del forged["execution_boundary"][required_field]
                self.assertFalse(sealed_run_evidence_eligible(forged))

        forged = copy.deepcopy(manifest)
        forged["execution_boundary"]["environment_allowlist"] = ["HOME"]
        self.assertFalse(sealed_run_evidence_eligible(forged))

    def test_linux_object_store_input_is_reexposed_read_only_after_masking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            object_root = root / "objects"
            object_root.mkdir()
            previous = object_root / "previous.json"
            previous.write_text('{"result": 0}\n', encoding="utf-8")
            output = object_root / "next.json"
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            with patch("platform.system", return_value="Linux"), patch(
                "shutil.which",
                side_effect=lambda value: (
                    "/usr/bin/bwrap"
                    if value in {"bwrap", "bubblewrap"}
                    else "/usr/bin/true"
                    if value == "true"
                    else sys.executable
                ),
            ), patch(
                "tools.studyctl.execution_backends._probe"
            ), patch(
                "tools.studyctl.execution_backends._tool_version",
                return_value="bubblewrap 0.10.0",
            ):
                plan = _capsule_plan(
                    root=root,
                    configured_cwd=root,
                    profile={
                        "source_roots": ["src"],
                        "object_root": "objects",
                        "execution": {
                            "backend_preference": ["linux-bubblewrap"],
                            "trusted_read_only_paths": [],
                        },
                    },
                    command=[
                        sys.executable,
                        "-c",
                        "print('consume previous output')",
                    ],
                    inputs=[(previous.resolve(), file_record(previous, root))],
                    output_paths=[output.relative_to(root).as_posix()],
                    capsule_home=capsule_home,
                )

            bind_object_index = next(
                index
                for index in range(len(plan.argv) - 2)
                if plan.argv[index : index + 3]
                == [
                    "--bind",
                    str(capsule_home.resolve() / "output-root"),
                    str(object_root.resolve()),
                ]
            )
            first_input_index = plan.argv.index(str(previous.resolve()))
            second_input_index = plan.argv.index(
                str(previous.resolve()), first_input_index + 1
            )
            self.assertLess(first_input_index, bind_object_index)
            self.assertGreater(second_input_index, bind_object_index)
            alias = Path(plan.argv[first_input_index + 1])
            self.assertTrue(alias.is_relative_to(capsule_home.resolve()))
            self.assertEqual(plan.argv[second_input_index - 2], "--ro-bind")
            self.assertEqual(Path(plan.argv[second_input_index - 1]), alias)
            self.assertEqual(
                previous.read_text(encoding="utf-8"), '{"result": 0}\n'
            )

    def test_linux_bubblewrap_empty_output_set_has_no_copy_out_mappings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            source.mkdir()
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            with patch("platform.system", return_value="Linux"), patch(
                "shutil.which",
                side_effect=lambda value: (
                    "/usr/bin/bwrap"
                    if value in {"bwrap", "bubblewrap"}
                    else "/usr/bin/true"
                    if value == "true"
                    else sys.executable
                ),
            ), patch(
                "tools.studyctl.execution_backends._probe"
            ), patch(
                "tools.studyctl.execution_backends._tool_version",
                return_value="bubblewrap 0.10.0",
            ):
                plan = _capsule_plan(
                    root=root,
                    configured_cwd=root,
                    profile={
                        "source_roots": ["src"],
                        "object_root": "objects",
                        "execution": {
                            "backend_preference": ["linux-bubblewrap"],
                            "trusted_read_only_paths": [],
                        },
                    },
                    command=[sys.executable, "-c", "print(1)"],
                    inputs=[],
                    output_paths=[],
                    capsule_home=capsule_home,
                )

            self.assertEqual(plan.output_mappings, ())
            plan.materialize_outputs()
            self.assertFalse((root / "objects").exists())

    def test_linux_backend_probe_failure_is_fail_closed(self) -> None:
        from tools.studyctl.execution_backends import _BackendUnavailable

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            with patch("platform.system", return_value="Linux"), patch(
                "shutil.which",
                side_effect=lambda value: (
                    "/usr/bin/bwrap"
                    if value in {"bwrap", "bubblewrap"}
                    else sys.executable
                ),
            ), patch(
                "tools.studyctl.execution_backends._probe",
                side_effect=_BackendUnavailable("user namespaces are disabled"),
            ):
                with self.assertRaisesRegex(
                    ValidationError, "user namespaces are disabled"
                ):
                    _capsule_plan(
                        root=root,
                        configured_cwd=root,
                        profile={
                            "source_roots": [],
                            "object_root": "objects",
                            "execution": {
                                "backend_preference": ["linux-bubblewrap"],
                                "trusted_read_only_paths": [],
                            },
                        },
                        command=[sys.executable, "-c", "print(1)"],
                        inputs=[],
                        output_paths=[],
                        capsule_home=capsule_home,
                    )

    def test_explicit_backend_must_be_allowed_by_protected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capsule_home = root / "capsule"
            (capsule_home / "tmp").mkdir(parents=True)
            with self.assertRaisesRegex(
                ValidationError, "not allowed by the protected repository profile"
            ):
                _capsule_plan(
                    root=root,
                    configured_cwd=root,
                    profile={
                        "source_roots": [],
                        "object_root": "objects",
                        "execution": {
                            "backend_preference": ["linux-bubblewrap"],
                            "trusted_read_only_paths": [],
                        },
                    },
                    command=[sys.executable, "-c", "print(1)"],
                    inputs=[],
                    output_paths=[],
                    capsule_home=capsule_home,
                    execution_backend="macos-seatbelt",
                )

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
            plan = _capsule_plan(
                root=root,
                configured_cwd=root,
                profile={
                    "source_roots": ["src"],
                    "object_root": ".objects",
                    "execution": {
                        "backend_preference": ["macos-seatbelt"],
                        "trusted_read_only_paths": [],
                    },
                },
                command=[sys.executable, "-c", attack],
                inputs=[],
                output_paths=[output.relative_to(root).as_posix()],
                capsule_home=capsule_home,
            )
            wrapped = plan.argv
            environment = plan.environment

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
