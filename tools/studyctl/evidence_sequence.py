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
    "origin",
    "sequence_sha256",
}
_ORIGIN_KEYS = {
    "kind",
    "visible_record_count",
    "checkpoint_high_water_mark",
    "pre_migration_deletion_assurance",
}
_NATIVE_ASSURANCE = "not_applicable"
_LEGACY_ASSURANCE = "unverifiable_before_sequence_initialization"


def empty_evidence_sequence(paths: StudyPaths) -> dict[str, Any]:
    """Return the digest-bound sequence authority for a new Study."""

    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "origin": {
            "kind": "native",
            "visible_record_count": 0,
            "checkpoint_high_water_mark": 0,
            "pre_migration_deletion_assurance": _NATIVE_ASSURANCE,
        },
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
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError("Evidence sequence has missing or unsupported fields")
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError("Evidence sequence schema_version is unsupported")
    if value.get("study_id") != paths.study_id:
        raise ValidationError("Evidence sequence study_id does not match the Study")

    high_water_mark = _nonnegative_integer(
        value.get("high_water_mark"),
        label="Evidence sequence high_water_mark",
    )
    origin = value.get("origin")
    if not isinstance(origin, dict) or set(origin) != _ORIGIN_KEYS:
        raise ValidationError("Evidence sequence origin has an invalid shape")
    visible_at_origin = _nonnegative_integer(
        origin.get("visible_record_count"),
        label="Evidence sequence origin visible_record_count",
    )
    checkpoint_at_origin = _nonnegative_integer(
        origin.get("checkpoint_high_water_mark"),
        label="Evidence sequence origin checkpoint_high_water_mark",
    )
    if checkpoint_at_origin > visible_at_origin:
        raise ValidationError(
            "Evidence sequence origin checkpoint high-water mark exceeds its "
            "visible record count"
        )
    if visible_at_origin > high_water_mark:
        raise ValidationError(
            "Evidence sequence high_water_mark is below its origin record count"
        )

    kind = origin.get("kind")
    assurance = origin.get("pre_migration_deletion_assurance")
    if kind == "native":
        if (
            visible_at_origin != 0
            or checkpoint_at_origin != 0
            or assurance != _NATIVE_ASSURANCE
        ):
            raise ValidationError("native Evidence sequence origin is invalid")
    elif kind == "legacy_migration":
        if assurance != _LEGACY_ASSURANCE:
            raise ValidationError(
                "legacy Evidence sequence must record the pre-migration deletion "
                "assurance gap"
            )
    else:
        raise ValidationError("Evidence sequence origin kind is unsupported")

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
        raise ValidationError(
            "Evidence sequence is missing; use migrate-evidence-sequence only "
            "for an intact pre-sequence Evidence history"
        )
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


def migrate_legacy_evidence_sequence(
    paths: StudyPaths,
    *,
    visible_record_count: int,
    checkpoint_high_water_mark: int,
) -> dict[str, Any]:
    """Create sequence authority from one explicitly inspected legacy history.

    A pre-sequence repository has no durable counter capable of proving that
    records were never deleted before migration.  The resulting digest-bound
    origin therefore records that unavoidable assurance gap rather than
    implying a proof that cannot be reconstructed.
    """

    if load_evidence_sequence(paths) is not None:
        raise ValidationError("Evidence sequence already exists")
    visible = _nonnegative_integer(
        visible_record_count, label="legacy visible Evidence record count"
    )
    checkpoint = _nonnegative_integer(
        checkpoint_high_water_mark,
        label="legacy checkpoint Evidence high-water mark",
    )
    if checkpoint > visible:
        raise ValidationError(
            "legacy Evidence history is not intact: a Checkpoint Evidence "
            "watermark exceeds the visible record count"
        )
    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": visible,
        "origin": {
            "kind": "legacy_migration",
            "visible_record_count": visible,
            "checkpoint_high_water_mark": checkpoint,
            "pre_migration_deletion_assurance": _LEGACY_ASSURANCE,
        },
        "sequence_sha256": None,
    }
    return write_evidence_sequence(paths, value, overwrite=False)


def evidence_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".EVIDENCE.sequence.json.*.tmp"))
