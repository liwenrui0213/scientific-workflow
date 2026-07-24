from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .budget import (
    budget_projection,
    format_budget_violation,
    parse_brief_hard_budget,
    requested_budget,
)
from .hashing import load_json
from .models import FormalizationResult, StudyPaths, ValidationError
from .run_ledger import bootstrap_or_reconcile_ledger, ledger_commitment_totals
from .validation import brief_approval_issues, protected_artifact_snapshot, run_index


_LEVEL_RANK = {
    "advisory": 0,
    "required_before_expensive_run": 1,
    "required_before_evidence": 2,
    "required_before_review": 3,
    "blocking_now": 4,
}


def load_policy(paths: StudyPaths) -> dict[str, Any]:
    value = load_json(paths.root / "scientific-workflow" / "policy.json")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValidationError("scientific-workflow/policy.json is invalid")
    return value


def _markdown_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    return (
        len(text.strip()) >= 100
        and "[replace:" not in lowered
        and "[replace]" not in lowered
        and ("status: active" in lowered or "status: finalized" in lowered)
        and "## scientific mapping" in lowered
        and "## algorithm" in lowered
    )


def _json_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        value = load_json(path)
    except ValidationError:
        return False
    if not isinstance(value, dict) or value.get("status") not in {"active", "finalized"}:
        return False
    meaningful = {key: item for key, item in value.items() if key not in {"schema_version", "status"}}
    if not meaningful:
        return False
    if path.name == "PROTOCOL.json":
        return (
            isinstance(value.get("purpose"), str)
            and bool(value["purpose"].strip())
            and isinstance(value.get("acceptance_criteria"), list)
            and bool(value["acceptance_criteria"])
            and isinstance(value.get("compute_budget"), dict)
            and isinstance(value.get("seeds"), list)
        )
    return True


def artifact_ready(paths: StudyPaths, artifact: str) -> bool:
    policy = load_policy(paths)
    relative = policy["formal_artifacts"][artifact]
    path = paths.study / relative
    if artifact == "PLAN":
        if not path.is_file():
            return False
        try:
            from .graph_records import active_control_graph

            return active_control_graph(paths) is not None
        except (OSError, ValidationError):
            return False
    return _markdown_ready(path) if path.suffix.lower() == ".md" else _json_ready(path)


def _path_is_scientific_critical(path: str, patterns: list[str], root: Path) -> bool:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            normalized = candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            normalized = candidate.as_posix().lstrip("/")
    else:
        normalized = path.replace("\\", "/").lstrip("./")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def _add_requirement(
    requirements: list[dict[str, str]], level: str, artifact: str, reason: str
) -> None:
    requirements.append({"level": level, "artifact": artifact, "reason": reason})


def _protected_change_is_approved(paths: StudyPaths) -> bool:
    if brief_approval_issues(paths):
        return False
    try:
        approval = load_json(paths.brief_approval)
    except ValidationError:
        return False
    return approval.get("protected_artifacts") == protected_artifact_snapshot(paths)


def check_formalization(paths: StudyPaths, options: dict[str, Any] | None = None) -> FormalizationResult:
    options = options or {}
    policy = load_policy(paths)
    requested = requested_budget(
        gpu_hours=options.get("estimated_gpu_hours", 0.0),
        cpu_hours=options.get("estimated_cpu_hours", 0.0),
        storage_gb=options.get("estimated_storage_gb", 0.0),
    )
    gpu_hours = requested["gpu_hours"]
    cpu_hours = requested["cpu_hours"]
    parallel_workers = int(options.get("parallel_workers", 1))
    if parallel_workers < 1:
        raise ValidationError("parallel_workers must be at least 1")
    requirements: list[dict[str, str]] = []

    if options.get("for_run") or options.get("check_hard_budget"):
        hard_limits = parse_brief_hard_budget(
            paths.brief.read_text(encoding="utf-8")
        )
        runs = run_index(paths)
        ledger = bootstrap_or_reconcile_ledger(
            paths, runs, write=False
        )
        committed = ledger_commitment_totals(ledger)
        projection = budget_projection(hard_limits, committed, requested)
        if projection["violations"]:
            _add_requirement(
                requirements,
                "blocking_now",
                "BRIEF",
                "; ".join(
                    format_budget_violation(item)
                    for item in projection["violations"]
                ),
            )

    expensive_gpu = gpu_hours >= float(policy["gpu_protocol_threshold_hours"])
    if expensive_gpu and not artifact_ready(paths, "PROTOCOL"):
        _add_requirement(
            requirements,
            "required_before_expensive_run",
            "PROTOCOL",
            f"estimated GPU use {gpu_hours:g} h meets the {policy['gpu_protocol_threshold_hours']:g} h threshold",
        )

    expensive_cpu = cpu_hours >= float(policy.get("expensive_cpu_threshold_hours", 100.0))
    if expensive_cpu and not artifact_ready(paths, "PROTOCOL"):
        _add_requirement(
            requirements,
            "required_before_expensive_run",
            "PROTOCOL",
            f"estimated CPU use {cpu_hours:g} h is expensive under policy",
        )

    changed_paths = [str(item) for item in options.get("changed_path", [])]
    from .workspace import load_repository_profile

    profile = load_repository_profile(paths.root)
    critical_patterns = profile.get("scientific_critical_patterns", [])
    critical = bool(options.get("scientific_critical")) or any(
        _path_is_scientific_critical(
            path, critical_patterns, paths.root
        )
        for path in changed_paths
    )
    shared = bool(options.get("shared_across_runs"))
    if (critical or shared) and not artifact_ready(paths, "METHOD"):
        if options.get("for_review"):
            level = "required_before_review"
        elif options.get("for_evidence"):
            level = "required_before_evidence"
        elif shared:
            level = "required_before_evidence"
        else:
            level = "advisory"
        reason = "scientific-critical implementation needs an explicit scientific-to-code mapping"
        if shared:
            reason = "scientific-critical implementation is shared across Runs or workers"
        _add_requirement(requirements, level, "METHOD", reason)

    parallel_dependency = bool(options.get("has_parallel_dependencies")) or parallel_workers > 1
    if parallel_dependency and not artifact_ready(paths, "PLAN"):
        _add_requirement(
            requirements,
            "blocking_now",
            "PLAN",
            "parallel dependency or multi-worker orchestration requires an explicit PLAN",
        )

    protected_change = any(
        bool(options.get(name))
        for name in (
            "changes_evaluator",
            "changes_dataset_split",
            "changes_acceptance_criteria",
        )
    )
    if protected_change:
        if not artifact_ready(paths, "EVALUATOR"):
            _add_requirement(
                requirements,
                "blocking_now",
                "EVALUATOR",
                "evaluator, data split, or acceptance-criteria change requires an active EVALUATOR",
            )
        if not _protected_change_is_approved(paths):
            _add_requirement(
                requirements,
                "blocking_now",
                "BRIEF",
                "protected evaluator conditions changed and require a new procedural human Brief approval",
            )

    if options.get("changes_claim_scope"):
        _add_requirement(
            requirements,
            "blocking_now",
            "BRIEF",
            "Claim scope crosses the active Brief boundary; start a new Brief version and obtain approval",
        )

    # Keep the smallest artifact per rule target, retaining the strictest level.
    deduplicated: dict[str, dict[str, str]] = {}
    for requirement in requirements:
        previous = deduplicated.get(requirement["artifact"])
        if previous is None or _LEVEL_RANK[requirement["level"]] > _LEVEL_RANK[previous["level"]]:
            deduplicated[requirement["artifact"]] = requirement
    ordered = sorted(
        deduplicated.values(),
        key=lambda item: (-_LEVEL_RANK[item["level"]], item["artifact"]),
    )
    blocking_levels = {
        "required_before_expensive_run" if expensive_gpu or expensive_cpu else "",
        "required_before_evidence" if options.get("for_evidence") or shared else "",
        "required_before_review" if options.get("for_review") else "",
        "blocking_now",
    }
    if any(item["level"] in blocking_levels for item in ordered):
        outcome = "BLOCKED"
    elif ordered:
        outcome = "ADVISORY"
    else:
        outcome = "PASS"
    return FormalizationResult(outcome, ordered)


def collect_formalization_debt(paths: StudyPaths) -> list[dict[str, Any]]:
    debt: list[dict[str, Any]] = []
    policy = load_policy(paths)
    for artifact in ("METHOD", "PROTOCOL", "EVALUATOR", "PLAN"):
        path = paths.study / policy["formal_artifacts"][artifact]
        if path.exists() and not artifact_ready(paths, artifact):
            debt.append(
                {
                    "debt_id": f"AUTO-DRAFT-{artifact}",
                    "level": "advisory",
                    "artifact": artifact,
                    "reason": f"{artifact} exists but remains draft or contains placeholders",
                    "source_refs": [path.relative_to(paths.root).as_posix()],
                }
            )
    return debt
