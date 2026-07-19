from __future__ import annotations

from collections import defaultdict
import copy
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Iterator

from .hashing import (
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from .models import (
    CLAIM_STATES,
    ID_PATTERNS,
    StudyPaths,
    ValidationError,
    ValidationIssue,
    errors_only,
    require_id,
)


REQUIRED_BRIEF_HEADINGS = (
    "Scientific Question",
    "Desired Claims",
    "Protected Conditions",
    "Required Evidence",
    "Resource Budget",
    "Escalation Conditions",
)

SCHEMA_FILES = {
    "run": "run.schema.json",
    "evidence": "evidence.schema.json",
    "claims": "claims.schema.json",
    "checkpoint": "checkpoint.schema.json",
    "review": "review.schema.json",
    "verdict": "verdict.schema.json",
    "brief_approval": "brief-approval.schema.json",
    "compaction_plan": "compaction-plan.schema.json",
}


def schema_path(root: Path, name: str) -> Path:
    try:
        filename = SCHEMA_FILES[name]
    except KeyError as exc:
        raise ValidationError(f"unknown schema: {name}") from exc
    return root / "scientific-workflow" / "schemas" / filename


def load_schema(root: Path, name: str) -> dict[str, Any]:
    value = load_json(schema_path(root, name))
    if not isinstance(value, dict):
        raise ValidationError(f"schema {name} is not an object")
    return value


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _resolve_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValidationError(f"only local JSON Schema references are supported: {ref}")
    current: Any = root_schema
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ValidationError(f"unresolved JSON Schema reference: {ref}")
        current = current[part]
    if not isinstance(current, dict):
        raise ValidationError(f"JSON Schema reference is not an object: {ref}")
    return current


def validate_schema_instance(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any] | None = None,
    location: str = "$",
) -> list[str]:
    """Validate the deliberately small Draft 2020-12 subset used by V1."""
    root = root_schema or schema
    if "$ref" in schema:
        target = _resolve_ref(root, schema["$ref"])
        return validate_schema_instance(value, target, root_schema=root, location=location)

    messages: list[str] = []
    if "const" in schema and value != schema["const"]:
        messages.append(f"{location}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        messages.append(f"{location}: value {value!r} is not in {schema['enum']!r}")

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not any(_json_type_matches(value, item) for item in expected_types):
            messages.append(f"{location}: expected type {expected!r}, got {type(value).__name__}")
            return messages

    if "oneOf" in schema:
        alternatives = [
            validate_schema_instance(value, candidate, root_schema=root, location=location)
            for candidate in schema["oneOf"]
        ]
        valid_count = sum(not result for result in alternatives)
        if valid_count != 1:
            messages.append(f"{location}: expected exactly one oneOf branch, got {valid_count}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                messages.append(f"{location}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in value:
                messages.extend(
                    validate_schema_instance(
                        value[key], child, root_schema=root, location=f"{location}.{key}"
                    )
                )
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            for key in extra:
                messages.append(f"{location}: additional property is not allowed: {key!r}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if minimum_items is not None and len(value) < minimum_items:
            messages.append(f"{location}: expected at least {minimum_items} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                messages.extend(
                    validate_schema_instance(
                        item, item_schema, root_schema=root, location=f"{location}[{index}]"
                    )
                )

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if minimum_length is not None and len(value) < minimum_length:
            messages.append(f"{location}: string is shorter than {minimum_length}")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            messages.append(f"{location}: value does not match {pattern!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            messages.append(f"{location}: value is below minimum {minimum}")
    return messages


def schema_issues(root: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for name, filename in SCHEMA_FILES.items():
        path = root / "scientific-workflow" / "schemas" / filename
        try:
            schema = load_schema(root, name)
            if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                issues.append(ValidationIssue("ERROR", str(path), "unexpected or missing $schema"))
            for ref in _walk_values(schema):
                if isinstance(ref, str) and ref.startswith("#/"):
                    _resolve_ref(schema, ref)
        except ValidationError as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues


def _walk_values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref":
                yield child
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)


def object_schema_issues(
    root: Path, name: str, path: Path, value: Any
) -> list[ValidationIssue]:
    schema = load_schema(root, name)
    return [
        ValidationIssue("ERROR", str(path), message)
        for message in validate_schema_instance(value, schema)
    ]


def parse_brief_metadata(text: str) -> dict[str, Any]:
    match = re.search(
        r"<!--\s*STUDYCTL-METADATA-BEGIN\s*(\{.*?\})\s*STUDYCTL-METADATA-END\s*-->",
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise ValidationError("Brief is missing the STUDYCTL-METADATA block")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Brief metadata is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("brief_version"), int):
        raise ValidationError("Brief metadata must contain integer brief_version")
    return value


def brief_content_issues(paths: StudyPaths) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    try:
        text = paths.brief.read_text(encoding="utf-8")
    except OSError as exc:
        return [ValidationIssue("ERROR", str(paths.brief), f"cannot read Brief: {exc}")]
    for heading in REQUIRED_BRIEF_HEADINGS:
        if re.search(rf"^##\s+{re.escape(heading)}\s*$", text, flags=re.MULTILINE) is None:
            issues.append(ValidationIssue("ERROR", str(paths.brief), f"missing heading: {heading}"))
    if "[REPLACE:" in text or "[REPLACE]" in text:
        issues.append(ValidationIssue("ERROR", str(paths.brief), "Brief still contains replacement placeholders"))
    try:
        metadata = parse_brief_metadata(text)
        if metadata["brief_version"] < 1:
            issues.append(ValidationIssue("ERROR", str(paths.brief), "brief_version must be at least 1"))
    except ValidationError as exc:
        issues.append(ValidationIssue("ERROR", str(paths.brief), str(exc)))
    return issues


def protected_artifact_snapshot(paths: StudyPaths) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name in ("EVALUATOR.json", "DATASET_SPLIT.json", "ACCEPTANCE_CRITERIA.json"):
        path = paths.formal / name
        key = name.removesuffix(".json").lower()
        if path.is_file():
            artifacts[key] = {
                "path": path.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(path),
            }
        else:
            artifacts[key] = None
    return artifacts


def brief_approval_issues(paths: StudyPaths) -> list[ValidationIssue]:
    path = paths.brief_approval
    if not path.is_file():
        return [ValidationIssue("ERROR", str(path), "Brief has not been approved")]
    try:
        approval = load_json(path)
    except ValidationError as exc:
        return [ValidationIssue("ERROR", str(path), str(exc))]
    issues = object_schema_issues(paths.root, "brief_approval", path, approval)
    if issues:
        return issues
    if approval.get("study_id") != paths.study_id:
        issues.append(ValidationIssue("ERROR", str(path), "approval study_id does not match directory"))
    expected_brief_path = paths.brief.relative_to(paths.root).as_posix()
    if approval.get("brief", {}).get("path") != expected_brief_path:
        issues.append(
            ValidationIssue(
                "ERROR", str(path), "approval Brief path does not match active Brief"
            )
        )
    expected_digest = record_digest(approval, "approval_sha256")
    if approval.get("approval_sha256") != expected_digest:
        issues.append(ValidationIssue("ERROR", str(path), "approval_sha256 does not match record"))
    if paths.brief.is_file():
        actual_brief_hash = sha256_file(paths.brief)
        if approval.get("brief", {}).get("sha256") != actual_brief_hash:
            issues.append(ValidationIssue("ERROR", str(path), "Brief changed after approval; approval is stale"))
    if approval.get("protected_artifacts") != protected_artifact_snapshot(paths):
        issues.append(
            ValidationIssue(
                "ERROR",
                str(path),
                "protected evaluator, data split, or acceptance criteria changed after approval",
            )
        )
    return issues


def brief_is_fresh(paths: StudyPaths) -> bool:
    return not errors_only(brief_content_issues(paths) + brief_approval_issues(paths))


def run_manifest_paths(paths: StudyPaths) -> list[Path]:
    if not paths.runs.is_dir():
        return []
    return sorted(paths.runs.glob("RUN-*/manifest.json"))


def evidence_paths(paths: StudyPaths) -> list[Path]:
    if not paths.evidence.is_dir():
        return []
    return sorted(paths.evidence.glob("EVID-*.v*.json"))


def checkpoint_paths(paths: StudyPaths) -> list[Path]:
    if not paths.checkpoints.is_dir():
        return []
    return sorted(paths.checkpoints.glob("CHECKPOINT-*.json"))


def evidence_index(paths: StudyPaths) -> dict[tuple[str, int], tuple[Path, dict[str, Any]]]:
    index: dict[tuple[str, int], tuple[Path, dict[str, Any]]] = {}
    for path in evidence_paths(paths):
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(f"Evidence is not an object: {path}")
        key = (str(value.get("evidence_id")), int(value.get("version", 0)))
        if key in index:
            raise ValidationError(f"duplicate Evidence identity/version: {key}")
        index[key] = (path, value)
    return index


def run_index(paths: StudyPaths) -> dict[str, tuple[Path, dict[str, Any]]]:
    index: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in run_manifest_paths(paths):
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(f"Run manifest is not an object: {path}")
        run_id = str(value.get("run_id"))
        if run_id in index:
            raise ValidationError(f"duplicate Run ID: {run_id}")
        index[run_id] = (path, value)
    return index


def _resolve_recorded_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _changed_cohort_fields(manifests: list[dict[str, Any]]) -> list[str]:
    fields = [manifest.get("cohort", {}).get("fields", {}) for manifest in manifests]
    keys = sorted({str(key) for item in fields if isinstance(item, dict) for key in item})
    changed: list[str] = []
    for key in keys:
        signatures = [
            ("value", sha256_json(item[key]))
            if isinstance(item, dict) and key in item
            else ("missing", "")
            for item in fields
        ]
        if any(signature != signatures[0] for signature in signatures[1:]):
            changed.append(key)
    return changed


def _run_issues(paths: StudyPaths) -> tuple[list[ValidationIssue], dict[str, tuple[Path, dict[str, Any]]]]:
    issues: list[ValidationIssue] = []
    runs: dict[str, tuple[Path, dict[str, Any]]] = {}
    cohort_ids: dict[str, str] = {}
    for path in run_manifest_paths(paths):
        try:
            manifest = load_json(path)
            if not isinstance(manifest, dict):
                raise ValidationError("manifest must be an object")
            issues.extend(object_schema_issues(paths.root, "run", path, manifest))
            run_id = str(manifest.get("run_id", ""))
            require_id("run", run_id)
            if manifest.get("study_id") != paths.study_id:
                issues.append(
                    ValidationIssue("ERROR", str(path), "Run study_id does not match Study directory")
                )
            if path.parent.name != run_id:
                issues.append(ValidationIssue("ERROR", str(path), "run_id does not match directory"))
            if run_id in runs:
                issues.append(ValidationIssue("ERROR", str(path), f"duplicate Run ID {run_id}"))
            runs[run_id] = (path, manifest)
            status = manifest.get("status")
            if status == "running":
                issues.append(ValidationIssue("ERROR", str(path), "Run is unsealed/running"))
            integrity = manifest.get("integrity", {})
            digest = nested_record_digest(manifest, "integrity", "manifest_sha256")
            if status != "running" and integrity.get("manifest_sha256") != digest:
                issues.append(ValidationIssue("ERROR", str(path), "manifest_sha256 does not match record"))
            code_state = manifest.get("code_state", {})
            expected_code_change = code_state.get("before") != code_state.get("after")
            if code_state.get("changed_during_run") != expected_code_change:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "tracked-code change flag does not match snapshots"
                    )
                )
            elif expected_code_change:
                issues.append(
                    ValidationIssue(
                        "WARNING", str(path), "tracked code changed during Run"
                    )
                )
            cohort = manifest.get("cohort", {})
            fields = cohort.get("fields", {})
            expected_fingerprint = sha256_json(fields)
            if cohort.get("fingerprint_sha256") != expected_fingerprint:
                issues.append(ValidationIssue("ERROR", str(path), "Cohort fingerprint does not match fields"))
            cohort_id = cohort.get("cohort_id")
            if cohort_id:
                previous = cohort_ids.setdefault(cohort_id, expected_fingerprint)
                if previous != expected_fingerprint:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"Cohort {cohort_id} has more than one fingerprint"
                        )
                    )
            for log_name in ("stdout", "stderr"):
                record = manifest.get("logs", {}).get(log_name, {})
                log_path = _resolve_recorded_path(paths.root, str(record.get("path", "")))
                if not log_path.is_file():
                    issues.append(ValidationIssue("ERROR", str(path), f"missing {log_name} log"))
                elif record.get("sha256") != sha256_file(log_path):
                    issues.append(ValidationIssue("ERROR", str(path), f"{log_name} hash mismatch"))
            for record in manifest.get("outputs", []):
                if not record.get("present"):
                    continue
                output_path = _resolve_recorded_path(paths.root, str(record.get("path", "")))
                if output_path.is_file() and record.get("sha256") != sha256_file(output_path):
                    issues.append(ValidationIssue("ERROR", str(path), f"output hash mismatch: {record.get('path')}"))
                elif not output_path.exists():
                    issues.append(ValidationIssue("WARNING", str(path), f"output is unavailable: {record.get('path')}"))
            for record in manifest.get("inputs", []):
                input_path = _resolve_recorded_path(paths.root, str(record.get("path", "")))
                if input_path.is_file() and record.get("sha256_after") != sha256_file(input_path):
                    issues.append(ValidationIssue("WARNING", str(path), f"input changed since Run: {record.get('path')}"))
                elif not input_path.exists():
                    issues.append(
                        ValidationIssue(
                            "WARNING", str(path), f"input is unavailable: {record.get('path')}"
                        )
                    )
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues, runs


def _evidence_issues(
    paths: StudyPaths,
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[list[ValidationIssue], dict[tuple[str, int], tuple[Path, dict[str, Any]]]]:
    issues: list[ValidationIssue] = []
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]] = {}
    for path in evidence_paths(paths):
        try:
            item = load_json(path)
            if not isinstance(item, dict):
                raise ValidationError("Evidence must be an object")
            issues.extend(object_schema_issues(paths.root, "evidence", path, item))
            evidence_id = str(item.get("evidence_id", ""))
            version = int(item.get("version", 0))
            require_id("evidence", evidence_id)
            expected_name = f"{evidence_id}.v{version:04d}.json"
            if path.name != expected_name:
                issues.append(ValidationIssue("ERROR", str(path), f"expected filename {expected_name}"))
            key = (evidence_id, version)
            if item.get("study_id") != paths.study_id:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "Evidence study_id does not match Study directory"
                    )
                )
            if key in evidence:
                issues.append(ValidationIssue("ERROR", str(path), f"duplicate Evidence version {key}"))
            evidence[key] = (path, item)
            actual_fingerprints: set[str] = set()
            referenced_manifests: list[dict[str, Any]] = []
            for run_ref in item.get("runs", []):
                run_id = run_ref.get("run_id")
                if run_id not in runs:
                    issues.append(ValidationIssue("ERROR", str(path), f"missing Run reference: {run_id}"))
                    continue
                _, manifest = runs[run_id]
                referenced_manifests.append(manifest)
                if run_ref.get("manifest_sha256") != manifest.get("integrity", {}).get("manifest_sha256"):
                    issues.append(ValidationIssue("ERROR", str(path), f"Run manifest hash mismatch: {run_id}"))
                if manifest.get("status") == "running":
                    issues.append(ValidationIssue("ERROR", str(path), f"Evidence references running Run: {run_id}"))
                actual_fingerprints.add(manifest.get("cohort", {}).get("fingerprint_sha256"))
                if item.get("status") == "finalized" and any(
                    record.get("changed_during_run") for record in manifest.get("inputs", [])
                ):
                    issues.append(ValidationIssue("ERROR", str(path), f"finalized Evidence uses Run with changing input: {run_id}"))
            declared = set(item.get("analysis", {}).get("comparison", {}).get("cohort_fingerprints", []))
            if actual_fingerprints != declared:
                issues.append(ValidationIssue("ERROR", str(path), "declared cohort fingerprints do not match Runs"))
            comparison = item.get("analysis", {}).get("comparison", {})
            expected_changed_fields = (
                _changed_cohort_fields(referenced_manifests)
                if len(actual_fingerprints) > 1
                else []
            )
            if comparison.get("changed_fields") != expected_changed_fields:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "declared changed_fields do not exactly match Run Cohorts",
                    )
                )
            if len(actual_fingerprints) > 1 and not str(comparison.get("compatibility_justification") or "").strip():
                issues.append(ValidationIssue("ERROR", str(path), "incompatible Cohorts lack compatibility justification"))
            if len(actual_fingerprints) > 1 and comparison.get("mode") != "compatible_cohorts":
                issues.append(ValidationIssue("ERROR", str(path), "multi-Cohort Evidence must use compatible_cohorts mode"))
            if len(actual_fingerprints) == 1 and comparison.get("mode") != "single_cohort":
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "single-Cohort Evidence must use single_cohort mode"
                    )
                )
            if item.get("status") == "finalized":
                for field_path, field_value in (
                    ("analysis.method", item.get("analysis", {}).get("method")),
                    ("result", item.get("result")),
                    ("scope", item.get("scope")),
                    ("uncertainty", item.get("uncertainty")),
                    ("assessment", item.get("assessment")),
                ):
                    if field_value is None or field_value == "":
                        issues.append(ValidationIssue("ERROR", str(path), f"finalized Evidence requires {field_path}"))
                if item.get("record_sha256") != record_digest(item, "record_sha256"):
                    issues.append(ValidationIssue("ERROR", str(path), "Evidence record_sha256 does not match"))
            elif item.get("record_sha256") is not None:
                issues.append(ValidationIssue("ERROR", str(path), "draft Evidence must not be sealed"))
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))

    # Validate related-Evidence edges only after every version has been indexed,
    # so forward references and deletions are handled deterministically.
    for key, (path, item) in evidence.items():
        related = item.get("related_evidence", {})
        if not isinstance(related, dict):
            continue
        seen_related: dict[tuple[str, int], str] = {}
        for role in ("supporting", "contradictory"):
            refs = related.get(role, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                target_key = _ref_key(ref)
                if target_key == key:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "Evidence cannot reference itself as related Evidence"
                        )
                    )
                    continue
                previous_role = seen_related.setdefault(target_key, role)
                if previous_role != role:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            f"related Evidence {target_key} appears in conflicting roles",
                        )
                    )
                target = evidence.get(target_key)
                if target is None:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"missing related Evidence reference: {target_key}"
                        )
                    )
                    continue
                _, target_item = target
                digest = target_item.get("record_sha256")
                if target_item.get("status") != "finalized":
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"related Evidence is not finalized: {target_key}"
                        )
                    )
                elif digest != record_digest(target_item, "record_sha256"):
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"related Evidence integrity failed: {target_key}"
                        )
                    )
                if ref.get("sha256") != digest:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"related Evidence hash is stale: {target_key}"
                        )
                    )
    return issues, evidence


def _ref_key(ref: dict[str, Any]) -> tuple[str, int]:
    return str(ref.get("evidence_id")), int(ref.get("version", 0))


def _claims_issues(
    paths: StudyPaths,
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
) -> tuple[list[ValidationIssue], dict[str, Any] | None]:
    issues: list[ValidationIssue] = []
    if not paths.claims.is_file():
        return [ValidationIssue("ERROR", str(paths.claims), "missing CLAIMS.json")], None
    try:
        claims_data = load_json(paths.claims)
        if not isinstance(claims_data, dict):
            raise ValidationError("CLAIMS.json must be an object")
        issues.extend(object_schema_issues(paths.root, "claims", paths.claims, claims_data))
        if claims_data.get("study_id") != paths.study_id:
            issues.append(ValidationIssue("ERROR", str(paths.claims), "study_id mismatch"))
        claim_ids: set[str] = set()
        refs_by_claim: dict[str, dict[str, set[tuple[str, int]]]] = {}
        for claim in claims_data.get("claims", []):
            claim_id = str(claim.get("claim_id", ""))
            if claim_id in claim_ids:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"duplicate Claim ID: {claim_id}"))
            claim_ids.add(claim_id)
            groups = {
                "supporting": {_ref_key(ref) for ref in claim.get("supporting_evidence", [])},
                "contradictory": {_ref_key(ref) for ref in claim.get("contradictory_evidence", [])},
                "other": {_ref_key(ref) for ref in claim.get("other_evidence", [])},
            }
            refs_by_claim[claim_id] = groups
            combined = list(groups.values())
            if any(combined[i] & combined[j] for i in range(3) for j in range(i + 1, 3)):
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} repeats Evidence across roles"))
            for field, group_name in (
                ("supporting_evidence", "supporting"),
                ("contradictory_evidence", "contradictory"),
                ("other_evidence", "other"),
            ):
                for ref in claim.get(field, []):
                    key = _ref_key(ref)
                    if key not in evidence:
                        issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} references missing Evidence {key}"))
                        continue
                    _, item = evidence[key]
                    if item.get("status") != "finalized":
                        issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} references draft Evidence {key}"))
                    if ref.get("sha256") != item.get("record_sha256"):
                        issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} has stale Evidence hash {key}"))
                    if claim_id not in item.get("addresses", {}).get("claim_ids", []):
                        issues.append(ValidationIssue("ERROR", str(paths.claims), f"Evidence {key} does not address Claim {claim_id}"))
                    assessment = item.get("assessment")
                    if group_name == "supporting" and assessment != "supports":
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(paths.claims),
                                f"Claim {claim_id} uses Evidence {key} with assessment {assessment!r} as supporting",
                            )
                        )
                    if group_name == "contradictory" and assessment not in {
                        "contradicts",
                        "mixed",
                    }:
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(paths.claims),
                                f"Claim {claim_id} uses Evidence {key} with assessment {assessment!r} as contradictory",
                            )
                        )
            state = claim.get("state")
            evidence_count = sum(len(group) for group in groups.values())
            if state in {"partially_supported", "numerically_supported"} and not groups["supporting"]:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} state {state} requires supporting Evidence"))
            if state == "contradicted" and not groups["contradictory"]:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} state contradicted requires contradictory Evidence"))
            if state == "inconclusive" and evidence_count == 0:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} state inconclusive requires Evidence"))
            if state not in CLAIM_STATES:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"invalid agent Claim state: {state}"))
        frontier_ids = set(claims_data.get("frontier", {}).get("claim_ids", []))
        for missing in sorted(frontier_ids - claim_ids):
            issues.append(ValidationIssue("ERROR", str(paths.claims), f"Frontier references missing Claim: {missing}"))
        for (evidence_id, version), (_, item) in evidence.items():
            if item.get("status") != "finalized":
                continue
            assessment = item.get("assessment")
            for claim_id in item.get("addresses", {}).get("claim_ids", []):
                groups = refs_by_claim.get(claim_id)
                if groups is None:
                    continue
                key = (evidence_id, version)
                if assessment in {"contradicts", "mixed"} and key not in groups["contradictory"]:
                    issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} omits contradictory Evidence {key}"))
        return issues, claims_data
    except (ValidationError, OSError, ValueError) as exc:
        issues.append(ValidationIssue("ERROR", str(paths.claims), str(exc)))
        return issues, None


def _checkpoint_issues(
    paths: StudyPaths,
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    previous: dict[str, Any] | None = None
    claims_schema = load_schema(paths.root, "claims")
    for path in checkpoint_paths(paths):
        try:
            item = load_json(path)
            if not isinstance(item, dict):
                raise ValidationError("Checkpoint must be an object")
            issues.extend(object_schema_issues(paths.root, "checkpoint", path, item))
            checkpoint_id = str(item.get("checkpoint_id", ""))
            if item.get("study_id") != paths.study_id:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "Checkpoint study_id does not match Study directory"
                    )
                )
            if path.name != f"{checkpoint_id}.json":
                issues.append(ValidationIssue("ERROR", str(path), "Checkpoint filename does not match ID"))
            if item.get("checkpoint_sha256") != record_digest(item, "checkpoint_sha256"):
                issues.append(ValidationIssue("ERROR", str(path), "checkpoint_sha256 does not match"))
            for index, claim in enumerate(item.get("claims_snapshot", [])):
                for message in validate_schema_instance(
                    claim,
                    claims_schema["$defs"]["claim"],
                    root_schema=claims_schema,
                    location=f"$.claims_snapshot[{index}]",
                ):
                    issues.append(ValidationIssue("ERROR", str(path), message))
            for message in validate_schema_instance(
                item.get("frontier"),
                claims_schema["$defs"]["frontier"],
                root_schema=claims_schema,
                location="$.frontier",
            ):
                issues.append(ValidationIssue("ERROR", str(path), message))
            for field in ("decisive_evidence", "contradictory_evidence"):
                for ref in item.get(field, []):
                    if not isinstance(ref, dict):
                        continue
                    key = _ref_key(ref)
                    target = evidence.get(key)
                    if target is None:
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"Checkpoint references missing Evidence {key}"
                            )
                        )
                        continue
                    target_item = target[1]
                    digest = target_item.get("record_sha256")
                    if target_item.get("status") != "finalized" or digest != record_digest(
                        target_item, "record_sha256"
                    ):
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"Checkpoint Evidence is not valid/finalized {key}"
                            )
                        )
                    if ref.get("sha256") != digest:
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"Checkpoint Evidence hash is stale {key}"
                            )
                        )
            for failure in item.get("representative_failures", []):
                if not isinstance(failure, dict):
                    continue
                if failure.get("kind") == "run":
                    run_id = str(failure.get("run_id"))
                    manifest_path = paths.runs / run_id / "manifest.json"
                    try:
                        manifest = load_json(manifest_path)
                    except ValidationError:
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"representative failure Run is missing: {run_id}"
                            )
                        )
                        continue
                    digest = manifest.get("integrity", {}).get("manifest_sha256")
                    if manifest.get("status") not in {"failed", "interrupted"}:
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"representative Run is not failed/interrupted: {run_id}"
                            )
                        )
                    if digest != nested_record_digest(
                        manifest, "integrity", "manifest_sha256"
                    ) or failure.get("manifest_sha256") != digest:
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"representative Run hash is stale: {run_id}"
                            )
                        )
                elif failure.get("kind") == "failed_direction":
                    failed_path = _resolve_recorded_path(
                        paths.root, str(failure.get("path", ""))
                    )
                    try:
                        failed_path.resolve(strict=True).relative_to(
                            (paths.study / "failed-directions").resolve()
                        )
                    except (OSError, ValueError):
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), "representative failed-direction path is missing or unsafe"
                            )
                        )
                        continue
                    if failed_path.is_symlink() or not failed_path.is_file():
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), "representative failed direction is not a regular file"
                            )
                        )
                    elif failure.get("size") != failed_path.stat().st_size or failure.get(
                        "sha256"
                    ) != sha256_file(failed_path):
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), "representative failed-direction hash/size is stale"
                            )
                        )
            for archived in item.get("archived_work_files", []):
                archived_path = _resolve_recorded_path(paths.root, str(archived.get("archived_path", "")))
                try:
                    archived_path.resolve(strict=True).relative_to(paths.archived_work.resolve())
                except (OSError, ValueError):
                    issues.append(
                        ValidationIssue("ERROR", str(path), f"archived work path is missing or unsafe: {archived.get('archived_path')}")
                    )
                    continue
                if archived_path.is_symlink() or not archived_path.is_file():
                    issues.append(ValidationIssue("ERROR", str(path), "archived work artifact is not a regular file"))
                    continue
                if archived.get("size") != archived_path.stat().st_size:
                    issues.append(ValidationIssue("ERROR", str(path), f"archived work size mismatch: {archived.get('archived_path')}"))
                if archived.get("sha256") != sha256_file(archived_path):
                    issues.append(ValidationIssue("ERROR", str(path), f"archived work hash mismatch: {archived.get('archived_path')}"))
            expected_previous = None if previous is None else {
                "checkpoint_id": previous["checkpoint_id"],
                "sha256": previous["checkpoint_sha256"],
            }
            if item.get("previous_checkpoint") != expected_previous:
                issues.append(ValidationIssue("ERROR", str(path), "previous Checkpoint link is invalid"))
            previous = item
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues


def verdict_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob("VERDICT*.json"))


def _verdict_issues(
    paths: StudyPaths,
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    checkpoint_index: dict[str, dict[str, Any]] = {}
    for checkpoint_path in checkpoint_paths(paths):
        try:
            checkpoint = load_json(checkpoint_path)
        except ValidationError:
            continue
        if isinstance(checkpoint, dict):
            checkpoint_index[str(checkpoint.get("checkpoint_id"))] = checkpoint
    known_brief_hashes = {
        sha256_file(path)
        for path in [paths.brief, *sorted((paths.study / "brief-history").glob("BRIEF.v*.md"))]
        if path.is_file() and not path.is_symlink()
    }
    verdict_ids: set[str] = set()
    for path in verdict_paths(paths):
        try:
            item = load_json(path)
            if not isinstance(item, dict):
                raise ValidationError("Verdict must be an object")
            issues.extend(object_schema_issues(paths.root, "verdict", path, item))
            if item.get("verdict_sha256") != record_digest(item, "verdict_sha256"):
                issues.append(ValidationIssue("ERROR", str(path), "verdict_sha256 does not match"))
            if item.get("study_id") != paths.study_id:
                issues.append(ValidationIssue("ERROR", str(path), "Verdict study_id mismatch"))
            verdict_id = str(item.get("verdict_id", ""))
            require_id("verdict", verdict_id)
            if verdict_id in verdict_ids:
                issues.append(
                    ValidationIssue("ERROR", str(path), f"duplicate Verdict ID: {verdict_id}")
                )
            verdict_ids.add(verdict_id)
            confirmation = item.get("confirmation", {})
            expected_phrase = f"RECORD VERDICT {paths.study_id} {verdict_id}"
            if confirmation.get("typed_text") != expected_phrase:
                issues.append(
                    ValidationIssue("ERROR", str(path), "Verdict confirmation phrase is invalid")
                )
            scope = item.get("judged_scope", {})
            if scope.get("brief_sha256") not in known_brief_hashes:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "Verdict references an unavailable Brief version"
                    )
                )

            checkpoint_ref = scope.get("checkpoint")
            checkpoint: dict[str, Any] | None = None
            if isinstance(checkpoint_ref, dict):
                checkpoint = checkpoint_index.get(str(checkpoint_ref.get("checkpoint_id")))
                if checkpoint is None:
                    issues.append(
                        ValidationIssue("ERROR", str(path), "Verdict references a missing Checkpoint")
                    )
                elif checkpoint_ref.get("sha256") != checkpoint.get("checkpoint_sha256"):
                    issues.append(
                        ValidationIssue("ERROR", str(path), "Verdict Checkpoint hash is stale")
                    )

            claim_refs = scope.get("claims", [])
            if claim_refs and checkpoint is None:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Verdict Claim references require a Checkpoint snapshot",
                    )
                )
            elif checkpoint is not None:
                snapshot = {
                    str(claim.get("claim_id")): claim
                    for claim in checkpoint.get("claims_snapshot", [])
                    if isinstance(claim, dict)
                }
                for ref in claim_refs:
                    claim_id = str(ref.get("claim_id"))
                    claim = snapshot.get(claim_id)
                    if claim is None:
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"Verdict references missing Claim {claim_id}"
                            )
                        )
                    elif ref.get("sha256") != sha256_json(claim):
                        issues.append(
                            ValidationIssue(
                                "ERROR", str(path), f"Verdict Claim hash is stale: {claim_id}"
                            )
                        )

            for ref in scope.get("evidence", []):
                key = _ref_key(ref)
                target = evidence.get(key)
                if target is None:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"Verdict references missing Evidence {key}"
                        )
                    )
                    continue
                target_item = target[1]
                digest = target_item.get("record_sha256")
                if target_item.get("status") != "finalized":
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"Verdict references draft Evidence {key}"
                        )
                    )
                elif digest != record_digest(target_item, "record_sha256"):
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"Verdict Evidence integrity failed {key}"
                        )
                    )
                if ref.get("sha256") != digest:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), f"Verdict Evidence hash is stale {key}"
                        )
                    )
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues


def validate_study(paths: StudyPaths) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(schema_issues(paths.root))
    issues.extend(brief_content_issues(paths))
    issues.extend(brief_approval_issues(paths))
    run_issues, runs = _run_issues(paths)
    issues.extend(run_issues)
    evidence_issues, evidence = _evidence_issues(paths, runs)
    issues.extend(evidence_issues)
    claim_issues, _ = _claims_issues(paths, evidence)
    issues.extend(claim_issues)
    issues.extend(_checkpoint_issues(paths, evidence))
    issues.extend(_verdict_issues(paths, evidence))
    return issues


def assert_valid_study(paths: StudyPaths) -> None:
    issues = errors_only(validate_study(paths))
    if issues:
        joined = "\n".join(issue.render() for issue in issues)
        raise ValidationError(f"Study validation failed:\n{joined}")


def authoritative_string_references(paths: StudyPaths) -> set[str]:
    references: set[str] = set()
    candidates = [
        paths.claims,
        *evidence_paths(paths),
        *checkpoint_paths(paths),
        *verdict_paths(paths),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        value = load_json(path)
        for child in _walk_all(value):
            if isinstance(child, str):
                references.add(child)
    return references


def run_file_references(paths: StudyPaths) -> set[str]:
    """Return input/output paths explicitly pinned by immutable Run manifests."""

    references: set[str] = set()
    for path in run_manifest_paths(paths):
        value = load_json(path)
        if not isinstance(value, dict):
            continue
        for field in ("inputs", "outputs"):
            records = value.get(field, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict) and isinstance(record.get("path"), str):
                    references.add(record["path"])
    return references


def _walk_all(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_all(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_all(child)
