from __future__ import annotations

from contextlib import contextmanager
import copy
import os
from pathlib import Path
import re
from typing import Any, Iterator, Sequence

from .hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from .locking import serialized_study_authority
from .models import (
    OBSERVATION_SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
    WorkflowError,
    require_id,
    utc_now,
)
from .observation_sequence import reserve_observation_creation
from .observation_triggers import (
    load_bound_registry,
    load_current_registry,
    normalize_registered_triggers,
    registry_binding,
    selected_trigger_definitions,
    validate_trigger_applicability,
)
from .validation import object_schema_issues


_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "interrupted", "incomplete"}
_EVIDENCE_DISPOSITIONS = {"included", "anomaly"}
def observation_paths(paths: StudyPaths) -> list[Path]:
    if not paths.observations.is_dir():
        return []
    return sorted(paths.observations.glob("OBS-*.v*.json"))


def observation_index(
    paths: StudyPaths,
) -> dict[tuple[str, int], tuple[Path, dict[str, Any]]]:
    records: dict[tuple[str, int], tuple[Path, dict[str, Any]]] = {}
    for path in observation_paths(paths):
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(f"Observation must be an object: {path}")
        observation_id = str(value.get("observation_id", ""))
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValidationError(f"Observation version is invalid: {path}")
        records[(observation_id, version)] = (path, value)
    return records


def analysis_fingerprint(item: dict[str, Any]) -> str:
    """Bind the source Run set, Cohorts, selection, and analysis method."""

    return sha256_json(
        {
            "runs": item.get("runs", []),
            "cohorts": item.get("cohorts", []),
            "analysis": item.get("analysis", {}),
        }
    )


def _invalid_object_message(kind: str, issues: Sequence[Any]) -> str:
    return f"invalid {kind}:\n" + "\n".join(issue.render() for issue in issues)


def _normalize_run_ids(run_ids: Sequence[str]) -> list[str]:
    if isinstance(run_ids, (str, bytes)):
        raise ValidationError("Run IDs must be supplied as a sequence")
    normalized: list[str] = []
    seen: set[str] = set()
    for run_id in run_ids:
        if not isinstance(run_id, str):
            raise ValidationError("Run ID must be a string")
        require_id("run", run_id)
        if run_id in seen:
            raise ValidationError(f"duplicate Run ID: {run_id}")
        seen.add(run_id)
        normalized.append(run_id)
    if not normalized:
        raise ValidationError("at least one Run ID is required")
    return normalized


def _load_terminal_run(paths: StudyPaths, run_id: str) -> dict[str, Any]:
    require_id("run", run_id)
    manifest_path = paths.runs / run_id / "manifest.json"
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValidationError(f"Run manifest must be an object: {manifest_path}")
    if manifest.get("study_id") != paths.study_id or manifest.get("run_id") != run_id:
        raise ValidationError(f"Run identity does not match Observation source: {run_id}")
    if manifest.get("status") not in _TERMINAL_RUN_STATUSES:
        raise ValidationError(
            f"Observation may reference only terminal Runs: {run_id}"
        )
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise ValidationError(f"Run {run_id} has no integrity record")
    digest = integrity.get("manifest_sha256")
    if (
        not isinstance(digest, str)
        or digest != nested_record_digest(manifest, "integrity", "manifest_sha256")
    ):
        raise ValidationError(f"Run manifest integrity check failed: {run_id}")
    cohort = manifest.get("cohort")
    if (
        not isinstance(cohort, dict)
        or not isinstance(cohort.get("fields"), dict)
        or cohort.get("fingerprint_sha256") != sha256_json(cohort["fields"])
    ):
        raise ValidationError(f"Run Cohort fingerprint is invalid: {run_id}")
    return manifest


def _changed_cohort_fields(manifests: Sequence[dict[str, Any]]) -> list[str]:
    fields = [manifest["cohort"]["fields"] for manifest in manifests]
    keys = sorted({str(key) for item in fields for key in item})
    return [
        key
        for key in keys
        if len(
            {
                ("value", sha256_json(item[key]))
                if key in item
                else ("missing", "")
                for item in fields
            }
        )
        > 1
    ]


def _cohort_refs(manifests: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    records = {
        (
            manifest["cohort"].get("cohort_id"),
            str(manifest["cohort"]["fingerprint_sha256"]),
        )
        for manifest in manifests
    }
    return [
        {"cohort_id": cohort_id, "fingerprint_sha256": fingerprint}
        for cohort_id, fingerprint in sorted(
            records, key=lambda item: (str(item[0]), item[1])
        )
    ]


@contextmanager
def _observation_lock(
    paths: StudyPaths, observation_id: str
) -> Iterator[None]:
    paths.observations.mkdir(parents=True, exist_ok=True)
    lock_path = paths.observations / f".{observation_id}.lock"
    try:
        descriptor = os.open(
            lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError as exc:
        raise WorkflowError(
            f"another operation is active for Observation {observation_id}"
        ) from exc
    os.close(descriptor)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _versions(
    paths: StudyPaths, observation_id: str
) -> list[tuple[int, Path, dict[str, Any]]]:
    pattern = re.compile(
        rf"^{re.escape(observation_id)}\.v([0-9]{{4,}})\.json$"
    )
    versions: list[tuple[int, Path, dict[str, Any]]] = []
    for path in sorted(paths.observations.glob(f"{observation_id}.v*.json")):
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ValidationError(
                f"malformed Observation version filename: {path.name}"
            )
        version = int(match.group(1))
        if version < 1 or path.name != f"{observation_id}.v{version:04d}.json":
            raise ValidationError(
                f"non-canonical Observation version filename: {path.name}"
            )
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(f"Observation must be an object: {path}")
        if (
            value.get("study_id") != paths.study_id
            or value.get("observation_id") != observation_id
            or value.get("version") != version
        ):
            raise ValidationError(
                f"Observation identity/version does not match filename: {path}"
            )
        versions.append((version, path, value))
    return versions


@serialized_study_authority
def create_observation_draft(
    paths: StudyPaths,
    observation_id: str,
    run_ids: Sequence[str],
    promotion_triggers: Sequence[str],
) -> Path:
    """Create an optional analysis record only after an explicit promotion."""

    require_id("observation", observation_id)
    normalized_runs = _normalize_run_ids(run_ids)
    registry = load_current_registry(paths.root)
    normalized_triggers = normalize_registered_triggers(
        registry, promotion_triggers
    )
    manifests = [_load_terminal_run(paths, run_id) for run_id in normalized_runs]
    if "multiple_runs" in normalized_triggers and len(manifests) < 2:
        raise ValidationError(
            "promotion trigger multiple_runs requires at least two Runs"
        )
    cohorts = _cohort_refs(manifests)
    if "multiple_cohorts" in normalized_triggers and len(cohorts) < 2:
        raise ValidationError(
            "promotion trigger multiple_cohorts requires at least two Cohorts"
        )
    changed_fields = _changed_cohort_fields(manifests)
    with _observation_lock(paths, observation_id):
        versions = _versions(paths, observation_id)
        drafts = [
            path for _, path, value in versions if value.get("status") == "draft"
        ]
        if drafts:
            raise WorkflowError(
                f"Observation {observation_id} already has an open draft: {drafts[0]}"
            )
        version = max((number for number, _, _ in versions), default=0) + 1
        timestamp = utc_now()
        draft: dict[str, Any] = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "study_id": paths.study_id,
            "observation_id": observation_id,
            "version": version,
            "status": "draft",
            "created_at": timestamp,
            "updated_at": timestamp,
            "promotion": {
                "registry": registry_binding(registry),
                "triggers": normalized_triggers,
                "rationale": None,
            },
            "runs": [
                {
                    "run_id": run_id,
                    "manifest_sha256": manifest["integrity"]["manifest_sha256"],
                    "disposition": "included",
                    "rationale": None,
                }
                for run_id, manifest in zip(
                    normalized_runs, manifests, strict=True
                )
            ],
            "cohorts": cohorts,
            "analysis": {
                "method": None,
                "implementation_sha256": None,
                "evaluator_sha256": None,
                "inclusion_rule": None,
                "exclusion_rule": None,
                "aggregation_rule": None,
                "changed_cohort_fields": changed_fields,
                "cohort_compatibility_justification": None,
            },
            "results": {
                "primary": None,
                "secondary": [],
                "distribution": None,
                "boundary_cases": [],
            },
            "uncertainty": {
                "statistical": None,
                "numerical": None,
                "measurement": None,
            },
            "scope": None,
            "anomalies": [],
            "representative_failures": [],
            "analysis_assumptions": [],
            "limitations": [],
            "analysis_fingerprint_sha256": None,
            "record_sha256": None,
        }
        destination = (
            paths.observations / f"{observation_id}.v{version:04d}.json"
        )
        issues = object_schema_issues(
            paths.root, "observation", destination, draft
        )
        if issues:
            raise ValidationError(
                _invalid_object_message("generated Observation draft", issues)
            )
        reserve_observation_creation(paths)
        atomic_write_json(destination, draft, overwrite=False)
        return destination


def _require_nonblank(label: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"finalized Observation requires explicit {label}")


def validate_observation_content(
    paths: StudyPaths,
    item: dict[str, Any],
    *,
    require_finalized: bool,
) -> list[dict[str, Any]]:
    """Validate source bindings and analysis semantics for one Observation."""

    manifests: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    dispositions: dict[str, str] = {}
    for run_ref in item.get("runs", []):
        if not isinstance(run_ref, dict):
            raise ValidationError("Observation Run reference must be an object")
        run_id = str(run_ref.get("run_id", ""))
        if run_id in seen_runs:
            raise ValidationError(f"Observation repeats Run reference: {run_id}")
        seen_runs.add(run_id)
        manifest = _load_terminal_run(paths, run_id)
        if run_ref.get("manifest_sha256") != manifest["integrity"][
            "manifest_sha256"
        ]:
            raise ValidationError(
                f"Observation Run manifest hash is stale: {run_id}"
            )
        dispositions[run_id] = str(run_ref.get("disposition"))
        manifests.append(manifest)

    expected_cohorts = _cohort_refs(manifests)
    if item.get("cohorts") != expected_cohorts:
        raise ValidationError(
            "Observation cohorts do not exactly match the source Runs"
        )
    analysis = item.get("analysis")
    if not isinstance(analysis, dict):
        raise ValidationError("Observation analysis must be an object")
    expected_changed = _changed_cohort_fields(manifests)
    if analysis.get("changed_cohort_fields") != expected_changed:
        raise ValidationError(
            "Observation changed_cohort_fields do not exactly match the source Runs"
        )
    if len(expected_cohorts) > 1:
        _require_nonblank(
            "analysis.cohort_compatibility_justification",
            analysis.get("cohort_compatibility_justification"),
        )
    elif analysis.get("cohort_compatibility_justification") not in {None, ""}:
        raise ValidationError(
            "single-Cohort Observation must not claim cross-Cohort compatibility"
        )

    anomaly_ids = [
        str(value.get("run_id"))
        for value in item.get("anomalies", [])
        if isinstance(value, dict)
    ]
    if len(anomaly_ids) != len(set(anomaly_ids)):
        raise ValidationError("Observation repeats an anomaly Run")
    expected_anomalies = {
        run_id
        for run_id, disposition in dispositions.items()
        if disposition == "anomaly"
    }
    if set(anomaly_ids) != expected_anomalies:
        raise ValidationError(
            "Observation anomalies must exactly match Runs with anomaly disposition"
        )
    failures = item.get("representative_failures", [])
    if len(failures) != len(set(failures)):
        raise ValidationError("Observation repeats a representative failure")
    expected_failures = {
        run_id
        for run_id, disposition in dispositions.items()
        if disposition == "representative_failure"
    }
    if set(failures) != expected_failures:
        raise ValidationError(
            "Observation representative_failures must exactly match Runs with "
            "representative_failure disposition"
        )
    manifests_by_id = {
        str(manifest["run_id"]): manifest for manifest in manifests
    }
    for run_id in expected_failures:
        if manifests_by_id[run_id].get("status") not in {
            "failed",
            "interrupted",
            "incomplete",
        }:
            raise ValidationError(
                f"representative failure Run is not failed/interrupted/incomplete: {run_id}"
            )
    for run_ref in item.get("runs", []):
        if (
            isinstance(run_ref, dict)
            and run_ref.get("disposition")
            in {"excluded", "anomaly", "representative_failure"}
        ):
            _require_nonblank(
                f"runs[{run_ref.get('run_id')}].rationale",
                run_ref.get("rationale"),
            )

    promotion = item.get("promotion")
    if not isinstance(promotion, dict):
        raise ValidationError("Observation promotion must be an object")
    registry = load_bound_registry(paths.root, promotion.get("registry"))
    normalized_triggers = normalize_registered_triggers(
        registry, promotion.get("triggers", [])
    )
    trigger_definitions = selected_trigger_definitions(
        registry, normalized_triggers
    )
    from .run_registry import effective_run_mode

    contains_confirmatory = any(
        effective_run_mode(manifest) == "confirmatory"
        for manifest in manifests
    )
    validate_trigger_applicability(
        trigger_definitions,
        run_count=len(manifests),
        cohort_count=len(expected_cohorts),
        anomaly_count=len(expected_anomalies),
        representative_failure_count=len(expected_failures),
        contains_confirmatory=contains_confirmatory,
    )
    if require_finalized:
        _require_nonblank("promotion.rationale", promotion.get("rationale"))
        for field in (
            "method",
            "inclusion_rule",
            "exclusion_rule",
            "aggregation_rule",
        ):
            _require_nonblank(f"analysis.{field}", analysis.get(field))
        _require_nonblank("scope", item.get("scope"))
        if item.get("results", {}).get("primary") is None:
            raise ValidationError(
                "finalized Observation requires an explicit results.primary"
            )
    return manifests


def load_final_observation(
    paths: StudyPaths, observation_id: str, version: int
) -> dict[str, Any]:
    require_id("observation", observation_id)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("Observation version must be a positive integer")
    path = paths.observations / f"{observation_id}.v{version:04d}.json"
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValidationError(f"Observation must be an object: {path}")
    if (
        value.get("observation_id") != observation_id
        or value.get("version") != version
        or value.get("study_id") != paths.study_id
    ):
        raise ValidationError(f"Observation identity does not match: {path}")
    if value.get("status") != "finalized":
        raise ValidationError(f"Observation is not finalized: {path}")
    if value.get("record_sha256") != record_digest(value, "record_sha256"):
        raise ValidationError(f"Observation integrity check failed: {path}")
    validate_observation_content(paths, value, require_finalized=True)
    return value


def observation_ref(
    paths: StudyPaths, observation_id: str, version: int
) -> dict[str, Any]:
    value = load_final_observation(paths, observation_id, version)
    return {
        "observation_id": observation_id,
        "version": version,
        "sha256": value["record_sha256"],
    }


def validate_evidence_observation_ref(
    paths: StudyPaths,
    reference: Any,
    evidence_runs: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate an optional exact Observation binding used by Evidence."""

    if reference is None:
        return None
    if not isinstance(reference, dict):
        raise ValidationError("Evidence observation_ref must be an object or null")
    observation_id = str(reference.get("observation_id", ""))
    version = reference.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationError("Evidence Observation version is invalid")
    value = load_final_observation(paths, observation_id, version)
    if reference.get("sha256") != value.get("record_sha256"):
        raise ValidationError(
            f"Evidence Observation reference is stale: {observation_id} v{version}"
        )
    eligible_sources = {
        (str(run_ref["run_id"]), str(run_ref["manifest_sha256"]))
        for run_ref in value.get("runs", [])
        if isinstance(run_ref, dict)
        and run_ref.get("disposition") in _EVIDENCE_DISPOSITIONS
    }
    evidence_sources = {
        (str(run_ref.get("run_id")), str(run_ref.get("manifest_sha256")))
        for run_ref in evidence_runs
        if isinstance(run_ref, dict)
    }
    if not evidence_sources.issubset(eligible_sources):
        raise ValidationError(
            "Evidence Runs must be an exact-hash subset of included or anomaly "
            "Runs in its Observation Record"
        )
    return value


@serialized_study_authority
def finalize_observation(paths: StudyPaths, source_path: Path) -> Path:
    source = source_path.resolve()
    item = load_json(source)
    if not isinstance(item, dict):
        raise ValidationError("Observation source must be a JSON object")
    issues = object_schema_issues(paths.root, "observation", source, item)
    if issues:
        raise ValidationError(_invalid_object_message("Observation source", issues))
    if item.get("study_id") != paths.study_id:
        raise ValidationError("Observation source study_id does not match Study")
    observation_id = str(item.get("observation_id", ""))
    require_id("observation", observation_id)
    version = item.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("Observation version must be a positive integer")
    if item.get("status") != "draft":
        raise ValidationError(
            "observation-finalize accepts only a draft Observation source"
        )
    if (
        item.get("record_sha256") is not None
        or item.get("analysis_fingerprint_sha256") is not None
    ):
        raise ValidationError("draft Observation digest fields must be null")
    destination = (
        paths.observations / f"{observation_id}.v{version:04d}.json"
    )
    with _observation_lock(paths, observation_id):
        current = next(
            (
                value
                for number, path, value in _versions(paths, observation_id)
                if number == version and path == destination
            ),
            None,
        )
        if current is None:
            raise ValidationError(
                f"no authoritative Observation draft exists at {destination}"
            )
        if current.get("status") != "draft":
            raise WorkflowError(
                f"refusing to overwrite finalized Observation: {destination}"
            )
        if item.get("created_at") != current.get("created_at"):
            raise ValidationError(
                "Observation source does not match the authoritative draft"
            )
        validate_observation_content(paths, item, require_finalized=True)
        fingerprint = analysis_fingerprint(item)
        for (other_id, _), (_, existing) in observation_index(paths).items():
            if (
                other_id != observation_id
                and existing.get("status") == "finalized"
                and existing.get("analysis_fingerprint_sha256") == fingerprint
            ):
                raise ValidationError(
                    "duplicate Observation analysis fingerprint already exists "
                    f"under {other_id}; reuse that Observation instead"
                )
        finalized = copy.deepcopy(item)
        finalized["status"] = "finalized"
        finalized["updated_at"] = utc_now()
        finalized["analysis_fingerprint_sha256"] = fingerprint
        finalized["record_sha256"] = record_digest(finalized, "record_sha256")
        final_issues = object_schema_issues(
            paths.root, "observation", destination, finalized
        )
        if final_issues:
            raise ValidationError(
                _invalid_object_message("finalized Observation", final_issues)
            )
        initial_digest = sha256_file(destination)

        def _ensure_draft_unchanged(_temporary_path: Path) -> None:
            latest = load_json(destination)
            if not isinstance(latest, dict) or latest.get("status") != "draft":
                raise WorkflowError(
                    f"refusing to overwrite non-draft Observation: {destination}"
                )
            if sha256_file(destination) != initial_digest:
                raise WorkflowError(
                    f"Observation draft changed during finalization: {destination}"
                )

        atomic_write_json(
            destination,
            finalized,
            overwrite=True,
            mode=0o444,
            before_replace=_ensure_draft_unchanged,
        )
        return destination
