from __future__ import annotations

import copy
import math
from pathlib import Path
import re
import stat
from typing import Any, Sequence

from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from .locking import serialized_study_authority
from .models import (
    CONTROL_GRAPH_SCHEMA_VERSION,
    EXPERIMENT_INTENT_SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
    ValidationIssue,
    WorkflowError,
    require_id,
    utc_now,
)


_INTENT_FINAL = re.compile(r"^(INTENT-[0-9]{4,})\.v([0-9]{4,})\.json$")
_INTENT_DRAFT = re.compile(
    r"^(INTENT-[0-9]{4,})\.v([0-9]{4,})\.experiment-intent\.draft\.json$"
)
_PLAN_FINAL = re.compile(r"^(CG-[0-9]{4,})\.v([0-9]{4,})\.json$")
_PLAN_DRAFT = re.compile(
    r"^(CG-[0-9]{4,})\.v([0-9]{4,})\.control-graph\.draft\.json$"
)
# The maximal current Claims/Frontier projection is intentionally kept below
# the 98,304-byte ACTIVE_CONTEXT hard limit. Reserve at most 8 KiB of that
# envelope for graph navigation so a large but schema-valid Frontier remains
# loadable. Complete inventories stay bound by counts and canonical hashes.
GRAPH_RECORD_LOCATOR_BUDGET_BYTES = 8 * 1024


def experiment_intent_paths(paths: StudyPaths) -> list[Path]:
    if not paths.experiment_intents.is_dir():
        return []
    return sorted(paths.experiment_intents.glob("INTENT-*.v*.json"))


def control_graph_paths(paths: StudyPaths) -> list[Path]:
    if not paths.control_graphs.is_dir():
        return []
    return sorted(paths.control_graphs.glob("CG-*.v*.json"))


def experiment_intent_draft_paths(paths: StudyPaths) -> list[Path]:
    if not paths.active_work.is_dir():
        return []
    return sorted(paths.active_work.glob("INTENT-*.experiment-intent.draft.json"))


def control_graph_draft_paths(paths: StudyPaths) -> list[Path]:
    if not paths.active_work.is_dir():
        return []
    return sorted(paths.active_work.glob("CG-*.control-graph.draft.json"))


def _schema_errors(
    paths: StudyPaths, kind: str, path: Path, value: Any
) -> list[ValidationIssue]:
    from .validation import errors_only, object_schema_issues

    return errors_only(object_schema_issues(paths.root, kind, path, value))


def _raise_schema_errors(
    paths: StudyPaths, kind: str, path: Path, value: Any, *, label: str
) -> None:
    issues = _schema_errors(paths, kind, path, value)
    if issues:
        raise ValidationError(
            f"invalid {label}:\n" + "\n".join(issue.render() for issue in issues)
        )


def _fresh_governance(paths: StudyPaths) -> dict[str, str]:
    from .validation import brief_approval_issues

    errors = [
        issue
        for issue in brief_approval_issues(paths)
        if issue.level == "ERROR"
    ]
    if errors:
        raise ValidationError(
            "a fresh approved Brief is required before creating or finalizing "
            "an Experiment Intent:\n"
            + "\n".join(issue.render() for issue in errors)
        )
    approval = load_json(paths.brief_approval)
    if not isinstance(approval, dict):
        raise ValidationError("BRIEF.approval.json must contain an object")
    approval_sha256 = approval.get("approval_sha256")
    if not isinstance(approval_sha256, str):
        raise ValidationError("Brief approval digest is missing")
    return {
        "brief_sha256": sha256_file(paths.brief),
        "approval_sha256": approval_sha256,
    }


def _claims_object_and_index(
    paths: StudyPaths,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    value = load_json(paths.claims)
    if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
        raise ValidationError("CLAIMS.json must contain a claims array")
    result: dict[str, dict[str, Any]] = {}
    for item in value["claims"]:
        if not isinstance(item, dict):
            raise ValidationError("CLAIMS.json contains a non-object Claim")
        claim_id = require_id("claim", str(item.get("claim_id", "")))
        if claim_id in result:
            raise ValidationError(f"duplicate Claim ID in CLAIMS.json: {claim_id}")
        result[claim_id] = item
    return value, result


def _claim_binding(paths: StudyPaths, claim_id: str | None) -> dict[str, Any] | None:
    if claim_id is None:
        return None
    from .confirmation import claim_spec_sha256

    normalized = require_id("claim", claim_id)
    claims_value, claims = _claims_object_and_index(paths)
    try:
        claim = claims[normalized]
    except KeyError as exc:
        raise ValidationError(
            f"Experiment Intent references missing Claim: {normalized}"
        ) from exc
    from .active_context import active_claims

    active_ids = {
        str(item.get("claim_id"))
        for item in active_claims(claims_value)
        if isinstance(item, dict)
    }
    if normalized not in active_ids:
        raise ValidationError(
            "Experiment Intent target must be an active Frontier Claim: "
            f"{normalized}"
        )
    digest = claim_spec_sha256(claim)
    return {
        "claim_id": normalized,
        "statement": str(claim["statement"]),
        "scope": claim.get("scope"),
        "spec_sha256": digest,
    }


def _normalized_unique_text(values: Sequence[str], *, label: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label} must be supplied as a sequence")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label} entries must be non-empty strings")
        result.append(value.strip())
    if not result:
        raise ValidationError(f"at least one {label} entry is required")
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} entries must be unique")
    return result


def _versioned_final_records(
    paths: StudyPaths,
    *,
    record_id: str,
    kind: str,
) -> list[tuple[int, Path, dict[str, Any]]]:
    if kind == "experiment_intent":
        files = experiment_intent_paths(paths)
        pattern = _INTENT_FINAL
        identity_field = "intent_id"
    else:
        files = control_graph_paths(paths)
        pattern = _PLAN_FINAL
        identity_field = "control_graph_id"
    records: list[tuple[int, Path, dict[str, Any]]] = []
    for path in files:
        match = pattern.fullmatch(path.name)
        if match is None:
            if path.name.startswith(f"{record_id}.v"):
                raise ValidationError(f"malformed version filename: {path}")
            continue
        if match.group(1) != record_id:
            continue
        version = int(match.group(2))
        if path.name != f"{record_id}.v{version:04d}.json":
            raise ValidationError(f"non-canonical version filename: {path}")
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(f"versioned graph record must be an object: {path}")
        if (
            value.get("study_id") != paths.study_id
            or value.get(identity_field) != record_id
            or value.get("version") != version
        ):
            raise ValidationError(f"record identity/version does not match filename: {path}")
        schema_errors = _schema_errors(paths, kind, path, value)
        if schema_errors:
            raise ValidationError(
                "invalid finalized graph record:\n"
                + "\n".join(issue.render() for issue in schema_errors)
            )
        if value.get("status") != "finalized":
            raise ValidationError(f"versioned graph record is not finalized: {path}")
        if value.get("record_sha256") != record_digest(value, "record_sha256"):
            raise ValidationError(f"versioned graph-record digest is invalid: {path}")
        sealed_issues = _sealed_file_issues(path, label="finalized graph record")
        if sealed_issues:
            raise ValidationError("; ".join(sealed_issues))
        records.append((version, path, value))
    ordered = sorted(records)
    for index, (version, path, value) in enumerate(ordered):
        if version != index + 1:
            raise ValidationError(
                f"{record_id} version history is not contiguous from v1: {path}"
            )
        expected_previous = (
            _record_ref(
                ordered[index - 1][2],
                kind=(
                    "Experiment Intent"
                    if kind == "experiment_intent"
                    else "Control Graph"
                ),
            )
            if index
            else None
        )
        if value.get("previous_ref") != expected_previous:
            raise ValidationError(
                f"{record_id} previous_ref does not match prior version: {path}"
            )
    return ordered


def _strict_final_record_ids(
    directory: Path,
    *,
    pattern: re.Pattern[str],
    label: str,
) -> list[str]:
    if not directory.is_dir():
        return []
    record_ids: set[str] = set()
    for path in sorted(directory.iterdir()):
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ValidationError(
                f"{label} directory contains an unknown or non-canonical entry: {path}"
            )
        record_ids.add(match.group(1))
    return sorted(record_ids)


def _visible_graph_record_inventory(paths: StudyPaths) -> list[dict[str, Any]]:
    """Return the strict full finalized inventory without consulting its sequence."""

    inventory: list[dict[str, Any]] = []
    for kind, directory, pattern in (
        (
            "experiment_intent",
            paths.experiment_intents,
            _INTENT_FINAL,
        ),
        ("control_graph", paths.control_graphs, _PLAN_FINAL),
    ):
        for record_id in _strict_final_record_ids(
            directory,
            pattern=pattern,
            label=(
                "Experiment Intent"
                if kind == "experiment_intent"
                else "Control Graph"
            ),
        ):
            for version, path, record in _versioned_final_records(
                paths, record_id=record_id, kind=kind
            ):
                inventory.append(
                    {
                        "kind": kind,
                        "id": record_id,
                        "version": version,
                        "record_sha256": record["record_sha256"],
                        "file_sha256": sha256_file(path),
                    }
                )
    inventory.sort(
        key=lambda item: (item["kind"], item["id"], item["version"])
    )
    return inventory


def _require_graph_record_history(paths: StudyPaths) -> list[dict[str, Any]]:
    from .graph_record_sequence import require_consistent_graph_record_sequence

    inventory = _visible_graph_record_inventory(paths)
    require_consistent_graph_record_sequence(paths, inventory)
    return inventory


def _record_ref(value: dict[str, Any], *, kind: str) -> dict[str, Any]:
    digest = value.get("record_sha256")
    if not isinstance(digest, str):
        raise ValidationError(f"finalized {kind} has no record digest")
    if kind == "Experiment Intent":
        return {
            "intent_id": value["intent_id"],
            "version": value["version"],
            "sha256": digest,
        }
    return {
        "control_graph_id": value["control_graph_id"],
        "version": value["version"],
        "sha256": digest,
    }


def _latest_final(
    paths: StudyPaths, *, record_id: str, kind: str
) -> dict[str, Any] | None:
    _require_graph_record_history(paths)
    records = _versioned_final_records(paths, record_id=record_id, kind=kind)
    return records[-1][2] if records else None


def _unfinalized_drafts(
    paths: StudyPaths, *, record_id: str, kind: str
) -> list[Path]:
    if kind == "experiment_intent":
        draft_paths = experiment_intent_draft_paths(paths)
        pattern = _INTENT_DRAFT
        final_root = paths.experiment_intents
    else:
        draft_paths = control_graph_draft_paths(paths)
        pattern = _PLAN_DRAFT
        final_root = paths.control_graphs
    result: list[Path] = []
    for path in draft_paths:
        match = pattern.fullmatch(path.name)
        if match is None or match.group(1) != record_id:
            continue
        version = int(match.group(2))
        if not (final_root / f"{record_id}.v{version:04d}.json").exists():
            result.append(path)
    return result


@serialized_study_authority
def create_experiment_intent_draft(
    paths: StudyPaths,
    intent_id: str,
    *,
    evidence_gap_id: str,
    evidence_gap: str,
    objective: str,
    requested_observations: Sequence[str],
    evidence_requirements: Sequence[str] = (),
    claim_id: str | None = None,
) -> Path:
    """Create a mutable cognitive contract that says why evidence is needed."""

    paths.assert_safe_layout()
    _require_graph_record_history(paths)
    normalized_intent = require_id("experiment_intent", intent_id)
    normalized_gap = require_id("evidence_gap", evidence_gap_id)
    if not isinstance(evidence_gap, str) or not evidence_gap.strip():
        raise ValidationError("evidence gap description must be non-empty")
    if not isinstance(objective, str) or not objective.strip():
        raise ValidationError("Experiment Intent objective must be non-empty")
    observations = _normalized_unique_text(
        requested_observations, label="requested observation"
    )
    requirements = [
        value.strip()
        for value in evidence_requirements
        if isinstance(value, str) and value.strip()
    ]
    if len(requirements) != len(evidence_requirements):
        raise ValidationError("evidence requirements must be non-empty strings")
    if len(requirements) != len(set(requirements)):
        raise ValidationError("evidence requirements must be unique")
    open_drafts = _unfinalized_drafts(
        paths, record_id=normalized_intent, kind="experiment_intent"
    )
    if open_drafts:
        raise WorkflowError(
            f"Experiment Intent {normalized_intent} already has an open draft: "
            f"{open_drafts[0]}"
        )
    previous = _latest_final(
        paths, record_id=normalized_intent, kind="experiment_intent"
    )
    version = int(previous["version"]) + 1 if previous is not None else 1
    template = load_json(
        paths.root
        / "scientific-workflow"
        / "templates"
        / "EXPERIMENT_INTENT.json"
    )
    if not isinstance(template, dict):
        raise ValidationError("Experiment Intent template must be an object")
    timestamp = utc_now()
    draft = copy.deepcopy(template)
    draft.update(
        {
            "schema_version": EXPERIMENT_INTENT_SCHEMA_VERSION,
            "study_id": paths.study_id,
            "intent_id": normalized_intent,
            "version": version,
            "status": "draft",
            "created_at": timestamp,
            "updated_at": timestamp,
            "previous_ref": (
                _record_ref(previous, kind="Experiment Intent")
                if previous is not None
                else None
            ),
            "governance": _fresh_governance(paths),
            "addresses": {
                "evidence_gap": {
                    "gap_id": normalized_gap,
                    "description": evidence_gap.strip(),
                },
                "target_claim": _claim_binding(paths, claim_id),
            },
            "objective": objective.strip(),
            "requested_observations": observations,
            "evidence_requirements": requirements,
        }
    )
    destination = (
        paths.active_work
        / f"{normalized_intent}.v{version:04d}.experiment-intent.draft.json"
    )
    _raise_schema_errors(
        paths,
        "experiment_intent",
        destination,
        draft,
        label="generated Experiment Intent draft",
    )
    atomic_write_json(destination, draft, overwrite=False)
    return destination


def _safe_draft_source(
    paths: StudyPaths, source: Path, *, pattern: re.Pattern[str], label: str
) -> tuple[Path, re.Match[str]]:
    paths.assert_safe_layout()
    candidate = source if source.is_absolute() else Path.cwd() / source
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValidationError(f"{label} draft cannot be inspected: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"{label} draft must be a regular non-symlink file")
    if metadata.st_nlink != 1:
        raise ValidationError(f"{label} draft must not be hard-linked")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(paths.active_work.resolve())
    except ValueError as exc:
        raise ValidationError(f"{label} draft must be inside work/active") from exc
    match = pattern.fullmatch(resolved.name)
    if match is None:
        raise ValidationError(f"{label} draft filename is not canonical")
    return resolved, match


def _validate_intent_content(item: dict[str, Any]) -> None:
    requirements = item.get("evidence_requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValidationError(
            "finalized Experiment Intent requires at least one evidence requirement"
        )
    semantics = item.get("assessment_semantics")
    criteria = semantics.get("criteria") if isinstance(semantics, dict) else None
    if not isinstance(criteria, list) or not criteria:
        raise ValidationError(
            "finalized Experiment Intent requires explicit assessment criteria"
        )
    observations = set(item.get("requested_observations", []))
    criterion_ids: list[str] = []
    target_claim = item.get("addresses", {}).get("target_claim")
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise ValidationError("Experiment Intent criterion must be an object")
        criterion_ids.append(str(criterion.get("criterion_id", "")))
        if criterion.get("observation") not in observations:
            raise ValidationError(
                "every assessment criterion must name a requested observation"
            )
        operator = criterion.get("operator")
        target = criterion.get("target")
        if operator in {"lt", "lte", "gt", "gte"} and (
            isinstance(target, bool)
            or not isinstance(target, (int, float))
            or not math.isfinite(float(target))
        ):
            raise ValidationError(
                f"Experiment Intent criterion {criterion.get('criterion_id')} "
                "uses an ordering operator with a non-finite or non-numeric target"
            )
        if operator in {"eq", "ne"} and (
            target is None
            or (
                isinstance(target, float)
                and not math.isfinite(target)
            )
        ):
            raise ValidationError(
                f"Experiment Intent criterion {criterion.get('criterion_id')} "
                "requires a finite, non-null equality target"
            )
        if target_claim is None and (
            criterion.get("on_pass") in {"supports", "contradicts"}
            or criterion.get("on_fail") in {"supports", "contradicts"}
        ):
            raise ValidationError(
                "Experiment Intent criteria cannot support or contradict a "
                "Claim when addresses.target_claim is null"
            )
    if len(criterion_ids) != len(set(criterion_ids)):
        raise ValidationError("Experiment Intent criterion IDs must be unique")
    scope = item.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ValidationError(
            "finalized Experiment Intent requires an explicit non-empty scope"
        )


def _validate_intent_semantics(paths: StudyPaths, item: dict[str, Any]) -> None:
    _validate_intent_content(item)
    if item.get("governance") != _fresh_governance(paths):
        raise ValidationError(
            "Experiment Intent governance is stale; create a new Intent version "
            "against the current approved Brief"
        )
    target = item.get("addresses", {}).get("target_claim")
    if isinstance(target, dict):
        current = _claim_binding(paths, str(target.get("claim_id", "")))
        if target != current:
            raise ValidationError(
                "Experiment Intent target Claim specification is stale"
            )


def _require_expected_previous(
    paths: StudyPaths,
    item: dict[str, Any],
    *,
    record_id: str,
    kind: str,
) -> None:
    version = int(item["version"])
    records = _versioned_final_records(paths, record_id=record_id, kind=kind)
    expected_version = records[-1][0] + 1 if records else 1
    if version != expected_version:
        raise ValidationError(
            f"{kind} draft version {version} is not the next version "
            f"{expected_version}"
        )
    expected_previous = (
        _record_ref(
            records[-1][2],
            kind="Experiment Intent" if kind == "experiment_intent" else "Control Graph",
        )
        if records
        else None
    )
    if item.get("previous_ref") != expected_previous:
        raise ValidationError(f"{kind} previous_ref does not match the prior version")


@serialized_study_authority
def finalize_experiment_intent(paths: StudyPaths, source: Path) -> Path:
    """Freeze one rigorous cognitive Intent without promoting its Claim."""

    previous_inventory = _require_graph_record_history(paths)
    draft_path, match = _safe_draft_source(
        paths,
        source,
        pattern=_INTENT_DRAFT,
        label="Experiment Intent",
    )
    item = load_json(draft_path)
    if not isinstance(item, dict):
        raise ValidationError("Experiment Intent draft must be an object")
    _raise_schema_errors(
        paths,
        "experiment_intent",
        draft_path,
        item,
        label="Experiment Intent draft",
    )
    intent_id = require_id("experiment_intent", match.group(1))
    version = int(match.group(2))
    if (
        item.get("study_id") != paths.study_id
        or item.get("intent_id") != intent_id
        or item.get("version") != version
    ):
        raise ValidationError(
            "Experiment Intent identity/version does not match its draft filename"
        )
    if item.get("status") != "draft" or item.get("record_sha256") is not None:
        raise ValidationError("only an unsealed Experiment Intent draft can be finalized")
    _require_expected_previous(
        paths,
        item,
        record_id=intent_id,
        kind="experiment_intent",
    )
    _validate_intent_semantics(paths, item)
    finalized = copy.deepcopy(item)
    finalized["status"] = "finalized"
    finalized["updated_at"] = utc_now()
    finalized["record_sha256"] = record_digest(finalized, "record_sha256")
    destination = paths.experiment_intents / f"{intent_id}.v{version:04d}.json"
    _raise_schema_errors(
        paths,
        "experiment_intent",
        destination,
        finalized,
        label="finalized Experiment Intent",
    )
    atomic_write_json(
        destination,
        finalized,
        overwrite=False,
        mode=0o444,
        require_parent_fsync=True,
    )
    from .graph_record_sequence import advance_graph_record_sequence

    advance_graph_record_sequence(
        paths,
        previous_inventory=previous_inventory,
        current_inventory=_visible_graph_record_inventory(paths),
    )
    return destination


def _load_final_experiment_intent_without_sequence(
    paths: StudyPaths, intent_id: str, version: int
) -> dict[str, Any]:
    normalized = require_id("experiment_intent", intent_id)
    records = _versioned_final_records(
        paths, record_id=normalized, kind="experiment_intent"
    )
    for record_version, _, value in records:
        if record_version == version:
            _validate_intent_content(value)
            return value
    raise ValidationError(
        f"Experiment Intent does not exist: {normalized} v{version}"
    )


def load_final_experiment_intent(
    paths: StudyPaths, intent_id: str, version: int
) -> dict[str, Any]:
    normalized = require_id("experiment_intent", intent_id)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("Experiment Intent version must be a positive integer")
    _require_graph_record_history(paths)
    return _load_final_experiment_intent_without_sequence(
        paths, normalized, version
    )


def require_current_experiment_intent(
    paths: StudyPaths, intent_id: str, version: int
) -> dict[str, Any]:
    value = load_final_experiment_intent(paths, intent_id, version)
    latest = _latest_final(
        paths, record_id=intent_id, kind="experiment_intent"
    )
    if latest is None or latest.get("version") != version:
        raise ValidationError(
            f"Experiment Intent {intent_id} v{version} is superseded"
        )
    _validate_intent_semantics(paths, value)
    return value


@serialized_study_authority
def create_control_graph_draft(
    paths: StudyPaths,
    control_graph_id: str,
    *,
    intent_id: str,
    intent_version: int,
    executor: str = "studyctl",
    cpu_hours: float = 0.0,
    gpu_hours: float = 0.0,
    storage_gb: float = 0.0,
    parallel_workers: int = 1,
) -> Path:
    """Create an editable ControlGraphSpec that says how to realize one Intent."""

    paths.assert_safe_layout()
    normalized_graph = require_id("control_graph", control_graph_id)
    intent = require_current_experiment_intent(
        paths, intent_id, intent_version
    )
    if not isinstance(executor, str) or not executor.strip():
        raise ValidationError("Control Graph executor must be non-empty")
    resources = {
        "cpu_hours": cpu_hours,
        "gpu_hours": gpu_hours,
        "storage_gb": storage_gb,
        "parallel_workers": parallel_workers,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        for key, value in resources.items()
        if key != "parallel_workers"
    ):
        raise ValidationError("Control Graph resource amounts must be finite non-negative numbers")
    if (
        isinstance(parallel_workers, bool)
        or not isinstance(parallel_workers, int)
        or parallel_workers < 1
    ):
        raise ValidationError("parallel_workers must be a positive integer")
    open_drafts = _unfinalized_drafts(
        paths, record_id=normalized_graph, kind="control_graph"
    )
    if open_drafts:
        raise WorkflowError(
            f"Control Graph {normalized_graph} already has an open draft: "
            f"{open_drafts[0]}"
        )
    previous = _latest_final(
        paths, record_id=normalized_graph, kind="control_graph"
    )
    version = int(previous["version"]) + 1 if previous is not None else 1
    template = load_json(
        paths.root
        / "scientific-workflow"
        / "templates"
        / "CONTROL_GRAPH.json"
    )
    if not isinstance(template, dict):
        raise ValidationError("Control Graph template must be an object")
    timestamp = utc_now()
    draft = copy.deepcopy(template)
    draft.update(
        {
            "schema_version": CONTROL_GRAPH_SCHEMA_VERSION,
            "study_id": paths.study_id,
            "control_graph_id": normalized_graph,
            "version": version,
            "status": "draft",
            "created_at": timestamp,
            "updated_at": timestamp,
            "previous_ref": (
                _record_ref(previous, kind="Control Graph")
                if previous is not None
                else None
            ),
            "realizes_intent": _record_ref(
                intent, kind="Experiment Intent"
            ),
            "executor": {"kind": executor.strip(), "parameters": {}},
            "resources": resources,
        }
    )
    destination = (
        paths.active_work
        / f"{normalized_graph}.v{version:04d}.control-graph.draft.json"
    )
    _raise_schema_errors(
        paths,
        "control_graph",
        destination,
        draft,
        label="generated Control Graph draft",
    )
    atomic_write_json(destination, draft, overwrite=False)
    return destination


def _validate_control_graph_semantics(
    paths: StudyPaths,
    item: dict[str, Any],
    *,
    require_current_intent: bool,
    enforce_sequence: bool = True,
) -> None:
    intent_ref = item.get("realizes_intent")
    if not isinstance(intent_ref, dict):
        raise ValidationError("Control Graph realizes_intent must be an object")
    intent_id = str(intent_ref.get("intent_id", ""))
    intent_version = intent_ref.get("version")
    if enforce_sequence:
        loader = (
            require_current_experiment_intent
            if require_current_intent
            else load_final_experiment_intent
        )
        intent = loader(paths, intent_id, intent_version)
    else:
        intent = _load_final_experiment_intent_without_sequence(
            paths, intent_id, intent_version
        )
        if require_current_intent:
            records = _versioned_final_records(
                paths,
                record_id=require_id("experiment_intent", intent_id),
                kind="experiment_intent",
            )
            if not records or records[-1][0] != intent_version:
                raise ValidationError(
                    f"Experiment Intent {intent_id} v{intent_version} is superseded"
                )
    if intent_ref != _record_ref(intent, kind="Experiment Intent"):
        raise ValidationError("Control Graph realizes_intent reference is stale")
    nodes = item.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("finalized Control Graph requires at least one node")
    node_ids: list[str] = []
    node_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise ValidationError("Control Graph node must be an object")
        node_id = str(node.get("node_id", ""))
        node_ids.append(node_id)
        node_by_id[node_id] = node
        is_loop = node.get("kind") == "loop"
        if is_loop != isinstance(node.get("loop_contract"), dict):
            raise ValidationError(
                f"Control Graph node {node_id} must use loop_contract exactly "
                "when kind is 'loop'"
            )
        if node.get("kind") in {"task", "validator", "loop", "publish"}:
            command = node.get("command")
            if not isinstance(command, list) or not command:
                raise ValidationError(
                    f"Control Graph executable node {node_id} requires a command"
                )
            if any(
                not isinstance(argument, str) or not argument.strip()
                for argument in command
            ):
                raise ValidationError(
                    f"Control Graph executable node {node_id} command "
                    "arguments must be non-empty strings"
                )
    if len(node_ids) != len(set(node_ids)):
        raise ValidationError("Control Graph node IDs must be unique")
    edges = item.get("edges")
    if not isinstance(edges, list):
        raise ValidationError("Control Graph edges must be an array")
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    edge_keys: set[tuple[str, str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValidationError("Control Graph edge must be an object")
        source = str(edge.get("from", ""))
        target = str(edge.get("to", ""))
        condition = str(edge.get("condition", ""))
        if source not in node_by_id or target not in node_by_id:
            raise ValidationError(
                f"Control Graph edge endpoint is missing: {source} -> {target}"
            )
        if source == target:
            raise ValidationError(
                "Control Graph top-level edges must be acyclic; use a bounded "
                "loop node instead of a self-edge"
            )
        key = (source, target, condition)
        if key in edge_keys:
            raise ValidationError("Control Graph repeats an edge")
        edge_keys.add(key)
        if target not in adjacency[source]:
            adjacency[source].add(target)
            indegree[target] += 1
    queue = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited: list[str] = []
    while queue:
        node_id = queue.pop(0)
        visited.append(node_id)
        for target in sorted(adjacency[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort()
    if len(visited) != len(node_ids):
        raise ValidationError(
            "Control Graph top-level edges contain a cycle; encapsulate iteration "
            "inside a bounded loop node"
        )
    terminal_nodes = {
        node_id for node_id, targets in adjacency.items() if not targets
    }
    completion = item.get("completion")
    required = (
        completion.get("required_node_ids")
        if isinstance(completion, dict)
        else None
    )
    if not isinstance(required, list) or not required:
        raise ValidationError(
            "finalized Control Graph requires at least one completion node"
        )
    missing = sorted(set(required) - set(node_ids))
    if missing:
        raise ValidationError(
            "Control Graph completion references missing node(s): "
            + ", ".join(missing)
        )
    nonterminal = sorted(set(required) - terminal_nodes)
    if nonterminal:
        raise ValidationError(
            "Control Graph completion nodes must be terminal: "
            + ", ".join(nonterminal)
        )


@serialized_study_authority
def finalize_control_graph(paths: StudyPaths, source: Path) -> Path:
    """Freeze one prospective execution topology for an exact Intent."""

    previous_inventory = _require_graph_record_history(paths)
    draft_path, match = _safe_draft_source(
        paths,
        source,
        pattern=_PLAN_DRAFT,
        label="Control Graph",
    )
    item = load_json(draft_path)
    if not isinstance(item, dict):
        raise ValidationError("Control Graph draft must be an object")
    _raise_schema_errors(
        paths, "control_graph", draft_path, item, label="Control Graph draft"
    )
    control_graph_id = require_id("control_graph", match.group(1))
    version = int(match.group(2))
    if (
        item.get("study_id") != paths.study_id
        or item.get("control_graph_id") != control_graph_id
        or item.get("version") != version
    ):
        raise ValidationError(
            "Control Graph identity/version does not match its draft filename"
        )
    if item.get("status") != "draft" or item.get("record_sha256") is not None:
        raise ValidationError("only an unsealed Control Graph draft can be finalized")
    _require_expected_previous(
        paths,
        item,
        record_id=control_graph_id,
        kind="control_graph",
    )
    _validate_control_graph_semantics(
        paths, item, require_current_intent=True
    )
    finalized = copy.deepcopy(item)
    finalized["status"] = "finalized"
    finalized["updated_at"] = utc_now()
    finalized["record_sha256"] = record_digest(finalized, "record_sha256")
    destination = paths.control_graphs / f"{control_graph_id}.v{version:04d}.json"
    _raise_schema_errors(
        paths,
        "control_graph",
        destination,
        finalized,
        label="finalized Control Graph",
    )
    atomic_write_json(
        destination,
        finalized,
        overwrite=False,
        mode=0o444,
        require_parent_fsync=True,
    )
    from .graph_record_sequence import advance_graph_record_sequence

    advance_graph_record_sequence(
        paths,
        previous_inventory=previous_inventory,
        current_inventory=_visible_graph_record_inventory(paths),
    )
    return destination


def _load_final_control_graph_without_sequence(
    paths: StudyPaths, control_graph_id: str, version: int
) -> dict[str, Any]:
    normalized = require_id("control_graph", control_graph_id)
    records = _versioned_final_records(
        paths, record_id=normalized, kind="control_graph"
    )
    for record_version, _, value in records:
        if record_version == version:
            _validate_control_graph_semantics(
                paths,
                value,
                require_current_intent=False,
                enforce_sequence=False,
            )
            return value
    raise ValidationError(f"Control Graph does not exist: {normalized} v{version}")


def load_final_control_graph(
    paths: StudyPaths, control_graph_id: str, version: int
) -> dict[str, Any]:
    normalized = require_id("control_graph", control_graph_id)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("Control Graph version must be a positive integer")
    _require_graph_record_history(paths)
    return _load_final_control_graph_without_sequence(
        paths, normalized, version
    )


def require_current_control_graph(
    paths: StudyPaths, control_graph_id: str, version: int
) -> dict[str, Any]:
    value = load_final_control_graph(paths, control_graph_id, version)
    latest = _latest_final(
        paths, record_id=control_graph_id, kind="control_graph"
    )
    if latest is None or latest.get("version") != version:
        raise ValidationError(
            f"Control Graph {control_graph_id} v{version} is superseded"
        )
    intent_ref = value["realizes_intent"]
    require_current_experiment_intent(
        paths, intent_ref["intent_id"], intent_ref["version"]
    )
    return value


@serialized_study_authority
def activate_control_graph(
    paths: StudyPaths, control_graph_id: str, version: int
) -> Path:
    """Materialize a current immutable ControlGraphSpec as formal/PLAN.json."""

    value = require_current_control_graph(paths, control_graph_id, version)
    source = (
        paths.control_graphs
        / f"{value['control_graph_id']}.v{int(value['version']):04d}.json"
    )
    payload = source.read_bytes()
    destination = paths.formal / "PLAN.json"
    atomic_write_bytes(destination, payload, overwrite=True, mode=0o444)
    return destination


def active_control_graph(paths: StudyPaths) -> dict[str, Any] | None:
    """Return the active ControlGraphSpec, or ``None`` when no PLAN is active."""

    plan_path = paths.formal / "PLAN.json"
    if not plan_path.exists() and not plan_path.is_symlink():
        return None
    sealed_issues = _sealed_file_issues(plan_path, label="active PLAN")
    if sealed_issues:
        raise ValidationError("; ".join(sealed_issues))
    value = load_json(plan_path)
    if not isinstance(value, dict):
        raise ValidationError("formal/PLAN.json must contain a ControlGraphSpec")
    if value.get("record_type") != "control_graph_spec":
        raise ValidationError(
            "formal/PLAN.json must be an activated ControlGraphSpec with "
            "record_type='control_graph_spec'"
        )
    _require_graph_record_history(paths)
    control_graph_id = str(value.get("control_graph_id", ""))
    version = value.get("version")
    current = require_current_control_graph(paths, control_graph_id, version)
    source = paths.control_graphs / f"{control_graph_id}.v{version:04d}.json"
    if plan_path.read_bytes() != source.read_bytes():
        raise ValidationError(
            "formal/PLAN.json does not exactly materialize its immutable "
            "Control Graph record"
        )
    if value != current:
        raise ValidationError("formal/PLAN.json content is stale")
    return current


def _sealed_file_issues(path: Path, *, label: str) -> list[str]:
    messages: list[str] = []
    try:
        metadata = path.lstat()
    except OSError as exc:
        return [str(exc)]
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        messages.append(f"{label} must be a regular non-symlink file")
    if metadata.st_nlink != 1:
        messages.append(f"{label} must not be hard-linked")
    if metadata.st_mode & 0o222:
        messages.append(f"{label} must be sealed read-only")
    return messages


def _record_issue(
    issues: list[ValidationIssue], path: Path, message: str, *, level: str = "ERROR"
) -> None:
    issues.append(ValidationIssue(level, str(path), message))


def graph_record_issues(paths: StudyPaths) -> list[ValidationIssue]:
    """Replay schema, lineage, exact-reference, and active-plan integrity."""

    issues: list[ValidationIssue] = []
    for kind, files, pattern, identity_field in (
        (
            "experiment_intent",
            experiment_intent_paths(paths),
            _INTENT_FINAL,
            "intent_id",
        ),
        (
            "control_graph",
            control_graph_paths(paths),
            _PLAN_FINAL,
            "control_graph_id",
        ),
    ):
        by_id: dict[str, list[tuple[int, Path, dict[str, Any]]]] = {}
        for path in files:
            try:
                match = pattern.fullmatch(path.name)
                if match is None:
                    raise ValidationError("non-canonical graph-record filename")
                record_id = match.group(1)
                version = int(match.group(2))
                if path.name != f"{record_id}.v{version:04d}.json":
                    raise ValidationError("non-canonical graph-record version")
                value = load_json(path)
                if not isinstance(value, dict):
                    raise ValidationError("graph record must be an object")
                schema_errors = _schema_errors(paths, kind, path, value)
                issues.extend(schema_errors)
                if schema_errors:
                    continue
                if (
                    value.get("study_id") != paths.study_id
                    or value.get(identity_field) != record_id
                    or value.get("version") != version
                    or value.get("status") != "finalized"
                ):
                    raise ValidationError(
                        "graph-record identity/status does not match filename"
                    )
                if value.get("record_sha256") != record_digest(
                    value, "record_sha256"
                ):
                    raise ValidationError("graph-record digest is invalid")
                for message in _sealed_file_issues(path, label="finalized graph record"):
                    _record_issue(issues, path, message)
                by_id.setdefault(record_id, []).append((version, path, value))
                if kind == "experiment_intent":
                    _validate_intent_content(value)
                else:
                    _validate_control_graph_semantics(
                        paths, value, require_current_intent=False
                    )
            except (OSError, ValidationError, ValueError) as exc:
                _record_issue(issues, path, str(exc))
        for record_id, records in by_id.items():
            records.sort()
            for index, (version, path, value) in enumerate(records):
                if version != index + 1:
                    _record_issue(
                        issues,
                        path,
                        f"{record_id} version history is not contiguous from v1",
                    )
                    continue
                expected_previous = (
                    _record_ref(
                        records[index - 1][2],
                        kind=(
                            "Experiment Intent"
                            if kind == "experiment_intent"
                            else "Control Graph"
                        ),
                    )
                    if index
                    else None
                )
                if value.get("previous_ref") != expected_previous:
                    _record_issue(
                        issues,
                        path,
                        f"{record_id} previous_ref does not match prior version",
                    )
    for kind, files, pattern in (
        (
            "experiment_intent",
            experiment_intent_draft_paths(paths),
            _INTENT_DRAFT,
        ),
        ("control_graph", control_graph_draft_paths(paths), _PLAN_DRAFT),
    ):
        for path in files:
            try:
                match = pattern.fullmatch(path.name)
                if match is None:
                    raise ValidationError("non-canonical graph-record draft filename")
                final_root = (
                    paths.experiment_intents
                    if kind == "experiment_intent"
                    else paths.control_graphs
                )
                version = int(match.group(2))
                if (final_root / f"{match.group(1)}.v{version:04d}.json").exists():
                    continue
                value = load_json(path)
                if not isinstance(value, dict):
                    raise ValidationError("graph-record draft must be an object")
                issues.extend(_schema_errors(paths, kind, path, value))
                if value.get("status") != "draft":
                    raise ValidationError("unfinalized graph draft must have draft status")
                if value.get("record_sha256") is not None:
                    raise ValidationError("unfinalized graph draft digest must be null")
            except (OSError, ValidationError, ValueError) as exc:
                _record_issue(issues, path, str(exc))
    plan_path = paths.formal / "PLAN.json"
    if plan_path.is_file():
        try:
            active_control_graph(paths)
        except (OSError, ValidationError, ValueError) as exc:
            _record_issue(issues, plan_path, str(exc))
    return issues


def _validate_all_visible_graph_semantics_without_sequence(
    paths: StudyPaths,
) -> None:
    for intent_id in _strict_final_record_ids(
        paths.experiment_intents,
        pattern=_INTENT_FINAL,
        label="Experiment Intent",
    ):
        for _, _, record in _versioned_final_records(
            paths, record_id=intent_id, kind="experiment_intent"
        ):
            _validate_intent_content(record)
    for graph_id in _strict_final_record_ids(
        paths.control_graphs,
        pattern=_PLAN_FINAL,
        label="Control Graph",
    ):
        for _, _, record in _versioned_final_records(
            paths, record_id=graph_id, kind="control_graph"
        ):
            _validate_control_graph_semantics(
                paths,
                record,
                require_current_intent=False,
                enforce_sequence=False,
            )


def graph_record_sequence_issues(paths: StudyPaths) -> list[ValidationIssue]:
    from .graph_record_sequence import (
        graph_record_sequence_temporary_paths,
        require_consistent_graph_record_sequence,
    )

    issues = [
        ValidationIssue(
            "ERROR",
            str(path),
            "unfinished Graph-record-sequence temporary file is present",
        )
        for path in graph_record_sequence_temporary_paths(paths)
    ]
    try:
        inventory = _visible_graph_record_inventory(paths)
        require_consistent_graph_record_sequence(paths, inventory)
    except (OSError, ValidationError, WorkflowError, ValueError) as exc:
        issues.append(
            ValidationIssue("ERROR", str(paths.graph_record_sequence), str(exc))
        )
    return issues


@serialized_study_authority
def recover_graph_record_sequence(paths: StudyPaths) -> Path:
    """Advance across one valid record left by an interrupted sequence update."""

    from .graph_record_sequence import (
        recover_unindexed_graph_record,
        require_consistent_graph_record_sequence,
    )

    paths.assert_safe_layout()
    _validate_all_visible_graph_semantics_without_sequence(paths)
    inventory = _visible_graph_record_inventory(paths)
    recover_unindexed_graph_record(paths, inventory)
    require_consistent_graph_record_sequence(paths, inventory)
    return paths.graph_record_sequence


def _bounded_graph_record_projection(
    *,
    sequence_locator: dict[str, Any],
    intent_history: list[dict[str, Any]],
    intent_items: list[dict[str, Any]],
    plan_history: list[dict[str, Any]],
    plan_items: list[dict[str, Any]],
    draft_items: list[dict[str, Any]],
    max_bytes: int = GRAPH_RECORD_LOCATOR_BUDGET_BYTES,
) -> dict[str, Any]:
    """Select deterministic locator prefixes within one shared byte budget."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValidationError("graph-record locator byte budget must be a positive integer")

    intent_inventory_sha256 = sha256_json(intent_history)
    plan_inventory_sha256 = sha256_json(plan_history)
    draft_inventory_sha256 = sha256_json(draft_items)

    selected_intents: list[dict[str, Any]] = []
    selected_plans: list[dict[str, Any]] = []
    selected_drafts: list[dict[str, Any]] = []

    def projection() -> dict[str, Any]:
        return {
            "sequence": sequence_locator,
            "experiment_intents": {
                "total_count": len(intent_history),
                "current_count": len(intent_items),
                "selected_count": len(selected_intents),
                "items": list(selected_intents),
                "truncated": len(selected_intents) != len(intent_items),
                "inventory_sha256": intent_inventory_sha256,
            },
            "control_graphs": {
                "total_count": len(plan_history),
                "current_count": len(plan_items),
                "selected_count": len(selected_plans),
                "items": list(selected_plans),
                "truncated": len(selected_plans) != len(plan_items),
                "inventory_sha256": plan_inventory_sha256,
            },
            "workspace_drafts": {
                "total_count": len(draft_items),
                "selected_count": len(selected_drafts),
                "items": list(selected_drafts),
                "truncated": len(selected_drafts) != len(draft_items),
                "inventory_sha256": draft_inventory_sha256,
                "assurance": "mutable_non_authoritative",
            },
        }

    bounded = projection()
    if len(canonical_json_bytes(bounded)) > max_bytes:
        raise ValidationError(
            "graph-record locator metadata alone exceeds its canonical byte budget"
        )

    categories = (
        (intent_items, selected_intents),
        (plan_items, selected_plans),
        (draft_items, selected_drafts),
    )
    blocked = [False, False, False]
    while True:
        added = False
        for index, (available, selected) in enumerate(categories):
            if blocked[index] or len(selected) >= len(available):
                blocked[index] = True
                continue
            selected.append(available[len(selected)])
            candidate = projection()
            if len(canonical_json_bytes(candidate)) <= max_bytes:
                bounded = candidate
                added = True
            else:
                selected.pop()
                blocked[index] = True
        if not added:
            return bounded


def current_graph_record_locators(paths: StudyPaths) -> dict[str, Any]:
    """Return bounded exact locators; record bodies stay outside active context."""

    _require_graph_record_history(paths)
    from .graph_record_sequence import require_graph_record_sequence

    sequence = require_graph_record_sequence(paths)
    sequence_locator = {
        "path": paths.graph_record_sequence.relative_to(paths.root).as_posix(),
        "size": paths.graph_record_sequence.stat().st_size,
        "file_sha256": sha256_file(paths.graph_record_sequence),
        "high_water_mark": sequence["high_water_mark"],
        "inventory_sha256": sequence["inventory_sha256"],
    }
    intent_items: list[dict[str, Any]] = []
    plan_items: list[dict[str, Any]] = []
    intent_history: list[dict[str, Any]] = []
    plan_history: list[dict[str, Any]] = []
    draft_items: list[dict[str, Any]] = []
    intent_ids = sorted(
        {
            match.group(1)
            for path in experiment_intent_paths(paths)
            if (match := _INTENT_FINAL.fullmatch(path.name)) is not None
        }
    )
    for intent_id in intent_ids:
        records = _versioned_final_records(
            paths, record_id=intent_id, kind="experiment_intent"
        )
        if not records:
            continue
        for _, path, record in records:
            intent_history.append(
                {
                    **_record_ref(record, kind="Experiment Intent"),
                    "path": path.relative_to(paths.root).as_posix(),
                    "size": path.stat().st_size,
                }
            )
        latest = records[-1][2]
        path = (
            paths.experiment_intents
            / f"{intent_id}.v{int(latest['version']):04d}.json"
        )
        intent_items.append(
            {
                **_record_ref(latest, kind="Experiment Intent"),
                "path": path.relative_to(paths.root).as_posix(),
                "size": path.stat().st_size,
            }
        )
    plan_ids = sorted(
        {
            match.group(1)
            for path in control_graph_paths(paths)
            if (match := _PLAN_FINAL.fullmatch(path.name)) is not None
        }
    )
    for plan_id in plan_ids:
        records = _versioned_final_records(
            paths, record_id=plan_id, kind="control_graph"
        )
        if not records:
            continue
        for _, path, record in records:
            plan_history.append(
                {
                    **_record_ref(record, kind="Control Graph"),
                    "realizes_intent": record["realizes_intent"],
                    "path": path.relative_to(paths.root).as_posix(),
                    "size": path.stat().st_size,
                }
            )
        latest = records[-1][2]
        path = paths.control_graphs / f"{plan_id}.v{int(latest['version']):04d}.json"
        plan_items.append(
            {
                **_record_ref(latest, kind="Control Graph"),
                "realizes_intent": latest["realizes_intent"],
                "path": path.relative_to(paths.root).as_posix(),
                "size": path.stat().st_size,
            }
        )
    for kind, draft_paths, pattern, final_root in (
        (
            "experiment_intent",
            experiment_intent_draft_paths(paths),
            _INTENT_DRAFT,
            paths.experiment_intents,
        ),
        (
            "control_graph_spec",
            control_graph_draft_paths(paths),
            _PLAN_DRAFT,
            paths.control_graphs,
        ),
    ):
        for path in draft_paths:
            match = pattern.fullmatch(path.name)
            if match is None:
                continue
            record_id = match.group(1)
            version = int(match.group(2))
            if (final_root / f"{record_id}.v{version:04d}.json").exists():
                continue
            draft_items.append(
                {
                    "kind": kind,
                    "id": record_id,
                    "version": version,
                    "path": path.relative_to(paths.root).as_posix(),
                    "size": path.stat().st_size,
                    "file_sha256": sha256_file(path),
                    "assurance": "mutable_non_authoritative",
                }
            )
    draft_items.sort(key=lambda item: (item["kind"], item["id"], item["version"]))
    return _bounded_graph_record_projection(
        sequence_locator=sequence_locator,
        intent_history=intent_history,
        intent_items=intent_items,
        plan_history=plan_history,
        plan_items=plan_items,
        draft_items=draft_items,
    )
