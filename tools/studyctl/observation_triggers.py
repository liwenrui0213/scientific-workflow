from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Sequence

from .hashing import load_json, record_digest
from .models import ValidationError, ValidationIssue


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_DIRECTORY = Path(
    "scientific-workflow/observation-trigger-registries"
)
_REGISTRY_NAME = re.compile(r"^v([0-9]{4,})\.json$")
_TRIGGER_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_STRUCTURAL_VALIDATORS = {
    "multiple_runs",
    "multiple_cohorts",
    "anomalies_or_failures",
    "confirmatory_use",
}
_REGISTRY_KEYS = {
    "schema_version",
    "registry_version",
    "previous_registry_sha256",
    "triggers",
    "registry_sha256",
}
_TRIGGER_KEYS = {
    "id",
    "kind",
    "validator",
    "description",
    "confirmatory_allowed",
    "governance",
}
_GOVERNANCE_KEYS = {
    "origin",
    "proposal",
    "independent_review",
    "human_adoption",
}
_PROPOSAL_KEYS = {
    "why_existing_triggers_are_insufficient",
    "expected_benefit",
    "abuse_risks",
}
_REVIEW_KEYS = {
    "assessment",
    "rationale",
    "reviewer_independence_statement",
}
_ADOPTION_KEYS = {
    "decision",
    "rationale",
    "human_authorization_statement",
}


def registry_directory(root: Path) -> Path:
    return root / REGISTRY_DIRECTORY


def registry_path(root: Path, version: int) -> Path:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError(
            "Observation trigger registry version must be a positive integer"
        )
    return registry_directory(root) / f"v{version:04d}.json"


def _require_exact_keys(
    value: Any,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    if set(value) != expected:
        raise ValidationError(f"{label} has missing or unsupported fields")
    return value


def _require_nonblank(value: Any, *, label: str, limit: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    if len(value) > limit:
        raise ValidationError(f"{label} exceeds {limit} characters")
    return value.strip()


def _validate_governance(
    value: Any,
    *,
    trigger_id: str,
) -> dict[str, Any]:
    governance = _require_exact_keys(
        value, _GOVERNANCE_KEYS, label=f"trigger {trigger_id} governance"
    )
    origin = governance.get("origin")
    if origin == "builtin":
        if any(
            governance.get(field) is not None
            for field in ("proposal", "independent_review", "human_adoption")
        ):
            raise ValidationError(
                f"builtin trigger {trigger_id} must not claim extension governance"
            )
        return governance
    if origin != "reviewed_extension":
        raise ValidationError(
            f"trigger {trigger_id} governance origin must be builtin or "
            "reviewed_extension"
        )

    proposal = _require_exact_keys(
        governance.get("proposal"),
        _PROPOSAL_KEYS,
        label=f"trigger {trigger_id} proposal",
    )
    _require_nonblank(
        proposal.get("why_existing_triggers_are_insufficient"),
        label=(
            f"trigger {trigger_id} proposal "
            "why_existing_triggers_are_insufficient"
        ),
    )
    _require_nonblank(
        proposal.get("expected_benefit"),
        label=f"trigger {trigger_id} proposal expected_benefit",
    )
    abuse_risks = proposal.get("abuse_risks")
    if (
        not isinstance(abuse_risks, list)
        or not abuse_risks
        or len(abuse_risks) > 32
        or any(not isinstance(item, str) or not item.strip() for item in abuse_risks)
        or any(len(item) > 2048 for item in abuse_risks)
    ):
        raise ValidationError(
            f"trigger {trigger_id} proposal abuse_risks must be a non-empty "
            "bounded list of non-empty strings"
        )

    review = _require_exact_keys(
        governance.get("independent_review"),
        _REVIEW_KEYS,
        label=f"trigger {trigger_id} independent review",
    )
    if review.get("assessment") != "endorsed":
        raise ValidationError(
            f"trigger {trigger_id} requires an endorsed independent review"
        )
    _require_nonblank(
        review.get("rationale"),
        label=f"trigger {trigger_id} independent review rationale",
    )
    _require_nonblank(
        review.get("reviewer_independence_statement"),
        label=(
            f"trigger {trigger_id} independent review "
            "reviewer_independence_statement"
        ),
    )

    adoption = _require_exact_keys(
        governance.get("human_adoption"),
        _ADOPTION_KEYS,
        label=f"trigger {trigger_id} human adoption",
    )
    if adoption.get("decision") != "adopted":
        raise ValidationError(
            f"trigger {trigger_id} requires an explicit human adoption decision"
        )
    _require_nonblank(
        adoption.get("rationale"),
        label=f"trigger {trigger_id} human adoption rationale",
    )
    _require_nonblank(
        adoption.get("human_authorization_statement"),
        label=(
            f"trigger {trigger_id} human adoption "
            "human_authorization_statement"
        ),
    )
    return governance


def _validate_trigger(
    value: Any,
) -> dict[str, Any]:
    trigger = _require_exact_keys(value, _TRIGGER_KEYS, label="trigger definition")
    trigger_id = trigger.get("id")
    if not isinstance(trigger_id, str) or _TRIGGER_ID.fullmatch(trigger_id) is None:
        raise ValidationError(
            "Observation promotion trigger id must match "
            "^[a-z][a-z0-9_]{2,63}$"
        )
    kind = trigger.get("kind")
    validator = trigger.get("validator")
    if kind == "structural":
        if validator not in _STRUCTURAL_VALIDATORS:
            raise ValidationError(
                f"structural trigger {trigger_id} requires a supported "
                "deterministic validator"
            )
    elif kind == "semantic":
        if validator is not None:
            raise ValidationError(
                f"semantic trigger {trigger_id} must not claim a structural validator"
            )
    else:
        raise ValidationError(
            f"trigger {trigger_id} kind must be structural or semantic"
        )
    _require_nonblank(
        trigger.get("description"), label=f"trigger {trigger_id} description"
    )
    if not isinstance(trigger.get("confirmatory_allowed"), bool):
        raise ValidationError(
            f"trigger {trigger_id} confirmatory_allowed must be boolean"
        )
    _validate_governance(
        trigger.get("governance"),
        trigger_id=trigger_id,
    )
    return trigger


def validate_registry_value(
    value: Any,
    *,
    path: Path,
    expected_version: int,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    registry = _require_exact_keys(
        value, _REGISTRY_KEYS, label="Observation trigger registry"
    )
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValidationError(
            "Observation trigger registry schema_version is unsupported"
        )
    if registry.get("registry_version") != expected_version:
        raise ValidationError(
            "Observation trigger registry version does not match its filename"
        )
    expected_previous = (
        None if previous is None else previous.get("registry_sha256")
    )
    if registry.get("previous_registry_sha256") != expected_previous:
        raise ValidationError(
            "Observation trigger registry previous_registry_sha256 does not "
            "match the preceding immutable version"
        )
    if registry.get("registry_sha256") != record_digest(
        registry, "registry_sha256"
    ):
        raise ValidationError(
            "Observation trigger registry registry_sha256 does not match"
        )
    raw_triggers = registry.get("triggers")
    if (
        not isinstance(raw_triggers, list)
        or not raw_triggers
        or len(raw_triggers) > 64
    ):
        raise ValidationError(
            "Observation trigger registry must contain between 1 and 64 triggers"
        )
    triggers = [
        _validate_trigger(item)
        for item in raw_triggers
    ]
    trigger_ids = [str(item["id"]) for item in triggers]
    if trigger_ids != sorted(trigger_ids):
        raise ValidationError(
            "Observation trigger registry triggers must be sorted by id"
        )
    if len(trigger_ids) != len(set(trigger_ids)):
        raise ValidationError(
            "Observation trigger registry contains duplicate trigger ids"
        )
    if previous is not None:
        previous_index = {
            str(item["id"]): item for item in previous.get("triggers", [])
        }
        current_index = {str(item["id"]): item for item in triggers}
        missing = sorted(set(previous_index) - set(current_index))
        if missing:
            raise ValidationError(
                "Observation trigger registry versions are append-only; removed: "
                + ", ".join(missing)
            )
        changed = sorted(
            trigger_id
            for trigger_id, definition in previous_index.items()
            if current_index.get(trigger_id) != definition
        )
        if changed:
            raise ValidationError(
                "Observation trigger registry versions must not reinterpret "
                "existing triggers; changed: "
                + ", ".join(changed)
            )
        for trigger_id in sorted(set(current_index) - set(previous_index)):
            governance = current_index[trigger_id]["governance"]
            if governance.get("origin") != "reviewed_extension":
                raise ValidationError(
                    f"new trigger {trigger_id} must be a reviewed_extension"
                )
    if path.name != f"v{expected_version:04d}.json":
        raise ValidationError(
            "Observation trigger registry filename is not canonical"
        )
    return registry


def load_registry_versions(root: Path) -> list[dict[str, Any]]:
    directory = registry_directory(root)
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError(
            "Observation trigger registry directory is missing or not a "
            "regular directory"
        )
    paths: list[tuple[int, Path]] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"Observation trigger registry entry must be a regular file: {path}"
            )
        if path.stat().st_size > 262144:
            raise ValidationError(
                f"Observation trigger registry entry exceeds 262144 bytes: {path}"
            )
        match = _REGISTRY_NAME.fullmatch(path.name)
        if match is None:
            raise ValidationError(
                f"unsupported Observation trigger registry entry: {path.name}"
            )
        paths.append((int(match.group(1)), path))
    versions = [version for version, _ in paths]
    if versions != list(range(1, len(paths) + 1)):
        raise ValidationError(
            "Observation trigger registry versions must be contiguous from 1"
        )
    records: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for version, path in paths:
        value = load_json(path)
        current = validate_registry_value(
            value,
            path=path,
            expected_version=version,
            previous=previous,
        )
        records.append(current)
        previous = current
    if not records:
        raise ValidationError(
            "Observation trigger registry must contain version 1"
        )
    return records


def load_current_registry(root: Path) -> dict[str, Any]:
    return load_registry_versions(root)[-1]


def load_bound_registry(
    root: Path,
    reference: Any,
) -> dict[str, Any]:
    ref = _require_exact_keys(
        reference,
        {"version", "sha256"},
        label="Observation promotion registry reference",
    )
    version = ref.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError(
            "Observation promotion registry version must be a positive integer"
        )
    records = load_registry_versions(root)
    if version > len(records):
        raise ValidationError(
            f"Observation promotion registry version does not exist: {version}"
        )
    registry = records[version - 1]
    if ref.get("sha256") != registry.get("registry_sha256"):
        raise ValidationError(
            "Observation promotion registry reference is stale"
        )
    return registry


def registry_binding(registry: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": registry["registry_version"],
        "sha256": registry["registry_sha256"],
    }


def normalize_registered_triggers(
    registry: dict[str, Any],
    triggers: Sequence[str],
) -> list[str]:
    if isinstance(triggers, (str, bytes)):
        raise ValidationError(
            "Observation promotion triggers must be supplied as a sequence"
        )
    registered = {
        str(item["id"]): item for item in registry.get("triggers", [])
    }
    normalized: list[str] = []
    seen: set[str] = set()
    for trigger in triggers:
        if not isinstance(trigger, str) or trigger not in registered:
            raise ValidationError(
                f"unsupported Observation promotion trigger: {trigger}"
            )
        if trigger in seen:
            raise ValidationError(
                f"duplicate Observation promotion trigger: {trigger}"
            )
        seen.add(trigger)
        normalized.append(trigger)
    if not normalized:
        raise ValidationError(
            "an Observation Record is optional and requires at least one "
            "registered promotion trigger"
        )
    return normalized


def selected_trigger_definitions(
    registry: dict[str, Any],
    triggers: Sequence[str],
) -> list[dict[str, Any]]:
    index = {str(item["id"]): item for item in registry.get("triggers", [])}
    return [index[trigger] for trigger in triggers]


def validate_trigger_applicability(
    definitions: Sequence[dict[str, Any]],
    *,
    run_count: int,
    cohort_count: int,
    anomaly_count: int,
    representative_failure_count: int,
    contains_confirmatory: bool,
) -> None:
    """Apply every registered structural predicate and confirmatory-use bound."""

    validators = {
        str(definition["validator"])
        for definition in definitions
        if definition.get("validator") is not None
    }
    predicate_results = {
        "multiple_runs": run_count >= 2,
        "multiple_cohorts": cohort_count >= 2,
        "anomalies_or_failures": (
            anomaly_count > 0 or representative_failure_count > 0
        ),
        "confirmatory_use": contains_confirmatory,
    }
    messages = {
        "multiple_runs": (
            "promotion trigger multiple_runs requires at least two Runs"
        ),
        "multiple_cohorts": (
            "promotion trigger multiple_cohorts requires at least two Cohorts"
        ),
        "anomalies_or_failures": (
            "promotion trigger anomalies_or_failures requires a recorded "
            "anomaly or representative failure"
        ),
        "confirmatory_use": (
            "promotion trigger confirmatory_use requires a confirmatory Run"
        ),
    }
    if set(predicate_results) != _STRUCTURAL_VALIDATORS or set(
        messages
    ) != _STRUCTURAL_VALIDATORS:
        raise ValidationError(
            "Observation structural-trigger validator registry is internally "
            "inconsistent"
        )
    for validator in sorted(validators):
        if validator not in predicate_results:
            raise ValidationError(
                f"no deterministic implementation exists for structural "
                f"Observation trigger validator: {validator}"
            )
        if not predicate_results[validator]:
            raise ValidationError(messages[validator])
    if contains_confirmatory and "confirmatory_use" not in validators:
        raise ValidationError(
            "an Observation containing a confirmatory Run requires the "
            "confirmatory_use trigger"
        )
    disallowed = sorted(
        str(definition["id"])
        for definition in definitions
        if contains_confirmatory
        and not definition.get("confirmatory_allowed", False)
    )
    if disallowed:
        raise ValidationError(
            "Observation promotion triggers are not approved for confirmatory "
            "use: "
            + ", ".join(disallowed)
        )


def observation_trigger_registry_issues(root: Path) -> list[ValidationIssue]:
    try:
        load_registry_versions(root)
    except (ValidationError, OSError, ValueError) as exc:
        return [
            ValidationIssue(
                "ERROR",
                str(registry_directory(root)),
                str(exc),
            )
        ]
    return []
