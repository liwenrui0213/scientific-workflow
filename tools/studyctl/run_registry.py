from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Sequence

from .active_context import require_growth_allowed
from .budget import (
    budget_projection,
    format_budget_violation,
    parse_brief_hard_budget,
    requested_budget,
)
from .formalization import check_formalization, load_policy
from .git_state import git_state, git_tracked_state
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    file_record,
    load_json,
    load_json_bytes,
    nested_record_digest,
    record_digest,
    sha256_bytes,
    sha256_file,
    sha256_json,
)
from .locking import study_authority_lock
from .models import (
    RunInterrupted,
    StudyPaths,
    ValidationError,
    ValidationIssue,
    WorkflowError,
    require_id,
    utc_now,
)
from .run_ledger import (
    bootstrap_or_reconcile_ledger,
    ledger_commitment_totals,
    mark_registration_aborted,
    migrate_legacy_ledger,
    record_manifest_in_ledger,
    reserve_run_id,
)
from .validation import (
    brief_text_issues,
    errors_only,
    normalized_run_output_key,
    object_schema_issues,
    protected_artifact_snapshot,
    retained_run_output_budget_issues,
    run_index,
    run_output_ownership,
)
from .workspace import (
    change_validation_path,
    change_state_evidence_eligible,
    changeset_path,
    critical_actual_paths,
    evaluate_changes,
    load_repository_profile,
    repository_profile_path,
)


_RUN_SCHEMA_VERSION = 3
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


def _tracked_state(paths: StudyPaths, root: Path) -> dict[str, Any]:
    ledger_relative = (paths.study / "RUNS.ledger.json").relative_to(
        root.resolve()
    )
    return git_tracked_state(
        root, exclude_paths=[ledger_relative.as_posix()]
    )


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


def _seal_recorded_output_paths(
    root: Path,
    records: Sequence[dict[str, Any]],
) -> list[str]:
    """Freeze every declared regular output that currently exists.

    ``present`` means that a stable size/hash record was established, not that
    the declared path may be ignored when it is false.  A hash failure can
    therefore leave ``present=false`` even though retained bytes exist.  Such
    bytes are still made read-only; if a stable record cannot be recovered,
    later Run admission treats the extant path as unverifiable and fails
    closed.
    """

    errors: list[str] = []
    for record in records:
        raw = record.get("path")
        if not isinstance(raw, str) or not raw:
            errors.append("recorded Run output has no usable path")
            continue
        candidate = _user_path(root, raw)
        link = _symlink_component(root, candidate)
        if link is not None:
            errors.append(f"Run output uses a symbolic-link component: {link}")
            continue
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            if record.get("present") is True:
                errors.append(f"Run output disappeared before it could be sealed: {raw}")
            continue
        except OSError as exc:
            errors.append(f"cannot inspect Run output {raw} before sealing: {exc}")
            continue
        try:
            if not stat.S_ISREG(metadata.st_mode):
                errors.append(f"Run output is no longer a regular file: {raw}")
                continue
            # Preserve the latest known byte lower bound even when hashing
            # failed.  The immutable ledger will charge it, and a surviving
            # unverifiable path blocks every later Run.
            if record.get("present") is not True:
                record["size"] = metadata.st_size
            os.chmod(candidate, 0o444)
            after = file_record(candidate, root)
            after_metadata = candidate.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(after_metadata.st_mode)
                or after_metadata.st_mode & 0o222
            ):
                errors.append(f"Run output was not sealed read-only: {raw}")
                continue
            if record.get("present") is True:
                if (
                    after.get("size") != record.get("size")
                    or after.get("sha256") != record.get("sha256")
                ):
                    errors.append(f"Run output changed before it could be sealed: {raw}")
            else:
                record.update(
                    {
                        "path": after["path"],
                        "present": True,
                        "size": after["size"],
                        "sha256": after["sha256"],
                    }
                )
        except (OSError, WorkflowError) as exc:
            errors.append(f"cannot seal Run output {raw}: {exc}")
    return errors


def _capture_fresh_brief_authority(
    paths: StudyPaths,
) -> tuple[bytes, str, bytes, dict[str, Any]]:
    """Capture and validate one exact approved Brief/approval byte pair."""

    for path, label in (
        (paths.brief, "Brief"),
        (paths.brief_approval, "Brief approval"),
    ):
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"{label} must be a regular, non-symbolic-link file")
    try:
        brief_bytes = paths.brief.read_bytes()
        brief_text = brief_bytes.decode("utf-8")
        approval_bytes = paths.brief_approval.read_bytes()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"cannot capture approved Brief authority: {exc}") from exc
    approval = load_json_bytes(approval_bytes, label=str(paths.brief_approval))
    issues = errors_only(brief_text_issues(paths.brief, brief_text))
    issues.extend(
        errors_only(
            object_schema_issues(
                paths.root,
                "brief_approval",
                paths.brief_approval,
                approval,
            )
        )
    )
    if not isinstance(approval, dict):
        raise ValidationError("Brief approval must be a JSON object")
    brief_hash = sha256_bytes(brief_bytes)
    approved_brief = approval.get("brief")
    expected_path = paths.brief.relative_to(paths.root).as_posix()
    if approval.get("study_id") != paths.study_id:
        issues.append(
            ValidationIssue("ERROR", str(paths.brief_approval), "approval study_id does not match Study")
        )
    if approval.get("approval_sha256") != record_digest(
        approval, "approval_sha256"
    ):
        issues.append(
            ValidationIssue("ERROR", str(paths.brief_approval), "approval_sha256 does not match record")
        )
    if not isinstance(approved_brief, dict) or (
        approved_brief.get("path") != expected_path
        or approved_brief.get("sha256") != brief_hash
    ):
        issues.append(
            ValidationIssue("ERROR", str(paths.brief_approval), "approval does not authorize the captured Brief")
        )
    if approval.get("protected_artifacts") != protected_artifact_snapshot(paths):
        issues.append(
            ValidationIssue(
                "ERROR",
                str(paths.brief_approval),
                "protected evaluator, data split, or acceptance criteria changed after approval",
            )
        )
    if issues:
        details = "\n".join(issue.render() for issue in issues)
        raise ValidationError(f"a fresh approved Brief is required before a Run:\n{details}")
    return brief_bytes, brief_text, approval_bytes, approval


def _require_fresh_brief(paths: StudyPaths) -> dict[str, Any]:
    return _capture_fresh_brief_authority(paths)[3]


@contextmanager
def _run_registry_lock(paths: StudyPaths) -> Iterator[None]:
    """Serialize budget projection, Run allocation, and the running record.

    Lock the Study directory inode rather than runs/. Replacing the entire Run
    directory therefore cannot create a second lock domain and budget ledger.
    POSIX advisory locks are released by the kernel if the registrar crashes.
    """

    if paths.runs.is_symlink() or not paths.runs.is_dir():
        raise ValidationError(
            "Run registry directory is missing or is not a regular directory"
        )
    with study_authority_lock(paths):
        yield


def migrate_legacy_run_ledger(paths: StudyPaths) -> Path:
    """Explicitly bind one intact pre-V3 Run history to a durable ledger."""

    with _run_registry_lock(paths):
        manifests = run_index(paths)
        migrate_legacy_ledger(paths, manifests)
    return paths.study / "RUNS.ledger.json"


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


def _captured_protected_artifacts(
    paths: StudyPaths,
    captured: Sequence[tuple[Path, str, str, bytes]],
) -> dict[str, Any]:
    by_source = {source.resolve(): digest for source, _, digest, _ in captured}
    result: dict[str, Any] = {}
    for filename in (
        "EVALUATOR.json",
        "DATASET_SPLIT.json",
        "ACCEPTANCE_CRITERIA.json",
    ):
        source = (paths.formal / filename).resolve()
        key = filename.removesuffix(".json").lower()
        digest = by_source.get(source)
        result[key] = (
            {
                "path": (paths.formal / filename)
                .relative_to(paths.root)
                .as_posix(),
                "sha256": digest,
            }
            if digest is not None
            else None
        )
    return result


def _planned_file_record(root: Path, path: Path, payload: bytes) -> dict[str, Any]:
    return {
        "path": _display_path(path, root),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _planned_formal_artifacts(
    root: Path,
    paths: StudyPaths,
    run_directory: Path,
    captured: Sequence[tuple[Path, str, str, bytes]],
) -> list[dict[str, Any]]:
    snapshot_root = run_directory / "formal-artifacts"
    records: list[dict[str, Any]] = []
    for source, kind, digest, payload in captured:
        destination = snapshot_root / source.relative_to(paths.formal)
        record = _planned_file_record(root, destination, payload)
        if record["sha256"] != digest:
            raise WorkflowError(
                f"captured formal-artifact digest is inconsistent: {source}"
            )
        record["kind"] = kind
        records.append(record)
    return records


def _snapshot_formal_artifacts(
    root: Path,
    paths: StudyPaths,
    run_directory: Path,
    captured: Sequence[tuple[Path, str, str, bytes]],
    *,
    record_run_directory: Path | None = None,
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
        if record_run_directory is not None:
            record["path"] = _display_path(
                record_run_directory / "formal-artifacts" / relative,
                root,
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


def _capture_change_authorities(
    root: Path,
    paths: StudyPaths,
) -> dict[str, tuple[str, bytes] | None]:
    """Capture mutable governance bytes before a Run identity is allocated."""

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
    captured: dict[str, tuple[str, bytes] | None] = {}
    for key, source, filename, required in sources:
        if source.is_symlink() or not source.is_file():
            if required:
                raise ValidationError(f"required Run authority is unavailable: {source}")
            captured[key] = None
            continue
        captured[key] = (filename, source.read_bytes())
    return captured


def _planned_change_authorities(
    root: Path,
    run_directory: Path,
    captured: dict[str, tuple[str, bytes] | None],
) -> dict[str, dict[str, Any] | None]:
    governance = run_directory / "governance"
    return {
        key: (
            None
            if item is None
            else _planned_file_record(root, governance / item[0], item[1])
        )
        for key, item in captured.items()
    }


def _snapshot_change_authorities(
    root: Path,
    run_directory: Path,
    captured: dict[str, tuple[str, bytes] | None],
    *,
    record_run_directory: Path | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Copy captured governance records into the immutable Run directory."""

    governance = run_directory / "governance"
    governance.mkdir(exist_ok=True)
    records: dict[str, dict[str, Any] | None] = {}
    for key, item in captured.items():
        if item is None:
            records[key] = None
            continue
        filename, payload = item
        destination = governance / filename
        atomic_write_bytes(
            destination,
            payload,
            overwrite=False,
            mode=0o444,
        )
        record = file_record(destination, root)
        if record_run_directory is not None:
            record["path"] = _display_path(
                record_run_directory / "governance" / filename,
                root,
            )
        records[key] = record
    return records


def _fsync_directory_tree(root: Path) -> None:
    """Durably publish every directory entry below a staged Run tree."""

    directories = [root]
    directories.extend(
        path
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    )
    for directory in sorted(
        directories,
        key=lambda item: len(item.relative_to(root).parts),
        reverse=True,
    ):
        try:
            descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise WorkflowError(
                f"cannot durably sync staged Run directory {directory}: {exc}"
            ) from exc


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
    records, errors = _inspect_output_records(root, raw_paths, policies)
    if errors:
        raise ValidationError("; ".join(errors))
    return records


def _lexical_output_key(root: Path, raw: str | os.PathLike[str]) -> Path:
    """Return a normalized key that is stable if the output becomes a symlink."""

    return normalized_run_output_key(root, raw)


def _display_lexical_output_path(
    root: Path, raw: str | os.PathLike[str]
) -> str:
    path = _lexical_output_key(root, raw)
    try:
        return path.relative_to(root.absolute()).as_posix()
    except ValueError:
        return str(path)


def _inspect_output_records(
    root: Path,
    raw_paths: Sequence[str | os.PathLike[str]] | None,
    policies: dict[Path, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Inspect each declared output independently.

    A malformed output must not erase the size and hash records of other safe
    outputs.  This matters for incomplete Runs: every retained regular file
    still consumes the human-authorized storage budget.
    """

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in raw_paths or ():
        path = _user_path(root, raw)
        policy = policies[_lexical_output_key(root, raw)]
        base: dict[str, Any] = {
            "path": _display_lexical_output_path(root, raw),
            "present": False,
            "size": None,
            "sha256": None,
            "classification": policy["classification"],
            "pinned": policy["pinned"],
        }
        link = _symlink_component(root, path)
        if link is not None:
            errors.append(f"Run output uses a symbolic-link component: {link}")
            records.append(base)
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            records.append(base)
            continue
        except OSError as exc:
            errors.append(f"cannot inspect Run output {base['path']}: {exc}")
            records.append(base)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            records.append(base)
            continue
        # Record the retained bytes even if hashing subsequently fails.
        base["size"] = metadata.st_size
        try:
            snapshot = file_record(path, root)
        except (OSError, WorkflowError) as exc:
            errors.append(f"cannot record Run output {base['path']}: {exc}")
        else:
            base.update(
                {
                    "path": snapshot["path"],
                    "present": True,
                    "size": snapshot["size"],
                    "sha256": snapshot["sha256"],
                }
            )
        records.append(base)
    return records, errors


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
        key = _lexical_output_key(root, raw)
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
            key = _lexical_output_key(root, raw)
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


def _open_registered_log(path: Path) -> Any:
    """Open a log that was created before the running Manifest was registered."""

    if path.is_symlink() or not path.is_file():
        raise WorkflowError(f"registered Run log is unavailable or unsafe: {path}")
    return path.open("ab")


def _flush_log(handle: Any) -> None:
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkflowError(f"cannot durably sync Run log: {exc}") from exc


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - same-user children are normal
        return True
    return True


def _drain_process_group(process_group_id: int, *, grace_seconds: float = 0.25) -> None:
    """Terminate every descendant in a Run's dedicated POSIX process group."""

    if os.name != "posix":  # pragma: no cover - POSIX is enforced for registry locks
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        kill_deadline = time.monotonic() + 1.0
        while (
            _process_group_exists(process_group_id)
            and time.monotonic() < kill_deadline
        ):
            time.sleep(0.01)
        if _process_group_exists(process_group_id):
            raise WorkflowError(
                "could not confirm that every Run descendant terminated"
            )


def _terminate_process(process: subprocess.Popen[Any]) -> int | None:
    if os.name == "posix":
        _drain_process_group(process.pid)
    elif process.poll() is None:  # pragma: no cover - exercised on non-POSIX hosts
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - exercised on non-POSIX hosts
                process.kill()
        except ProcessLookupError:
            pass
        return process.wait()


def _planned_output_records(
    root: Path,
    raw_paths: Sequence[str | os.PathLike[str]] | None,
    policies: dict[Path, dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raw_paths or ():
        path = _user_path(root, raw)
        policy = policies[_lexical_output_key(root, raw)]
        records.append(
            {
                "path": _display_path(path, root),
                "present": False,
                "size": None,
                "sha256": None,
                "classification": policy["classification"],
                "pinned": policy["pinned"],
            }
        )
    return records


def _actual_output_storage_gb(outputs: Sequence[dict[str, Any]]) -> float:
    total_bytes = sum(
        int(record.get("size") or 0)
        for record in outputs
        if isinstance(record.get("size"), int)
        and not isinstance(record.get("size"), bool)
        and int(record["size"]) >= 0
    )
    return total_bytes / 1_000_000_000.0


def _seal_terminal_manifest(path: Path, manifest: dict[str, Any]) -> None:
    current = load_json(path)
    if not isinstance(current, dict) or current.get("status") != "running":
        raise WorkflowError(
            f"refusing to replace a Run manifest that is not running: {path}"
        )
    integrity = current.get("integrity")
    if not isinstance(integrity, dict) or any(
        integrity.get(key) is not None for key in ("sealed_at", "manifest_sha256")
    ):
        raise WorkflowError(f"running Run manifest has unexpected terminal integrity: {path}")
    atomic_write_json(
        path,
        manifest,
        overwrite=True,
        mode=0o444,
        require_parent_fsync=True,
    )


def _failure_record(phase: str, exc: BaseException) -> dict[str, str]:
    return {
        "phase": phase,
        "type": type(exc).__name__,
        "message": str(exc) or repr(exc),
    }


def _seal_incomplete_after_failure(
    *,
    paths: StudyPaths,
    root: Path,
    policy: dict[str, Any],
    manifest_path: Path,
    initial_manifest: dict[str, Any],
    inputs: list[tuple[Path, dict[str, Any]]],
    output_paths: Sequence[str | os.PathLike[str]] | None,
    output_policies: dict[Path, dict[str, Any]],
    stdout_path: Path,
    stderr_path: Path,
    formal_capture_digest: str,
    started_monotonic: float,
    exit_code: int | None,
    phase: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Best-effort conversion of a visible running record to incomplete."""

    manifest = copy.deepcopy(initial_manifest)
    manifest["status"] = "incomplete"
    manifest["failure"] = _failure_record(phase, exc)
    manifest["execution"].update(
        {
            "ended_at": utc_now(),
            "duration_seconds": max(0.0, time.monotonic() - started_monotonic),
            "exit_code": exit_code,
        }
    )
    try:
        code_after = _tracked_state(paths, root)
    except BaseException:
        code_after = manifest["code_state"]["before"]
    manifest["code_state"].update(
        {
            "after": code_after,
            "changed_during_run": manifest["code_state"]["before"] != code_after,
        }
    )
    try:
        scope_after = evaluate_changes(paths)
    except BaseException:
        scope_after = manifest["change_scope"]["before"]
    manifest["change_scope"].update(
        {"after": scope_after, "evidence_eligible": False}
    )
    try:
        manifest["inputs"] = _finalize_input_records(inputs)
    except BaseException:
        pass
    try:
        outputs, output_errors = _inspect_output_records(
            root, output_paths, output_policies
        )
    except BaseException as output_exc:
        outputs = manifest["outputs"]
        output_errors = [f"output inspection failed: {output_exc}"]
    manifest["outputs"] = outputs
    output_errors.extend(_seal_recorded_output_paths(root, outputs))
    if output_errors:
        manifest["failure"]["message"] += "; " + "; ".join(output_errors)
    actual_storage = _actual_output_storage_gb(outputs)
    manifest["budget"]["actual_output_storage_gb"] = actual_storage
    requested = dict(manifest["budget"]["requested"])
    requested["storage_gb"] = max(requested["storage_gb"], actual_storage)
    projection = budget_projection(
        manifest["budget"]["hard_limits"],
        manifest["budget"]["committed_before"],
        requested,
    )
    manifest["budget"]["requested"] = requested
    manifest["budget"]["committed_after"] = projection["committed_after"]
    manifest["budget"]["violations"] = projection["violations"]
    manifest["formalization"]["artifacts_unchanged_during_run"] = (
        _formal_artifacts_unchanged(paths, policy, formal_capture_digest)
    )
    for path in (stdout_path, stderr_path):
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
    try:
        manifest["logs"] = {
            "stdout": file_record(stdout_path, root),
            "stderr": file_record(stderr_path, root),
        }
    except BaseException:
        # The initial records still identify the intended files; validation
        # will report any missing or changed log explicitly.
        pass
    manifest["integrity"] = {
        "sealed_at": utc_now(),
        "manifest_sha256": None,
    }
    manifest["integrity"]["manifest_sha256"] = nested_record_digest(
        manifest, "integrity", "manifest_sha256"
    )
    _seal_terminal_manifest(manifest_path, manifest)
    return manifest


def execute_run(
    paths: StudyPaths,
    *,
    argv: list[str],
    purpose: str,
    cohort_id: str | None = None,
    estimated_gpu_hours: float = 0.0,
    estimated_cpu_hours: float = 0.0,
    estimated_storage_gb: float = 0.0,
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
    require_growth_allowed(paths, "the next Run")

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
    requested_resources = requested_budget(
        gpu_hours=estimated_gpu_hours,
        cpu_hours=estimated_cpu_hours,
        storage_gb=estimated_storage_gb,
    )
    gpu_hours = requested_resources["gpu_hours"]
    cpu_hours = requested_resources["cpu_hours"]
    storage_gb = requested_resources["storage_gb"]
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
            "estimated_storage_gb": storage_gb,
            "changed_path": effective_changed_paths,
            "scientific_critical": effective_scientific_critical,
            "shared_across_runs": bool(shared_across_runs),
            "for_run": True,
        },
    )
    if formalization.blocked:
        requirements = "\n".join(
            f"- {item['level']}: {item['artifact']}: {item['reason']}"
            for item in formalization.requirements
        )
        raise ValidationError(f"formalization gate blocked Run:\n{requirements}")

    command = list(argv)

    # Budget check, reservation, Run-ID allocation, and the initial Manifest
    # are one serialized registration transaction.  No child process can
    # start before its reservation is durable and visible.
    with _run_registry_lock(paths):
        # Recheck under the shared Study authority lock so concurrent Run or
        # compaction transitions cannot cross the hard pressure boundary.
        require_growth_allowed(paths, "the next Run")
        brief_bytes, brief_text, approval_payload, approval = (
            _capture_fresh_brief_authority(paths)
        )
        brief_digest = sha256_bytes(brief_bytes)
        approval_file_digest = sha256_bytes(approval_payload)
        hard_limits = parse_brief_hard_budget(brief_text)
        change_scope_before = evaluate_changes(paths)
        if change_scope_before["outcome"] == "BLOCKED":
            details = "\n".join(
                f"- {item['rule']}: {item['path'] or '<repository>'}: {item['reason']}"
                for item in change_scope_before["violations"]
            )
            raise ValidationError(f"change-scope gate blocked Run:\n{details}")
        actual_critical_paths = critical_actual_paths(change_scope_before, profile)
        effective_changed_paths = list(
            dict.fromkeys([*declared_changed_paths, *actual_critical_paths])
        )
        effective_scientific_critical = bool(
            scientific_critical or actual_critical_paths
        )
        formalization = check_formalization(
            paths,
            {
                "estimated_gpu_hours": gpu_hours,
                "estimated_cpu_hours": cpu_hours,
                "estimated_storage_gb": storage_gb,
                "changed_path": effective_changed_paths,
                "scientific_critical": effective_scientific_critical,
                "shared_across_runs": bool(shared_across_runs),
                "for_run": True,
            },
        )
        if formalization.blocked:
            requirements = "\n".join(
                f"- {item['level']}: {item['artifact']}: {item['reason']}"
                for item in formalization.requirements
            )
            raise ValidationError(f"formalization gate blocked Run:\n{requirements}")

        existing_runs = run_index(paths)
        # The early output check is only a fast preflight. Re-check filesystem
        # novelty and reserve every normalized path while registration is
        # serialized. The running Manifest is published before this lock is
        # released, so a concurrent registrar must observe the reservation
        # even when the first Run never produces the declared file.
        _require_output_root(root, object_root, output_paths)
        claimed_outputs, _ = run_output_ownership(root, existing_runs)
        for output_path in output_policies:
            owner = claimed_outputs.get(output_path)
            if owner is not None:
                owner_run_id, _ = owner
                raise ValidationError(
                    f"Run output path {_display_path(output_path, root)} is "
                    f"already claimed by {owner_run_id}"
                )
        _require_new_output_paths(root, output_paths)
        unresolved = sorted(
            run_id
            for run_id, (_, item) in existing_runs.items()
            if item.get("status") == "running" and item.get("outputs")
        )
        if unresolved:
            raise ValidationError(
                "unresolved output-producing running Run(s) block new "
                "registration because their final storage use is not yet "
                "known: "
                + ", ".join(unresolved)
            )
        retained_output_issues = [
            issue
            for _, manifest_record in existing_runs.values()
            for issue in retained_run_output_budget_issues(
                paths, manifest_record
            )
        ]
        if retained_output_issues:
            details = "\n".join(issue.render() for issue in retained_output_issues)
            raise ValidationError(
                "retained Run output integrity blocks budget admission:\n"
                + details
            )
        ledger = bootstrap_or_reconcile_ledger(
            paths, existing_runs, write=True
        )
        committed_before = ledger_commitment_totals(ledger)
        reservation = budget_projection(
            hard_limits, committed_before, requested_resources
        )
        if reservation["violations"]:
            details = "; ".join(
                format_budget_violation(item)
                for item in reservation["violations"]
            )
            raise ValidationError(f"hard-budget gate blocked Run: {details}")

        policy = load_policy(paths)
        protocol_path, protocol = _load_protocol(paths)
        captured_formal_artifacts = _capture_formal_artifacts(paths, policy)
        if _captured_protected_artifacts(
            paths, captured_formal_artifacts
        ) != approval.get("protected_artifacts"):
            raise ValidationError(
                "protected artifacts changed while Run authority was captured"
            )
        formal_capture_digest = _formal_capture_digest(
            paths, captured_formal_artifacts
        )
        captured_change_authorities = _capture_change_authorities(root, paths)
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
        code_state_before = _tracked_state(paths, root)
        environment = _environment_record(
            effective_hardware,
            effective_precision,
            runtime_environment,
        )
        brief = {
            "path": _display_path(paths.brief, root),
            "sha256": brief_digest,
            "approval_sha256": str(approval["approval_sha256"]),
        }

        ledger, run_id = reserve_run_id(
            paths, ledger, reservation["requested"]
        )
        run_directory = paths.runs / run_id
        staging_directory = Path(
            tempfile.mkdtemp(
                prefix=f".{run_id}.",
                suffix=".registration.tmp",
                dir=paths.runs,
            )
        )
        formal_artifacts = _planned_formal_artifacts(
            root,
            paths,
            run_directory,
            captured_formal_artifacts,
        )
        change_authorities = _planned_change_authorities(
            root,
            run_directory,
            captured_change_authorities,
        )
        stdout_path = run_directory / "stdout.log"
        stderr_path = run_directory / "stderr.log"
        staged_stdout_path = staging_directory / "stdout.log"
        staged_stderr_path = staging_directory / "stderr.log"
        brief_snapshot_path = run_directory / "governance" / "BRIEF.md"
        approval_snapshot_path = (
            run_directory / "governance" / "BRIEF.approval.json"
        )
        staged_brief_snapshot_path = (
            staging_directory / "governance" / "BRIEF.md"
        )
        staged_approval_snapshot_path = (
            staging_directory / "governance" / "BRIEF.approval.json"
        )
        brief_snapshot = _planned_file_record(
            root, brief_snapshot_path, brief_bytes
        )
        approval_snapshot = _planned_file_record(
            root, approval_snapshot_path, approval_payload
        )
        brief["snapshot"] = brief_snapshot
        brief["approval_snapshot"] = approval_snapshot
        started_at = utc_now()
        started_monotonic = time.monotonic()
        initial_manifest: dict[str, Any] = {
            "schema_version": _RUN_SCHEMA_VERSION,
            "study_id": paths.study_id,
            "run_id": run_id,
            "purpose": purpose,
            "status": "running",
            "execution": {
                "argv": command,
                "cwd": str(configured_cwd),
                "cwd_relative": str(profile["run_cwd"]),
                "started_at": started_at,
                "ended_at": None,
                "duration_seconds": None,
                "exit_code": None,
                "seed": seed,
            },
            "git": git,
            "code_state": {
                "before": code_state_before,
                "after": code_state_before,
                "changed_during_run": False,
            },
            "change_scope": {
                "repository_profile": change_authorities["repository_profile"],
                "changeset": change_authorities["changeset"],
                "validation": change_authorities["validation"],
                "before": change_scope_before,
                "after": change_scope_before,
                "evidence_eligible": False,
            },
            "brief": brief,
            "formal_artifacts": formal_artifacts,
            "formalization": {
                "changed_paths": effective_changed_paths,
                "declared_changed_paths": declared_changed_paths,
                "actual_changed_paths": [
                    record["path"]
                    for record in change_scope_before["changed_paths"]
                ],
                "scientific_critical": effective_scientific_critical,
                "shared_across_runs": bool(shared_across_runs),
                "artifacts_unchanged_during_run": True,
                "outcome": formalization.outcome,
                "requirements": formalization.requirements,
            },
            "cohort": cohort,
            "environment": environment,
            "budget": {
                "estimated_gpu_hours": gpu_hours,
                "estimated_cpu_hours": cpu_hours,
                "estimated_storage_gb": storage_gb,
                "actual_output_storage_gb": None,
                "hard_limits": hard_limits,
                "committed_before": reservation["committed_before"],
                "requested": reservation["requested"],
                "committed_after": reservation["committed_after"],
                "violations": [],
            },
            "inputs": [dict(record) for _, record in inputs],
            "outputs": _planned_output_records(
                root, output_paths, output_policies
            ),
            "logs": {
                "stdout": _planned_file_record(root, stdout_path, b""),
                "stderr": _planned_file_record(root, stderr_path, b""),
            },
            "failure": None,
            "integrity": {
                "sealed_at": None,
                "manifest_sha256": None,
            },
        }
        try:
            atomic_write_bytes(
                staged_stdout_path, b"", overwrite=False, mode=0o600
            )
            atomic_write_bytes(
                staged_stderr_path, b"", overwrite=False, mode=0o600
            )
            staged_brief_snapshot_path.parent.mkdir(exist_ok=True)
            atomic_write_bytes(
                staged_brief_snapshot_path,
                brief_bytes,
                overwrite=False,
                mode=0o444,
            )
            atomic_write_bytes(
                staged_approval_snapshot_path,
                approval_payload,
                overwrite=False,
                mode=0o444,
            )
            recorded_brief = file_record(staged_brief_snapshot_path, root)
            recorded_brief["path"] = brief_snapshot["path"]
            if recorded_brief != brief_snapshot:
                raise WorkflowError(
                    f"{run_id} Brief snapshot differs from registration"
                )
            recorded_approval = file_record(
                staged_approval_snapshot_path, root
            )
            recorded_approval["path"] = approval_snapshot["path"]
            if recorded_approval != approval_snapshot:
                raise WorkflowError(
                    f"{run_id} Brief-approval snapshot differs from registration"
                )
            recorded_formal_artifacts = _snapshot_formal_artifacts(
                root,
                paths,
                staging_directory,
                captured_formal_artifacts,
                record_run_directory=run_directory,
            )
            recorded_change_authorities = _snapshot_change_authorities(
                root,
                staging_directory,
                captured_change_authorities,
                record_run_directory=run_directory,
            )
            if recorded_formal_artifacts != formal_artifacts:
                raise WorkflowError(
                    f"{run_id} formal-artifact snapshots differ from registration"
                )
            if recorded_change_authorities != change_authorities:
                raise WorkflowError(
                    f"{run_id} governance snapshots differ from registration"
                )
            staged_manifest_path = staging_directory / "manifest.json"
            atomic_write_json(
                staged_manifest_path,
                initial_manifest,
                overwrite=False,
                mode=0o600,
            )
            _fsync_directory_tree(staging_directory)
            if (
                paths.brief.is_symlink()
                or paths.brief_approval.is_symlink()
                or sha256_file(paths.brief) != brief_digest
                or sha256_file(paths.brief_approval) != approval_file_digest
                or protected_artifact_snapshot(paths)
                != approval.get("protected_artifacts")
            ):
                raise WorkflowError(
                    "Brief authority changed during Run registration"
                )
            os.rename(staging_directory, run_directory)
            try:
                registry_fd = os.open(paths.runs, os.O_RDONLY)
                try:
                    os.fsync(registry_fd)
                finally:
                    os.close(registry_fd)
            except OSError as exc:
                raise WorkflowError(
                    f"cannot durably publish {run_id} in the Run registry: {exc}"
                ) from exc
            manifest_path = run_directory / "manifest.json"
            ledger = record_manifest_in_ledger(
                paths,
                ledger,
                run_id,
                manifest_path,
                initial_manifest,
            )
        except BaseException:
            # Nothing below a RUN-* name becomes authoritative until the
            # complete staged directory, including its running Manifest, is
            # atomically renamed into place. A registration failure therefore
            # cannot create an orphan Run or launch the child process.
            if staging_directory.exists() and not staging_directory.is_symlink():
                shutil.rmtree(staging_directory, ignore_errors=True)
            if not run_directory.exists() and not run_directory.is_symlink():
                try:
                    mark_registration_aborted(paths, ledger, run_id)
                except BaseException:
                    # A visible reservation is safer than silently reusing
                    # its ID or dropping its conservative budget charge.
                    pass
            raise

    exit_code: int | None = None
    interrupted = False
    execution_failed = False
    execution_error: BaseException | None = None
    unexpected_error: BaseException | None = None
    process: subprocess.Popen[Any] | None = None
    manifest: dict[str, Any] | None = None

    try:
        with _open_registered_log(stdout_path) as stdout_log, _open_registered_log(
            stderr_path
        ) as stderr_log:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=configured_cwd,
                    stdout=stdout_log,
                    stderr=stderr_log,
                    shell=False,
                    start_new_session=os.name == "posix",
                )
                exit_code = process.wait()
                # A command may exit after daemonizing descendants.  Do not
                # seal a Run while same-session workers can still mutate its
                # outputs or repository state.
                if os.name == "posix":
                    _drain_process_group(process.pid)
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
                            (
                                "studyctl: interrupted command cleanup failed: "
                                f"{exc}\n"
                            ).encode("utf-8")
                        )
            except (OSError, ValueError) as exc:
                execution_failed = True
                execution_error = exc
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
            except BaseException as exc:
                unexpected_error = exc
                if process is not None:
                    try:
                        exit_code = _terminate_process(process)
                    except BaseException:
                        exit_code = None
                stderr_log.write(
                    f"studyctl: unexpected execution failure: {exc}\n".encode(
                        "utf-8"
                    )
                )
            finally:
                _flush_log(stdout_log)
                _flush_log(stderr_log)

        if unexpected_error is not None:
            raise unexpected_error

        code_state_after = _tracked_state(paths, root)
        change_scope_after = evaluate_changes(paths)
        outputs = _output_records(root, output_paths, output_policies)
        sealing_errors = _seal_recorded_output_paths(root, outputs)
        if sealing_errors:
            raise ValidationError("; ".join(sealing_errors))
        finalized_inputs = _finalize_input_records(inputs)
        formal_artifacts_unchanged = _formal_artifacts_unchanged(
            paths, policy, formal_capture_digest
        )
        dependencies_evidence_eligible = (
            all(
                record.get("changed_during_run") is False
                for record in finalized_inputs
            )
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
        manifest = copy.deepcopy(initial_manifest)
        manifest["status"] = status
        manifest["execution"].update(
            {
                "ended_at": utc_now(),
                "duration_seconds": max(
                    0.0, time.monotonic() - started_monotonic
                ),
                "exit_code": exit_code,
            }
        )
        manifest["code_state"].update(
            {
                "after": code_state_after,
                "changed_during_run": code_state_before != code_state_after,
            }
        )
        manifest["change_scope"].update(
            {
                "after": change_scope_after,
                "evidence_eligible": (
                    change_state_evidence_eligible(change_scope_before)
                    and change_state_evidence_eligible(change_scope_after)
                    and dependencies_evidence_eligible
                ),
            }
        )
        manifest["formalization"][
            "artifacts_unchanged_during_run"
        ] = formal_artifacts_unchanged
        manifest["inputs"] = finalized_inputs
        manifest["outputs"] = outputs
        actual_storage = _actual_output_storage_gb(outputs)
        os.chmod(stdout_path, 0o444)
        os.chmod(stderr_path, 0o444)
        terminal_logs = {
            "stdout": file_record(stdout_path, root),
            "stderr": file_record(stderr_path, root),
        }

        with _run_registry_lock(paths):
            current_runs = run_index(paths)
            current_ledger = bootstrap_or_reconcile_ledger(
                paths, current_runs, write=True
            )
            committed_without_self = ledger_commitment_totals(
                current_ledger, exclude_run_id=run_id
            )
            charged = dict(requested_resources)
            charged["storage_gb"] = max(storage_gb, actual_storage)
            final_budget = budget_projection(
                hard_limits, committed_without_self, charged
            )
            manifest["budget"].update(
                {
                    "actual_output_storage_gb": actual_storage,
                    "committed_before": final_budget["committed_before"],
                    "requested": final_budget["requested"],
                    "committed_after": final_budget["committed_after"],
                    "violations": final_budget["violations"],
                }
            )
            if final_budget["violations"]:
                manifest["status"] = "incomplete"
                manifest["failure"] = {
                    "phase": "budget_finalization",
                    "type": "HardBudgetExceeded",
                    "message": "; ".join(
                        format_budget_violation(item)
                        for item in final_budget["violations"]
                    ),
                }
                manifest["change_scope"]["evidence_eligible"] = False
            elif execution_error is not None:
                manifest["failure"] = _failure_record(
                    "execution", execution_error
                )

            manifest["logs"] = terminal_logs
            manifest["integrity"] = {
                "sealed_at": utc_now(),
                "manifest_sha256": None,
            }
            manifest["integrity"]["manifest_sha256"] = nested_record_digest(
                manifest, "integrity", "manifest_sha256"
            )
            _seal_terminal_manifest(manifest_path, manifest)
            record_manifest_in_ledger(
                paths,
                current_ledger,
                run_id,
                manifest_path,
                manifest,
            )
    except BaseException as exc:
        try:
            with _run_registry_lock(paths):
                current_runs = run_index(paths)
                current_ledger = bootstrap_or_reconcile_ledger(
                    paths, current_runs, write=True
                )
                committed_without_self = ledger_commitment_totals(
                    current_ledger, exclude_run_id=run_id
                )
                initial_manifest["budget"][
                    "committed_before"
                ] = committed_without_self
                incomplete_manifest = _seal_incomplete_after_failure(
                    paths=paths,
                    root=root,
                    policy=policy,
                    manifest_path=manifest_path,
                    initial_manifest=initial_manifest,
                    inputs=inputs,
                    output_paths=output_paths,
                    output_policies=output_policies,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    formal_capture_digest=formal_capture_digest,
                    started_monotonic=started_monotonic,
                    exit_code=exit_code,
                    phase=(
                        "execution"
                        if unexpected_error is not None
                        else "finalization"
                    ),
                    exc=exc,
                )
                record_manifest_in_ledger(
                    paths,
                    current_ledger,
                    run_id,
                    manifest_path,
                    incomplete_manifest,
                )
        except BaseException:
            # A durable running Manifest still exposes the unfinished Run and
            # keeps its budget reserved until explicit recovery.
            pass
        raise

    if interrupted:
        raise RunInterrupted(f"{run_id} was interrupted and sealed")
    if manifest is None:  # pragma: no cover - defensive invariant
        raise WorkflowError(f"{run_id} completed without a terminal manifest")
    return manifest
