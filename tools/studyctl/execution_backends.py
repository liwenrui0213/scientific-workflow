from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterable, Sequence

from .hashing import atomic_write_bytes, sha256_bytes, sha256_json
from .models import ValidationError


MACOS_SEATBELT = "macos-seatbelt"
LINUX_BUBBLEWRAP = "linux-bubblewrap"
SUPPORTED_EXECUTION_BACKENDS = (LINUX_BUBBLEWRAP, MACOS_SEATBELT)
DEFAULT_BACKEND_PREFERENCE = SUPPORTED_EXECUTION_BACKENDS


class _BackendUnavailable(Exception):
    """Internal signal used while trying fail-closed backend candidates."""


@dataclass(frozen=True)
class OutputMapping:
    staged: Path
    destination: Path


@dataclass
class ExecutionPlan:
    argv: list[str]
    environment: dict[str, str]
    boundary: dict[str, Any]
    host_cwd: Path
    output_mappings: tuple[OutputMapping, ...] = ()

    def materialize_outputs(self) -> None:
        """Copy only declared regular outputs out of a private backend staging tree."""

        for mapping in self.output_mappings:
            try:
                metadata = mapping.staged.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValidationError(
                    f"cannot inspect staged Run output {mapping.staged}: {exc}"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ValidationError(
                    f"staged Run output is not a regular file: {mapping.staged}"
                )
            if mapping.destination.is_symlink() or mapping.destination.exists():
                raise ValidationError(
                    "Run output appeared outside the sealed child before copy-out: "
                    f"{mapping.destination}"
                )
            mapping.destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, raw_temporary = tempfile.mkstemp(
                prefix=f".{mapping.destination.name}.",
                suffix=".studyctl-copy.tmp",
                dir=mapping.destination.parent,
            )
            temporary = Path(raw_temporary)
            try:
                with mapping.staged.open("rb") as source, os.fdopen(
                    descriptor, "wb"
                ) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temporary, 0o600)
                try:
                    os.link(temporary, mapping.destination, follow_symlinks=False)
                except FileExistsError as exc:
                    raise ValidationError(
                        "Run output appeared outside the sealed child before copy-out: "
                        f"{mapping.destination}"
                    ) from exc
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


@dataclass(frozen=True)
class ExecutionRequest:
    root: Path
    configured_cwd: Path
    command: tuple[str, ...]
    executable: Path
    environment: dict[str, str]
    environment_allowlist: tuple[str, ...]
    capsule_home: Path
    object_root: Path
    read_subpaths: tuple[Path, ...]
    read_literals: tuple[Path, ...]
    outputs: tuple[Path, ...]


class ExecutionBackend:
    name: str

    def prepare(self, request: ExecutionRequest) -> ExecutionPlan:
        raise NotImplementedError


def _probe(argv: Sequence[str], *, backend: str) -> None:
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _BackendUnavailable(f"{backend} capability probe failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no detail"
        detail = " ".join(detail.split())[:500]
        raise _BackendUnavailable(
            f"{backend} capability probe exited {completed.returncode}: {detail}"
        )


def _tool_version(argv: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    value = " ".join(completed.stdout.strip().split())
    return value[:256] or "unknown"


def _existing(paths: Iterable[Path]) -> list[Path]:
    return [path.absolute() for path in paths if path.exists()]


def _deduplicated_mounts(paths: Iterable[Path]) -> list[Path]:
    ordered = sorted(
        {path.absolute() for path in paths if path.exists()},
        key=lambda item: (len(item.parts), str(item)),
    )
    selected: list[Path] = []
    for candidate in ordered:
        if any(
            parent.is_dir() and candidate != parent and candidate.is_relative_to(parent)
            for parent in selected
        ):
            continue
        selected.append(candidate)
    return selected


def _sandbox_literal(path: Path) -> str:
    import json

    return json.dumps(str(path.resolve(strict=False)))


class MacOSSeatbeltBackend(ExecutionBackend):
    name = MACOS_SEATBELT

    def prepare(self, request: ExecutionRequest) -> ExecutionPlan:
        if platform.system() != "Darwin":
            raise _BackendUnavailable("macos-seatbelt requires Darwin")
        sandbox = shutil.which("sandbox-exec")
        if sandbox is None:
            raise _BackendUnavailable("sandbox-exec is not installed")
        true_executable = shutil.which("true") or "/usr/bin/true"
        _probe(
            [
                sandbox,
                "-p",
                "(version 1) (allow default)",
                "--",
                true_executable,
            ],
            backend=self.name,
        )

        read_subpaths = _deduplicated_mounts(
            [
                Path("/System"),
                Path("/usr/lib"),
                Path("/dev"),
                *request.read_subpaths,
            ]
        )
        read_literals = _deduplicated_mounts(
            [*request.read_literals, request.executable]
        )
        profile_lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read-metadata)",
        ]
        for path in read_subpaths:
            profile_lines.append(
                f"(allow file-read* (subpath {_sandbox_literal(path)}))"
            )
        for path in read_literals:
            profile_lines.append(
                f"(allow file-read* (literal {_sandbox_literal(path)}))"
            )
        profile_lines.append(
            "(allow file-read* file-write* "
            f"(subpath {_sandbox_literal(request.capsule_home)}))"
        )
        for path in sorted(
            {item.resolve(strict=False) for item in request.outputs}
        ):
            profile_lines.append(
                "(allow file-read* file-write* "
                f"(literal {_sandbox_literal(path)}))"
            )
        sandbox_profile = "\n".join(profile_lines) + "\n"
        profile_path = request.capsule_home / "sandbox.sb"
        atomic_write_bytes(profile_path, sandbox_profile.encode("utf-8"), mode=0o400)
        boundary = _boundary_record(
            backend=self.name,
            backend_version=platform.mac_ver()[0] or "unknown",
            policy_format="seatbelt-profile-v1",
            policy_sha256=sha256_bytes(sandbox_profile.encode("utf-8")),
            read_only_paths=[*read_subpaths, *read_literals],
            writable_paths=request.outputs,
            output_staging="direct",
            device_paths=(),
            environment=request.environment,
            capsule_home=request.capsule_home,
        )
        return ExecutionPlan(
            argv=[sandbox, "-f", str(profile_path), "--", *request.command],
            environment=dict(request.environment),
            boundary=boundary,
            host_cwd=request.configured_cwd,
        )


def _bubblewrap_system_paths() -> list[Path]:
    return _existing(
        [
            Path("/usr"),
            Path("/bin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/sbin"),
            Path("/sys"),
            Path("/etc/ld.so.cache"),
            Path("/etc/ld.so.conf"),
            Path("/etc/ld.so.conf.d"),
            Path("/etc/localtime"),
            Path("/etc/passwd"),
            Path("/etc/group"),
            Path("/etc/nsswitch.conf"),
        ]
    )


def _device_selection_is_enabled(
    environment: dict[str, str], names: Sequence[str]
) -> bool:
    disabled = {"", "-1", "none", "void"}
    return any(
        name in environment
        and environment[name].strip().lower() not in disabled
        for name in names
    )


def _bubblewrap_device_paths(
    environment: dict[str, str], device_root: Path = Path("/dev")
) -> list[Path]:
    devices: set[Path] = set()
    if _device_selection_is_enabled(
        environment,
        (
            "HIP_VISIBLE_DEVICES",
            "ONEAPI_DEVICE_SELECTOR",
            "ROCR_VISIBLE_DEVICES",
            "ZE_AFFINITY_MASK",
        ),
    ):
        for candidate in (device_root / "dri", device_root / "kfd"):
            if candidate.exists():
                devices.add(candidate.absolute())
    nvidia_selection = environment.get(
        "CUDA_VISIBLE_DEVICES",
        environment.get("NVIDIA_VISIBLE_DEVICES"),
    )
    if (
        isinstance(nvidia_selection, str)
        and nvidia_selection.strip().lower() not in {"", "-1", "none", "void"}
        and device_root.is_dir()
    ):
        tokens = [
            token.strip()
            for token in nvidia_selection.split(",")
            if token.strip()
        ]
        controls = [
            path
            for name in (
                "nvidiactl",
                "nvidia-uvm",
                "nvidia-uvm-tools",
                "nvidia-modeset",
            )
            if (path := device_root / name).exists()
        ]
        devices.update(path.absolute() for path in controls)
        if any(token.lower() == "all" or not token.isdecimal() for token in tokens):
            devices.update(
                path.absolute()
                for path in device_root.glob("nvidia[0-9]*")
                if path.name.removeprefix("nvidia").isdecimal()
            )
        else:
            for token in tokens:
                candidate = device_root / f"nvidia{token}"
                if candidate.exists():
                    devices.add(candidate.absolute())
    return sorted(devices, key=str)


def _parent_directories(paths: Iterable[Path]) -> list[Path]:
    directories: set[Path] = set()
    for raw in paths:
        current = raw.absolute()
        if not current.is_dir():
            current = current.parent
        while current != current.parent:
            directories.add(current)
            current = current.parent
    directories.discard(Path("/proc"))
    directories.discard(Path("/dev"))
    return sorted(directories, key=lambda item: (len(item.parts), str(item)))


def _private_output_mappings(request: ExecutionRequest) -> tuple[
    Path, tuple[OutputMapping, ...]
]:
    staging_root = request.capsule_home / "output-root"
    staging_root.mkdir(mode=0o700)
    mappings: list[OutputMapping] = []
    for output in request.outputs:
        try:
            relative = output.resolve(strict=False).relative_to(
                request.object_root.resolve(strict=False)
            )
        except ValueError as exc:
            raise ValidationError(
                f"Run output is outside the configured object_root: {output}"
            ) from exc
        staged = staging_root / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        mappings.append(OutputMapping(staged=staged, destination=output))
    return staging_root, tuple(mappings)


class LinuxBubblewrapBackend(ExecutionBackend):
    name = LINUX_BUBBLEWRAP

    def prepare(self, request: ExecutionRequest) -> ExecutionPlan:
        if platform.system() != "Linux":
            raise _BackendUnavailable("linux-bubblewrap requires Linux")
        bubblewrap = shutil.which("bwrap") or shutil.which("bubblewrap")
        if bubblewrap is None:
            raise _BackendUnavailable("bwrap/bubblewrap is not installed")
        true_executable = shutil.which("true") or "/bin/true"
        _probe(
            [
                bubblewrap,
                "--die-with-parent",
                "--new-session",
                "--unshare-ipc",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-uts",
                "--unshare-cgroup-try",
                "--ro-bind",
                "/",
                "/",
                "--",
                true_executable,
            ],
            backend=self.name,
        )

        system_paths = _bubblewrap_system_paths()
        read_mounts = _deduplicated_mounts(
            [*system_paths, *request.read_subpaths, *request.read_literals]
        )
        device_paths = _bubblewrap_device_paths(request.environment)
        staging_root, mappings = _private_output_mappings(request)
        object_root = request.object_root.resolve(strict=False)

        # The private object-root bind masks all retained outputs. Re-expose
        # declared read dependencies below it as exact read-only mounts.
        masked_reads = [
            path
            for path in read_mounts
            if path.resolve(strict=False).is_relative_to(object_root)
        ]
        ordinary_reads = [
            path
            for path in read_mounts
            if not path.resolve(strict=False).is_relative_to(object_root)
        ]

        mount_targets = [
            *ordinary_reads,
            *masked_reads,
            request.capsule_home,
            object_root,
            request.configured_cwd,
            *device_paths,
            Path("/proc"),
            Path("/dev"),
        ]
        argv = [
            bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--cap-drop",
            "ALL",
            "--hostname",
            "studyctl",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        for directory in _parent_directories(mount_targets):
            argv.extend(["--dir", str(directory)])
        for path in ordinary_reads:
            argv.extend(["--ro-bind", str(path), str(path)])
        argv.extend(
            [
                "--bind",
                str(request.capsule_home),
                str(request.capsule_home),
            ]
        )
        masked_aliases: list[tuple[Path, Path]] = []
        for path in masked_reads:
            alias = (
                request.capsule_home
                / "masked-read-roots"
                / sha256_json(str(path.resolve(strict=False)))
            )
            if path.is_dir():
                alias.mkdir(parents=True, exist_ok=True)
            else:
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.touch(exist_ok=True)
            masked_aliases.append((path, alias))
            argv.extend(["--ro-bind", str(path), str(alias)])
        argv.extend(["--bind", str(staging_root), str(object_root)])
        for path, alias in masked_aliases:
            argv.extend(["--ro-bind", str(alias), str(path)])
        for path in device_paths:
            argv.extend(["--dev-bind", str(path), str(path)])
        argv.append("--clearenv")
        for key, value in sorted(request.environment.items()):
            argv.extend(["--setenv", key, value])
        argv.extend(
            [
                "--chdir",
                str(request.configured_cwd),
                "--",
                *request.command,
            ]
        )

        policy = {
            "format": "bubblewrap-mount-policy-v1",
            "required_namespaces": ["ipc", "mount", "network", "pid", "uts"],
            "optional_namespaces": ["cgroup", "user"],
            "capabilities": "none",
            "read_only_paths": sorted(str(path) for path in read_mounts),
            "private_home": str(request.capsule_home),
            "private_object_root": str(object_root),
            "masked_object_reads": sorted(str(path) for path in masked_reads),
            "persistent_outputs": sorted(
                str(mapping.destination.resolve(strict=False)) for mapping in mappings
            ),
            "device_paths": [str(path) for path in device_paths],
            "environment": _portable_environment(
                request.environment, request.capsule_home
            ),
        }
        boundary = _boundary_record(
            backend=self.name,
            backend_version=_tool_version([bubblewrap, "--version"]),
            policy_format="bubblewrap-mount-policy-v1",
            policy_sha256=sha256_json(policy),
            read_only_paths=read_mounts,
            writable_paths=request.outputs,
            output_staging="private-copy-out",
            device_paths=device_paths,
            environment=request.environment,
            capsule_home=request.capsule_home,
        )
        return ExecutionPlan(
            argv=argv,
            environment=dict(request.environment),
            boundary=boundary,
            host_cwd=request.configured_cwd,
            output_mappings=mappings,
        )


def _boundary_record(
    *,
    backend: str,
    backend_version: str,
    policy_format: str,
    policy_sha256: str,
    read_only_paths: Iterable[Path],
    writable_paths: Iterable[Path],
    output_staging: str,
    device_paths: Iterable[Path],
    environment: dict[str, str],
    capsule_home: Path,
) -> dict[str, Any]:
    portable_environment = _portable_environment(environment, capsule_home)
    return {
        "mode": "sealed",
        "backend": backend,
        "backend_version": backend_version,
        "policy_format": policy_format,
        "policy_sha256": policy_sha256,
        "environment_allowlist": [],
        "network_access": False,
        "repository_write_access": False,
        "declared_inputs_only": True,
        "declared_outputs_only": True,
        "read_only_paths": sorted(
            {str(path.absolute()) for path in read_only_paths}
        ),
        "writable_paths": sorted(
            {str(path.absolute()) for path in writable_paths}
        ),
        "output_staging": output_staging,
        "device_paths": sorted(
            {str(path.absolute()) for path in device_paths}
        ),
        "environment_variables": portable_environment,
        "environment_sha256": sha256_json(portable_environment),
    }


def _portable_environment(
    environment: dict[str, str], capsule_home: Path
) -> dict[str, str]:
    portable = dict(environment)
    home = str(capsule_home)
    replacements = {
        home: "${CAPSULE_HOME}",
        str(capsule_home / "tmp"): "${CAPSULE_HOME}/tmp",
    }
    for key, value in list(portable.items()):
        portable[key] = replacements.get(value, value)
    return dict(sorted(portable.items()))


_BACKENDS: dict[str, ExecutionBackend] = {
    LINUX_BUBBLEWRAP: LinuxBubblewrapBackend(),
    MACOS_SEATBELT: MacOSSeatbeltBackend(),
}


def build_execution_plan(
    request: ExecutionRequest,
    *,
    backend: str,
    preference: Sequence[str],
) -> ExecutionPlan:
    if backend != "auto" and backend not in _BACKENDS:
        raise ValidationError(
            f"unsupported execution backend {backend!r}; choose one of "
            f"{', '.join(SUPPORTED_EXECUTION_BACKENDS)}"
        )
    configured = list(preference)
    if not configured:
        raise ValidationError("execution backend preference must not be empty")
    if len(configured) != len(set(configured)):
        raise ValidationError("execution backend preference must not contain duplicates")
    unknown = [candidate for candidate in configured if candidate not in _BACKENDS]
    if unknown:
        raise ValidationError(
            "execution backend preference contains unsupported value(s): "
            + ", ".join(unknown)
        )
    if backend != "auto" and backend not in configured:
        raise ValidationError(
            f"execution backend {backend!r} is not allowed by the protected "
            "repository profile"
        )
    candidates = configured if backend == "auto" else [backend]

    failures: list[str] = []
    for candidate in candidates:
        try:
            plan = _BACKENDS[candidate].prepare(request)
        except _BackendUnavailable as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        plan.boundary["environment_allowlist"] = sorted(request.environment_allowlist)
        return plan
    details = "; ".join(failures) or "no backend candidates were attempted"
    raise ValidationError(
        "sealed Run execution requires a supported isolation backend; "
        f"no configured backend passed its capability probe ({details})"
    )
