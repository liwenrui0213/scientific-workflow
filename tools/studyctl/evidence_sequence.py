from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .hashing import atomic_write_json, load_json, record_digest
from .models import StudyPaths, ValidationError


_SEQUENCE_SCHEMA_VERSION = 2
_SEQUENCE_KEYS = {
    "schema_version",
    "study_id",
    "high_water_mark",
    "sequence_sha256",
}


def empty_evidence_sequence(paths: StudyPaths) -> dict[str, Any]:
    """Return the digest-bound sequence authority for a new Study."""

    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "sequence_sha256": None,
    }
    value["sequence_sha256"] = record_digest(value, "sequence_sha256")
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def validate_evidence_sequence_value(
    paths: StudyPaths, value: Any
) -> dict[str, Any]:
    """Validate and normalize the complete Evidence sequence authority."""

    if not isinstance(value, dict):
        raise ValidationError("Evidence sequence must be a JSON object")
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError("Evidence sequence schema_version is unsupported")
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError("Evidence sequence has missing or unsupported fields")
    if value.get("study_id") != paths.study_id:
        raise ValidationError("Evidence sequence study_id does not match the Study")

    _nonnegative_integer(
        value.get("high_water_mark"),
        label="Evidence sequence high_water_mark",
    )

    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Evidence sequence digest is invalid")
    return deepcopy(value)


def load_evidence_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.evidence_sequence
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError(
            "Evidence sequence must be a regular, non-symbolic-link file"
        )
    return validate_evidence_sequence_value(paths, load_json(path))


def require_evidence_sequence(paths: StudyPaths) -> dict[str, Any]:
    sequence = load_evidence_sequence(paths)
    if sequence is None:
        raise ValidationError("Evidence sequence is missing")
    return sequence


def write_evidence_sequence(
    paths: StudyPaths,
    value: dict[str, Any],
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    candidate = deepcopy(value)
    candidate["sequence_sha256"] = None
    candidate["sequence_sha256"] = record_digest(candidate, "sequence_sha256")
    normalized = validate_evidence_sequence_value(paths, candidate)
    atomic_write_json(
        paths.evidence_sequence,
        normalized,
        overwrite=overwrite,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def reserve_evidence_creation(
    paths: StudyPaths,
) -> tuple[dict[str, Any], int]:
    """Durably burn the next monotonic creation sequence number."""

    sequence = require_evidence_sequence(paths)
    number = int(sequence["high_water_mark"]) + 1
    sequence["high_water_mark"] = number
    return write_evidence_sequence(paths, sequence), number


def evidence_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".EVIDENCE.sequence.json.*.tmp"))
