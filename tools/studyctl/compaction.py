from __future__ import annotations

from collections import Counter, defaultdict
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .budget import (
    budget_projection,
    budget_totals_from_manifests,
    parse_brief_hard_budget,
)
from .formalization import collect_formalization_debt
from .hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from .locking import study_authority_lock
from .models import SCHEMA_VERSION, StudyPaths, ValidationError, WorkflowError, utc_now
from .rendering import active_formal_artifacts, render_status
from .run_ledger import (
    bootstrap_or_reconcile_ledger,
    ledger_commitment_totals,
    ledger_path,
    load_ledger,
)
from .validation import (
    assert_valid_study,
    authoritative_string_references,
    checkpoint_paths,
    evidence_index,
    evidence_paths,
    object_schema_issues,
    run_file_references,
    run_index,
)
from .workspace import evaluate_changes, repository_profile_path


_STUDY_CHANGE_CLASSIFICATIONS = {"study_state", "other_study"}


def _claim_evidence_keys(claims: dict[str, Any]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for claim in claims.get("claims", []):
        for field in ("supporting_evidence", "contradictory_evidence", "other_evidence"):
            for ref in claim.get(field, []):
                keys.add((str(ref.get("evidence_id")), int(ref.get("version", 0))))
    return keys


def _evidence_run_ids(evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]]) -> set[str]:
    return {
        str(ref.get("run_id"))
        for _, item in evidence.values()
        for ref in item.get("runs", [])
    }


def _file_inventory(base: Path, root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not base.is_dir():
        return records
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def _tree_inventory(base: Path, root: Path) -> list[dict[str, Any]]:
    """Inventory files and directories without following symbolic links."""

    records: list[dict[str, Any]] = []
    if not base.is_dir():
        return records
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": os.readlink(path),
                }
            )
        elif path.is_file():
            metadata = path.stat()
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": metadata.st_mode & 0o7777,
                    "size": metadata.st_size,
                    "sha256": sha256_file(path),
                }
            )
        elif path.is_dir():
            records.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "mode": path.stat().st_mode & 0o7777,
                }
            )
        else:
            records.append({"path": relative, "kind": "other"})
    return records


def _repository_path_identity(root: Path, relative: str) -> dict[str, Any]:
    """Return a deterministic identity for one Git-reported repository path."""

    path = root / relative
    if path.is_symlink():
        return {
            "kind": "symlink",
            "target": os.readlink(path),
        }
    if not path.exists():
        return {"kind": "missing"}
    if path.is_file():
        metadata = path.stat()
        return {
            "kind": "file",
            "mode": metadata.st_mode & 0o7777,
            "size": metadata.st_size,
            "sha256": sha256_file(path),
        }
    if path.is_dir():
        return {
            "kind": "directory",
            "mode": path.stat().st_mode & 0o7777,
        }
    return {
        "kind": "other",
        "mode": path.stat().st_mode & 0o7777,
    }


def _host_change_scope(paths: StudyPaths) -> dict[str, Any]:
    """Seal only host-repository changes, excluding every Study-state path."""

    state = evaluate_changes(paths, require_validation=True)
    if state.get("outcome") == "BLOCKED":
        details = "; ".join(
            f"{item.get('rule')}: {item.get('reason')}"
            for item in state.get("violations", [])
        )
        suffix = f": {details}" if details else ""
        raise ValidationError(
            "cannot compact while the current host repository change scope is BLOCKED"
            + suffix
        )
    records: list[dict[str, Any]] = []
    for item in state.get("changed_paths", []):
        if item.get("classification") in _STUDY_CHANGE_CLASSIFICATIONS:
            continue
        relative = str(item.get("path"))
        records.append(
            {
                "path": relative,
                "classification": str(item.get("classification")),
                "tracked": bool(item.get("tracked")),
                "states": sorted(str(value) for value in item.get("states", [])),
                "identity": _repository_path_identity(paths.root, relative),
            }
        )
    records.sort(key=lambda item: item["path"])
    return {
        "outcome": state.get("outcome"),
        "git_available": bool(state.get("git", {}).get("available")),
        "consequential_paths": records,
        "fingerprint_sha256": sha256_json(records),
    }


def _validate_prepared_bindings(
    paths: StudyPaths,
    compaction_state: dict[str, Any],
) -> None:
    source_hashes = compaction_state.get("source_hashes")
    if not isinstance(source_hashes, dict):
        raise ValidationError("COMPACTION_INPUT.json lacks source bindings")
    if source_hashes.get("brief") != sha256_file(paths.brief):
        raise ValidationError("Brief changed after compact-prepare")
    current_approval_hash = (
        sha256_file(paths.brief_approval)
        if paths.brief_approval.is_file()
        else None
    )
    if source_hashes.get("brief_approval") != current_approval_hash:
        raise ValidationError("Brief approval changed after compact-prepare")
    if source_hashes.get("claims") != sha256_file(paths.claims):
        raise ValidationError("CLAIMS.json changed after compact-prepare")
    if source_hashes.get("evidence") != current_evidence_hashes(paths):
        raise ValidationError("Evidence set changed after compact-prepare")

    profile_record = compaction_state.get("repository_profile")
    if not isinstance(profile_record, dict):
        raise ValidationError("COMPACTION_INPUT.json lacks a repository profile binding")
    profile_path = repository_profile_path(paths.root)
    expected_profile_path = profile_path.relative_to(paths.root).as_posix()
    if profile_record.get("path") != expected_profile_path:
        raise ValidationError("COMPACTION_INPUT.json repository profile path is invalid")
    if profile_record.get("sha256") != sha256_file(profile_path):
        raise ValidationError("repository profile changed after compact-prepare")

    prepared_scope = compaction_state.get("host_change_scope")
    if not isinstance(prepared_scope, dict):
        raise ValidationError("COMPACTION_INPUT.json lacks a host change-scope binding")
    prepared_paths = prepared_scope.get("consequential_paths")
    if not isinstance(prepared_paths, list) or prepared_scope.get(
        "fingerprint_sha256"
    ) != sha256_json(prepared_paths):
        raise ValidationError("COMPACTION_INPUT.json host change-scope binding is invalid")
    current_scope = _host_change_scope(paths)
    if (
        current_scope["outcome"] != prepared_scope.get("outcome")
        or current_scope["git_available"] != prepared_scope.get("git_available")
        or current_scope["fingerprint_sha256"]
        != prepared_scope.get("fingerprint_sha256")
    ):
        raise ValidationError(
            "host consequential change scope changed after compact-prepare"
        )

    prepared_work = compaction_state.get("active_work_inventory")
    prepared_work_hash = compaction_state.get("active_work_inventory_sha256")
    if not isinstance(prepared_work, list) or prepared_work_hash != sha256_json(
        prepared_work
    ):
        raise ValidationError("COMPACTION_INPUT.json active-work binding is invalid")
    current_work = _tree_inventory(paths.active_work, paths.root)
    if sha256_json(current_work) != prepared_work_hash:
        raise ValidationError("work/active inventory changed after compact-prepare")


def budget_totals(runs: dict[str, tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    committed = budget_totals_from_manifests(
        manifest for _, manifest in runs.values()
    )
    duration = 0.0
    for _, manifest in runs.values():
        duration += float(manifest.get("execution", {}).get("duration_seconds") or 0.0)
    return {
        "estimated_gpu_hours": committed["gpu_hours"],
        "estimated_cpu_hours": committed["cpu_hours"],
        "charged_storage_gb": committed["storage_gb"],
        "recorded_wall_clock_hours": duration / 3600.0,
        "run_count": len(runs),
    }


def _compaction_budget_state(
    paths: StudyPaths,
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return budget totals plus the exact authority used to derive them.

    Resource commitments for a current Study come exclusively from the
    durable ledger.  Visible Manifests still supply wall-clock duration, but
    cannot reduce the charged GPU/CPU/storage projection.  An intact V1/V2
    history without a ledger is the only supported fallback, and its resulting
    budget state carries an explicit lower-assurance marker into a Checkpoint.
    """

    ledger = load_ledger(paths)
    duration = sum(
        float(manifest.get("execution", {}).get("duration_seconds") or 0.0)
        for _, manifest in runs.values()
    )
    if ledger is not None:
        reconciled = bootstrap_or_reconcile_ledger(paths, runs, write=False)
        if reconciled != ledger:
            raise ValidationError(
                "Run ledger is stale relative to visible immutable Manifests"
            )
        committed = ledger_commitment_totals(ledger)
        totals = {
            "estimated_gpu_hours": committed["gpu_hours"],
            "estimated_cpu_hours": committed["cpu_hours"],
            "charged_storage_gb": committed["storage_gb"],
            "recorded_wall_clock_hours": duration / 3600.0,
            "run_count": sum(
                entry["status"] != "aborted" for entry in ledger["runs"].values()
            ),
        }
        hard_limits = parse_brief_hard_budget(
            paths.brief.read_text(encoding="utf-8")
        )
        projection = budget_projection(
            hard_limits,
            committed,
            {"gpu_hours": 0.0, "cpu_hours": 0.0, "storage_gb": 0.0},
        )
        totals["hard_limits"] = hard_limits
        totals["existing_hard_budget_violations"] = projection["violations"]
        authority = {
            "kind": "durable_run_ledger",
            "assurance": "authoritative",
            "path": ledger_path(paths).relative_to(paths.root).as_posix(),
            "sha256": sha256_file(ledger_path(paths)),
            "high_water_mark": ledger["high_water_mark"],
        }
        return totals, authority

    if not runs or any(
        manifest.get("schema_version") not in {1, 2}
        for _, manifest in runs.values()
    ):
        raise ValidationError(
            "Run ledger is missing; compaction cannot establish authoritative "
            "Run identity or budget history"
        )

    totals = budget_totals(runs)
    hard_limits = parse_brief_hard_budget(
        paths.brief.read_text(encoding="utf-8")
    )
    committed = {
        "gpu_hours": totals["estimated_gpu_hours"],
        "cpu_hours": totals["estimated_cpu_hours"],
        "storage_gb": totals["charged_storage_gb"],
    }
    projection = budget_projection(
        hard_limits,
        committed,
        {"gpu_hours": 0.0, "cpu_hours": 0.0, "storage_gb": 0.0},
    )
    totals["hard_limits"] = hard_limits
    totals["existing_hard_budget_violations"] = projection["violations"]
    totals["authority"] = "legacy_manifest_fallback"
    totals["authority_warning"] = (
        "Unindexed pre-ledger V1/V2 Manifests are a lower-assurance fallback; "
        "migrate the intact Run history before relying on it as authoritative."
    )
    authority = {
        "kind": "legacy_manifest_fallback",
        "assurance": "legacy_unindexed_lower_assurance",
        "manifest_sha256": {
            run_id: sha256_file(path)
            for run_id, (path, _) in sorted(runs.items())
        },
    }
    return totals, authority


def current_evidence_hashes(paths: StudyPaths) -> dict[str, str]:
    return {
        path.relative_to(paths.root).as_posix(): sha256_file(path)
        for path in evidence_paths(paths)
    }


def _reference_mentions_path(references: set[str], *candidates: str) -> bool:
    material = [candidate for candidate in candidates if candidate]
    return any(candidate in reference for reference in references for candidate in material)


def prepare_compaction(paths: StudyPaths) -> Path:
    assert_valid_study(paths)
    host_change_scope = _host_change_scope(paths)
    runs = run_index(paths)
    budget_state, budget_authority = _compaction_budget_state(paths, runs)
    evidence = evidence_index(paths)
    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must be an object")
    evidence_run_ids = _evidence_run_ids(evidence)
    claim_evidence = _claim_evidence_keys(claims)
    status_counts = Counter(str(item.get("status")) for _, item in runs.values())
    cohort_status: dict[str, Counter[str]] = defaultdict(Counter)
    for _, item in runs.values():
        cohort_record = item.get("cohort", {})
        cohort = cohort_record.get("cohort_id") or (
            f"FINGERPRINT-{cohort_record.get('fingerprint_sha256')}"
        )
        cohort_status[str(cohort)][str(item.get("status"))] += 1
    formal = active_formal_artifacts(paths)
    work_files = _file_inventory(paths.active_work, paths.root)
    work_inventory = _tree_inventory(paths.active_work, paths.root)
    authoritative_refs = authoritative_string_references(paths) | run_file_references(paths)
    candidates: list[str] = []
    for record in work_files:
        full = record["path"]
        active_relative = (paths.root / full).relative_to(paths.active_work).as_posix()
        if not _reference_mentions_path(authoritative_refs, full, active_relative):
            candidates.append(active_relative)
    checkpoints = []
    for path in checkpoint_paths(paths):
        item = load_json(path)
        checkpoints.append(
            {
                "checkpoint_id": item.get("checkpoint_id"),
                "sha256": item.get("checkpoint_sha256"),
                "frontier": item.get("frontier"),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "source_hashes": {
            "brief": sha256_file(paths.brief),
            "brief_approval": sha256_file(paths.brief_approval) if paths.brief_approval.is_file() else None,
            "claims": sha256_file(paths.claims),
            "evidence": current_evidence_hashes(paths),
        },
        "repository_profile": {
            "path": repository_profile_path(paths.root)
            .relative_to(paths.root)
            .as_posix(),
            "sha256": sha256_file(repository_profile_path(paths.root)),
        },
        "host_change_scope": host_change_scope,
        "run_counts_by_status": dict(sorted(status_counts.items())),
        "run_counts_by_cohort_and_status": {
            cohort: dict(sorted(counts.items())) for cohort, counts in sorted(cohort_status.items())
        },
        "runs_not_referenced_by_evidence": sorted(set(runs) - evidence_run_ids),
        "evidence_not_referenced_by_claims": [
            {"evidence_id": key[0], "version": key[1], "status": item.get("status")}
            for key, (_, item) in sorted(evidence.items())
            if key not in claim_evidence
        ],
        "active_formal_artifacts": [item for item in formal if item["active"]],
        "stale_formal_artifacts": [item for item in formal if not item["active"]],
        "active_work_files": work_files,
        "active_work_inventory": work_inventory,
        "active_work_inventory_sha256": sha256_json(work_inventory),
        "previous_checkpoints": checkpoints,
        "current_claims": claims.get("claims", []),
        "current_frontier": claims.get("frontier", {}),
        "failed_direction_records": _file_inventory(paths.study / "failed-directions", paths.root),
        "budget_totals": budget_state,
        "budget_authority": budget_authority,
        "candidate_archive_items": candidates,
        "formalization_debt": collect_formalization_debt(paths),
    }
    output = paths.generated / "COMPACTION_INPUT.json"
    atomic_write_json(output, payload)
    return output


def _next_checkpoint_id(paths: StudyPaths) -> str:
    highest = 0
    for path in checkpoint_paths(paths):
        try:
            highest = max(highest, int(path.stem.removeprefix("CHECKPOINT-")))
        except ValueError:
            continue
    return f"CHECKPOINT-{highest + 1:06d}"


def _evidence_ref_exists(
    ref: dict[str, Any], evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]]
) -> bool:
    key = (str(ref.get("evidence_id")), int(ref.get("version", 0)))
    record = evidence.get(key)
    if record is None:
        return False
    item = record[1]
    digest = item.get("record_sha256")
    return (
        item.get("status") == "finalized"
        and isinstance(digest, str)
        and digest == record_digest(item, "record_sha256")
        and ref.get("sha256") == digest
    )


def _normalize_archive_source(paths: StudyPaths, raw: str) -> tuple[Path, Path]:
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValidationError(f"unsafe archive path: {raw!r}")
    parts = candidate.parts
    prefix = ("work", "active")
    if parts[:2] == prefix:
        candidate = PurePosixPath(*parts[2:])
    source = paths.active_work.joinpath(*candidate.parts)
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(paths.active_work.resolve())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"archive source is outside work/active or missing: {raw!r}") from exc
    if source.is_symlink() or not resolved.is_file():
        raise ValidationError(f"archive source must be a regular non-symlink file: {raw!r}")
    return resolved, Path(*candidate.parts)


def finalize_compaction(paths: StudyPaths, plan_path: Path) -> Path:
    lock_path = paths.generated / ".compaction.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkflowError("another compaction finalize operation is active") from exc
    os.close(lock_fd)
    try:
        with study_authority_lock(paths):
            return _finalize_compaction_locked(paths, plan_path)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _finalize_compaction_locked(paths: StudyPaths, plan_path: Path) -> Path:
    plan = load_json(plan_path.resolve())
    if not isinstance(plan, dict):
        raise ValidationError("compaction plan must be a JSON object")
    schema_issues = object_schema_issues(paths.root, "compaction_plan", plan_path, plan)
    if schema_issues:
        raise ValidationError("invalid compaction plan:\n" + "\n".join(issue.render() for issue in schema_issues))
    if plan.get("study_id") != paths.study_id:
        raise ValidationError("compaction plan study_id mismatch")
    compaction_input = paths.generated / "COMPACTION_INPUT.json"
    if not compaction_input.is_file():
        raise ValidationError("run compact-prepare before compact-finalize")
    if plan.get("compaction_input_sha256") != sha256_file(compaction_input):
        raise ValidationError("compaction plan is stale relative to COMPACTION_INPUT.json")
    compaction_state = load_json(compaction_input)
    if not isinstance(compaction_state, dict):
        raise ValidationError("COMPACTION_INPUT.json must be a JSON object")
    _validate_prepared_bindings(paths, compaction_state)
    if plan.get("claims_sha256") != sha256_file(paths.claims):
        raise ValidationError("CLAIMS.json changed after the compaction plan was written")
    if plan.get("evidence_sha256") != current_evidence_hashes(paths):
        raise ValidationError("Evidence set changed after the compaction plan was written")
    assert_valid_study(paths)
    claims = load_json(paths.claims)
    if plan.get("frontier") != claims.get("frontier"):
        raise ValidationError("compaction plan Frontier must equal the authoritative CLAIMS.json Frontier")
    frontier = claims.get("frontier", {})
    if plan.get("open_questions") != frontier.get("open_questions", []):
        raise ValidationError("compaction plan open_questions must equal the authoritative Frontier")
    if plan.get("next_actions") != frontier.get("next_actions", []):
        raise ValidationError("compaction plan next_actions must equal the authoritative Frontier")
    evidence = evidence_index(paths)
    runs = run_index(paths)
    expected_budget, expected_budget_authority = _compaction_budget_state(
        paths,
        runs,
    )
    if compaction_state.get("budget_authority") != expected_budget_authority:
        raise ValidationError(
            "Run budget authority changed after compact-prepare"
        )
    if compaction_state.get("budget_totals") != expected_budget:
        raise ValidationError(
            "Run budget totals changed after compact-prepare"
        )
    for field in ("decisive_evidence", "contradictory_evidence"):
        for ref in plan.get(field, []):
            if not _evidence_ref_exists(ref, evidence):
                raise ValidationError(f"{field} contains a missing or stale Evidence reference: {ref}")
    expected_decisive = {
        (str(ref.get("evidence_id")), int(ref.get("version", 0)))
        for claim in claims.get("claims", [])
        for ref in claim.get("supporting_evidence", [])
    }
    expected_contradictory = {
        (str(ref.get("evidence_id")), int(ref.get("version", 0)))
        for claim in claims.get("claims", [])
        for ref in claim.get("contradictory_evidence", [])
    }
    actual_decisive = {
        (str(ref.get("evidence_id")), int(ref.get("version", 0)))
        for ref in plan.get("decisive_evidence", [])
    }
    actual_contradictory = {
        (str(ref.get("evidence_id")), int(ref.get("version", 0)))
        for ref in plan.get("contradictory_evidence", [])
    }
    if not expected_decisive.issubset(actual_decisive):
        raise ValidationError("compaction plan omits supporting Evidence referenced by a Claim")
    if not expected_contradictory.issubset(actual_contradictory):
        raise ValidationError("compaction plan omits contradictory Evidence referenced by a Claim")
    if plan.get("budget_state") != expected_budget:
        raise ValidationError(
            "compaction plan budget_state must equal authoritative Run budget totals"
        )
    failed_direction_records = {
        item["path"]: item
        for item in _file_inventory(paths.study / "failed-directions", paths.root)
    }
    representative_failure_records: list[dict[str, Any]] = []
    for failure in plan.get("representative_failures", []):
        if failure in runs:
            manifest = runs[failure][1]
            if manifest.get("status") not in {"failed", "interrupted", "incomplete"}:
                raise ValidationError(
                    "representative failure Run is not failed/interrupted/incomplete: "
                    f"{failure}"
                )
            representative_failure_records.append(
                {
                    "kind": "run",
                    "run_id": failure,
                    "manifest_sha256": manifest.get("integrity", {}).get(
                        "manifest_sha256"
                    ),
                }
            )
        elif failure in failed_direction_records:
            record = failed_direction_records[failure]
            representative_failure_records.append(
                {
                    "kind": "failed_direction",
                    "path": record["path"],
                    "size": record["size"],
                    "sha256": record["sha256"],
                }
            )
        else:
            raise ValidationError(f"representative failure reference does not exist: {failure}")

    references = authoritative_string_references(paths) | run_file_references(paths)
    checkpoint_id = _next_checkpoint_id(paths)
    mappings: list[tuple[Path, Path, int, dict[str, Any]]] = []
    normalized_sources: set[Path] = set()
    for raw in plan.get("archive_work_files", []):
        source, relative = _normalize_archive_source(paths, raw)
        if source in normalized_sources:
            raise ValidationError(f"compaction plan repeats archive source: {raw}")
        normalized_sources.add(source)
        source_from_root = source.relative_to(paths.root).as_posix()
        active_relative = source.relative_to(paths.active_work.resolve()).as_posix()
        if _reference_mentions_path(references, source_from_root, active_relative, str(raw)):
            raise ValidationError(f"refusing to archive authoritative referenced work file: {raw}")
        destination = paths.archived_work / checkpoint_id / relative
        if destination.exists():
            raise ValidationError(f"archive destination already exists: {destination}")
        archive_record = {
            "source_path": source_from_root,
            "archived_path": destination.relative_to(paths.root).as_posix(),
            "size": source.stat().st_size,
            "sha256": sha256_file(source),
        }
        mappings.append((source, destination, source.stat().st_mode & 0o777, archive_record))

    previous = None
    existing = checkpoint_paths(paths)
    if existing:
        previous_item = load_json(existing[-1])
        previous = {
            "checkpoint_id": previous_item["checkpoint_id"],
            "sha256": previous_item["checkpoint_sha256"],
        }
    approval = load_json(paths.brief_approval)
    formal = active_formal_artifacts(paths)
    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "checkpoint_id": checkpoint_id,
        "created_at": utc_now(),
        "brief": {
            "sha256": approval["brief"]["sha256"],
            "approval_sha256": approval["approval_sha256"],
        },
        "repository_profile": compaction_state["repository_profile"],
        "host_change_scope": compaction_state["host_change_scope"],
        "prepared_active_work_inventory_sha256": compaction_state[
            "active_work_inventory_sha256"
        ],
        "active_formal_artifacts": [item for item in formal if item["active"]],
        "claims_file_sha256": sha256_file(paths.claims),
        "claims_snapshot": claims.get("claims", []),
        "frontier": frontier,
        "decisive_evidence": plan.get("decisive_evidence", []),
        "contradictory_evidence": plan.get("contradictory_evidence", []),
        "open_questions": frontier.get("open_questions", []),
        "next_actions": frontier.get("next_actions", []),
        "budget_state": plan.get("budget_state", {}),
        "formalization_debt": collect_formalization_debt(paths),
        "representative_failures": representative_failure_records,
        "archived_work_files": [record for _, _, _, record in mappings],
        "previous_checkpoint": previous,
        "compaction_plan_sha256": sha256_file(plan_path.resolve()),
        "checkpoint_sha256": "",
    }
    checkpoint["checkpoint_sha256"] = record_digest(checkpoint, "checkpoint_sha256")
    checkpoint_path = paths.checkpoints / f"{checkpoint_id}.json"
    moved: list[tuple[Path, Path, int]] = []
    try:
        for source, destination, original_mode, _ in mappings:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            os.chmod(destination, 0o444)
            moved.append((source, destination, original_mode))
        atomic_write_json(checkpoint_path, checkpoint, overwrite=False, mode=0o444)
    except Exception:
        for source, destination, original_mode in reversed(moved):
            source.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not source.exists():
                os.replace(destination, source)
                os.chmod(source, original_mode)
        raise
    render_status(paths)
    return checkpoint_path
