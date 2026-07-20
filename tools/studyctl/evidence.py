from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
import os
from pathlib import Path
import re
from typing import Any, Iterator

from .formalization import check_formalization
from .hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from .models import (
    SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
    WorkflowError,
    require_id,
    utc_now,
)
from .validation import (
    brief_approval_issues,
    brief_content_issues,
    errors_only,
    object_schema_issues,
    run_dependency_integrity_issues,
    sealed_run_evidence_eligible,
)


_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "interrupted"}


def _invalid_object_message(kind: str, issues: Sequence[Any]) -> str:
    return f"invalid {kind}:\n" + "\n".join(issue.render() for issue in issues)


def _normalize_ids(kind: str, values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{kind} IDs must be supplied as a sequence")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValidationError(f"{kind} ID must be a string: {value!r}")
        require_id(kind, value)
        if value in seen:
            raise ValidationError(f"duplicate {kind} ID: {value}")
        seen.add(value)
        normalized.append(value)
    if not normalized:
        raise ValidationError(f"at least one {kind} ID is required")
    return normalized


def _claims_object(paths: StudyPaths) -> dict[str, Any]:
    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must be an object")
    if claims.get("study_id") != paths.study_id:
        raise ValidationError("CLAIMS.json study_id does not match Study")
    return claims


def _validate_claim_references(paths: StudyPaths, claim_ids: Sequence[str]) -> None:
    requested = _normalize_ids("claim", claim_ids)
    claims = _claims_object(paths)
    available: set[str] = set()
    for claim in claims.get("claims", []):
        if not isinstance(claim, dict):
            raise ValidationError("CLAIMS.json contains a non-object Claim")
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str):
            raise ValidationError("CLAIMS.json contains a Claim without a string claim_id")
        require_id("claim", claim_id)
        if claim_id in available:
            raise ValidationError(f"CLAIMS.json contains duplicate Claim ID: {claim_id}")
        available.add(claim_id)
    missing = sorted(set(requested) - available)
    if missing:
        raise ValidationError(f"Evidence references missing Claim(s): {', '.join(missing)}")


def _terminal_run(paths: StudyPaths, run_id: str) -> dict[str, Any]:
    require_id("run", run_id)
    manifest_path = paths.runs / run_id / "manifest.json"
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValidationError(f"Run manifest must be an object: {manifest_path}")
    schema_issues = object_schema_issues(paths.root, "run", manifest_path, manifest)
    if schema_issues:
        raise ValidationError(_invalid_object_message(f"Run manifest {run_id}", schema_issues))
    if manifest.get("study_id") != paths.study_id:
        raise ValidationError(f"Run {run_id} belongs to a different Study")
    if manifest.get("run_id") != run_id:
        raise ValidationError(f"Run manifest identity does not match reference: {run_id}")
    status = manifest.get("status")
    if status not in _TERMINAL_RUN_STATUSES:
        raise ValidationError(f"Evidence may reference only terminal Runs: {run_id} is {status!r}")
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise ValidationError(f"Run {run_id} has no integrity record")
    manifest_digest = integrity.get("manifest_sha256")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        raise ValidationError(f"terminal Run {run_id} has no manifest_sha256")
    expected_digest = nested_record_digest(manifest, "integrity", "manifest_sha256")
    if manifest_digest != expected_digest:
        raise ValidationError(f"Run manifest integrity check failed: {run_id}")
    if not str(integrity.get("sealed_at") or "").strip():
        raise ValidationError(f"terminal Run {run_id} has no sealed_at timestamp")
    code_state = manifest.get("code_state", {})
    expected_code_change = code_state.get("before") != code_state.get("after")
    if code_state.get("changed_during_run") != expected_code_change:
        raise ValidationError(f"Run tracked-code change flag is invalid: {run_id}")
    if expected_code_change:
        raise ValidationError(
            f"Evidence cannot use a Run whose tracked code changed during execution: {run_id}"
        )
    cohort = manifest.get("cohort")
    if not isinstance(cohort, dict) or not isinstance(cohort.get("fields"), dict):
        raise ValidationError(f"Run {run_id} has no valid Cohort fields")
    fingerprint = cohort.get("fingerprint_sha256")
    if fingerprint != sha256_json(cohort["fields"]):
        raise ValidationError(f"Run Cohort fingerprint is invalid: {run_id}")
    dependency_issues = errors_only(
        run_dependency_integrity_issues(paths, manifest, for_evidence=True)
    )
    if dependency_issues:
        raise ValidationError(
            _invalid_object_message(
                f"Run dependency integrity for Evidence {run_id}",
                dependency_issues,
            )
        )
    expected_eligibility = sealed_run_evidence_eligible(manifest)
    recorded_eligibility = manifest.get("change_scope", {}).get("evidence_eligible")
    if recorded_eligibility is not expected_eligibility:
        raise ValidationError(
            f"Run sealed Evidence eligibility is internally inconsistent: {run_id}"
        )
    if not expected_eligibility:
        raise ValidationError(
            f"Evidence cannot use a Run with unverifiable or blocked change scope: {run_id}"
        )
    return manifest


def _changed_cohort_fields(manifests: Sequence[dict[str, Any]]) -> list[str]:
    fields = [manifest["cohort"]["fields"] for manifest in manifests]
    keys = sorted({str(key) for item in fields for key in item})
    changed: list[str] = []
    for key in keys:
        signatures = [
            ("value", sha256_json(item[key])) if key in item else ("missing", "")
            for item in fields
        ]
        if any(signature != signatures[0] for signature in signatures[1:]):
            changed.append(key)
    return changed


def _run_references_for_draft(
    paths: StudyPaths, run_ids: Sequence[str]
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    normalized = _normalize_ids("run", run_ids)
    manifests = [_terminal_run(paths, run_id) for run_id in normalized]
    references = [
        {
            "run_id": run_id,
            "manifest_sha256": str(manifest["integrity"]["manifest_sha256"]),
            "role": "context",
        }
        for run_id, manifest in zip(normalized, manifests, strict=True)
    ]
    fingerprints = sorted(
        {str(manifest["cohort"]["fingerprint_sha256"]) for manifest in manifests}
    )
    changed_fields = _changed_cohort_fields(manifests) if len(fingerprints) > 1 else []
    return references, fingerprints, changed_fields


@contextmanager
def _evidence_lock(paths: StudyPaths, evidence_id: str) -> Iterator[None]:
    paths.evidence.mkdir(parents=True, exist_ok=True)
    lock_path = paths.evidence / f".{evidence_id}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkflowError(f"another operation is active for Evidence {evidence_id}") from exc
    os.close(lock_fd)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _versions(paths: StudyPaths, evidence_id: str) -> list[tuple[int, Path, dict[str, Any]]]:
    pattern = re.compile(rf"^{re.escape(evidence_id)}\.v([0-9]{{4,}})\.json$")
    versions: list[tuple[int, Path, dict[str, Any]]] = []
    for path in sorted(paths.evidence.glob(f"{evidence_id}.v*.json")):
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ValidationError(f"malformed Evidence version filename: {path.name}")
        version = int(match.group(1))
        if version < 1 or path.name != f"{evidence_id}.v{version:04d}.json":
            raise ValidationError(f"non-canonical Evidence version filename: {path.name}")
        item = load_json(path)
        if not isinstance(item, dict):
            raise ValidationError(f"Evidence must be an object: {path}")
        if item.get("study_id") != paths.study_id:
            raise ValidationError(f"Evidence belongs to a different Study: {path}")
        if item.get("evidence_id") != evidence_id or item.get("version") != version:
            raise ValidationError(f"Evidence identity/version does not match filename: {path}")
        if item.get("status") not in {"draft", "finalized"}:
            raise ValidationError(f"Evidence has invalid status: {path}")
        versions.append((version, path, item))
    return versions


def create_evidence_draft(
    paths: StudyPaths,
    evidence_id: str,
    claim_ids: Sequence[str],
    run_ids: Sequence[str],
) -> Path:
    """Create the next editable version of an Evidence record."""
    require_id("evidence", evidence_id)
    normalized_claim_ids = _normalize_ids("claim", claim_ids)
    _validate_claim_references(paths, normalized_claim_ids)
    run_refs, fingerprints, changed_fields = _run_references_for_draft(paths, run_ids)
    with _evidence_lock(paths, evidence_id):
        versions = _versions(paths, evidence_id)
        drafts = [path for _, path, item in versions if item.get("status") == "draft"]
        if drafts:
            raise WorkflowError(
                f"Evidence {evidence_id} already has an open draft: {drafts[0]}"
            )
        version = max((number for number, _, _ in versions), default=0) + 1
        timestamp = utc_now()
        draft: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "study_id": paths.study_id,
            "evidence_id": evidence_id,
            "version": version,
            "status": "draft",
            "created_at": timestamp,
            "updated_at": timestamp,
            "addresses": {
                "claim_ids": normalized_claim_ids,
                "question": None,
            },
            "runs": run_refs,
            "analysis": {
                "method": None,
                "comparison": {
                    "mode": "single_cohort" if len(fingerprints) == 1 else "compatible_cohorts",
                    "cohort_fingerprints": fingerprints,
                    "changed_fields": changed_fields,
                    "compatibility_justification": None,
                },
            },
            "result": None,
            "scope": None,
            "uncertainty": None,
            "limitations": [],
            "assessment": None,
            "related_evidence": {
                "supporting": [],
                "contradictory": [],
            },
            "record_sha256": None,
        }
        destination = paths.evidence / f"{evidence_id}.v{version:04d}.json"
        schema_issues = object_schema_issues(paths.root, "evidence", destination, draft)
        if schema_issues:
            raise ValidationError(_invalid_object_message("generated Evidence draft", schema_issues))
        atomic_write_json(destination, draft, overwrite=False)
        return destination


def _validate_run_references(
    paths: StudyPaths, item: dict[str, Any]
) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    fingerprints: set[str] = set()
    manifests: list[dict[str, Any]] = []
    for run_ref in item.get("runs", []):
        run_id = str(run_ref.get("run_id"))
        if run_id in seen:
            raise ValidationError(f"Evidence repeats Run reference: {run_id}")
        seen.add(run_id)
        manifest = _terminal_run(paths, run_id)
        recorded_digest = manifest["integrity"]["manifest_sha256"]
        if run_ref.get("manifest_sha256") != recorded_digest:
            raise ValidationError(f"Evidence has a stale Run manifest reference: {run_id}")
        if any(record.get("changed_during_run") for record in manifest.get("inputs", [])):
            raise ValidationError(
                f"finalized Evidence cannot use a Run whose input changed: {run_id}"
            )
        if manifest.get("code_state", {}).get("changed_during_run"):
            raise ValidationError(
                f"finalized Evidence cannot use a Run whose tracked code changed: {run_id}"
            )
        if not manifest.get("change_scope", {}).get("evidence_eligible", False):
            raise ValidationError(
                f"finalized Evidence cannot use a Run with unverifiable or blocked change scope: {run_id}"
            )
        manifests.append(manifest)
        fingerprints.add(str(manifest["cohort"]["fingerprint_sha256"]))
    declared = item.get("analysis", {}).get("comparison", {}).get(
        "cohort_fingerprints", []
    )
    if len(declared) != len(set(declared)) or set(declared) != fingerprints:
        raise ValidationError("declared Cohort fingerprints do not exactly match referenced Runs")
    changed_fields = _changed_cohort_fields(manifests) if len(fingerprints) > 1 else []
    return sorted(fingerprints), changed_fields


def _validate_related_evidence(paths: StudyPaths, item: dict[str, Any]) -> None:
    current_key = (str(item["evidence_id"]), int(item["version"]))
    seen: dict[tuple[str, int], str] = {}
    related = item.get("related_evidence", {})
    for role in ("supporting", "contradictory"):
        for reference in related.get(role, []):
            evidence_id = str(reference.get("evidence_id"))
            version = int(reference.get("version", 0))
            key = (evidence_id, version)
            if key == current_key:
                raise ValidationError("Evidence cannot reference itself as related Evidence")
            previous_role = seen.setdefault(key, role)
            if previous_role != role:
                raise ValidationError(
                    f"related Evidence {evidence_id} v{version} appears in conflicting roles"
                )
            elif sum(
                1
                for candidate in related.get(role, [])
                if (
                    str(candidate.get("evidence_id")),
                    int(candidate.get("version", 0)),
                )
                == key
            ) > 1:
                raise ValidationError(
                    f"related Evidence reference is duplicated: {evidence_id} v{version}"
                )
            target = paths.evidence / f"{evidence_id}.v{version:04d}.json"
            existing = load_json(target)
            if not isinstance(existing, dict):
                raise ValidationError(f"related Evidence must be an object: {target}")
            if existing.get("study_id") != paths.study_id:
                raise ValidationError(f"related Evidence belongs to a different Study: {target}")
            if existing.get("evidence_id") != evidence_id or existing.get("version") != version:
                raise ValidationError(f"related Evidence identity does not match filename: {target}")
            if existing.get("status") != "finalized":
                raise ValidationError(f"related Evidence is not finalized: {target}")
            digest = existing.get("record_sha256")
            if digest != record_digest(existing, "record_sha256"):
                raise ValidationError(f"related Evidence integrity check failed: {target}")
            if reference.get("sha256") != digest:
                raise ValidationError(f"related Evidence reference is stale: {target}")


def _require_nonblank(name: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"finalized Evidence requires explicit {name}")


def _require_evidence_formalization(
    paths: StudyPaths,
    item: dict[str, Any] | None = None,
) -> None:
    changed_paths: list[str] = []
    scientific_critical = False
    shared_across_runs = False
    if item is not None:
        for run_ref in item.get("runs", []):
            manifest = _terminal_run(paths, str(run_ref.get("run_id")))
            context = manifest.get("formalization", {})
            for path in context.get("changed_paths", []):
                if path not in changed_paths:
                    changed_paths.append(path)
            scientific_critical = scientific_critical or bool(
                context.get("scientific_critical")
            )
            shared_across_runs = shared_across_runs or bool(
                context.get("shared_across_runs")
            )
    formalization = check_formalization(
        paths,
        {
            "for_evidence": True,
            "changed_path": changed_paths,
            "scientific_critical": scientific_critical,
            "shared_across_runs": shared_across_runs,
        },
    )
    if formalization.blocked:
        details = "\n".join(
            f"- {requirement['level']}: {requirement['artifact']}: {requirement['reason']}"
            for requirement in formalization.requirements
        )
        raise ValidationError(f"formalization gate blocked Evidence finalization:\n{details}")


def _validate_final_content(
    item: dict[str, Any],
    fingerprints: Sequence[str],
    expected_changed_fields: Sequence[str],
) -> None:
    analysis = item.get("analysis", {})
    _require_nonblank("analysis.method", analysis.get("method"))
    _require_nonblank("result", item.get("result"))
    _require_nonblank("scope", item.get("scope"))
    _require_nonblank("uncertainty", item.get("uncertainty"))
    _require_nonblank("assessment", item.get("assessment"))
    comparison = analysis.get("comparison", {})
    if len(fingerprints) > 1:
        if comparison.get("mode") != "compatible_cohorts":
            raise ValidationError(
                "multi-Cohort Evidence requires comparison.mode=compatible_cohorts"
            )
        changed_fields = comparison.get("changed_fields")
        if (
            not isinstance(changed_fields, list)
            or not changed_fields
            or any(not isinstance(field, str) or not field.strip() for field in changed_fields)
            or len(changed_fields) != len(set(changed_fields))
        ):
            raise ValidationError(
                "multi-Cohort Evidence requires explicit, unique changed_fields"
            )
        if changed_fields != list(expected_changed_fields):
            raise ValidationError(
                "analysis.comparison.changed_fields does not exactly match the Run Cohorts"
            )
        _require_nonblank(
            "analysis.comparison.compatibility_justification",
            comparison.get("compatibility_justification"),
        )
    else:
        if comparison.get("mode") != "single_cohort":
            raise ValidationError("single-Cohort Evidence requires comparison.mode=single_cohort")
        if comparison.get("changed_fields") != []:
            raise ValidationError("single-Cohort Evidence must have no changed_fields")


def finalize_evidence(paths: StudyPaths, source_path: Path) -> Path:
    """Validate an edited draft and atomically replace it with an immutable record."""
    brief_issues = errors_only(brief_content_issues(paths) + brief_approval_issues(paths))
    if brief_issues:
        details = "\n".join(issue.render() for issue in brief_issues)
        raise ValidationError(
            f"a fresh approved Brief is required before Evidence finalization:\n{details}"
        )
    _require_evidence_formalization(paths)
    source = source_path.resolve()
    item = load_json(source)
    if not isinstance(item, dict):
        raise ValidationError("Evidence source must be a JSON object")
    schema_issues = object_schema_issues(paths.root, "evidence", source, item)
    if schema_issues:
        raise ValidationError(_invalid_object_message("Evidence source", schema_issues))
    if item.get("study_id") != paths.study_id:
        raise ValidationError("Evidence source study_id does not match Study")
    evidence_id = str(item.get("evidence_id"))
    require_id("evidence", evidence_id)
    version = item.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError("Evidence version must be a positive integer")
    if item.get("status") != "draft":
        raise ValidationError("evidence-finalize accepts only a draft Evidence source")
    if item.get("record_sha256") is not None:
        raise ValidationError("draft Evidence record_sha256 must be null")
    _require_evidence_formalization(paths, item)

    destination = paths.evidence / f"{evidence_id}.v{version:04d}.json"
    with _evidence_lock(paths, evidence_id):
        versions = _versions(paths, evidence_id)
        drafts = [(number, path, value) for number, path, value in versions if value["status"] == "draft"]
        if len(drafts) > 1:
            raise ValidationError(f"Evidence {evidence_id} has more than one open draft")
        current = next(
            (value for number, path, value in versions if number == version and path == destination),
            None,
        )
        if current is None:
            raise ValidationError(f"no authoritative draft exists at {destination}")
        if current.get("status") == "finalized":
            raise WorkflowError(f"refusing to overwrite finalized Evidence: {destination}")
        if current.get("status") != "draft":
            raise ValidationError(f"authoritative Evidence is not a draft: {destination}")
        if item.get("created_at") != current.get("created_at"):
            raise ValidationError("Evidence source does not match the authoritative draft")

        claim_ids = item.get("addresses", {}).get("claim_ids", [])
        _validate_claim_references(paths, claim_ids)
        fingerprints, changed_fields = _validate_run_references(paths, item)
        _validate_related_evidence(paths, item)
        _validate_final_content(item, fingerprints, changed_fields)

        finalized = dict(item)
        finalized["status"] = "finalized"
        finalized["updated_at"] = utc_now()
        finalized["record_sha256"] = record_digest(finalized, "record_sha256")
        final_schema_issues = object_schema_issues(
            paths.root, "evidence", destination, finalized
        )
        if final_schema_issues:
            raise ValidationError(
                _invalid_object_message("finalized Evidence", final_schema_issues)
            )

        initial_digest = sha256_file(destination)

        def _ensure_draft_unchanged(_temporary_path: Path) -> None:
            latest = load_json(destination)
            if not isinstance(latest, dict) or latest.get("status") != "draft":
                raise WorkflowError(
                    f"refusing to overwrite non-draft Evidence: {destination}"
                )
            if sha256_file(destination) != initial_digest:
                raise WorkflowError(f"Evidence draft changed during finalization: {destination}")

        atomic_write_json(
            destination,
            finalized,
            overwrite=True,
            mode=0o444,
            before_replace=_ensure_draft_unchanged,
        )
        return destination
