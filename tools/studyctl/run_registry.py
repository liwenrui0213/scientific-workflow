from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any, Sequence

from .formalization import check_formalization, load_policy
from .git_state import git_state, git_tracked_state
from .hashing import (
    atomic_write_json,
    file_record,
    load_json,
    nested_record_digest,
    require_nonnegative_finite,
    sha256_file,
    sha256_json,
)
from .models import (
    RunInterrupted,
    SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
    WorkflowError,
    require_id,
    utc_now,
)
from .validation import brief_approval_issues, brief_content_issues, errors_only


_RUN_DIRECTORY_RE = re.compile(r"^RUN-([0-9]{6})$")
_REPRODUCIBILITY_ENVIRONMENT_KEYS = (
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "CUDA_VISIBLE_DEVICES",
    "JAX_PLATFORM_NAME",
    "JAX_PLATFORMS",
    "NVIDIA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "XLA_FLAGS",
)


def _display_path(path: Path, root: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _user_path(root: Path, raw: str | os.PathLike[str]) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _require_fresh_brief(paths: StudyPaths) -> dict[str, Any]:
    issues = errors_only(brief_content_issues(paths) + brief_approval_issues(paths))
    if issues:
        details = "\n".join(issue.render() for issue in issues)
        raise ValidationError(f"a fresh approved Brief is required before a Run:\n{details}")
    approval = load_json(paths.brief_approval)
    if not isinstance(approval, dict):
        raise ValidationError("Brief approval must be a JSON object")
    return approval


def _allocate_run_directory(paths: StudyPaths) -> tuple[str, Path]:
    paths.runs.mkdir(parents=True, exist_ok=True)
    highest = 0
    for entry in paths.runs.iterdir():
        match = _RUN_DIRECTORY_RE.fullmatch(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    candidate = highest + 1
    while candidate <= 999_999:
        run_id = f"RUN-{candidate:06d}"
        run_directory = paths.runs / run_id
        try:
            # Allocation, rather than a preceding existence check, arbitrates
            # concurrent registrars. A loser retries with the next Run ID.
            os.mkdir(run_directory, 0o755)
            return run_id, run_directory
        except FileExistsError:
            candidate += 1
    raise WorkflowError("Run ID space is exhausted")


def _formal_artifact_records(paths: StudyPaths, policy: dict[str, Any]) -> list[dict[str, Any]]:
    if not paths.formal.is_dir():
        return []
    known_kinds = {
        (paths.study / str(relative)).resolve(): str(kind)
        for kind, relative in policy.get("formal_artifacts", {}).items()
    }
    records: list[dict[str, Any]] = []
    for path in sorted(paths.formal.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValidationError(f"symbolic links are not accepted as formal artifacts: {path}")
        if not path.is_file():
            continue
        record = file_record(path, paths.root)
        record["kind"] = known_kinds.get(
            path.resolve(), path.relative_to(paths.formal).as_posix()
        )
        records.append(record)
    return records


def _load_protocol(paths: StudyPaths) -> tuple[Path, dict[str, Any] | None]:
    protocol_path = paths.formal / "PROTOCOL.json"
    if not protocol_path.is_file():
        return protocol_path, None
    protocol = load_json(protocol_path)
    if not isinstance(protocol, dict):
        raise ValidationError(f"formal protocol must be a JSON object: {protocol_path}")
    return protocol_path, protocol


def _component_hash(
    paths: StudyPaths,
    protocol: dict[str, Any] | None,
    *,
    filename: str,
    protocol_key: str,
) -> str | None:
    dedicated = paths.formal / filename
    if dedicated.is_file():
        if dedicated.is_symlink():
            raise ValidationError(f"symbolic links are not accepted as formal artifacts: {dedicated}")
        return sha256_file(dedicated)
    if protocol is None or protocol.get(protocol_key) is None:
        return None
    return sha256_json(protocol[protocol_key])


def _parse_cohort_fields(raw_fields: Sequence[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in raw_fields or ():
        if not isinstance(raw, str) or "=" not in raw:
            raise ValidationError("cohort fields must use KEY=JSON-or-string syntax")
        key, raw_value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValidationError("cohort field key must not be empty")
        if key in parsed:
            raise ValidationError(f"duplicate cohort field: {key!r}")
        try:
            value = json.loads(
                raw_value,
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {constant}")
                ),
            )
        except (json.JSONDecodeError, ValueError):
            value = raw_value
        parsed[key] = value
    return parsed


def _cohort_record(
    paths: StudyPaths,
    policy: dict[str, Any],
    protocol_path: Path,
    protocol: dict[str, Any] | None,
    cohort_id: str | None,
    hardware_class: str | None,
    precision: str | None,
    raw_fields: Sequence[str] | None,
    runtime_environment: dict[str, str],
) -> tuple[dict[str, Any], str, str]:
    if cohort_id is not None:
        require_id("cohort", cohort_id)
    hardware = (
        hardware_class
        if hardware_class is not None
        else str(policy.get("default_hardware_class", "unspecified"))
    )
    numeric_precision = (
        precision if precision is not None else str(policy.get("default_precision", "unspecified"))
    )
    if not isinstance(hardware, str) or not hardware.strip():
        raise ValidationError("hardware_class must be a non-empty string")
    if not isinstance(numeric_precision, str) or not numeric_precision.strip():
        raise ValidationError("precision must be a non-empty string")

    fields: dict[str, Any] = {
        "evaluator_sha256": _component_hash(
            paths, protocol, filename="EVALUATOR.json", protocol_key="evaluator"
        ),
        "dataset_split_sha256": _component_hash(
            paths, protocol, filename="DATASET_SPLIT.json", protocol_key="dataset_split"
        ),
        "baseline_sha256": _component_hash(
            paths, protocol, filename="BASELINE.json", protocol_key="baseline"
        ),
        "acceptance_criteria_sha256": _component_hash(
            paths,
            protocol,
            filename="ACCEPTANCE_CRITERIA.json",
            protocol_key="acceptance_criteria",
        ),
        "protocol_sha256": sha256_file(protocol_path) if protocol is not None else None,
        "hardware_class": hardware,
        "precision": numeric_precision,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "runtime": platform.platform(),
        "runtime_environment_sha256": sha256_json(runtime_environment),
    }
    protocol_fields = protocol.get("cohort_fields", {}) if protocol is not None else {}
    if protocol_fields is None:
        protocol_fields = {}
    if not isinstance(protocol_fields, dict):
        raise ValidationError("formal PROTOCOL.json cohort_fields must be an object")
    for key, value in protocol_fields.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("formal protocol cohort field keys must be non-empty strings")
        if key in fields:
            raise ValidationError(f"formal protocol cohort field is reserved: {key!r}")
        fields[key] = value
    for key, value in _parse_cohort_fields(raw_fields).items():
        if key in fields:
            raise ValidationError(f"cohort field is already defined or reserved: {key!r}")
        fields[key] = value
    return (
        {
            "cohort_id": cohort_id,
            "fields": fields,
            "fingerprint_sha256": sha256_json(fields),
        },
        hardware,
        numeric_precision,
    )


def _selected_runtime_environment() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in _REPRODUCIBILITY_ENVIRONMENT_KEYS
        if key in os.environ
    }


def _environment_record(
    hardware_class: str,
    precision: str,
    selected_environment: dict[str, str],
) -> dict[str, Any]:
    return {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "hardware_class": hardware_class,
        "precision": precision,
        "environment_variables": selected_environment,
    }


def _input_records(
    root: Path, raw_paths: Sequence[str | os.PathLike[str]] | None
) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    for raw in raw_paths or ():
        path = _user_path(root, raw)
        snapshot = file_record(path, root)
        records.append(
            (
                path,
                {
                    "path": snapshot["path"],
                    "size": snapshot["size"],
                    "sha256_before": snapshot["sha256"],
                    "sha256_after": None,
                    "changed_during_run": None,
                },
            )
        )
    return records


def _finalize_input_records(
    snapshots: list[tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for path, original in snapshots:
        record = dict(original)
        if path.is_symlink() or not path.is_file():
            after_hash = None
        else:
            try:
                after_hash = sha256_file(path)
            except WorkflowError:
                after_hash = None
        record["sha256_after"] = after_hash
        record["changed_during_run"] = after_hash != record["sha256_before"]
        finalized.append(record)
    return finalized


def _output_records(
    root: Path,
    raw_paths: Sequence[str | os.PathLike[str]] | None,
    policies: dict[Path, dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raw_paths or ():
        path = _user_path(root, raw)
        base: dict[str, Any] = {
            "path": _display_path(path, root),
            "present": False,
            "size": None,
            "sha256": None,
            "classification": policies[path.resolve(strict=False)]["classification"],
            "pinned": policies[path.resolve(strict=False)]["pinned"],
        }
        if not path.is_symlink() and path.is_file():
            snapshot = file_record(path, root)
            base.update(
                {
                    "path": snapshot["path"],
                    "present": True,
                    "size": snapshot["size"],
                    "sha256": snapshot["sha256"],
                }
            )
        records.append(base)
    return records


def _output_policies(
    root: Path,
    raw_outputs: Sequence[str | os.PathLike[str]] | None,
    *,
    pinned_outputs: Sequence[str | os.PathLike[str]] | None,
    baseline_outputs: Sequence[str | os.PathLike[str]] | None,
    unique_anomaly_outputs: Sequence[str | os.PathLike[str]] | None,
) -> dict[Path, dict[str, Any]]:
    policies: dict[Path, dict[str, Any]] = {}
    for raw in raw_outputs or ():
        key = _user_path(root, raw).resolve(strict=False)
        if key in policies:
            raise ValidationError(f"duplicate output path: {_display_path(key, root)}")
        policies[key] = {"classification": "ordinary", "pinned": False}

    def require_declared(
        values: Sequence[str | os.PathLike[str]] | None,
        label: str,
    ) -> list[Path]:
        selected: list[Path] = []
        seen: set[Path] = set()
        for raw in values or ():
            key = _user_path(root, raw).resolve(strict=False)
            if key in seen:
                raise ValidationError(f"duplicate {label} path: {_display_path(key, root)}")
            seen.add(key)
            if key not in policies:
                raise ValidationError(
                    f"{label} path must also be declared with --output: {_display_path(key, root)}"
                )
            selected.append(key)
        return selected

    for key in require_declared(pinned_outputs, "pinned output"):
        policies[key]["pinned"] = True
    for key in require_declared(baseline_outputs, "baseline output"):
        policies[key]["classification"] = "baseline"
    for key in require_declared(unique_anomaly_outputs, "unique-anomaly output"):
        if policies[key]["classification"] == "baseline":
            raise ValidationError(
                f"output cannot be both baseline and unique anomaly: {_display_path(key, root)}"
            )
        policies[key]["classification"] = "unique_anomaly"
    return policies


def _open_exclusive_log(path: Path) -> Any:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def _flush_log(handle: Any) -> None:
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _terminate_process(process: subprocess.Popen[Any]) -> int | None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return process.wait()


def execute_run(
    paths: StudyPaths,
    *,
    argv: list[str],
    purpose: str,
    cohort_id: str | None = None,
    estimated_gpu_hours: float = 0.0,
    estimated_cpu_hours: float = 0.0,
    input_paths: Sequence[str | os.PathLike[str]] | None = None,
    output_paths: Sequence[str | os.PathLike[str]] | None = None,
    pinned_outputs: Sequence[str | os.PathLike[str]] | None = None,
    baseline_outputs: Sequence[str | os.PathLike[str]] | None = None,
    unique_anomaly_outputs: Sequence[str | os.PathLike[str]] | None = None,
    changed_paths: Sequence[str] | None = None,
    scientific_critical: bool = False,
    shared_across_runs: bool = False,
    seed: int | str | None = None,
    hardware_class: str | None = None,
    precision: str | None = None,
    cohort_fields: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Execute ``argv`` and atomically seal its immutable Run manifest."""
    if not isinstance(argv, list) or not argv:
        raise ValidationError("argv must be a non-empty list")
    if any(not isinstance(argument, str) for argument in argv):
        raise ValidationError("every argv item must be a string")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValidationError("purpose must be a non-empty string")
    if isinstance(seed, bool) or not isinstance(seed, (int, str, type(None))):
        raise ValidationError("seed must be an integer, string, or null")
    declared_changed_paths = list(changed_paths or ())
    if any(not isinstance(path, str) or not path.strip() for path in declared_changed_paths):
        raise ValidationError("changed paths must be non-empty strings")
    if len(declared_changed_paths) != len(set(declared_changed_paths)):
        raise ValidationError("changed paths must not be repeated")

    root = paths.root.resolve()
    output_policies = _output_policies(
        root,
        output_paths,
        pinned_outputs=pinned_outputs,
        baseline_outputs=baseline_outputs,
        unique_anomaly_outputs=unique_anomaly_outputs,
    )
    approval = _require_fresh_brief(paths)
    gpu_hours = require_nonnegative_finite("estimated GPU hours", estimated_gpu_hours)
    cpu_hours = require_nonnegative_finite("estimated CPU hours", estimated_cpu_hours)
    formalization = check_formalization(
        paths,
        {
            "estimated_gpu_hours": gpu_hours,
            "estimated_cpu_hours": cpu_hours,
            "changed_path": declared_changed_paths,
            "scientific_critical": bool(scientific_critical),
            "shared_across_runs": bool(shared_across_runs),
        },
    )
    if formalization.blocked:
        requirements = "\n".join(
            f"- {item['level']}: {item['artifact']}: {item['reason']}"
            for item in formalization.requirements
        )
        raise ValidationError(f"formalization gate blocked Run:\n{requirements}")

    policy = load_policy(paths)
    protocol_path, protocol = _load_protocol(paths)
    formal_artifacts = _formal_artifact_records(paths, policy)
    runtime_environment = _selected_runtime_environment()
    cohort, effective_hardware, effective_precision = _cohort_record(
        paths,
        policy,
        protocol_path,
        protocol,
        cohort_id,
        hardware_class,
        precision,
        cohort_fields,
        runtime_environment,
    )
    inputs = _input_records(root, input_paths)
    git = git_state(root)
    code_state_before = git_tracked_state(root)
    environment = _environment_record(
        effective_hardware,
        effective_precision,
        runtime_environment,
    )
    brief = {
        "path": _display_path(paths.brief, root),
        "sha256": sha256_file(paths.brief),
        "approval_sha256": str(approval["approval_sha256"]),
    }

    run_id, run_directory = _allocate_run_directory(paths)
    stdout_path = run_directory / "stdout.log"
    stderr_path = run_directory / "stderr.log"
    command = list(argv)
    started_at = utc_now()
    started_monotonic = time.monotonic()
    exit_code: int | None = None
    interrupted = False
    execution_failed = False
    process: subprocess.Popen[Any] | None = None

    with _open_exclusive_log(stdout_path) as stdout_log, _open_exclusive_log(
        stderr_path
    ) as stderr_log:
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=stdout_log,
                stderr=stderr_log,
                shell=False,
            )
            exit_code = process.wait()
        except KeyboardInterrupt:
            interrupted = True
            if process is not None:
                try:
                    exit_code = _terminate_process(process)
                except (
                    OSError,
                    ValueError,
                    subprocess.SubprocessError,
                    KeyboardInterrupt,
                ) as exc:
                    exit_code = None
                    stderr_log.write(
                        f"studyctl: interrupted command cleanup failed: {exc}\n".encode("utf-8")
                    )
        except (OSError, ValueError) as exc:
            execution_failed = True
            if process is None:
                exit_code = 127
                message = f"studyctl: failed to start command: {exc}\n"
            else:
                try:
                    exit_code = _terminate_process(process)
                except (OSError, ValueError, subprocess.SubprocessError):
                    exit_code = None
                message = f"studyctl: command execution failed: {exc}\n"
            stderr_log.write(message.encode("utf-8"))
        finally:
            _flush_log(stdout_log)
            _flush_log(stderr_log)

    ended_at = utc_now()
    duration_seconds = max(0.0, time.monotonic() - started_monotonic)
    code_state_after = git_tracked_state(root)
    os.chmod(stdout_path, 0o444)
    os.chmod(stderr_path, 0o444)
    status = (
        "interrupted"
        if interrupted
        else "succeeded"
        if exit_code == 0 and not execution_failed
        else "failed"
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "run_id": run_id,
        "purpose": purpose,
        "status": status,
        "execution": {
            "argv": command,
            "cwd": str(root),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
            "exit_code": exit_code,
            "seed": seed,
        },
        "git": git,
        "code_state": {
            "before": code_state_before,
            "after": code_state_after,
            "changed_during_run": code_state_before != code_state_after,
        },
        "brief": brief,
        "formal_artifacts": formal_artifacts,
        "formalization": {
            "changed_paths": declared_changed_paths,
            "scientific_critical": bool(scientific_critical),
            "shared_across_runs": bool(shared_across_runs),
            "outcome": formalization.outcome,
            "requirements": formalization.requirements,
        },
        "cohort": cohort,
        "environment": environment,
        "budget": {
            "estimated_gpu_hours": gpu_hours,
            "estimated_cpu_hours": cpu_hours,
        },
        "inputs": _finalize_input_records(inputs),
        "outputs": _output_records(root, output_paths, output_policies),
        "logs": {
            "stdout": file_record(stdout_path, root),
            "stderr": file_record(stderr_path, root),
        },
        "integrity": {
            "sealed_at": utc_now(),
            "manifest_sha256": None,
        },
    }
    manifest["integrity"]["manifest_sha256"] = nested_record_digest(
        manifest, "integrity", "manifest_sha256"
    )
    atomic_write_json(
        run_directory / "manifest.json",
        manifest,
        overwrite=False,
        mode=0o444,
    )
    if interrupted:
        raise RunInterrupted(f"{run_id} was interrupted and sealed")
    return manifest
