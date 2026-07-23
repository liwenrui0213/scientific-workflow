from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .hashing import atomic_write_json, load_json, record_digest
from .models import StudyPaths, ValidationError


_SEQUENCE_SCHEMA_VERSION = 1
_SEQUENCE_KEYS = {
    "schema_version",
    "study_id",
    "high_water_mark",
    "sequence_sha256",
}


def empty_observation_sequence(paths: StudyPaths) -> dict[str, Any]:
    """Return the digest-bound Observation creation authority for a new Study."""

    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "sequence_sha256": None,
    }
    value["sequence_sha256"] = record_digest(value, "sequence_sha256")
    return value


def validate_observation_sequence_value(
    paths: StudyPaths, value: Any
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Observation sequence must be a JSON object")
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError(
            "Observation sequence has missing or unsupported fields"
        )
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError("Observation sequence schema_version is unsupported")
    if value.get("study_id") != paths.study_id:
        raise ValidationError(
            "Observation sequence study_id does not match the Study"
        )
    high_water_mark = value.get("high_water_mark")
    if (
        isinstance(high_water_mark, bool)
        or not isinstance(high_water_mark, int)
        or high_water_mark < 0
    ):
        raise ValidationError(
            "Observation sequence high_water_mark must be a non-negative integer"
        )
    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Observation sequence digest is invalid")
    return deepcopy(value)


def load_observation_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.observation_sequence
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError(
            "Observation sequence must be a regular, non-symbolic-link file"
        )
    return validate_observation_sequence_value(paths, load_json(path))


def require_observation_sequence(paths: StudyPaths) -> dict[str, Any]:
    sequence = load_observation_sequence(paths)
    if sequence is None:
        raise ValidationError("Observation sequence is missing")
    return sequence


def write_observation_sequence(
    paths: StudyPaths,
    value: dict[str, Any],
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    candidate = deepcopy(value)
    candidate["sequence_sha256"] = None
    candidate["sequence_sha256"] = record_digest(candidate, "sequence_sha256")
    normalized = validate_observation_sequence_value(paths, candidate)
    atomic_write_json(
        paths.observation_sequence,
        normalized,
        overwrite=overwrite,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def reserve_observation_creation(
    paths: StudyPaths,
) -> tuple[dict[str, Any], int]:
    """Durably burn the next monotonic Observation creation number."""

    sequence = require_observation_sequence(paths)
    number = int(sequence["high_water_mark"]) + 1
    sequence["high_water_mark"] = number
    return write_observation_sequence(paths, sequence), number


def observation_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".OBSERVATIONS.sequence.json.*.tmp"))
