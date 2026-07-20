from __future__ import annotations

import json
import hashlib
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
    atomic_write_bytes,
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
    StudyPaths,
    ValidationError,
    WorkflowError,
    require_id,
    utc_now,
)
from .validation import brief_approval_issues, brief_content_issues, errors_only
from .workspace import (
    change_validation_path,
    change_state_evidence_eligible,
    changeset_path,
    critical_actual_paths,
    evaluate_changes,
    load_repository_profile,
    repository_profile_path,
)


_RUN_DIRECTORY_RE = re.compile(r"^RUN-([0-9]{6})$")
_RUN_SCHEMA_VERSION = 2
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


def _symlink_component(root: Path, path: Path) -> Path | None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        # External scientific inputs are allowed and are canonicalized by
        # file_record(). Their leaf may not itself be a symbolic link, but a
        # platform path alias such as macOS /var -> /private/var is harmless
        # once the canonical absolute path and content hash are recorded.
        return None
    current = root.absolute()
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return current
    return None


def _require_output_root(
    root: Path,
    object_root: Path,
    raw_paths: Sequence[str | os.PathLike[str]] | None,
) -> None:
    resolved_object_root = object_root.resolve(strict=False)
    for raw in raw_paths or ():
        if Path(raw).is_absolute():
            raise ValidationError(
                "Run output paths must be repository-relative paths below the "
                f"configured object_root {resolved_object_root}: {raw!r}"
            )
        lexical_candidate = _user_path(root, raw)
        link = _symlink_component(root, lexical_candidate)
        if link is not None:
            raise ValidationError(
                f"Run output must not use a symbolic-link component: {link}"
            )
        candidate = lexical_candidate.resolve(strict=False)
        try:
            candidate.relative_to(resolved_object_root)
        except ValueError as exc:
            raise ValidationError(
                f"Run output must stay below configured object_root "
                f"{resolved_object_root}: {raw!r}"
            ) from exc


def _require_new_output_paths(
    root: Path,
    raw_paths: Sequence[str | os.PathLike[str]] | None,
) -> None:
    for raw in raw_paths or ():
        candidate = _user_path(root, raw)
        if candidate.is_symlink() or candidate.exists():
            raise ValidationError(
                f"Run output path must be new and immutable; refusing to overwrite: {raw!r}"
            )


def _argument_literal_values(argument: str) -> list[str]:
    raw_candidates = [argument]
    if "=" in argument:
        _, value = argument.split("=", 1)
        if value:
            raw_candidates.append(value)
    raw_candidates.extend(
        match.group(1)
        for match in re.finditer(r"['\"]([^'\"]+)['\"]", argument)
        if match.group(1)
    )
    return raw_candidates


def _argument_file_candidates(configured_cwd: Path, argument: str) -> list[Path]:
    raw_candidates = _argument_literal_values(argument)
    candidates: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_candidates:
        path = Path(raw)
        candidate = path if path.is_absolute() else configured_cwd / path
        if not candidate.is_symlink() and not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(candidate)
    return candidates


def _fixed_by_head(root: Path, candidate: Path) -> bool:
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if tracked.returncode != 0:
        return False
    clean = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", relative],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    return clean.returncode == 0


def _active_work_mentions(
    paths: StudyPaths,
    configured_cwd: Path,
    argv: Sequence[str],
) -> list[Path]:
    active = paths.active_work.resolve()
    if not active.is_dir():
        return []
    files = [
        path
        for path in sorted(active.rglob("*"))
        if not path.is_symlink() and path.is_file()
    ]
    root = paths.root.resolve()
    active_representations = {
        str(active),
        active.relative_to(root).as_posix(),
    }
    try:
        active_representations.add(active.relative_to(configured_cwd).as_posix())
    except ValueError:
        pass
    mentioned: set[Path] = set()
    literal_values = {
        raw
        for argument in argv
        for raw in _argument_literal_values(argument)
    }
    for file_path in files:
        representations = {
            str(file_path),
            file_path.relative_to(root).as_posix(),
        }
        try:
            representations.add(file_path.relative_to(configured_cwd).as_posix())
        except ValueError:
            pass
        if any(representation in literal_values for representation in representations):
            mentioned.add(file_path)
            continue
        if any(
            representation in literal_values
            for representation in active_representations
        ):
            mentioned.add(file_path)
    return sorted(mentioned)


def _python_module_file_candidates(
    configured_cwd: Path,
    argv: Sequence[str],
) -> list[Path]:
    """Resolve the direct target of ``python -m package.module`` when local.

    This is a narrow static safeguard, not general import tracing.  It catches
    the common case where an ignored or untracked research module would
    otherwise be invisible to Git and absent as a literal path in ``argv``.
    """

    if len(argv) < 3 or argv[1] != "-m":
        return []
    executable = Path(argv[0]).name.lower()
    if not executable.startswith("python"):
        return []
    module = argv[2]
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) is None:
        return []
    relative = Path(*module.split("."))
    candidates = (
        configured_cwd / relative.with_suffix(".py"),
        configured_cwd / relative / "__main__.py",
    )
    return [path for path in candidates if not path.is_symlink() and path.is_file()]


def _require_declared_mutable_command_inputs(
    paths: StudyPaths,
    configured_cwd: Path,
    argv: Sequence[str],
    raw_inputs: Sequence[str | os.PathLike[str]] | None,
) -> None:
    """Require mutable files named by argv to be pinned as Run inputs.

    Clean files tracked by ``HEAD`` are already fixed by the Run commit. Study
    files, external files, ignored files, and dirty/untracked repository files
    are not, so every statically visible dependency must be declared.
    """
    root = paths.root.resolve()
    study = paths.study.resolve()
    declared = {
        _user_path(root, raw).resolve(strict=False)
        for raw in raw_inputs or ()
    }
    missing: list[str] = []
    candidates: list[tuple[int, Path]] = []
    for index, argument in enumerate(argv):
        for candidate in _argument_file_candidates(configured_cwd, argument):
            candidates.append((index, candidate))
    candidates.extend(
        (-1, candidate)
        for candidate in _python_module_file_candidates(configured_cwd, argv)
    )
    candidates.extend((-1, candidate) for candidate in _active_work_mentions(paths, configured_cwd, argv))
    seen: set[Path] = set()
    for index, candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            resolved.relative_to(root)
            inside_repository = True
        except ValueError:
            inside_repository = False
        try:
            resolved.relative_to(study)
            inside_study = True
        except ValueError:
            inside_study = False
        # argv[0] may be an external system executable whose identity is
        # captured in the environment record rather than as scientific input.
        if index == 0 and not inside_repository:
            continue
        requires_input = inside_study or not _fixed_by_head(root, candidate)
        if requires_input and resolved not in declared:
            missing.append(_display_path(candidate, root))
    if missing:
        joined = ", ".join(sorted(set(missing)))
        raise ValidationError(
            "command references mutable or uncommitted file(s) that are not "
            f"declared Run inputs: {joined}; add each path with --input"
        )


def _seal_output_paths(
    root: Path,
    raw_paths: Sequence[str | os.PathLike[str]] | None,
) -> None:
    for raw in raw_paths or ():
        candidate = _user_path(root, raw)
        if not candidate.is_symlink() and candidate.is_file():
            os.chmod(candidate, 0o444)


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


def _capture_formal_artifacts(
    paths: StudyPaths,
    policy: dict[str, Any],
) -> list[tuple[Path, str, str, bytes]]:
    if not paths.formal.is_dir():
        return []
    known_kinds = {
        (paths.study / str(relative)).resolve(): str(kind)
        for kind, relative in policy.get("formal_artifacts", {}).items()
    }
    records: list[tuple[Path, str, str, bytes]] = []
    for path in sorted(paths.formal.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValidationError(f"symbolic links are not accepted as formal artifacts: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(paths.formal)
        if path.name in {"CHANGESET.json", "VALIDATION.json"} or (
            relative.parts and relative.parts[0] == "changeset-history"
        ):
            # These governance records are pinned separately in change_scope.
            continue
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        kind = known_kinds.get(
            path.resolve(), path.relative_to(paths.formal).as_posix()
        )
        records.append((path, kind, digest, payload))
    return records


def _formal_capture_digest(
    paths: StudyPaths,
    captured: Sequence[tuple[Path, str, str, bytes]],
) -> str:
    return sha256_json(
        [
            {
                "path": source.relative_to(paths.formal).as_posix(),
                "kind": kind,
                "sha256": digest,
            }
            for source, kind, digest, _ in captured
        ]
    )


def _snapshot_formal_artifacts(
    root: Path,
    paths: StudyPaths,
    run_directory: Path,
    captured: Sequence[tuple[Path, str, str, bytes]],
) -> list[dict[str, Any]]:
    snapshot_root = run_directory / "formal-artifacts"
    records: list[dict[str, Any]] = []
    for source, kind, digest, payload in captured:
        relative = source.relative_to(paths.formal)
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(destination, payload, overwrite=False, mode=0o444)
        record = file_record(destination, root)
        if record["sha256"] != digest:
            raise ValidationError(
                f"formal artifact changed while its Run snapshot was created: {source}"
            )
        record["kind"] = kind
        records.append(record)
    return records


def _formal_artifacts_unchanged(
    paths: StudyPaths,
    policy: dict[str, Any],
    before_digest: str,
) -> bool:
    try:
        current = _capture_formal_artifacts(paths, policy)
    except (OSError, WorkflowError, ValidationError):
        return False
    return _formal_capture_digest(paths, current) == before_digest


def _snapshot_change_authorities(
    root: Path,
    paths: StudyPaths,
    run_directory: Path,
) -> dict[str, dict[str, Any] | None]:
    """Copy mutable governance records into the immutable Run directory."""

    governance = run_directory / "governance"
    governance.mkdir()
    sources: tuple[tuple[str, Path, str, bool], ...] = (
        (
            "repository_profile",
            repository_profile_path(root),
            "repository-profile.json",
            True,
        ),
        ("changeset", changeset_path(paths), "CHANGESET.json", False),
        ("validation", change_validation_path(paths), "VALIDATION.json", False),
    )
    records: dict[str, dict[str, Any] | None] = {}
    for key, source, filename, required in sources:
        if not source.is_file():
            if required:
                raise ValidationError(f"required Run authority is unavailable: {source}")
            records[key] = None
            continue
        destination = governance / filename
        atomic_write_bytes(
            destination,
            source.read_bytes(),
            overwrite=False,
            mode=0o444,
        )
        records[key] = file_record(destination, root)
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
        link = _symlink_component(root, path)
        if link is not None:
            raise ValidationError(f"Run input uses a symbolic-link component: {link}")
        snapshot = file_record(path, root)
        recorded_path = Path(snapshot["path"])
        canonical_path = recorded_path if recorded_path.is_absolute() else root / recorded_path
        records.append(
            (
                canonical_path,
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
        link = _symlink_component(root, path)
        if link is not None:
            raise ValidationError(f"Run output uses a symbolic-link component: {link}")
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
    profile = load_repository_profile(root)
    configured_cwd = _user_path(root, str(profile["run_cwd"])).resolve()
    try:
        configured_cwd.relative_to(root)
    except ValueError as exc:
        raise ValidationError("repository profile run_cwd must stay inside the repository") from exc
    if not configured_cwd.is_dir():
        raise ValidationError(f"repository profile run_cwd does not exist: {configured_cwd}")
    object_root = _user_path(root, str(profile["object_root"]))
    _require_output_root(root, object_root, output_paths)
    _require_new_output_paths(root, output_paths)
    output_policies = _output_policies(
        root,
        output_paths,
        pinned_outputs=pinned_outputs,
        baseline_outputs=baseline_outputs,
        unique_anomaly_outputs=unique_anomaly_outputs,
    )
    _require_declared_mutable_command_inputs(
        paths,
        configured_cwd,
        argv,
        input_paths,
    )
    approval = _require_fresh_brief(paths)
    change_scope_before = evaluate_changes(paths)
    if change_scope_before["outcome"] == "BLOCKED":
        details = "\n".join(
            f"- {item['rule']}: {item['path'] or '<repository>'}: {item['reason']}"
            for item in change_scope_before["violations"]
        )
        raise ValidationError(f"change-scope gate blocked Run:\n{details}")
    gpu_hours = require_nonnegative_finite("estimated GPU hours", estimated_gpu_hours)
    cpu_hours = require_nonnegative_finite("estimated CPU hours", estimated_cpu_hours)
    actual_critical_paths = critical_actual_paths(change_scope_before, profile)
    effective_changed_paths = list(
        dict.fromkeys([*declared_changed_paths, *actual_critical_paths])
    )
    effective_scientific_critical = bool(scientific_critical or actual_critical_paths)
    formalization = check_formalization(
        paths,
        {
            "estimated_gpu_hours": gpu_hours,
            "estimated_cpu_hours": cpu_hours,
            "changed_path": effective_changed_paths,
            "scientific_critical": effective_scientific_critical,
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
    captured_formal_artifacts = _capture_formal_artifacts(paths, policy)
    formal_capture_digest = _formal_capture_digest(paths, captured_formal_artifacts)
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
    formal_artifacts = _snapshot_formal_artifacts(
        root,
        paths,
        run_directory,
        captured_formal_artifacts,
    )
    change_authorities = _snapshot_change_authorities(
        root,
        paths,
        run_directory,
    )
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
                cwd=configured_cwd,
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
    change_scope_after = evaluate_changes(paths)
    os.chmod(stdout_path, 0o444)
    os.chmod(stderr_path, 0o444)
    outputs = _output_records(root, output_paths, output_policies)
    _seal_output_paths(root, output_paths)
    finalized_inputs = _finalize_input_records(inputs)
    formal_artifacts_unchanged = _formal_artifacts_unchanged(
        paths,
        policy,
        formal_capture_digest,
    )
    dependencies_evidence_eligible = (
        all(record.get("changed_during_run") is False for record in finalized_inputs)
        and all(record.get("present") is True for record in outputs)
        and formal_artifacts_unchanged
    )
    status = (
        "interrupted"
        if interrupted
        else "succeeded"
        if exit_code == 0 and not execution_failed
        else "failed"
    )
    manifest: dict[str, Any] = {
        "schema_version": _RUN_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "run_id": run_id,
        "purpose": purpose,
        "status": status,
        "execution": {
            "argv": command,
            "cwd": str(configured_cwd),
            "cwd_relative": str(profile["run_cwd"]),
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
        "change_scope": {
            "repository_profile": change_authorities["repository_profile"],
            "changeset": change_authorities["changeset"],
            "validation": change_authorities["validation"],
            "before": change_scope_before,
            "after": change_scope_after,
            "evidence_eligible": (
                change_state_evidence_eligible(change_scope_before)
                and change_state_evidence_eligible(change_scope_after)
                and dependencies_evidence_eligible
            ),
        },
        "brief": brief,
        "formal_artifacts": formal_artifacts,
        "formalization": {
            "changed_paths": effective_changed_paths,
            "declared_changed_paths": declared_changed_paths,
            "actual_changed_paths": [
                record["path"] for record in change_scope_before["changed_paths"]
            ],
            "scientific_critical": effective_scientific_critical,
            "shared_across_runs": bool(shared_across_runs),
            "artifacts_unchanged_during_run": formal_artifacts_unchanged,
            "outcome": formalization.outcome,
            "requirements": formalization.requirements,
        },
        "cohort": cohort,
        "environment": environment,
        "budget": {
            "estimated_gpu_hours": gpu_hours,
            "estimated_cpu_hours": cpu_hours,
        },
        "inputs": finalized_inputs,
        "outputs": outputs,
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
