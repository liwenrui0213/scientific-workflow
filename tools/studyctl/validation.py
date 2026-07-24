from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator

from .active_context import CLAIM_LIFECYCLES, claim_lifecycle
from .budget import (
    budget_projection,
    manifest_budget_commitment,
    parse_brief_hard_budget,
)
from .checkpoint_sequence import (
    checkpoint_sequence_temporary_paths,
    load_checkpoint_sequence,
)
from .evidence_sequence import (
    evidence_sequence_temporary_paths,
    load_evidence_sequence,
)
from .observation_sequence import (
    load_observation_sequence,
    observation_sequence_temporary_paths,
)
from .hashing import (
    canonical_json_bytes,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from .models import (
    CHECKPOINT_SCHEMA_VERSION,
    CLAIMS_SCHEMA_VERSION,
    CLAIM_STATES,
    EVIDENCE_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    ID_PATTERNS,
    StudyPaths,
    ValidationError,
    ValidationIssue,
    WorkflowError,
    errors_only,
    require_id,
)
from .run_ledger import (
    bootstrap_or_reconcile_ledger,
    ledger_path,
    load_ledger,
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
    "observation": "observation.schema.json",
    "evidence": "evidence.schema.json",
    "confirmation": "confirmation.schema.json",
    "claims": "claims.schema.json",
    "checkpoint": "checkpoint.schema.json",
    "review": "review.schema.json",
    "verdict": "verdict.schema.json",
    "brief_approval": "brief-approval.schema.json",
    "compaction_plan": "compaction-plan.schema.json",
    "repository_profile": "repository-profile.schema.json",
    "changeset": "changeset.schema.json",
    "change_validation": "change-validation.schema.json",
}

CURRENT_ARTIFACT_SCHEMA_VERSIONS = {
    "observation": OBSERVATION_SCHEMA_VERSION,
    "evidence": EVIDENCE_SCHEMA_VERSION,
    "claims": CLAIMS_SCHEMA_VERSION,
    "checkpoint": CHECKPOINT_SCHEMA_VERSION,
}

_RUN_V2_TOP_LEVEL_KEYS = {
    "schema_version",
    "study_id",
    "run_id",
    "purpose",
    "status",
    "execution",
    "git",
    "code_state",
    "change_scope",
    "brief",
    "formal_artifacts",
    "formalization",
    "cohort",
    "environment",
    "budget",
    "inputs",
    "outputs",
    "logs",
    "integrity",
}
_RUN_V1_TOP_LEVEL_KEYS = _RUN_V2_TOP_LEVEL_KEYS - {"change_scope"}


def _run_v2_shape_messages(value: dict[str, Any]) -> list[str]:
    """Validate fields that differ between the frozen V2 and current schema."""

    messages: list[str] = []
    missing = sorted(_RUN_V2_TOP_LEVEL_KEYS - set(value))
    extra = sorted(set(value) - _RUN_V2_TOP_LEVEL_KEYS)
    messages.extend(f"$: missing required property {key!r}" for key in missing)
    messages.extend(f"$: additional property is not allowed: {key!r}" for key in extra)
    if value.get("status") not in {"succeeded", "failed", "interrupted", "running"}:
        messages.append("$.status: value is not valid for Run schema V2")
    brief = value.get("brief")
    expected_brief = {"path", "sha256", "approval_sha256"}
    if not isinstance(brief, dict):
        messages.append("$.brief: expected type 'object'")
    else:
        messages.extend(
            f"$.brief: missing required property {key!r}"
            for key in sorted(expected_brief - set(brief))
        )
        messages.extend(
            f"$.brief: additional property is not allowed: {key!r}"
            for key in sorted(set(brief) - expected_brief)
        )
    budget = value.get("budget")
    expected_budget = {"estimated_gpu_hours", "estimated_cpu_hours"}
    if not isinstance(budget, dict):
        messages.append("$.budget: expected type 'object'")
    else:
        messages.extend(
            f"$.budget: missing required property {key!r}"
            for key in sorted(expected_budget - set(budget))
        )
        messages.extend(
            f"$.budget: additional property is not allowed: {key!r}"
            for key in sorted(set(budget) - expected_budget)
        )
    return messages


def _run_v1_shape_messages(value: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    missing = sorted(_RUN_V1_TOP_LEVEL_KEYS - set(value))
    extra = sorted(set(value) - _RUN_V1_TOP_LEVEL_KEYS)
    messages.extend(f"$: missing required property {key!r}" for key in missing)
    messages.extend(f"$: additional property is not allowed: {key!r}" for key in extra)
    execution = value.get("execution")
    expected_execution = {
        "argv",
        "cwd",
        "started_at",
        "ended_at",
        "duration_seconds",
        "exit_code",
        "seed",
    }
    if not isinstance(execution, dict):
        messages.append("$.execution: expected type 'object'")
    else:
        messages.extend(
            f"$.execution: missing required property {key!r}"
            for key in sorted(expected_execution - set(execution))
        )
        messages.extend(
            f"$.execution: additional property is not allowed: {key!r}"
            for key in sorted(set(execution) - expected_execution)
        )
    formalization = value.get("formalization")
    expected_formalization = {
        "changed_paths",
        "scientific_critical",
        "shared_across_runs",
        "outcome",
        "requirements",
    }
    if not isinstance(formalization, dict):
        messages.append("$.formalization: expected type 'object'")
    else:
        messages.extend(
            f"$.formalization: missing required property {key!r}"
            for key in sorted(expected_formalization - set(formalization))
        )
        messages.extend(
            f"$.formalization: additional property is not allowed: {key!r}"
            for key in sorted(set(formalization) - expected_formalization)
        )
    # V1 and V2 shared the same exact Brief and budget shapes.
    shadow = dict(value)
    shadow["change_scope"] = {}
    messages.extend(_run_v2_shape_messages(shadow))
    return [
        message
        for message in messages
        if "missing required property 'change_scope'" not in message
    ]


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
    """Validate the deliberately small Draft 2020-12 subset used by the workflow."""
    root = root_schema or schema
    if "$ref" in schema:
        target = _resolve_ref(root, schema["$ref"])
        return validate_schema_instance(value, target, root_schema=root, location=location)

    messages: list[str] = []
    maximum_canonical_bytes = schema.get("x-maxCanonicalBytes")
    if maximum_canonical_bytes is not None:
        if (
            isinstance(maximum_canonical_bytes, bool)
            or not isinstance(maximum_canonical_bytes, int)
            or maximum_canonical_bytes < 0
        ):
            raise ValidationError(
                f"{location}: schema x-maxCanonicalBytes must be a non-negative integer"
            )
        statuses = schema.get("x-maxCanonicalBytesStatuses")
        enforce_size = statuses is None
        if statuses is not None:
            if not isinstance(statuses, list) or any(
                not isinstance(item, str) for item in statuses
            ):
                raise ValidationError(
                    f"{location}: schema x-maxCanonicalBytesStatuses must be a string array"
                )
            enforce_size = (
                isinstance(value, dict) and value.get("status") in statuses
            )
        if enforce_size:
            actual_bytes = len(canonical_json_bytes(value))
            if actual_bytes > maximum_canonical_bytes:
                messages.append(
                    f"{location}: canonical JSON is {actual_bytes} bytes; "
                    f"maximum is {maximum_canonical_bytes}"
                )
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

    all_of = schema.get("allOf")
    if all_of is not None:
        if not isinstance(all_of, list) or any(
            not isinstance(candidate, dict) for candidate in all_of
        ):
            raise ValidationError(f"{location}: schema allOf must be an object array")
        for candidate in all_of:
            messages.extend(
                validate_schema_instance(
                    value,
                    candidate,
                    root_schema=root,
                    location=location,
                )
            )

    condition = schema.get("if")
    if condition is not None:
        if not isinstance(condition, dict):
            raise ValidationError(f"{location}: schema if must be an object")
        branch_name = (
            "then"
            if not validate_schema_instance(
                value,
                condition,
                root_schema=root,
                location=location,
            )
            else "else"
        )
        branch = schema.get(branch_name)
        if branch is not None:
            if not isinstance(branch, dict):
                raise ValidationError(
                    f"{location}: schema {branch_name} must be an object"
                )
            messages.extend(
                validate_schema_instance(
                    value,
                    branch,
                    root_schema=root,
                    location=location,
                )
            )

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
        maximum_items = schema.get("maxItems")
        if maximum_items is not None and len(value) > maximum_items:
            messages.append(f"{location}: expected at most {maximum_items} item(s)")
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(item == previous for previous in value[:index]):
                    messages.append(
                        f"{location}[{index}]: duplicate array item is not allowed"
                    )
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
        maximum_length = schema.get("maxLength")
        if maximum_length is not None and len(value) > maximum_length:
            messages.append(f"{location}: string is longer than {maximum_length}")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            messages.append(f"{location}: value does not match {pattern!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            messages.append(f"{location}: value is below minimum {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            messages.append(f"{location}: value is above maximum {maximum}")
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
    current_version = CURRENT_ARTIFACT_SCHEMA_VERSIONS.get(name)
    if current_version is not None:
        raw_version = value.get("schema_version") if isinstance(value, dict) else None
        if (
            isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version != current_version
        ):
            display_name = {
                "claims": "Claims",
                "observation": "Observation",
                "evidence": "Evidence",
                "checkpoint": "Checkpoint",
            }[name]
            return [
                ValidationIssue(
                    "ERROR",
                    str(path),
                    f"unsupported {display_name} schema_version: {raw_version!r}; "
                    f"current schema_version {current_version} is required",
                )
            ]
    schema = load_schema(root, name)
    validation_value = value
    original_run_version = (
        value.get("schema_version")
        if name == "run" and isinstance(value, dict)
        else None
    )
    if original_run_version == 2:
        v2_messages = _run_v2_shape_messages(value)
        if v2_messages:
            return [
                ValidationIssue("ERROR", str(path), message)
                for message in v2_messages
            ]
        # Validate all fields shared with the current Run contract through a
        # transient V4 view, adding only fields that did not exist in V2.
        validation_value = copy.deepcopy(value)
        validation_value["schema_version"] = 4
        validation_value["epistemic_role"] = {
            "mode": "exploratory",
            "confirmation_id": None,
            "confirmation_sha256": None,
            "slot_id": None,
        }
        brief = validation_value["brief"]
        brief["snapshot"] = {
            "path": "<legacy-v2-unattested-brief>",
            "size": 0,
            "sha256": str(brief.get("sha256") or "0" * 64),
        }
        brief["approval_snapshot"] = {
            "path": "<legacy-v2-unattested-approval>",
            "size": 0,
            "sha256": "0" * 64,
        }
        old_budget = validation_value["budget"]
        gpu = old_budget["estimated_gpu_hours"]
        cpu = old_budget["estimated_cpu_hours"]
        validation_value["budget"] = {
            "estimated_gpu_hours": gpu,
            "estimated_cpu_hours": cpu,
            "estimated_storage_gb": 0.0,
            "actual_output_storage_gb": None,
            "hard_limits": {
                "gpu_hours": None,
                "cpu_hours": None,
                "storage_gb": None,
            },
            "committed_before": {
                "gpu_hours": 0.0,
                "cpu_hours": 0.0,
                "storage_gb": 0.0,
            },
            "requested": {
                "gpu_hours": gpu,
                "cpu_hours": cpu,
                "storage_gb": 0.0,
            },
            "committed_after": {
                "gpu_hours": gpu,
                "cpu_hours": cpu,
                "storage_gb": 0.0,
            },
            "violations": [],
        }
        validation_value["failure"] = None
    elif (
        isinstance(original_run_version, int)
        and not isinstance(original_run_version, bool)
        and original_run_version == 1
    ):
        v1_messages = _run_v1_shape_messages(value)
        if v1_messages:
            return [
                ValidationIssue("ERROR", str(path), message)
                for message in v1_messages
            ]
        # V1 manifests are immutable historical records. Validate a transient
        # V4-shaped view instead of rewriting disk; their synthetic scope stays
        # deliberately Evidence-ineligible and their epistemic role is
        # conservatively exploratory.
        validation_value = copy.deepcopy(value)
        validation_value["schema_version"] = 4
        validation_value["epistemic_role"] = {
            "mode": "exploratory",
            "confirmation_id": None,
            "confirmation_sha256": None,
            "slot_id": None,
        }
        execution = validation_value.get("execution")
        if isinstance(execution, dict):
            execution.setdefault("cwd_relative", ".")
        formalization = validation_value.get("formalization")
        if isinstance(formalization, dict):
            changed_paths = list(formalization.get("changed_paths", []))
            formalization.setdefault("declared_changed_paths", changed_paths)
            formalization.setdefault("actual_changed_paths", [])
            formalization.setdefault("artifacts_unchanged_during_run", False)
        brief = validation_value.get("brief")
        if isinstance(brief, dict):
            brief.setdefault(
                "snapshot",
                {
                    "path": f"<legacy-v{original_run_version}-unattested-brief>",
                    "size": 0,
                    "sha256": str(brief.get("sha256") or "0" * 64),
                },
            )
            brief.setdefault(
                "approval_snapshot",
                {
                    "path": f"<legacy-v{original_run_version}-unattested-approval>",
                    "size": 0,
                    "sha256": "0" * 64,
                },
            )
        budget = validation_value.setdefault("budget", {})
        if isinstance(budget, dict):
            gpu = budget.setdefault("estimated_gpu_hours", 0.0)
            cpu = budget.setdefault("estimated_cpu_hours", 0.0)
            storage = budget.setdefault("estimated_storage_gb", 0.0)
            budget.setdefault("actual_output_storage_gb", None)
            budget.setdefault(
                "hard_limits",
                {"gpu_hours": None, "cpu_hours": None, "storage_gb": None},
            )
            budget.setdefault(
                "committed_before",
                {"gpu_hours": 0.0, "cpu_hours": 0.0, "storage_gb": 0.0},
            )
            budget.setdefault(
                "requested",
                {
                    "gpu_hours": gpu,
                    "cpu_hours": cpu,
                    "storage_gb": storage,
                },
            )
            budget.setdefault(
                "committed_after",
                {
                    "gpu_hours": gpu,
                    "cpu_hours": cpu,
                    "storage_gb": storage,
                },
            )
            budget.setdefault("violations", [])
        validation_value.setdefault("failure", None)
        if original_run_version == 1:
            validation_value.setdefault(
                "change_scope",
                {
                    "repository_profile": {
                        "path": "<legacy-v1-unattested>",
                        "size": 0,
                        "sha256": "0" * 64,
                    },
                    "changeset": None,
                    "validation": None,
                    "before": {
                        "schema_version": 1,
                        "study_id": str(value.get("study_id") or "SC-0000"),
                        "outcome": "ADVISORY",
                        "git": {},
                        "changeset": None,
                        "validation": None,
                        "changed_paths": [],
                        "violations": [],
                        "advisories": [
                            "legacy V1 Run has no attested repository change scope"
                        ],
                    },
                    "after": {
                        "schema_version": 1,
                        "study_id": str(value.get("study_id") or "SC-0000"),
                        "outcome": "ADVISORY",
                        "git": {},
                        "changeset": None,
                        "validation": None,
                        "changed_paths": [],
                        "violations": [],
                        "advisories": [
                            "legacy V1 Run has no attested repository change scope"
                        ],
                    },
                    "evidence_eligible": False,
                },
            )
            legacy_scope = validation_value.get("change_scope")
            if isinstance(legacy_scope, dict):
                legacy_scope.setdefault("validation", None)
                for stage in ("before", "after"):
                    check = legacy_scope.get(stage)
                    if isinstance(check, dict):
                        check.setdefault("validation", None)
    elif original_run_version == 3:
        # V3 was the first durable-ledger Run contract, but predates explicit
        # exploratory/confirmatory provenance.  Its immutable absence of an
        # epistemic role is always interpreted as exploratory; it can never be
        # upgraded after the fact merely by changing a label.
        if "epistemic_role" in value:
            return [
                ValidationIssue(
                    "ERROR",
                    str(path),
                    "$: additional property is not allowed: 'epistemic_role'",
                )
            ]
        validation_value = copy.deepcopy(value)
        validation_value["schema_version"] = 4
        validation_value["epistemic_role"] = {
            "mode": "exploratory",
            "confirmation_id": None,
            "confirmation_sha256": None,
            "slot_id": None,
        }
    return [
        ValidationIssue("ERROR", str(path), message)
        for message in validate_schema_instance(validation_value, schema)
    ]


def parse_brief_metadata(
    text: str, *, allow_legacy_hard_budget: bool = False
) -> dict[str, Any]:
    if (
        text.count("STUDYCTL-METADATA-BEGIN") != 1
        or text.count("STUDYCTL-METADATA-END") != 1
    ):
        raise ValidationError("Brief must contain exactly one STUDYCTL-METADATA block")
    matches = list(re.finditer(
        r"<!--\s*STUDYCTL-METADATA-BEGIN\s*(\{.*?\})\s*STUDYCTL-METADATA-END\s*-->",
        text,
        flags=re.DOTALL,
    ))
    if len(matches) != 1:
        raise ValidationError("Brief is missing the STUDYCTL-METADATA block")
    match = matches[0]
    def reject_constant(value: str) -> None:
        raise ValidationError(f"non-finite Brief metadata number is not allowed: {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValidationError(f"duplicate Brief metadata key: {key!r}")
            result[key] = item
        return result

    try:
        value = json.loads(
            match.group(1),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Brief metadata is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("brief_version"), int):
        raise ValidationError("Brief metadata must contain integer brief_version")
    if isinstance(value.get("brief_version"), bool):
        raise ValidationError("Brief metadata brief_version must be an integer, not boolean")
    if "hard_budget" in value and not allow_legacy_hard_budget:
        raise ValidationError(
            "Brief hard_budget must appear only in the visible STUDYCTL-HARD-BUDGET block"
        )
    return value


def brief_text_issues(path: Path, text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for heading in REQUIRED_BRIEF_HEADINGS:
        if re.search(rf"^##\s+{re.escape(heading)}\s*$", text, flags=re.MULTILINE) is None:
            issues.append(ValidationIssue("ERROR", str(path), f"missing heading: {heading}"))
    if "[REPLACE:" in text or "[REPLACE]" in text:
        issues.append(ValidationIssue("ERROR", str(path), "Brief still contains replacement placeholders"))
    try:
        metadata = parse_brief_metadata(text)
        if metadata["brief_version"] < 1:
            issues.append(ValidationIssue("ERROR", str(path), "brief_version must be at least 1"))
    except ValidationError as exc:
        issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    try:
        parse_brief_hard_budget(text)
    except ValidationError as exc:
        issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues


def brief_content_issues(paths: StudyPaths) -> list[ValidationIssue]:
    try:
        text = paths.brief.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [ValidationIssue("ERROR", str(paths.brief), f"cannot read Brief: {exc}")]
    return brief_text_issues(paths.brief, text)


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


def run_manifest_paths(paths: StudyPaths) -> list[Path]:
    if not paths.runs.is_dir():
        return []
    return sorted(paths.runs.glob("RUN-*/manifest.json"))


def run_registry_structure_issues(paths: StudyPaths) -> list[ValidationIssue]:
    """Detect allocated Run directories that are absent from manifest scans."""

    issues: list[ValidationIssue] = []
    if not paths.runs.is_dir():
        return issues
    for entry in sorted(paths.runs.iterdir(), key=lambda item: item.name):
        if re.fullmatch(
            r"\.RUN-[0-9]{6}\..+\.registration\.tmp",
            entry.name,
        ):
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(entry),
                    "unfinished Run registration staging directory is present",
                )
            )
            continue
        if entry.name.startswith(".ledger.json.") and entry.name.endswith(".tmp"):
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(entry),
                    "unfinished Run-ledger temporary file is present",
                )
            )
            continue
        if entry.name == ".registry.lock":
            if entry.is_symlink() or not entry.is_file():
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(entry),
                        "Run registry lock must be a regular file",
                    )
                )
            continue
        if not entry.name.startswith("RUN-"):
            continue
        if ID_PATTERNS["run"].fullmatch(entry.name) is None:
            issues.append(
                ValidationIssue("ERROR", str(entry), "malformed Run directory name")
            )
            continue
        if entry.is_symlink() or not entry.is_dir():
            issues.append(
                ValidationIssue(
                    "ERROR", str(entry), "Run registry entry must be a directory"
                )
            )
            continue
        manifest_path = entry / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(entry),
                    "Run directory is missing a regular manifest.json",
                )
            )
        for child in entry.iterdir():
            if child.name.startswith(".manifest.json.") and child.name.endswith(
                ".tmp"
            ):
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(child),
                        "unfinished Run Manifest temporary file is present",
                    )
                )
    return issues


def run_ledger_issues(
    paths: StudyPaths,
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> list[ValidationIssue]:
    path = ledger_path(paths)
    try:
        ledger_temps = sorted(
            paths.study.glob(".RUNS.ledger.json.*.tmp")
        )
        if ledger_temps:
            return [
                ValidationIssue(
                    "ERROR",
                    str(item),
                    "unfinished Run-ledger temporary file is present",
                )
                for item in ledger_temps
            ]
        current = load_ledger(paths)
        if current is None:
            if any(
                manifest.get("schema_version") in {3, 4}
                for _, manifest in runs.values()
            ):
                return [
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Run ledger is missing for current V3/V4 Run history",
                    )
                ]
            if runs:
                return [
                    ValidationIssue(
                        "WARNING",
                        str(path),
                        "legacy Run history has not yet been indexed into the durable ledger",
                    )
                ]
            return [
                ValidationIssue(
                    "ERROR",
                    str(path),
                    "Run ledger is missing; Run identity and budget history cannot be verified",
                )
            ]
        reconciled = bootstrap_or_reconcile_ledger(
            paths, runs, write=False
        )
        if reconciled != current:
            return [
                ValidationIssue(
                    "ERROR",
                    str(path),
                    "Run ledger is stale relative to visible immutable Manifests",
                )
            ]
        issues: list[ValidationIssue] = []
        previous_watermark = 0
        high_water_mark = int(current["high_water_mark"])
        for checkpoint_path in checkpoint_paths(paths):
            checkpoint = load_json(checkpoint_path)
            if not isinstance(checkpoint, dict):
                continue
            watermarks = checkpoint.get("active_context_watermarks")
            if not isinstance(watermarks, dict):
                continue
            watermark = watermarks.get("run_count")
            if (
                isinstance(watermark, bool)
                or not isinstance(watermark, int)
                or watermark < 0
            ):
                continue
            if watermark < previous_watermark:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(checkpoint_path),
                        "Checkpoint Run watermark regressed from "
                        f"{previous_watermark} to {watermark}",
                    )
                )
            if watermark > high_water_mark:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Run ledger high_water_mark is below Checkpoint "
                        f"{checkpoint.get('checkpoint_id')} watermark {watermark}",
                    )
                )
            previous_watermark = max(previous_watermark, watermark)
        return issues
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        return [ValidationIssue("ERROR", str(path), str(exc))]


def evidence_paths(paths: StudyPaths) -> list[Path]:
    if not paths.evidence.is_dir():
        return []
    return sorted(paths.evidence.glob("EVID-*.v*.json"))


def observation_paths(paths: StudyPaths) -> list[Path]:
    if not paths.observations.is_dir():
        return []
    return sorted(paths.observations.glob("OBS-*.v*.json"))


def checkpoint_paths(paths: StudyPaths) -> list[Path]:
    if not paths.checkpoints.is_dir():
        return []
    return sorted(paths.checkpoints.glob("CHECKPOINT-*.json"))


def checkpoint_sequence_issues(paths: StudyPaths) -> list[ValidationIssue]:
    """Reject Checkpoint deletion, renaming, gaps, and tail rollback."""

    issues = [
        ValidationIssue(
            "ERROR",
            str(path),
            "unfinished Checkpoint-sequence temporary file is present",
        )
        for path in checkpoint_sequence_temporary_paths(paths)
    ]
    try:
        sequence = load_checkpoint_sequence(paths)
        if sequence is None:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.checkpoint_sequence),
                    "Checkpoint sequence is missing; Checkpoint history cannot be verified",
                )
            )
            return issues
        visible = checkpoint_paths(paths)
        high_water_mark = int(sequence["high_water_mark"])
        expected_names = [
            f"CHECKPOINT-{number:06d}.json"
            for number in range(1, high_water_mark + 1)
        ]
        actual_names = [path.name for path in visible]
        if actual_names != expected_names:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.checkpoint_sequence),
                    "visible Checkpoints do not match the monotone sequence; "
                    "a Checkpoint may be missing, renamed, duplicated, or unindexed",
                )
            )
        latest = sequence.get("latest_checkpoint")
        if high_water_mark == 0:
            if visible:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.checkpoint_sequence),
                        "Checkpoint sequence is empty but Checkpoint files exist",
                    )
                )
        elif isinstance(latest, dict) and visible:
            tail = load_json(visible[-1])
            if not isinstance(tail, dict):
                raise ValidationError("latest Checkpoint must be an object")
            if (
                latest.get("checkpoint_id") != tail.get("checkpoint_id")
                or latest.get("sha256") != tail.get("checkpoint_sha256")
            ):
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.checkpoint_sequence),
                        "Checkpoint sequence tail binding does not match the latest Checkpoint",
                    )
                )
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        issues.append(
            ValidationIssue("ERROR", str(paths.checkpoint_sequence), str(exc))
        )
    return issues


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
    structure_errors = errors_only(run_registry_structure_issues(paths))
    if structure_errors:
        details = "\n".join(issue.render() for issue in structure_errors)
        raise ValidationError(f"Run registry structure is invalid:\n{details}")
    index: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in run_manifest_paths(paths):
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(f"Run manifest is not an object: {path}")
        schema_errors = errors_only(
            object_schema_issues(paths.root, "run", path, value)
        )
        if schema_errors:
            details = "\n".join(issue.render() for issue in schema_errors)
            raise ValidationError(f"Run manifest schema is invalid:\n{details}")
        run_id = str(value.get("run_id", ""))
        require_id("run", run_id)
        if value.get("study_id") != paths.study_id:
            raise ValidationError(
                f"Run {run_id} study_id does not match Study directory"
            )
        if path.parent.name != run_id:
            raise ValidationError(
                f"Run {run_id} identity does not match directory {path.parent.name}"
            )
        if value.get("schema_version") in {1, 2, 3, 4}:
            # Budget registration is a hard authorization boundary. Validate
            # its reservation and terminal digest before any caller can use
            # this index to admit a later Run.
            manifest_budget_commitment(value)
        if run_id in index:
            raise ValidationError(f"duplicate Run ID: {run_id}")
        index[run_id] = (path, value)
    _, ownership_conflicts = run_output_ownership(paths.root, index)
    if ownership_conflicts:
        output_path, first_run, _, second_run, _ = ownership_conflicts[0]
        try:
            display_path = output_path.relative_to(paths.root.absolute()).as_posix()
        except ValueError:
            display_path = str(output_path)
        raise ValidationError(
            f"Run output path {display_path} is claimed by multiple Runs: "
            f"{first_run}, {second_run}"
        )
    return index


def _resolve_recorded_path(
    root: Path, raw: str | os.PathLike[str]
) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def normalized_run_output_key(
    root: Path, raw: str | os.PathLike[str]
) -> Path:
    """Return a lexical ownership key without following output symlinks."""

    return Path(os.path.abspath(os.fspath(_resolve_recorded_path(root, raw))))


def run_output_ownership(
    root: Path,
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[
    dict[Path, tuple[str, Path]],
    list[tuple[Path, str, Path, str, Path]],
]:
    """Index declared output paths and report duplicate Run ownership.

    Every declaration reserves its lexical path, including declarations that
    were absent at terminal sealing or belong to failed/incomplete Runs.  The
    key deliberately avoids ``resolve()`` so a later-created symlink cannot
    change which path was reserved.
    """

    owners: dict[Path, tuple[str, Path]] = {}
    conflicts: list[tuple[Path, str, Path, str, Path]] = []
    for run_id, (manifest_path, manifest) in sorted(runs.items()):
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list):
            continue
        for record in outputs:
            if not isinstance(record, dict):
                continue
            raw = record.get("path")
            if not isinstance(raw, str) or not raw:
                continue
            key = normalized_run_output_key(root, raw)
            previous = owners.get(key)
            if previous is None:
                owners[key] = (run_id, manifest_path)
                continue
            previous_run_id, previous_manifest_path = previous
            conflicts.append(
                (
                    key,
                    previous_run_id,
                    previous_manifest_path,
                    run_id,
                    manifest_path,
                )
            )
    return owners, conflicts


def _has_symlink_component(path: Path) -> bool:
    """Return whether any existing component of ``path`` is a symbolic link."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _recorded_regular_file_issue(
    root: Path,
    record: dict[str, Any],
    *,
    label: str,
    missing_level: str,
) -> list[ValidationIssue]:
    raw = record.get("path")
    if not isinstance(raw, str) or not raw:
        return [ValidationIssue("ERROR", label, "recorded path is missing")]
    path = _resolve_recorded_path(root, raw)
    if _has_symlink_component(path):
        return [ValidationIssue("ERROR", raw, f"{label} uses a symbolic-link path")]
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return [ValidationIssue(missing_level, raw, f"{label} is unavailable")]
    except OSError as exc:
        return [ValidationIssue("ERROR", raw, f"cannot inspect {label}: {exc}")]
    if not stat.S_ISREG(metadata.st_mode):
        return [ValidationIssue("ERROR", raw, f"{label} is not a regular file")]

    issues: list[ValidationIssue] = []
    expected_size = record.get("size")
    if expected_size != metadata.st_size:
        issues.append(
            ValidationIssue(
                "ERROR",
                raw,
                f"{label} size mismatch: recorded {expected_size!r}, current {metadata.st_size}",
            )
        )
    expected_hash = record.get("sha256")
    try:
        current_hash = sha256_file(path)
    except WorkflowError as exc:
        issues.append(ValidationIssue("ERROR", raw, f"cannot hash {label}: {exc}"))
    else:
        if expected_hash != current_hash:
            issues.append(ValidationIssue("ERROR", raw, f"{label} hash mismatch"))
    return issues


def _run_brief_authority_issues(
    paths: StudyPaths,
    manifest: dict[str, Any],
    *,
    for_evidence: bool,
) -> list[ValidationIssue]:
    """Verify the immutable Brief and approval that authorized a V3/V4 Run."""

    if manifest.get("schema_version") not in {3, 4}:
        return []
    issues: list[ValidationIssue] = []
    run_id = str(manifest.get("run_id") or "<unknown Run>")
    brief = manifest.get("brief")
    if not isinstance(brief, dict):
        return [ValidationIssue("ERROR", run_id, "Run Brief authority is invalid")]

    missing_level = (
        "WARNING"
        if manifest.get("status") == "incomplete" and not for_evidence
        else "ERROR"
    )
    resolved: dict[str, Path] = {}
    for key, label in (
        ("snapshot", "Run Brief snapshot"),
        ("approval_snapshot", "Run Brief-approval snapshot"),
    ):
        record = brief.get(key)
        if not isinstance(record, dict):
            issues.append(
                ValidationIssue("ERROR", run_id, f"{label} record is invalid")
            )
            continue
        record_issues = _recorded_regular_file_issue(
            paths.root,
            record,
            label=label,
            missing_level=missing_level,
        )
        issues.extend(record_issues)
        if not record_issues:
            resolved[key] = _resolve_recorded_path(
                paths.root, str(record["path"])
            )

    snapshot_record = brief.get("snapshot")
    if (
        isinstance(snapshot_record, dict)
        and snapshot_record.get("sha256") != brief.get("sha256")
    ):
        issues.append(
            ValidationIssue(
                "ERROR",
                run_id,
                "Run Brief snapshot hash does not match brief.sha256",
            )
        )

    if set(resolved) != {"snapshot", "approval_snapshot"}:
        return issues

    try:
        snapshot_text = resolved["snapshot"].read_text(encoding="utf-8")
        snapshot_limits = parse_brief_hard_budget(snapshot_text)
        approval = load_json(resolved["approval_snapshot"])
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        issues.append(
            ValidationIssue(
                "ERROR", run_id, f"cannot verify Run Brief authority: {exc}"
            )
        )
        return issues

    budget = manifest.get("budget")
    if not isinstance(budget, dict) or budget.get("hard_limits") != snapshot_limits:
        issues.append(
            ValidationIssue(
                "ERROR",
                run_id,
                "Run hard limits do not match its immutable Brief snapshot",
            )
        )
    if not isinstance(approval, dict):
        issues.append(
            ValidationIssue("ERROR", run_id, "Run Brief approval is not an object")
        )
        return issues
    approval_schema_issues = object_schema_issues(
        paths.root,
        "brief_approval",
        resolved["approval_snapshot"],
        approval,
    )
    issues.extend(approval_schema_issues)
    if errors_only(approval_schema_issues):
        return issues
    if approval.get("approval_sha256") != record_digest(
        approval, "approval_sha256"
    ):
        issues.append(
            ValidationIssue(
                "ERROR", run_id, "Run Brief approval digest is invalid"
            )
        )
    if approval.get("approval_sha256") != brief.get("approval_sha256"):
        issues.append(
            ValidationIssue(
                "ERROR",
                run_id,
                "Run Brief approval does not match brief.approval_sha256",
            )
        )
    approved_brief = approval.get("brief")
    if not isinstance(approved_brief, dict) or (
        approved_brief.get("sha256") != brief.get("sha256")
        or approved_brief.get("path") != brief.get("path")
    ):
        issues.append(
            ValidationIssue(
                "ERROR",
                run_id,
                "Run Brief approval does not authorize the recorded Brief",
            )
        )
    if approval.get("study_id") != manifest.get("study_id"):
        issues.append(
            ValidationIssue(
                "ERROR",
                run_id,
                "Run Brief approval belongs to a different Study",
            )
        )
    protected = approval.get("protected_artifacts")
    formal = manifest.get("formal_artifacts")
    if isinstance(protected, dict) and isinstance(formal, list):
        for key, filename in (
            ("evaluator", "EVALUATOR.json"),
            ("dataset_split", "DATASET_SPLIT.json"),
            ("acceptance_criteria", "ACCEPTANCE_CRITERIA.json"),
        ):
            matches = [
                record
                for record in formal
                if isinstance(record, dict)
                and str(record.get("path", "")).endswith(
                    "/formal-artifacts/" + filename
                )
            ]
            approved_record = protected.get(key)
            if approved_record is None:
                if matches:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            run_id,
                            f"Run captured unapproved protected artifact {filename}",
                        )
                    )
                continue
            expected_hash = (
                approved_record.get("sha256")
                if isinstance(approved_record, dict)
                else None
            )
            if len(matches) != 1 or matches[0].get("sha256") != expected_hash:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        run_id,
                        f"Run protected-artifact snapshot does not match approval: {filename}",
                    )
                )
    return issues


def run_dependency_integrity_issues(
    paths: StudyPaths,
    manifest: dict[str, Any],
    *,
    for_evidence: bool,
) -> list[ValidationIssue]:
    """Reverify immutable Run logs, declared outputs, and declared inputs.

    A legacy V1 manifest remains a readable historical fact, but it predates
    the V2 dependency-integrity contract and therefore cannot support formal
    Evidence without a separate attestation mechanism.
    """
    issues: list[ValidationIssue] = []
    run_id = str(manifest.get("run_id") or "<unknown Run>")
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, int) and not isinstance(schema_version, bool) and schema_version == 1:
        issues.append(
            ValidationIssue(
                "ERROR" if for_evidence else "WARNING",
                run_id,
                "legacy V1 Run is historical and Evidence-ineligible without separate attestation",
            )
        )
    elif (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {2, 3, 4}
    ):
        issues.append(
            ValidationIssue("ERROR", run_id, f"unsupported Run schema_version: {schema_version!r}")
        )

    issues.extend(
        _run_brief_authority_issues(
            paths,
            manifest,
            for_evidence=for_evidence,
        )
    )

    change_scope = manifest.get("change_scope")
    if isinstance(change_scope, dict) and schema_version in {2, 3, 4}:
        for key, required in (
            ("repository_profile", True),
            ("changeset", False),
            ("validation", False),
        ):
            record = change_scope.get(key)
            if record is None:
                if required:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            run_id,
                            f"Run change-scope {key} snapshot is missing",
                        )
                    )
                continue
            if not isinstance(record, dict):
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        run_id,
                        f"Run change-scope {key} snapshot is invalid",
                    )
                )
                continue
            authority_issues = _recorded_regular_file_issue(
                paths.root,
                record,
                label=f"Run change-scope {key} snapshot",
                missing_level="ERROR" if for_evidence else "WARNING",
            )
            if not for_evidence:
                authority_issues = [
                    ValidationIssue(
                        "WARNING" if issue.level == "ERROR" else issue.level,
                        issue.path,
                        issue.message,
                    )
                    for issue in authority_issues
                ]
            issues.extend(authority_issues)

    formal_artifacts = manifest.get("formal_artifacts")
    if isinstance(formal_artifacts, list):
        for index, record in enumerate(formal_artifacts):
            if not isinstance(record, dict):
                continue
            formal_issues = _recorded_regular_file_issue(
                paths.root,
                record,
                label=f"formal artifact[{index}]",
                missing_level="ERROR" if for_evidence else "WARNING",
            )
            if not for_evidence:
                formal_issues = [
                    ValidationIssue(
                        "WARNING" if issue.level == "ERROR" else issue.level,
                        issue.path,
                        issue.message,
                    )
                    for issue in formal_issues
                ]
            issues.extend(formal_issues)

    logs = manifest.get("logs")
    if isinstance(logs, dict):
        for name in ("stdout", "stderr"):
            record = logs.get(name)
            if isinstance(record, dict):
                issues.extend(
                    _recorded_regular_file_issue(
                        paths.root,
                        record,
                        label=f"{name} log",
                        missing_level="ERROR",
                    )
                )

    outputs = manifest.get("outputs")
    if isinstance(outputs, list):
        for index, record in enumerate(outputs):
            if not isinstance(record, dict):
                continue
            raw = str(record.get("path") or f"output[{index}]")
            if record.get("present") is not True:
                issues.append(
                    ValidationIssue(
                        "ERROR" if for_evidence else "WARNING",
                        raw,
                        "declared Run output was not produced",
                    )
                )
                continue
            issues.extend(
                _recorded_regular_file_issue(
                    paths.root,
                    record,
                    label="Run output",
                    missing_level="ERROR" if for_evidence else "WARNING",
                )
            )

    inputs = manifest.get("inputs")
    if isinstance(inputs, list):
        for index, record in enumerate(inputs):
            if not isinstance(record, dict):
                continue
            raw = str(record.get("path") or f"input[{index}]")
            if record.get("changed_during_run") is not False:
                issues.append(
                    ValidationIssue(
                        "ERROR" if for_evidence else "WARNING",
                        raw,
                        "Run input changed or became unavailable during execution",
                    )
                )
            current_record = {
                "path": record.get("path"),
                "size": record.get("size"),
                "sha256": record.get("sha256_after"),
            }
            input_issues = _recorded_regular_file_issue(
                paths.root,
                current_record,
                label="Run input",
                missing_level="ERROR" if for_evidence else "WARNING",
            )
            if not for_evidence:
                input_issues = [
                    ValidationIssue(
                        "WARNING" if issue.level == "ERROR" else issue.level,
                        issue.path,
                        issue.message,
                    )
                    for issue in input_issues
                ]
            issues.extend(input_issues)
    return issues


def retained_run_output_budget_issues(
    paths: StudyPaths,
    manifest: dict[str, Any],
) -> list[ValidationIssue]:
    """Detect mutable/drifted retained outputs before admitting another Run.

    Missing files remain conservatively charged by the ledger and therefore do
    not lower the budget.  If a recorded output still exists, however, it must
    remain the same read-only regular file; otherwise its recorded byte charge
    is no longer a trustworthy upper bound on retained storage.
    """

    issues: list[ValidationIssue] = []
    run_id = str(manifest.get("run_id") or "<unknown Run>")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        return issues
    for index, record in enumerate(outputs):
        if not isinstance(record, dict):
            continue
        recorded_present = record.get("present") is True
        raw = record.get("path")
        label = f"{run_id} output[{index}]"
        if not isinstance(raw, str) or not raw:
            issues.append(ValidationIssue("ERROR", label, "recorded output path is missing"))
            continue
        path = _resolve_recorded_path(paths.root, raw)
        if _has_symlink_component(path):
            issues.append(
                ValidationIssue("ERROR", raw, f"retained {label} uses a symbolic-link path")
            )
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            # Deletion never refunds the immutable ledger commitment.  A
            # future recreation will be checked before the next admission.
            continue
        except OSError as exc:
            issues.append(ValidationIssue("ERROR", raw, f"cannot inspect retained {label}: {exc}"))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            issues.append(ValidationIssue("ERROR", raw, f"retained {label} is not a regular file"))
            continue
        if not recorded_present:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    raw,
                    f"retained {label} exists but was absent or unverifiable when the Run was sealed",
                )
            )
            continue
        if metadata.st_mode & 0o222:
            issues.append(ValidationIssue("ERROR", raw, f"retained {label} is writable"))
        if metadata.st_size != record.get("size"):
            issues.append(ValidationIssue("ERROR", raw, f"retained {label} size changed"))
            continue
        try:
            digest = sha256_file(path)
        except OSError as exc:
            issues.append(ValidationIssue("ERROR", raw, f"cannot hash retained {label}: {exc}"))
            continue
        if digest != record.get("sha256"):
            issues.append(ValidationIssue("ERROR", raw, f"retained {label} content changed"))
    return issues


def sealed_run_evidence_eligible(manifest: dict[str, Any]) -> bool:
    schema_version = manifest.get("schema_version")
    if schema_version not in {2, 3, 4}:
        return False
    if manifest.get("status") not in {"succeeded", "failed", "interrupted"}:
        return False
    from .workspace import change_state_evidence_eligible

    change_scope = manifest.get("change_scope", {})
    boundary = manifest.get("execution_boundary", {})
    supported_backends = {"linux-bubblewrap", "macos-seatbelt"}
    backend = boundary.get("backend") if isinstance(boundary, dict) else None
    boundary_environment = (
        boundary.get("environment_variables") if isinstance(boundary, dict) else None
    )
    environment_binding_valid = (
        backend == "macos-seatbelt"
        and (
            boundary_environment is None
            or (
                isinstance(boundary_environment, dict)
                and boundary.get("environment_sha256")
                == sha256_json(boundary_environment)
            )
        )
    ) or (
        backend == "linux-bubblewrap"
        and isinstance(boundary_environment, dict)
        and boundary.get("environment_sha256") == sha256_json(boundary_environment)
    )
    backend_policy_valid = (
        backend == "macos-seatbelt"
        and boundary.get("policy_format") in {None, "seatbelt-profile-v1"}
        and boundary.get("output_staging") in {None, "direct"}
    ) or (
        backend == "linux-bubblewrap"
        and boundary.get("policy_format") == "bubblewrap-mount-policy-v1"
        and boundary.get("output_staging") == "private-copy-out"
    )
    dependency_state_valid = (
        isinstance(change_scope, dict)
        and change_state_evidence_eligible(change_scope.get("before", {}))
        and change_state_evidence_eligible(change_scope.get("after", {}))
        and all(
            record.get("changed_during_run") is False
            for record in manifest.get("inputs", [])
            if isinstance(record, dict)
        )
        and all(
            record.get("present") is True
            for record in manifest.get("outputs", [])
            if isinstance(record, dict)
        )
        and manifest.get("formalization", {}).get(
            "artifacts_unchanged_during_run"
        )
        is True
    )
    if schema_version == 2:
        # Frozen V2 predates the sealed execution-boundary field. Preserve its
        # original endpoint-integrity semantics without pretending that it had
        # a runtime sandbox. It remains permanently exploratory elsewhere.
        return (
            isinstance(change_scope, dict)
            and change_state_evidence_eligible(change_scope.get("before", {}))
            and change_state_evidence_eligible(change_scope.get("after", {}))
            and all(
                record.get("changed_during_run") is False
                for record in manifest.get("inputs", [])
                if isinstance(record, dict)
            )
            and all(
                record.get("present") is True
                for record in manifest.get("outputs", [])
                if isinstance(record, dict)
            )
            and manifest.get("formalization", {}).get(
                "artifacts_unchanged_during_run"
            )
            is True
        )
    return (
        dependency_state_valid
        and isinstance(boundary, dict)
        and boundary.get("mode") == "sealed"
        and boundary.get("declared_inputs_only") is True
        and boundary.get("backend") in supported_backends
        and backend_policy_valid
        and environment_binding_valid
        and isinstance(boundary.get("policy_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", boundary["policy_sha256"]) is not None
        and boundary.get("repository_write_access") is False
        and boundary.get("declared_outputs_only") is True
        and boundary.get("network_access") is False
    )


def effective_run_epistemic_mode(manifest: dict[str, Any]) -> str:
    """Return the only epistemic mode supported by the immutable Run bytes.

    V1--V3 predate the V4 role binding and are therefore permanently
    exploratory.  For V4, schema validation is responsible for the exact role
    shape; this helper still fails conservatively when used on malformed input.
    """

    if manifest.get("schema_version") != 4:
        return "exploratory"
    role = manifest.get("epistemic_role")
    if isinstance(role, dict) and role.get("mode") == "confirmatory":
        return "confirmatory"
    return "exploratory"


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
    issues: list[ValidationIssue] = run_registry_structure_issues(paths)
    runs: dict[str, tuple[Path, dict[str, Any]]] = {}
    cohort_ids: dict[str, str] = {}
    for path in run_manifest_paths(paths):
        try:
            manifest = load_json(path)
            if not isinstance(manifest, dict):
                raise ValidationError("manifest must be an object")
            schema_validation = object_schema_issues(
                paths.root, "run", path, manifest
            )
            issues.extend(schema_validation)
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
            if errors_only(schema_validation):
                # Schema errors already identify the malformed record. Do not
                # dereference invalid nested values or expose the record to
                # Evidence validation as if it were usable.
                continue
            runs[run_id] = (path, manifest)
            status = manifest.get("status")
            integrity = manifest.get("integrity", {})
            if status == "running":
                issues.append(ValidationIssue("ERROR", str(path), "Run is unsealed/running"))
                execution = manifest.get("execution", {})
                if any(
                    execution.get(key) is not None
                    for key in ("ended_at", "duration_seconds", "exit_code")
                ):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "running Run has terminal execution fields",
                        )
                    )
                if any(
                    integrity.get(key) is not None
                    for key in ("sealed_at", "manifest_sha256")
                ):
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "running Run has terminal integrity"
                        )
                    )
                if manifest.get("failure") is not None:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "running Run cannot record terminal failure"
                        )
                    )
                if manifest.get("change_scope", {}).get("evidence_eligible") is not False:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "running Run must be Evidence-ineligible"
                        )
                    )
            elif status == "incomplete":
                if not isinstance(manifest.get("failure"), dict):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "incomplete Run must record its failure phase",
                        )
                    )
                issues.append(
                    ValidationIssue(
                        "WARNING",
                        str(path),
                        "Run was sealed incomplete and cannot support Evidence",
                    )
                )
            if status != "running":
                execution = manifest.get("execution", {})
                if execution.get("ended_at") is None or execution.get(
                    "duration_seconds"
                ) is None:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "terminal Run is missing completion timing",
                        )
                    )
                if integrity.get("sealed_at") is None or integrity.get(
                    "manifest_sha256"
                ) is None:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "terminal Run is not sealed"
                        )
                    )
            if status == "succeeded" and manifest.get("execution", {}).get(
                "exit_code"
            ) != 0:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "succeeded Run must have exit_code 0"
                    )
                )
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
            change_scope = manifest.get("change_scope", {})
            if manifest.get("schema_version") in {2, 3, 4}:
                expected_eligibility = sealed_run_evidence_eligible(manifest)
                if change_scope.get("evidence_eligible") is not expected_eligibility:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "change_scope.evidence_eligible does not match sealed dependencies",
                        )
                    )
            if not change_scope.get("evidence_eligible", False):
                issues.append(
                    ValidationIssue(
                        "WARNING",
                        str(path),
                        "Run is not eligible for formal Evidence because its change scope is unverifiable or blocked",
                    )
                )
            budget = manifest.get("budget", {})
            if manifest.get("schema_version") in {3, 4} and isinstance(budget, dict):
                try:
                    manifest_budget_commitment(manifest)
                    expected_budget = budget_projection(
                        budget.get("hard_limits", {}),
                        budget.get("committed_before", {}),
                        budget.get("requested", {}),
                    )
                except ValidationError as exc:
                    issues.append(ValidationIssue("ERROR", str(path), str(exc)))
                else:
                    if budget.get("committed_after") != expected_budget[
                        "committed_after"
                    ]:
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                "Run budget committed_after is inconsistent",
                            )
                        )
                    if budget.get("violations") != expected_budget["violations"]:
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                "Run budget violations are inconsistent",
                            )
                        )
                    if expected_budget["violations"] and status != "incomplete":
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                "hard-budget violation requires incomplete status",
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
            if status != "running":
                issues.extend(
                    run_dependency_integrity_issues(
                        paths,
                        manifest,
                        for_evidence=False,
                    )
                )
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    _, ownership_conflicts = run_output_ownership(paths.root, runs)
    for output_path, first_run, first_manifest, second_run, second_manifest in (
        ownership_conflicts
    ):
        try:
            display_path = output_path.relative_to(paths.root.absolute()).as_posix()
        except ValueError:
            display_path = str(output_path)
        issues.append(
            ValidationIssue(
                "ERROR",
                str(second_manifest),
                f"Run output path {display_path} is claimed by multiple Runs: "
                f"{first_run} ({first_manifest}), {second_run} ({second_manifest})",
            )
        )
    return issues, runs


def _observation_issues(
    paths: StudyPaths,
) -> tuple[
    list[ValidationIssue],
    dict[tuple[str, int], tuple[Path, dict[str, Any]]],
]:
    """Validate optional Observation records without making them mandatory."""

    from .observation import (
        analysis_fingerprint,
        validate_observation_content,
    )

    issues: list[ValidationIssue] = []
    observations: dict[
        tuple[str, int], tuple[Path, dict[str, Any]]
    ] = {}
    fingerprints: dict[str, str] = {}
    for path in observation_paths(paths):
        try:
            item = load_json(path)
            if not isinstance(item, dict):
                raise ValidationError("Observation must be an object")
            schema_validation = object_schema_issues(
                paths.root, "observation", path, item
            )
            issues.extend(schema_validation)
            observation_id = str(item.get("observation_id", ""))
            version = int(item.get("version", 0))
            require_id("observation", observation_id)
            expected_name = f"{observation_id}.v{version:04d}.json"
            if path.name != expected_name:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), f"expected filename {expected_name}"
                    )
                )
            key = (observation_id, version)
            if key in observations:
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), f"duplicate Observation version {key}"
                    )
                )
            observations[key] = (path, item)
            if item.get("study_id") != paths.study_id:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Observation study_id does not match Study directory",
                    )
                )
            if not errors_only(schema_validation):
                try:
                    validate_observation_content(
                        paths,
                        item,
                        require_finalized=item.get("status") == "finalized",
                    )
                except (ValidationError, WorkflowError, OSError) as exc:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            f"Observation source or analysis is invalid: {exc}",
                        )
                    )
            if item.get("status") == "finalized":
                expected_fingerprint = analysis_fingerprint(item)
                actual_fingerprint = item.get("analysis_fingerprint_sha256")
                if actual_fingerprint != expected_fingerprint:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Observation analysis_fingerprint_sha256 does not match",
                        )
                    )
                if item.get("record_sha256") != record_digest(
                    item, "record_sha256"
                ):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Observation record_sha256 does not match",
                        )
                    )
                previous_id = fingerprints.setdefault(
                    str(actual_fingerprint), observation_id
                )
                if previous_id != observation_id:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "duplicate Observation analysis fingerprint already "
                            f"exists under {previous_id}",
                        )
                    )
            elif (
                item.get("record_sha256") is not None
                or item.get("analysis_fingerprint_sha256") is not None
            ):
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "draft Observation must not be sealed",
                    )
                )
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    return issues, observations


def observation_sequence_issues(paths: StudyPaths) -> list[ValidationIssue]:
    """Validate the durable Observation creation high-water authority."""

    issues = [
        ValidationIssue(
            "ERROR",
            str(path),
            "unfinished Observation-sequence temporary file is present",
        )
        for path in observation_sequence_temporary_paths(paths)
    ]
    try:
        sequence = load_observation_sequence(paths)
        if sequence is None:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.observation_sequence),
                    "Observation sequence is missing; creation history cannot be verified",
                )
            )
            return issues
        if int(sequence["high_water_mark"]) < len(observation_paths(paths)):
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.observation_sequence),
                    "Observation sequence high_water_mark is below the visible "
                    "Observation record count",
                )
            )
        high_water_mark = int(sequence["high_water_mark"])
        previous_watermark = 0
        for checkpoint_path in checkpoint_paths(paths):
            checkpoint = load_json(checkpoint_path)
            if not isinstance(checkpoint, dict):
                continue
            watermarks = checkpoint.get("active_context_watermarks")
            if not isinstance(watermarks, dict):
                continue
            watermark = watermarks.get("observation_record_count")
            if (
                isinstance(watermark, bool)
                or not isinstance(watermark, int)
                or watermark < 0
            ):
                continue
            if watermark > high_water_mark:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.observation_sequence),
                        "Observation sequence high_water_mark is below "
                        f"Checkpoint {checkpoint.get('checkpoint_id')} "
                        f"watermark {watermark}",
                    )
                )
            if watermark < previous_watermark:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(checkpoint_path),
                        "Checkpoint Observation watermark regressed from "
                        f"{previous_watermark} to {watermark}",
                    )
                )
            previous_watermark = max(previous_watermark, watermark)
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        issues.append(
            ValidationIssue("ERROR", str(paths.observation_sequence), str(exc))
        )
    return issues


def _evidence_issues(
    paths: StudyPaths,
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[list[ValidationIssue], dict[tuple[str, int], tuple[Path, dict[str, Any]]]]:
    # Local import avoids the validation/evidence module cycle while ensuring
    # ordinary full-Study validation replays the same deterministic epistemic
    # audit used at Evidence finalization.
    from .evidence import validate_evidence_basis

    issues: list[ValidationIssue] = []
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]] = {}
    for path in evidence_paths(paths):
        try:
            item = load_json(path)
            if not isinstance(item, dict):
                raise ValidationError("Evidence must be an object")
            schema_validation = object_schema_issues(
                paths.root, "evidence", path, item
            )
            issues.extend(schema_validation)
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
            if not errors_only(schema_validation):
                try:
                    validate_evidence_basis(paths, item)
                except (ValidationError, WorkflowError, OSError) as exc:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            f"Evidence epistemic basis is invalid: {exc}",
                        )
                    )
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
                if manifest.get("status") not in {
                    "succeeded",
                    "failed",
                    "interrupted",
                }:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            f"Evidence references non-terminal or incomplete Run: {run_id}",
                        )
                    )
                actual_fingerprints.add(manifest.get("cohort", {}).get("fingerprint_sha256"))
                if item.get("status") == "finalized" and any(
                    record.get("changed_during_run") for record in manifest.get("inputs", [])
                ):
                    issues.append(ValidationIssue("ERROR", str(path), f"finalized Evidence uses Run with changing input: {run_id}"))
                if item.get("status") == "finalized":
                    for dependency_issue in errors_only(
                        run_dependency_integrity_issues(
                            paths,
                            manifest,
                            for_evidence=True,
                        )
                    ):
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                f"finalized Evidence uses integrity-invalid Run {run_id}: "
                                f"{dependency_issue.message}",
                            )
                        )
                if item.get("status") == "finalized" and not manifest.get(
                    "change_scope", {}
                ).get("evidence_eligible", False):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            f"finalized Evidence uses Run with unverifiable or blocked change scope: {run_id}",
                        )
                    )
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
                inference = item.get("inference")
                if not isinstance(inference, dict):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "finalized Evidence requires an explicit inference object",
                        )
                    )
                else:
                    bridge = inference.get("observation_to_claim")
                    if not isinstance(bridge, str) or not bridge.strip():
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                "finalized Evidence requires inference.observation_to_claim",
                            )
                        )
                    for field in (
                        "auxiliary_assumptions",
                        "competing_explanations",
                        "falsification_conditions",
                    ):
                        values = inference.get(field)
                        if (
                            not isinstance(values, list)
                            or not values
                            or any(
                                not isinstance(value, str) or not value.strip()
                                for value in values
                            )
                        ):
                            issues.append(
                                ValidationIssue(
                                    "ERROR",
                                    str(path),
                                    "finalized Evidence requires at least one explicit "
                                    f"inference.{field} entry",
                                )
                            )
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


def _evidence_basis_mode(item: dict[str, Any]) -> str | None:
    """Return an explicitly declared, valid Evidence basis mode."""
    basis = item.get("evidence_basis")
    if isinstance(basis, dict) and basis.get("mode") in {
        "exploratory",
        "confirmatory",
        "mixed",
    }:
        return str(basis["mode"])
    return None


def _strong_confirmatory_evidence(
    paths: StudyPaths,
    item: dict[str, Any],
) -> bool:
    basis = item.get("evidence_basis")
    if not isinstance(basis, dict) or basis.get("mode") not in {
        "confirmatory",
        "mixed",
    }:
        return False
    campaign = basis.get("confirmation_campaign")
    campaign_confirmations = (
        campaign.get("confirmations") if isinstance(campaign, dict) else None
    )
    if not isinstance(campaign_confirmations, list) or not campaign_confirmations:
        return False
    latest_ref = campaign_confirmations[-1]
    if not isinstance(latest_ref, dict):
        return False
    try:
        from .confirmation import (
            confirmation_campaign_records,
            load_final_confirmation,
        )

        latest = load_final_confirmation(
            paths, str(latest_ref.get("confirmation_id", ""))
        )
        current_campaign = confirmation_campaign_records(paths, latest)
    except (OSError, ValidationError, WorkflowError):
        return False
    current_latest = current_campaign[-1]
    if (
        latest_ref.get("confirmation_id") != current_latest.get("confirmation_id")
        or latest_ref.get("sha256") != current_latest.get("record_sha256")
        or latest_ref.get("sequence")
        != current_latest.get("campaign", {}).get("sequence")
    ):
        return False
    held_out = basis.get("held_out")
    if not isinstance(held_out, dict):
        return False
    status = held_out.get("status")
    freshness = held_out.get("freshness")
    return (status == "held_out" and freshness == "fresh") or (
        status == "not_applicable" and freshness == "not_applicable"
    )


def evidence_sequence_issues(paths: StudyPaths) -> list[ValidationIssue]:
    """Validate the durable Evidence creation high-water authority."""

    issues = [
        ValidationIssue(
            "ERROR",
            str(path),
            "unfinished Evidence-sequence temporary file is present",
        )
        for path in evidence_sequence_temporary_paths(paths)
    ]
    try:
        sequence = load_evidence_sequence(paths)
        if sequence is None:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.evidence_sequence),
                    "Evidence sequence is missing; creation history cannot be verified",
                )
            )
            return issues
        high_water_mark = int(sequence["high_water_mark"])
        visible_record_count = len(evidence_paths(paths))
        if high_water_mark < visible_record_count:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.evidence_sequence),
                    "Evidence sequence high_water_mark is below the visible Evidence record count",
                )
            )
        previous_watermark = 0
        for checkpoint_path in checkpoint_paths(paths):
            checkpoint = load_json(checkpoint_path)
            if not isinstance(checkpoint, dict):
                continue
            watermarks = checkpoint.get("active_context_watermarks")
            if not isinstance(watermarks, dict):
                continue
            watermark = watermarks.get("evidence_record_count")
            if (
                isinstance(watermark, bool)
                or not isinstance(watermark, int)
                or watermark < 0
            ):
                continue
            if high_water_mark < watermark:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.evidence_sequence),
                        "Evidence sequence high_water_mark is below Checkpoint "
                        f"{checkpoint.get('checkpoint_id')} watermark {watermark}",
                    )
                )
            if watermark < previous_watermark:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(checkpoint_path),
                        "Checkpoint Evidence watermark regressed from "
                        f"{previous_watermark} to {watermark}",
                    )
                )
            previous_watermark = max(previous_watermark, watermark)
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        issues.append(
            ValidationIssue("ERROR", str(paths.evidence_sequence), str(exc))
        )
    return issues


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
        claims_by_id: dict[str, dict[str, Any]] = {}
        lifecycles: dict[str, str] = {}
        refs_by_claim: dict[str, dict[str, set[tuple[str, int]]]] = {}
        for claim in claims_data.get("claims", []):
            if not isinstance(claim, dict):
                issues.append(
                    ValidationIssue(
                        "ERROR", str(paths.claims), "CLAIMS.json contains a non-object Claim"
                    )
                )
                continue
            claim_id = str(claim.get("claim_id", ""))
            if claim_id in claim_ids:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"duplicate Claim ID: {claim_id}"))
            claim_ids.add(claim_id)
            claims_by_id[claim_id] = claim
            lifecycle = claim_lifecycle(claim)
            lifecycles[claim_id] = lifecycle
            superseded_by = claim.get("superseded_by")
            if lifecycle not in CLAIM_LIFECYCLES:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} has invalid lifecycle: {lifecycle!r}",
                    )
                )
            elif lifecycle == "superseded":
                if not isinstance(superseded_by, str) or not superseded_by:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(paths.claims),
                            f"superseded Claim {claim_id} requires superseded_by",
                        )
                    )
            elif superseded_by is not None:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} may set superseded_by only when lifecycle is superseded",
                    )
                )
            groups = {
                "supporting": {_ref_key(ref) for ref in claim.get("supporting_evidence", [])},
                "contradictory": {_ref_key(ref) for ref in claim.get("contradictory_evidence", [])},
                "other": {_ref_key(ref) for ref in claim.get("other_evidence", [])},
            }
            refs_by_claim[claim_id] = groups
            combined = list(groups.values())
            if any(combined[i] & combined[j] for i in range(3) for j in range(i + 1, 3)):
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} repeats Evidence across roles"))
            supporting_basis_modes: list[str] = []
            has_strong_confirmation = False
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
                    if group_name == "supporting" and item.get("status") == "finalized":
                        basis_mode = _evidence_basis_mode(item)
                        if basis_mode is None:
                            issues.append(
                                ValidationIssue(
                                    "ERROR",
                                    str(paths.claims),
                                    f"Claim {claim_id} uses Evidence {key} without a valid evidence_basis",
                                )
                            )
                        else:
                            supporting_basis_modes.append(basis_mode)
                        has_strong_confirmation = (
                            has_strong_confirmation
                            or _strong_confirmatory_evidence(paths, item)
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
            if not supporting_basis_modes:
                computed_basis = "none"
            elif set(supporting_basis_modes) == {"exploratory"}:
                computed_basis = "exploratory"
            elif set(supporting_basis_modes) == {"confirmatory"}:
                computed_basis = "confirmatory"
            else:
                computed_basis = "mixed"
            declared_basis = claim.get("evidence_basis")
            if declared_basis != computed_basis:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} evidence_basis {declared_basis!r} does not match supporting Evidence basis {computed_basis!r}",
                    )
                )
            if state in {"partially_supported", "numerically_supported"} and not groups["supporting"]:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} state {state} requires supporting Evidence"))
            if state == "partially_supported" and (
                not isinstance(claim.get("scope"), str)
                or not str(claim.get("scope")).strip()
            ):
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} state partially_supported requires an explicit bounded scope",
                    )
                )
            if state == "numerically_supported" and not has_strong_confirmation:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} state numerically_supported requires fresh held-out or not-applicable confirmatory Evidence",
                    )
                )
            if state == "contradicted" and not groups["contradictory"]:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} state contradicted requires contradictory Evidence"))
            if state == "inconclusive" and evidence_count == 0:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"Claim {claim_id} state inconclusive requires Evidence"))
            if state not in CLAIM_STATES:
                issues.append(ValidationIssue("ERROR", str(paths.claims), f"invalid agent Claim state: {state}"))
        frontier = claims_data.get("frontier")
        frontier_values = (
            frontier.get("claim_ids", []) if isinstance(frontier, dict) else []
        )
        if not isinstance(frontier_values, list):
            frontier_values = []
        frontier_ids = set(frontier_values)
        if len(frontier_values) != len(frontier_ids):
            issues.append(
                ValidationIssue(
                    "ERROR", str(paths.claims), "Frontier repeats a Claim ID"
                )
            )
        for missing in sorted(frontier_ids - claim_ids):
            issues.append(ValidationIssue("ERROR", str(paths.claims), f"Frontier references missing Claim: {missing}"))
        for claim_id in sorted(frontier_ids & claim_ids):
            if lifecycles.get(claim_id) != "active":
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Frontier Claim {claim_id} must have active lifecycle",
                    )
                )
        supersession_edges: dict[str, str] = {}
        for claim_id, claim in claims_by_id.items():
            if lifecycles.get(claim_id) != "superseded":
                continue
            target = claim.get("superseded_by")
            if not isinstance(target, str):
                continue
            if target == claim_id:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} cannot supersede itself",
                    )
                )
            elif target not in claims_by_id:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim {claim_id} superseded_by references missing Claim: {target}",
                    )
                )
            else:
                supersession_edges[claim_id] = target
        for origin in sorted(supersession_edges):
            seen: set[str] = set()
            current = origin
            cycle_found = False
            while current in supersession_edges:
                if current in seen:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(paths.claims),
                            f"Claim supersession cycle includes {origin}",
                        )
                    )
                    cycle_found = True
                    break
                seen.add(current)
                current = supersession_edges[current]
            if not cycle_found and lifecycles.get(current) != "active":
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"Claim supersession chain from {origin} must end at an "
                        f"active Claim; {current} has lifecycle "
                        f"{lifecycles.get(current)!r}",
                    )
                )
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


def _checkpoint_claim_lifecycles(
    item: dict[str, Any],
) -> tuple[dict[str, tuple[str, str | None]], list[str], list[str]]:
    states: dict[str, tuple[str, str | None]] = {}
    snapshot_ids: list[str] = []
    inactive_ids: list[str] = []
    for claim in item.get("claims_snapshot", []):
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        claim_id = str(claim["claim_id"])
        snapshot_ids.append(claim_id)
        target = claim.get("superseded_by")
        states[claim_id] = (
            claim_lifecycle(claim),
            target if isinstance(target, str) else None,
        )
    for ref in item.get("inactive_claim_refs", []):
        if not isinstance(ref, dict) or not isinstance(ref.get("claim_id"), str):
            continue
        claim_id = str(ref["claim_id"])
        inactive_ids.append(claim_id)
        target = ref.get("superseded_by")
        states[claim_id] = (
            str(ref.get("lifecycle", "")),
            target if isinstance(target, str) else None,
        )
    return states, snapshot_ids, inactive_ids


def _lifecycle_transition_message(
    claim_id: str,
    previous: tuple[str, str | None],
    current: tuple[str, str | None],
) -> str | None:
    previous_lifecycle, previous_target = previous
    current_lifecycle, current_target = current
    if previous_lifecycle == "active":
        return None
    if current_lifecycle != previous_lifecycle:
        return (
            f"Claim {claim_id} lifecycle regressed or changed from "
            f"{previous_lifecycle} to {current_lifecycle}"
        )
    if previous_lifecycle == "superseded" and current_target != previous_target:
        return (
            f"Claim {claim_id} changed superseded_by from "
            f"{previous_target!r} to {current_target!r}"
        )
    return None


def _checkpoint_claim_record_issues(
    paths: StudyPaths,
    checkpoint_path: Path,
    ref: dict[str, Any],
    claims_schema: dict[str, Any],
) -> tuple[list[ValidationIssue], dict[str, Any] | None]:
    """Verify one content-addressed non-Frontier Claim record."""

    issues: list[ValidationIssue] = []
    raw = ref.get("record_path")
    if not isinstance(raw, str) or not raw:
        return issues, None  # The schema already reports the missing/invalid field.
    lexical = Path(raw)
    if lexical.is_absolute() or ".." in lexical.parts:
        return [
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"unsafe Checkpoint Claim record path: {raw!r}",
            )
        ], None
    candidate = paths.root / lexical
    records_root = paths.checkpoints / "claim-records"
    claim_id = ref.get("claim_id")
    advertised_digest = ref.get("sha256")
    if isinstance(claim_id, str) and isinstance(advertised_digest, str):
        expected_path = (
            records_root / f"{claim_id}.{advertised_digest}.json"
        ).relative_to(paths.root).as_posix()
        if raw != expected_path:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(checkpoint_path),
                    "Checkpoint Claim record path is not the canonical content-addressed path: "
                    f"{raw!r}",
                )
            )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(records_root.resolve(strict=True))
    except (OSError, ValueError):
        return issues + [
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"Checkpoint Claim record is missing or outside claim-records: {raw!r}",
            )
        ], None
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        return issues + [
            ValidationIssue("ERROR", str(checkpoint_path), str(exc))
        ], None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return issues + [
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"Checkpoint Claim record must be a regular non-symlink file: {raw!r}",
            )
        ], None
    if metadata.st_nlink != 1:
        issues.append(
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"Checkpoint Claim record must not be hard-linked: {raw!r}",
            )
        )
    if metadata.st_mode & 0o222:
        issues.append(
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"Checkpoint Claim record must be sealed read-only: {raw!r}",
            )
        )
    try:
        claim = load_json(resolved)
    except ValidationError as exc:
        return issues + [ValidationIssue("ERROR", str(checkpoint_path), str(exc))], None
    if not isinstance(claim, dict):
        return issues + [
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"Checkpoint Claim record must contain an object: {raw!r}",
            )
        ], None
    for message in validate_schema_instance(
        claim,
        claims_schema["$defs"]["claim"],
        root_schema=claims_schema,
        location=f"$.inactive_claim_refs[{claim_id!r}]",
    ):
        issues.append(ValidationIssue("ERROR", str(checkpoint_path), message))
    digest = sha256_json(claim)
    if digest != ref.get("sha256"):
        issues.append(
            ValidationIssue(
                "ERROR",
                str(checkpoint_path),
                f"Checkpoint Claim record hash mismatch: {raw!r}",
            )
        )
    expected_fields = {
        "claim_id": claim.get("claim_id"),
        "lifecycle": claim_lifecycle(claim),
        "state": claim.get("state"),
        "superseded_by": claim.get("superseded_by"),
    }
    for field, expected in expected_fields.items():
        actual = ref.get(field)
        if actual is None and expected is None:
            continue
        if actual != expected:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(checkpoint_path),
                    f"Checkpoint Claim record {field} mismatch for {raw!r}",
                )
            )
    return issues, claim


def _checkpoint_issues(
    paths: StudyPaths,
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
    claims_data: dict[str, Any] | None = None,
    observations: dict[
        tuple[str, int], tuple[Path, dict[str, Any]]
    ] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    observation_records = observations or {}
    previous: dict[str, Any] | None = None
    previous_claim_states: dict[str, tuple[str, str | None]] = {}
    terminal_claim_states: dict[str, tuple[str, str | None]] = {}
    terminal_claim_digests: dict[str, str] = {}
    latest_known_claims: dict[str, dict[str, Any]] = {}
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
                if isinstance(claim, dict) and isinstance(
                    claim.get("claim_id"), str
                ):
                    latest_known_claims[str(claim["claim_id"])] = claim
            for message in validate_schema_instance(
                item.get("frontier"),
                claims_schema["$defs"]["frontier"],
                root_schema=claims_schema,
                location="$.frontier",
            ):
                issues.append(ValidationIssue("ERROR", str(path), message))
            claim_states, snapshot_ids, inactive_ids = _checkpoint_claim_lifecycles(
                item
            )
            if len(snapshot_ids) != len(set(snapshot_ids)):
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "Checkpoint repeats a Claim snapshot"
                    )
                )
            if len(inactive_ids) != len(set(inactive_ids)):
                issues.append(
                    ValidationIssue(
                        "ERROR", str(path), "Checkpoint repeats an inactive Claim ref"
                    )
                )
            for ref in item.get("inactive_claim_refs", []):
                if isinstance(ref, dict):
                    record_issues, archived_claim = _checkpoint_claim_record_issues(
                        paths, path, ref, claims_schema
                    )
                    issues.extend(record_issues)
                    if archived_claim is not None and isinstance(
                        archived_claim.get("claim_id"), str
                    ):
                        archived_id = str(archived_claim["claim_id"])
                        latest_known_claims[archived_id] = archived_claim
                        if claim_lifecycle(archived_claim) in {
                            "retired",
                            "superseded",
                        }:
                            archived_digest = sha256_json(archived_claim)
                            sealed_digest = terminal_claim_digests.get(archived_id)
                            if (
                                sealed_digest is not None
                                and archived_digest != sealed_digest
                            ):
                                issues.append(
                                    ValidationIssue(
                                        "ERROR",
                                        str(path),
                                        f"terminal Claim {archived_id} content changed after it was sealed",
                                    )
                                )
                            else:
                                terminal_claim_digests[archived_id] = archived_digest
            overlap = set(snapshot_ids) & set(inactive_ids)
            if overlap:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Checkpoint Claim snapshot and inactive refs overlap: "
                        + ", ".join(sorted(overlap)),
                    )
                )
            if "inactive_claim_refs" in item:
                frontier = item.get("frontier")
                frontier_ids = (
                    frontier.get("claim_ids", [])
                    if isinstance(frontier, dict)
                    and isinstance(frontier.get("claim_ids", []), list)
                    else []
                )
                if snapshot_ids != frontier_ids:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Checkpoint claims_snapshot must exactly follow Frontier Claim order",
                        )
                    )
                for claim_id in snapshot_ids:
                    if claim_states.get(claim_id, ("", None))[0] != "active":
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                f"Checkpoint active snapshot Claim {claim_id} is not active",
                            )
                        )
            for claim_id, current_state in sorted(claim_states.items()):
                historical_terminal = terminal_claim_states.get(claim_id)
                if historical_terminal is not None:
                    message = _lifecycle_transition_message(
                        claim_id, historical_terminal, current_state
                    )
                    if message is not None:
                        issues.append(ValidationIssue("ERROR", str(path), message))
                if current_state[0] in {"retired", "superseded"}:
                    terminal_claim_states[claim_id] = current_state
            for claim_id, previous_state in sorted(previous_claim_states.items()):
                if previous_state[0] == "active" and claim_id not in claim_states:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            f"active Claim {claim_id} disappeared without a lifecycle record",
                        )
                    )
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
            expected_observations: dict[
                tuple[str, int], dict[str, Any]
            ] = {}
            for field in ("decisive_evidence", "contradictory_evidence"):
                for reference in item.get(field, []):
                    if not isinstance(reference, dict):
                        continue
                    target = evidence.get(_ref_key(reference))
                    if target is None:
                        continue
                    observation_ref = target[1].get("observation_ref")
                    if isinstance(observation_ref, dict):
                        observation_key = (
                            str(observation_ref.get("observation_id")),
                            int(observation_ref.get("version", 0)),
                        )
                        expected_observations[observation_key] = observation_ref
            declared_observations: dict[
                tuple[str, int], dict[str, Any]
            ] = {}
            for reference in item.get("decisive_observations", []):
                if not isinstance(reference, dict):
                    continue
                observation_key = (
                    str(reference.get("observation_id")),
                    int(reference.get("version", 0)),
                )
                declared_observations[observation_key] = reference
                target = observation_records.get(observation_key)
                if target is None:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Checkpoint references missing Observation "
                            f"{observation_key}",
                        )
                    )
                    continue
                target_item = target[1]
                digest = target_item.get("record_sha256")
                if (
                    target_item.get("status") != "finalized"
                    or digest != record_digest(target_item, "record_sha256")
                ):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Checkpoint Observation is not valid/finalized "
                            f"{observation_key}",
                        )
                    )
                if reference.get("sha256") != digest:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Checkpoint Observation hash is stale "
                            f"{observation_key}",
                        )
                    )
            if declared_observations != expected_observations:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Checkpoint decisive_observations must exactly equal "
                        "Observation refs reached through decisive and "
                        "contradictory Evidence",
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
                    if manifest.get("status") not in {
                        "failed",
                        "interrupted",
                        "incomplete",
                    }:
                        issues.append(
                            ValidationIssue(
                                "ERROR",
                                str(path),
                                "representative Run is not failed/interrupted/incomplete: "
                                f"{run_id}",
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
            previous_claim_states = claim_states
        except (ValidationError, OSError, ValueError) as exc:
            issues.append(ValidationIssue("ERROR", str(path), str(exc)))
    if claims_data is not None:
        current_states: dict[str, tuple[str, str | None]] = {}
        for claim in claims_data.get("claims", []):
            if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
                continue
            target = claim.get("superseded_by")
            current_states[str(claim["claim_id"])] = (
                claim_lifecycle(claim),
                target if isinstance(target, str) else None,
            )
            claim_id = str(claim["claim_id"])
            lifecycle = claim_lifecycle(claim)
            if lifecycle in {"retired", "superseded"}:
                sealed_digest = terminal_claim_digests.get(claim_id)
                if sealed_digest is not None and sha256_json(claim) != sealed_digest:
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(paths.claims),
                            f"terminal Claim {claim_id} content changed after it was sealed",
                        )
                    )
            latest_known_claims[claim_id] = claim
        for claim_id, current_state in sorted(current_states.items()):
            historical_terminal = terminal_claim_states.get(claim_id)
            if historical_terminal is not None:
                message = _lifecycle_transition_message(
                    claim_id, historical_terminal, current_state
                )
                if message is not None:
                    issues.append(
                        ValidationIssue("ERROR", str(paths.claims), message)
                    )
        for claim_id, previous_state in sorted(previous_claim_states.items()):
            if previous_state[0] == "active" and claim_id not in current_states:
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(paths.claims),
                        f"active Claim {claim_id} disappeared without retirement or supersession",
                    )
                )
    supersession_edges: dict[str, str] = {}
    for claim_id, claim in sorted(latest_known_claims.items()):
        if claim_lifecycle(claim) != "superseded":
            continue
        target = claim.get("superseded_by")
        if not isinstance(target, str) or target not in latest_known_claims:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.claims),
                    f"historical superseded Claim {claim_id} references missing Claim {target!r}",
                )
            )
        elif target == claim_id:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.claims),
                    f"historical Claim {claim_id} cannot supersede itself",
                )
            )
        else:
            supersession_edges[claim_id] = target
    for origin in sorted(supersession_edges):
        seen: set[str] = set()
        current = origin
        while current in supersession_edges and current not in seen:
            seen.add(current)
            current = supersession_edges[current]
        if current in seen:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.claims),
                    f"historical Claim supersession cycle includes {origin}",
                )
            )
            continue
        terminal = latest_known_claims.get(current)
        if terminal is not None and claim_lifecycle(terminal) != "active":
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(paths.claims),
                    f"historical Claim supersession chain from {origin} must end at an active Claim; "
                    f"{current} has lifecycle {claim_lifecycle(terminal)!r}",
                )
            )
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
            raw_confirmation = item.get("confirmation", {})
            confirmation = raw_confirmation if isinstance(raw_confirmation, dict) else {}
            authorization = item.get("authorization")
            if authorization is None:
                expected_phrase = f"RECORD VERDICT {paths.study_id} {verdict_id}"
                if confirmation.get("typed_text") != expected_phrase:
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "Verdict confirmation phrase is invalid"
                        )
                    )
            else:
                if not isinstance(authorization, dict):
                    issues.append(
                        ValidationIssue(
                            "ERROR", str(path), "Verdict authorization must be an object"
                        )
                    )
                    authorization = {}
                instruction = authorization.get("instruction")
                if (
                    not isinstance(instruction, str)
                    or authorization.get("instruction_sha256") != sha256_json(instruction)
                ):
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Verdict authorization instruction hash is invalid",
                        )
                    )
                if confirmation.get("mode") != "agent_initiated":
                    issues.append(
                        ValidationIssue(
                            "ERROR",
                            str(path),
                            "Agent-initiated Verdict recording mode is invalid",
                        )
                    )
            scope = item.get("judged_scope", {})
            if (
                isinstance(scope, dict)
                and isinstance(scope.get("claims"), list)
                and not scope["claims"]
                and isinstance(item.get("scientific_verdict"), dict)
                and item["scientific_verdict"].get("decision")
                != "requires_more_evidence"
            ):
                issues.append(
                    ValidationIssue(
                        "ERROR",
                        str(path),
                        "Verdict without selected Claims must use scientific decision "
                        "requires_more_evidence",
                    )
                )
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
    # The Confirmation workflow calls validation primitives, so use a local
    # import while still replaying its immutable semantic checks here.
    from .confirmation import confirmation_record_issues, confirmation_run_issues
    from .observation_triggers import observation_trigger_registry_issues
    from .workspace import changeset_issues, repository_profile_issues

    issues.extend(repository_profile_issues(paths.root))
    issues.extend(observation_trigger_registry_issues(paths.root))
    try:
        paths.assert_safe_layout(must_exist=True)
    except (ValidationError, WorkflowError) as exc:
        issues.append(ValidationIssue("ERROR", str(paths.root), str(exc)))
        return issues
    issues.extend(changeset_issues(paths))
    issues.extend(brief_content_issues(paths))
    issues.extend(brief_approval_issues(paths))
    issues.extend(confirmation_record_issues(paths))
    run_issues, runs = _run_issues(paths)
    issues.extend(run_issues)
    issues.extend(run_ledger_issues(paths, runs))
    issues.extend(confirmation_run_issues(paths, runs))
    observation_issues, observations = _observation_issues(paths)
    issues.extend(observation_issues)
    issues.extend(observation_sequence_issues(paths))
    evidence_issues, evidence = _evidence_issues(paths, runs)
    issues.extend(evidence_issues)
    issues.extend(evidence_sequence_issues(paths))
    claim_issues, claims_data = _claims_issues(paths, evidence)
    issues.extend(claim_issues)
    issues.extend(_checkpoint_issues(paths, evidence, claims_data, observations))
    issues.extend(checkpoint_sequence_issues(paths))
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
        *observation_paths(paths),
        *evidence_paths(paths),
        *checkpoint_paths(paths),
        *sorted((paths.checkpoints / "claim-records").glob("*.json")),
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
