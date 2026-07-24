from __future__ import annotations

import copy
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Iterable, Sequence

from .hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_bytes,
    sha256_file,
    sha256_json,
)
from .models import SCHEMA_VERSION, StudyPaths, ValidationError, ValidationIssue, WorkflowError, utc_now
from .validation import object_schema_issues


PROFILE_RELATIVE_PATH = Path("scientific-workflow/repository-profile.json")
CHANGESET_FILENAME = "CHANGESET.json"
VALIDATION_FILENAME = "VALIDATION.json"
_BUILT_IN_PROTECTED_PATTERNS = (
    "AGENTS.md",
    ".agents/skills/**",
    ".codex/**",
    "scientific-workflow/**",
    "tools/studyctl/**",
)


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def _normalize_relative(value: str, *, label: str, allow_glob: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty repository-relative path")
    if "\x00" in value:
        raise ValidationError(f"{label} must not contain a NUL byte")
    if "\\" in value:
        raise ValidationError(f"{label} must use POSIX '/' separators, not backslashes")
    normalized = value.strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValidationError(f"{label} must stay inside the repository: {value!r}")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", "."}:
        return "."
    if not allow_glob and any(token in normalized for token in ("*", "?", "[", "]")):
        raise ValidationError(f"{label} must not contain glob syntax: {value!r}")
    return normalized.rstrip("/")


def _validate_pattern(value: str, *, label: str) -> str:
    return _normalize_relative(value, label=label, allow_glob=True)


def _normalize_trusted_runtime_path(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty path")
    if "\x00" in value:
        raise ValidationError(f"{label} must not contain a NUL byte")
    if "\\" in value:
        raise ValidationError(f"{label} must use POSIX '/' separators")
    normalized = value.strip()
    path = PurePosixPath(normalized)
    if any(part == ".." for part in path.parts):
        raise ValidationError(f"{label} must not contain '..': {value!r}")
    if path.is_absolute():
        if path == PurePosixPath("/"):
            raise ValidationError(
                f"{label} must not expose the entire host filesystem"
            )
        return str(path)
    return _normalize_relative(normalized, label=label)


def _normalize_git_path(value: str) -> str:
    """Validate a Git-reported POSIX path without changing legal filename bytes."""
    if not isinstance(value, str) or value == "":
        raise ValidationError("Git changed path must be non-empty")
    if "\x00" in value:
        raise ValidationError("Git changed path must not contain a NUL byte")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValidationError(f"Git changed path escapes the worktree: {value!r}")
    return value


def _glob_parts_match(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    pattern = pattern_parts[0]
    if pattern == "**":
        return _glob_parts_match(path_parts, pattern_parts[1:]) or (
            bool(path_parts) and _glob_parts_match(path_parts[1:], pattern_parts)
        )
    if not path_parts:
        return False
    # fnmatch is safe at component granularity: '*' and '?' cannot consume '/'.
    import fnmatch

    return fnmatch.fnmatchcase(path_parts[0], pattern) and _glob_parts_match(
        path_parts[1:], pattern_parts[1:]
    )


def _matches(path: str, pattern: str) -> bool:
    if not any(token in pattern for token in ("*", "?", "[")):
        return path == pattern or path.startswith(pattern.rstrip("/") + "/")
    return _glob_parts_match(
        PurePosixPath(path).parts,
        PurePosixPath(pattern).parts,
    )


def _under_root(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root.rstrip("/") + "/")


def _symlink_component(root: Path, relative: str) -> Path | None:
    current = root.resolve()
    for part in PurePosixPath(relative).parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            return current
    return None


def repository_profile_path(root: Path) -> Path:
    return root.resolve() / PROFILE_RELATIVE_PATH


def _command_records(profile: dict[str, Any]) -> list[dict[str, Any]]:
    commands = profile.get("commands", {})
    if not isinstance(commands, dict):
        raise ValidationError("repository profile commands must be an object")
    records: list[dict[str, Any]] = []
    for name in sorted(commands):
        argv = commands[name]
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("repository profile command names must be non-empty strings")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise ValidationError(
                f"repository profile command {name!r} must be a non-empty argv array"
            )
        records.append({"name": name, "argv": list(argv)})
    return records


def _normalized_profile(value: dict[str, Any]) -> dict[str, Any]:
    profile = copy.deepcopy(value)
    for key in ("study_root", "object_root", "run_cwd"):
        profile[key] = _normalize_relative(
            str(profile[key]), label=f"repository profile {key}"
        )
    for key in ("source_roots", "test_roots", "experiment_roots", "workflow_roots"):
        profile[key] = [
            _normalize_relative(item, label=f"repository profile {key} item")
            for item in profile[key]
        ]
    for key in (
        "scientific_critical_patterns",
        "protected_patterns",
        "generated_patterns",
        "vendor_patterns",
    ):
        profile[key] = [
            _validate_pattern(item, label=f"repository profile {key} item")
            for item in profile[key]
        ]
    execution = profile.get("execution")
    if isinstance(execution, dict):
        execution["trusted_read_only_paths"] = [
            _normalize_trusted_runtime_path(
                item,
                label="repository profile execution trusted_read_only_paths item",
            )
            for item in execution.get("trusted_read_only_paths", [])
        ]
    return profile


def repository_profile_issues(root: Path) -> list[ValidationIssue]:
    path = repository_profile_path(root)
    if path.is_symlink():
        return [
            ValidationIssue(
                "ERROR", str(path), "repository profile must not be a symbolic link"
            )
        ]
    try:
        value = load_json(path)
    except ValidationError as exc:
        return [ValidationIssue("ERROR", str(path), str(exc))]
    if not isinstance(value, dict):
        return [ValidationIssue("ERROR", str(path), "repository profile must be an object")]
    issues = object_schema_issues(root, "repository_profile", path, value)
    if issues:
        return issues
    try:
        git_top = _git(root, ["rev-parse", "--show-toplevel"])
        git_available = git_top.returncode == 0
        if git_available:
            actual_top = Path(git_top.stdout.strip()).resolve()
            if actual_top != root.resolve():
                raise ValidationError(
                    "workflow root must equal the Git worktree root; nested installations "
                    "must either move the workflow to the worktree root or use a separate worktree"
                )
        normalized_roots: dict[str, str] = {}
        for key in ("study_root", "object_root", "run_cwd"):
            normalized = _normalize_relative(
                str(value[key]), label=f"repository profile {key}"
            )
            normalized_roots[key] = normalized
            if key in {"study_root", "object_root"} and normalized == ".":
                raise ValidationError(f"repository profile {key} must not be the repository root")
            link = _symlink_component(root, normalized)
            if link is not None:
                raise ValidationError(
                    f"repository profile {key} contains a symbolic-link component: {link}"
                )
            try:
                (root / normalized).resolve(strict=False).relative_to(root.resolve())
            except ValueError as exc:
                raise ValidationError(
                    f"repository profile {key} resolves outside the repository"
                ) from exc
        study_root = normalized_roots["study_root"]
        object_root = normalized_roots["object_root"]
        run_cwd = root / normalized_roots["run_cwd"]
        if not run_cwd.is_dir():
            raise ValidationError(
                f"repository profile run_cwd must exist and be a directory: {normalized_roots['run_cwd']}"
            )
        if _under_root(study_root, object_root) or _under_root(object_root, study_root):
            raise ValidationError(
                "repository profile study_root and object_root must not overlap"
            )
        normalized_lists: dict[str, list[str]] = {}
        for key in ("source_roots", "test_roots", "experiment_roots", "workflow_roots"):
            values = value[key]
            normalized_values: list[str] = []
            for item in values:
                normalized = _normalize_relative(
                    item, label=f"repository profile {key} item"
                )
                normalized_values.append(normalized)
                link = _symlink_component(root, normalized)
                if link is not None:
                    raise ValidationError(
                        f"repository profile {key} item contains a symbolic-link component: {link}"
                    )
                try:
                    (root / normalized).resolve(strict=False).relative_to(root.resolve())
                except ValueError as exc:
                    raise ValidationError(
                        f"repository profile {key} item resolves outside the repository: {item!r}"
                    ) from exc
            if len(normalized_values) != len(set(normalized_values)):
                raise ValidationError(f"repository profile {key} must not contain duplicates")
            normalized_lists[key] = normalized_values
        for source_root in normalized_lists["source_roots"]:
            if _under_root(source_root, object_root) or _under_root(
                object_root, source_root
            ):
                raise ValidationError(
                    "repository profile source_roots and object_root must not overlap"
                )
        execution = value.get("execution")
        if isinstance(execution, dict):
            trusted_paths = [
                _normalize_trusted_runtime_path(
                    item,
                    label=(
                        "repository profile execution "
                        "trusted_read_only_paths item"
                    ),
                )
                for item in execution.get("trusted_read_only_paths", [])
            ]
            if len(trusted_paths) != len(set(trusted_paths)):
                raise ValidationError(
                    "repository profile execution trusted_read_only_paths "
                    "must not contain duplicates"
                )
            for trusted in trusted_paths:
                trusted_path = Path(trusted)
                if trusted_path.is_absolute():
                    resolved_trusted = trusted_path.resolve(strict=False)
                    resolved_object = (root / object_root).resolve(strict=False)
                    if resolved_object == resolved_trusted or resolved_object.is_relative_to(
                        resolved_trusted
                    ):
                        raise ValidationError(
                            "trusted runtime paths must not expose object_root"
                        )
                elif (
                    _under_root(trusted, object_root)
                    or _under_root(object_root, trusted)
                ):
                    raise ValidationError(
                        "trusted runtime paths must not overlap object_root"
                    )
        for key in (
            "scientific_critical_patterns",
            "protected_patterns",
            "generated_patterns",
            "vendor_patterns",
        ):
            values = value[key]
            normalized_values = []
            for item in values:
                normalized_values.append(
                    _validate_pattern(item, label=f"repository profile {key} item")
                )
            if len(normalized_values) != len(set(normalized_values)):
                raise ValidationError(f"repository profile {key} must not contain duplicates")
        protected_patterns = [
            *_BUILT_IN_PROTECTED_PATTERNS,
            *[
                _validate_pattern(item, label="repository profile protected_patterns item")
                for item in value["protected_patterns"]
            ],
        ]
        for workflow_root in normalized_lists["workflow_roots"]:
            if not any(_matches(workflow_root, pattern) for pattern in protected_patterns):
                raise ValidationError(
                    "every workflow_root must be covered by a protected pattern; "
                    f"unprotected root: {workflow_root!r}"
                )
        for key in ("source_roots", "test_roots", "experiment_roots"):
            for configured_root in normalized_lists[key]:
                candidate = root / configured_root
                if not candidate.exists():
                    issues.append(
                        ValidationIssue(
                            "WARNING",
                            str(path),
                            f"configured {key} item does not yet exist: {configured_root}",
                        )
                    )
        if git_available:
            probe_path = f"{object_root}/.studyctl-ignore-probe"
            ignored = _git(
                root,
                ["check-ignore", "-q", "--no-index", "--", probe_path],
            )
            if ignored.returncode == 1:
                raise ValidationError(
                    "repository profile object_root contents must be ignored by Git; "
                    f"add an ignore rule covering {probe_path!r}"
                )
            if ignored.returncode not in {0, 1}:
                detail = ignored.stderr.strip() or "unknown Git error"
                raise ValidationError(f"cannot verify object_root Git ignore policy: {detail}")
        _command_records(value)
        template = str(value["git"]["branch_template"])
        if "{study_id}" not in template:
            raise ValidationError("repository profile branch_template must contain {study_id}")
        if "{slug}" not in template:
            raise ValidationError("repository profile branch_template must contain {slug}")
        if not value["commands"]:
            raise ValidationError(
                "repository profile commands must contain at least one validation command"
            )
    except (KeyError, TypeError, ValidationError) as exc:
        issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues


def load_repository_profile(root: Path) -> dict[str, Any]:
    issues = [issue for issue in repository_profile_issues(root) if issue.level == "ERROR"]
    if issues:
        raise ValidationError(
            "repository profile is invalid:\n" + "\n".join(issue.render() for issue in issues)
        )
    value = load_json(repository_profile_path(root))
    if not isinstance(value, dict):
        raise ValidationError("repository profile must be an object")
    return _normalized_profile(value)


def changeset_path(paths: StudyPaths) -> Path:
    return paths.formal / CHANGESET_FILENAME


def change_validation_path(paths: StudyPaths) -> Path:
    return paths.formal / VALIDATION_FILENAME


def changeset_issues(paths: StudyPaths) -> list[ValidationIssue]:
    path = changeset_path(paths)
    if not path.exists():
        return []
    if path.is_symlink():
        return [
            ValidationIssue("ERROR", str(path), "CHANGESET must not be a symbolic link")
        ]
    try:
        value = load_json(path)
    except ValidationError as exc:
        return [ValidationIssue("ERROR", str(path), str(exc))]
    if not isinstance(value, dict):
        return [ValidationIssue("ERROR", str(path), "CHANGESET must be an object")]
    issues = object_schema_issues(paths.root, "changeset", path, value)
    if issues:
        return issues
    try:
        if value.get("study_id") != paths.study_id:
            raise ValidationError("CHANGESET study_id does not match Study")
        patterns = value.get("allowed_write_patterns", [])
        normalized_patterns = [
            _validate_pattern(item, label="CHANGESET allowed_write_patterns item")
            for item in patterns
        ]
        if len(normalized_patterns) != len(set(normalized_patterns)):
            raise ValidationError("CHANGESET allowed_write_patterns must not contain duplicates")
        for record in value.get("required_validation", []):
            argv = record.get("argv")
            if not isinstance(argv, list) or any(not isinstance(item, str) or not item for item in argv):
                raise ValidationError("CHANGESET validation commands must be argv arrays")
        if value.get("record_sha256") != record_digest(value, "record_sha256"):
            raise ValidationError("CHANGESET record_sha256 does not match record")
    except ValidationError as exc:
        issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues


def load_changeset(paths: StudyPaths, *, required: bool = False) -> dict[str, Any] | None:
    path = changeset_path(paths)
    if not path.is_file():
        if required:
            raise ValidationError(f"source or test changes require {path.relative_to(paths.root)}")
        return None
    issues = [issue for issue in changeset_issues(paths) if issue.level == "ERROR"]
    if issues:
        raise ValidationError("CHANGESET is invalid:\n" + "\n".join(issue.render() for issue in issues))
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValidationError("CHANGESET must be an object")
    normalized = copy.deepcopy(value)
    normalized["allowed_write_patterns"] = [
        _validate_pattern(item, label="CHANGESET allowed_write_patterns item")
        for item in value.get("allowed_write_patterns", [])
    ]
    return normalized


def _worktree_policy_issue(root: Path, profile: dict[str, Any]) -> str | None:
    if not profile["git"].get("require_linked_worktree", False):
        return None
    listing = _git(root, ["worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        return "cannot verify linked-worktree policy"
    worktrees = [
        Path(line.removeprefix("worktree ")).resolve()
        for line in listing.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    if not worktrees:
        return "Git did not report any worktrees"
    if root.resolve() == worktrees[0]:
        return "Study changes require a linked Git worktree, not the primary worktree"
    return None


def _expected_branch_pattern(profile: dict[str, Any], study_id: str) -> str:
    return str(profile["git"]["branch_template"]).replace(
        "{study_id}", study_id
    ).replace("{slug}", "*")


def _git_value(root: Path, args: Sequence[str], *, label: str) -> str:
    result = _git(root, args)
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        detail = result.stderr.strip() or "unknown Git error"
        raise WorkflowError(f"cannot determine {label}: {detail}")
    return value


def create_changeset(
    paths: StudyPaths,
    allowed_patterns: Sequence[str],
    *,
    base_ref: str | None = None,
) -> Path:
    destination = changeset_path(paths)
    if destination.exists():
        raise WorkflowError(f"refusing to overwrite existing CHANGESET: {destination}")
    record = _build_changeset_record(
        paths,
        allowed_patterns,
        base_ref=base_ref,
        supersedes_sha256=None,
    )
    atomic_write_json(destination, record, overwrite=False)
    return destination


def _build_changeset_record(
    paths: StudyPaths,
    allowed_patterns: Sequence[str],
    *,
    base_ref: str | None,
    supersedes_sha256: str | None,
) -> dict[str, Any]:
    destination = changeset_path(paths)
    if not allowed_patterns:
        raise ValidationError("changeset-new requires at least one --allow pattern")
    profile = load_repository_profile(paths.root)
    normalized = [
        _validate_pattern(item, label="--allow pattern") for item in allowed_patterns
    ]
    study_pattern = f"{profile['study_root']}/{paths.study_id}/**"
    if study_pattern not in normalized:
        normalized.append(study_pattern)
    if len(normalized) != len(set(normalized)):
        raise ValidationError("--allow patterns must not be repeated")

    probe = _git(paths.root, ["rev-parse", "--show-toplevel"])
    if probe.returncode != 0:
        raise WorkflowError("changeset-new requires a Git worktree")
    effective_base = base_ref or str(profile["git"]["base_ref"])
    base_commit = _git_value(
        paths.root, ["rev-parse", "--verify", effective_base], label=f"base ref {effective_base!r}"
    )
    branch = _git_value(paths.root, ["branch", "--show-current"], label="current branch")
    if profile["git"].get("require_study_branch", True):
        branch_pattern = _expected_branch_pattern(profile, paths.study_id)
        if not _matches(branch, branch_pattern):
            raise ValidationError(
                f"Study changes require a branch matching {branch_pattern!r}; current branch is {branch!r}"
            )
    worktree_issue = _worktree_policy_issue(paths.root, profile)
    if worktree_issue is not None:
        raise ValidationError(worktree_issue)

    record = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "status": "active",
        "created_at": utc_now(),
        "base_ref": effective_base,
        "base_commit": base_commit,
        "branch": branch,
        "allowed_write_patterns": normalized,
        "required_validation": _command_records(profile),
        "supersedes_sha256": supersedes_sha256,
    }
    record["record_sha256"] = record_digest(record, "record_sha256")
    schema_issues = object_schema_issues(paths.root, "changeset", destination, record)
    if schema_issues:
        raise ValidationError(
            "generated CHANGESET is invalid:\n" + "\n".join(issue.render() for issue in schema_issues)
        )
    return record


def renew_changeset(
    paths: StudyPaths,
    allowed_patterns: Sequence[str] | None = None,
    *,
    base_ref: str | None = None,
) -> Path:
    destination = changeset_path(paths)
    current = load_changeset(paths, required=True)
    assert current is not None
    current_hash = sha256_file(destination)
    selected_patterns = list(allowed_patterns or current["allowed_write_patterns"])
    record = _build_changeset_record(
        paths,
        selected_patterns,
        base_ref=base_ref or str(current["base_ref"]),
        supersedes_sha256=current_hash,
    )
    history = paths.formal / "changeset-history"
    history.mkdir(parents=True, exist_ok=True)
    archived = history / f"CHANGESET.{current_hash}.json"
    atomic_write_json(archived, current, overwrite=False)
    atomic_write_json(destination, record)
    validation = change_validation_path(paths)
    if validation.exists():
        stale_history = history / f"VALIDATION.{current_hash}.json"
        atomic_write_json(stale_history, load_json(validation), overwrite=False)
        validation.unlink()
    return destination


def _nul_paths(result: subprocess.CompletedProcess[str], *, label: str) -> list[str]:
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise WorkflowError(f"cannot collect {label}: {detail}")
    return [item for item in result.stdout.split("\0") if item]


def _actual_git_paths(
    root: Path,
    base_commit: str,
    *,
    base_ref: str,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    probe = _git(root, ["rev-parse", "--show-toplevel"])
    if probe.returncode != 0:
        return {}, {
            "available": False,
            "base_ref": base_ref,
            "base_commit": None,
            "head": None,
            "branch": None,
            "deviation": "repository is not a Git worktree",
        }
    verified_base_commit = _git_value(
        root,
        ["rev-parse", "--verify", f"{base_commit}^{{commit}}"],
        label=f"base commit {base_commit!r}",
    )
    head = _git_value(root, ["rev-parse", "HEAD"], label="HEAD")
    branch_result = _git(root, ["branch", "--show-current"])
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    merge_base = _git_value(root, ["merge-base", verified_base_commit, head], label="merge base")
    committed = _nul_paths(
        _git(root, ["diff", "--no-renames", "--name-only", "-z", merge_base, head, "--"]),
        label="committed changed paths",
    )
    staged = _nul_paths(
        _git(root, ["diff", "--cached", "--no-renames", "--name-only", "-z", "--"]),
        label="staged changed paths",
    )
    unstaged = _nul_paths(
        _git(root, ["diff", "--no-renames", "--name-only", "-z", "--"]),
        label="unstaged changed paths",
    )
    untracked = _nul_paths(
        _git(root, ["ls-files", "--others", "--exclude-standard", "-z"]),
        label="untracked paths",
    )
    states: dict[str, set[str]] = {}
    for path in committed:
        states.setdefault(path, set()).add("committed")
    for path in staged:
        states.setdefault(path, set()).add("staged")
    for path in unstaged:
        states.setdefault(path, set()).add("unstaged")
    for path in untracked:
        states.setdefault(path, set()).add("untracked")
    return states, {
        "available": True,
        "base_ref": base_ref,
        "base_commit": verified_base_commit,
        "merge_base": merge_base,
        "head": head,
        "branch": branch or None,
        "deviation": None,
    }


def _validation_required(records: Sequence[dict[str, Any]]) -> bool:
    return any(
        record.get("classification") in {"source", "test", "experiment"}
        for record in records
    )


def _path_content_snapshot(root: Path, raw_path: str) -> dict[str, Any]:
    path = root / raw_path
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": raw_path, "kind": "missing"}
    if path.is_symlink():
        return {"path": raw_path, "kind": "symlink", "target": str(path.readlink())}
    if path.is_file():
        return {
            "path": raw_path,
            "kind": "file",
            "size": metadata.st_size,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        return {"path": raw_path, "kind": "directory"}
    return {"path": raw_path, "kind": "special", "mode": metadata.st_mode}


def _changed_content_snapshot(
    root: Path,
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _path_content_snapshot(root, str(record["path"]))
        for record in sorted(records, key=lambda item: str(item["path"]))
    ]


def _validated_tree_sha256(root: Path, records: Sequence[dict[str, Any]]) -> str:
    consequential = [
        record
        for record in records
        if record.get("classification") in {"source", "test", "experiment"}
    ]
    return sha256_json(_changed_content_snapshot(root, consequential))


def _validation_summary(paths: StudyPaths) -> dict[str, Any] | None:
    path = change_validation_path(paths)
    if not path.is_file():
        return None
    try:
        value = load_json(path)
    except ValidationError:
        value = None
    return {
        "path": path.relative_to(paths.root).as_posix(),
        "sha256": sha256_file(path),
        "passed": value.get("passed") if isinstance(value, dict) else None,
    }


def _change_validation_violations(
    paths: StudyPaths,
    profile: dict[str, Any],
    changeset: dict[str, Any] | None,
    git: dict[str, Any],
    records: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    if not _validation_required(records):
        return []
    path = change_validation_path(paths)
    if changeset is None:
        return []  # the missing CHANGESET violation is more fundamental
    if not path.is_file():
        return [{
            "path": path.relative_to(paths.root).as_posix(),
            "rule": "missing_validation_proof",
            "reason": "source, test, or experiment changes require studyctl validate-changes",
        }]
    try:
        proof = load_json(path)
        if not isinstance(proof, dict):
            raise ValidationError("validation proof must be an object")
        schema_issues = object_schema_issues(
            paths.root, "change_validation", path, proof
        )
        if schema_issues:
            raise ValidationError("; ".join(item.message for item in schema_issues))
        expected_commands = changeset.get("required_validation", [])
        actual_commands = [
            {"name": item.get("name"), "argv": item.get("argv")}
            for item in proof.get("commands", [])
        ]
        expected_paths = sorted(
            record["path"]
            for record in records
            if record.get("classification") in {"source", "test", "experiment"}
        )
        checks = (
            (proof.get("study_id") == paths.study_id, "study ID differs"),
            (proof.get("passed") is True, "one or more validation commands failed"),
            (
                proof.get("record_sha256") == record_digest(proof, "record_sha256"),
                "record digest differs",
            ),
            (
                proof.get("repository_profile", {}).get("sha256")
                == sha256_file(repository_profile_path(paths.root)),
                "repository profile changed",
            ),
            (
                proof.get("changeset", {}).get("sha256")
                == sha256_file(changeset_path(paths)),
                "CHANGESET changed",
            ),
            (proof.get("git", {}).get("branch") == git.get("branch"), "branch changed"),
            (actual_commands == expected_commands, "validation command set changed"),
            (proof.get("changed_paths") == expected_paths, "validated path set changed"),
            (
                proof.get("validated_tree_sha256")
                == _validated_tree_sha256(paths.root, records),
                "validated source/test/experiment content changed",
            ),
        )
        failures = [message for passed, message in checks if not passed]
        proof_commit = proof.get("git", {}).get("commit")
        if isinstance(proof_commit, str) and git.get("head"):
            ancestry = _git(
                paths.root,
                ["merge-base", "--is-ancestor", proof_commit, str(git["head"])],
            )
            if ancestry.returncode != 0:
                failures.append("validation commit is no longer an ancestor of HEAD")
        if failures:
            raise ValidationError(", ".join(failures))
    except (ValidationError, WorkflowError) as exc:
        return [{
            "path": path.relative_to(paths.root).as_posix(),
            "rule": "stale_validation_proof",
            "reason": str(exc),
        }]
    return []


def run_change_validation(paths: StudyPaths) -> dict[str, Any]:
    profile = load_repository_profile(paths.root)
    changeset = load_changeset(paths, required=True)
    assert changeset is not None
    profile_sha256 = sha256_file(repository_profile_path(paths.root))
    changeset_sha256 = sha256_file(changeset_path(paths))
    state = evaluate_changes(paths, require_validation=False)
    if state["outcome"] != "PASS":
        details = "; ".join(
            f"{item['rule']}: {item['reason']}" for item in state["violations"]
        )
        raise ValidationError(f"cannot validate an invalid change scope: {details}")
    consequential = [
        record
        for record in state["changed_paths"]
        if record["classification"] in {"source", "test", "experiment"}
    ]
    for record in consequential:
        dirty = set(record["states"]).intersection({"staged", "unstaged", "untracked"})
        if dirty:
            raise ValidationError(
                f"validation requires committed source/test/experiment state: {record['path']}"
            )
    commands = changeset.get("required_validation", [])
    if consequential and not commands:
        raise ValidationError("CHANGESET has no required validation commands")
    run_cwd = (paths.root / str(profile["run_cwd"])).resolve(strict=True)
    try:
        run_cwd.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise ValidationError("configured run_cwd resolves outside the repository") from exc
    pre_content_snapshot = _changed_content_snapshot(
        paths.root,
        state["changed_paths"],
    )
    validated_tree_sha256 = _validated_tree_sha256(
        paths.root,
        state["changed_paths"],
    )
    results: list[dict[str, Any]] = []
    for command in commands:
        started_at = utc_now()
        completed = subprocess.run(
            list(command["argv"]),
            cwd=run_cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        results.append({
            "name": command["name"],
            "argv": list(command["argv"]),
            "started_at": started_at,
            "ended_at": utc_now(),
            "exit_code": completed.returncode,
            "stdout_sha256": sha256_bytes(completed.stdout.encode("utf-8")),
            "stderr_sha256": sha256_bytes(completed.stderr.encode("utf-8")),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        })
    post_state_error: str | None = None
    try:
        post_state = evaluate_changes(paths, require_validation=False)
    except (ValidationError, WorkflowError) as exc:
        # A validator may corrupt or remove a governance file.  That is a
        # failed validation result which must itself be sealed, not an excuse
        # to exit before recording the failure.
        post_state_error = str(exc)
        post_state = {
            "outcome": "BLOCKED",
            "git": state.get("git", {}),
            "changed_paths": state.get("changed_paths", []),
        }
    post_content_snapshot = _changed_content_snapshot(
        paths.root,
        post_state.get("changed_paths", []),
    )
    repository_state_unchanged = (
        post_state.get("outcome") == "PASS"
        and post_state.get("git", {}).get("head") == state.get("git", {}).get("head")
        and post_state.get("changed_paths") == state.get("changed_paths")
        and post_content_snapshot == pre_content_snapshot
    )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "created_at": utc_now(),
        "repository_profile": {
            "path": repository_profile_path(paths.root).relative_to(paths.root).as_posix(),
            "sha256": profile_sha256,
        },
        "changeset": {
            "path": changeset_path(paths).relative_to(paths.root).as_posix(),
            "sha256": changeset_sha256,
        },
        "git": {
            "commit": state["git"]["head"],
            "branch": state["git"]["branch"],
        },
        "changed_paths": sorted(record["path"] for record in consequential),
        "validated_tree_sha256": validated_tree_sha256,
        "commands": results,
        "repository_state_unchanged": repository_state_unchanged,
        "passed": (
            bool(results)
            and all(item["exit_code"] == 0 for item in results)
            and repository_state_unchanged
        ),
    }
    if post_state_error is not None:
        record["commands"][-1]["stderr_tail"] = (
            str(record["commands"][-1].get("stderr_tail") or "")
            + "\nstudyctl post-validation inspection failed: "
            + post_state_error
        )[-4000:]
    record["record_sha256"] = record_digest(record, "record_sha256")
    issues = object_schema_issues(
        paths.root, "change_validation", change_validation_path(paths), record
    )
    if issues:
        raise ValidationError(
            "generated validation proof is invalid:\n"
            + "\n".join(item.render() for item in issues)
        )
    atomic_write_json(change_validation_path(paths), record)
    return record


def _classify(path: str, profile: dict[str, Any], study_id: str) -> str:
    study_root = str(profile["study_root"])
    if _under_root(path, f"{study_root}/{study_id}"):
        return "study_state"
    if _under_root(path, study_root):
        return "other_study"
    if _under_root(path, str(profile["object_root"])):
        return "output_object"
    if any(_matches(path, pattern) for pattern in profile["generated_patterns"]):
        return "generated"
    if any(_matches(path, pattern) for pattern in profile["vendor_patterns"]):
        return "vendor"
    if any(_under_root(path, root) for root in profile["test_roots"]):
        return "test"
    if any(_under_root(path, root) for root in profile["experiment_roots"]):
        return "experiment"
    if any(_under_root(path, root) for root in profile["workflow_roots"]):
        return "workflow"
    if any(_under_root(path, root) for root in profile["source_roots"]):
        return "source"
    return "other"


def _deduplicate_violations(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        unique[(item.get("path", ""), item["rule"])] = item
    return [unique[key] for key in sorted(unique)]


def evaluate_changes(
    paths: StudyPaths,
    *,
    write_projection: bool = False,
    require_validation: bool = True,
) -> dict[str, Any]:
    profile = load_repository_profile(paths.root)
    changeset = load_changeset(paths)
    base_ref = str(changeset["base_ref"] if changeset else profile["git"]["base_ref"])
    if changeset is not None:
        base_commit = str(changeset["base_commit"])
    else:
        resolved = _git(paths.root, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
        base_commit = resolved.stdout.strip() if resolved.returncode == 0 else base_ref
    actual, git = _actual_git_paths(
        paths.root,
        base_commit,
        base_ref=base_ref,
    )
    violations: list[dict[str, str]] = []
    advisories: list[str] = []
    records: list[dict[str, Any]] = []

    if not git["available"]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "study_id": paths.study_id,
            "outcome": "ADVISORY",
            "git": git,
            "changeset": None,
            "validation": None,
            "changed_paths": [],
            "violations": [],
            "advisories": ["Git is unavailable; source and test write scope cannot be verified"],
        }
        if write_projection:
            atomic_write_json(paths.generated / "CHANGES.json", result)
        return result

    for raw_path in sorted(actual):
        normalized = _normalize_git_path(raw_path)
        classification = _classify(normalized, profile, paths.study_id)
        records.append(
            {
                "path": normalized,
                "classification": classification,
                "tracked": "untracked" not in actual[raw_path],
                "states": sorted(actual[raw_path]),
            }
        )
        protected_patterns = [
            *_BUILT_IN_PROTECTED_PATTERNS,
            *profile["protected_patterns"],
        ]
        if any(_matches(normalized, pattern) for pattern in protected_patterns):
            violations.append(
                {
                    "path": normalized,
                    "rule": "protected_path",
                    "reason": "path matches a repository-level protected pattern",
                }
            )
        if classification == "other_study":
            advisories.append(
                f"unrelated Study state is present and excluded from {paths.study_id} write scope: {normalized}"
            )
        if classification in {"generated", "vendor", "output_object"}:
            violations.append(
                {
                    "path": normalized,
                    "rule": f"{classification}_write",
                    "reason": f"{classification.replace('_', ' ')} paths are not Study source outputs",
                }
            )
        if classification == "other":
            violations.append(
                {
                    "path": normalized,
                    "rule": "unclassified_path",
                    "reason": "path is not mapped to a source, test, experiment, Study, or protected workflow root in the repository profile",
                }
            )

    consequential = [
        record
        for record in records
        if record["classification"] not in {"study_state", "other_study"}
    ]
    if consequential and changeset is None:
        for record in consequential:
            violations.append(
                {
                    "path": record["path"],
                    "rule": "missing_changeset",
                    "reason": "source, test, experiment, workflow, or other repository changes require formal/CHANGESET.json",
                }
            )
    if changeset is not None:
        allowed = changeset["allowed_write_patterns"]
        for record in records:
            if record["classification"] == "other_study":
                continue
            if not any(_matches(record["path"], pattern) for pattern in allowed):
                violations.append(
                    {
                        "path": record["path"],
                        "rule": "outside_allowlist",
                        "reason": "path is outside CHANGESET allowed_write_patterns",
                    }
                )
        if changeset.get("status") != "active":
            violations.append(
                {
                    "path": changeset_path(paths).relative_to(paths.root).as_posix(),
                    "rule": "inactive_changeset",
                    "reason": "only an active CHANGESET may authorize new Study work",
                }
            )
        branch_pattern = _expected_branch_pattern(profile, paths.study_id)
        if profile["git"].get("require_study_branch", True) and not _matches(
            str(git.get("branch") or ""), branch_pattern
        ):
            violations.append(
                {
                    "path": "",
                    "rule": "branch_template_mismatch",
                    "reason": f"current branch must match {branch_pattern!r}",
                }
            )
        if git.get("branch") != changeset.get("branch"):
            violations.append(
                {
                    "path": "",
                    "rule": "branch_mismatch",
                    "reason": f"current branch {git.get('branch')!r} differs from CHANGESET branch {changeset.get('branch')!r}",
                }
            )
        worktree_issue = _worktree_policy_issue(paths.root, profile)
        if worktree_issue is not None:
            violations.append(
                {
                    "path": "",
                    "rule": "worktree_policy",
                    "reason": worktree_issue,
                }
            )
        expected_commands = _command_records(profile)
        if changeset.get("required_validation") != expected_commands:
            violations.append(
                {
                    "path": changeset_path(paths).relative_to(paths.root).as_posix(),
                    "rule": "validation_contract_mismatch",
                    "reason": "repository validation commands changed after CHANGESET creation",
                }
            )
        ancestry = _git(
            paths.root,
            ["merge-base", "--is-ancestor", str(changeset["base_commit"]), str(git["head"])],
        )
        if ancestry.returncode != 0:
            violations.append(
                {
                    "path": "",
                    "rule": "base_commit_not_ancestor",
                    "reason": "CHANGESET base_commit is not an ancestor of HEAD",
                }
            )
        base_ref_result = _git(
            paths.root,
            ["rev-parse", "--verify", f"{changeset['base_ref']}^{{commit}}"],
        )
        if base_ref_result.returncode != 0:
            violations.append(
                {
                    "path": "",
                    "rule": "base_ref_unavailable",
                    "reason": f"CHANGESET base_ref {changeset['base_ref']!r} is unavailable",
                }
            )
        else:
            expected_merge_base = _git(
                paths.root,
                ["merge-base", base_ref_result.stdout.strip(), str(git["head"])],
            )
            if (
                expected_merge_base.returncode != 0
                or expected_merge_base.stdout.strip() != changeset.get("base_commit")
            ):
                violations.append(
                    {
                        "path": "",
                        "rule": "base_anchor_mismatch",
                        "reason": "CHANGESET base_commit is not the current merge base of base_ref and HEAD",
                    }
                )

    if require_validation:
        violations.extend(
            _change_validation_violations(paths, profile, changeset, git, records)
        )

    violations = _deduplicate_violations(violations)
    result = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "outcome": "BLOCKED" if violations else "PASS",
        "git": git,
        "changeset": (
            {
                "path": changeset_path(paths).relative_to(paths.root).as_posix(),
                "sha256": sha256_file(changeset_path(paths)),
                "status": changeset["status"],
            }
            if changeset is not None
            else None
        ),
        "validation": _validation_summary(paths),
        "changed_paths": records,
        "violations": violations,
        "advisories": sorted(set(advisories)),
    }
    if write_projection:
        atomic_write_json(paths.generated / "CHANGES.json", result)
    return result


def critical_actual_paths(change_state: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    return [
        record["path"]
        for record in change_state.get("changed_paths", [])
        if any(
            _matches(str(record["path"]), pattern)
            for pattern in profile.get("scientific_critical_patterns", [])
        )
    ]


def change_state_evidence_eligible(change_state: dict[str, Any]) -> bool:
    if change_state.get("outcome") != "PASS" or not change_state.get("git", {}).get("available"):
        return False
    for record in change_state.get("changed_paths", []):
        if record.get("classification") == "study_state":
            continue
        if record.get("classification") == "other_study":
            return False
        states = set(record.get("states", []))
        if states.intersection({"staged", "unstaged", "untracked"}):
            return False
    return True


def profile_summary(root: Path) -> dict[str, Any]:
    profile = load_repository_profile(root)
    return {
        "path": repository_profile_path(root).relative_to(root).as_posix(),
        "sha256": sha256_file(repository_profile_path(root)),
        "profile": profile,
    }
