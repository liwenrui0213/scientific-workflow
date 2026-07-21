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
    "latest_checkpoint",
    "origin",
    "sequence_sha256",
}
_ORIGIN_KEYS = {
    "kind",
    "visible_checkpoint_count",
    "pre_migration_deletion_assurance",
}
_NATIVE_ASSURANCE = "not_applicable"
_LEGACY_ASSURANCE = "unverifiable_before_sequence_initialization"


def empty_checkpoint_sequence(paths: StudyPaths) -> dict[str, Any]:
    """Return the monotone Checkpoint-chain authority for a new Study."""

    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "latest_checkpoint": None,
        "origin": {
            "kind": "native",
            "visible_checkpoint_count": 0,
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


def _checkpoint_number(checkpoint_id: str) -> int:
    prefix = "CHECKPOINT-"
    if not checkpoint_id.startswith(prefix) or len(checkpoint_id) != len(prefix) + 6:
        raise ValidationError("Checkpoint sequence contains an invalid checkpoint_id")
    suffix = checkpoint_id[len(prefix) :]
    if not suffix.isdigit():
        raise ValidationError("Checkpoint sequence contains an invalid checkpoint_id")
    return int(suffix)


def validate_checkpoint_sequence_value(
    paths: StudyPaths, value: Any
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Checkpoint sequence must be a JSON object")
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError("Checkpoint sequence has missing or unsupported fields")
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError("Checkpoint sequence schema_version is unsupported")
    if value.get("study_id") != paths.study_id:
        raise ValidationError("Checkpoint sequence study_id does not match the Study")

    high_water_mark = _nonnegative_integer(
        value.get("high_water_mark"), label="Checkpoint sequence high_water_mark"
    )
    latest = value.get("latest_checkpoint")
    if high_water_mark == 0:
        if latest is not None:
            raise ValidationError(
                "empty Checkpoint sequence must not name a latest Checkpoint"
            )
    else:
        if not isinstance(latest, dict) or set(latest) != {
            "checkpoint_id",
            "sha256",
        }:
            raise ValidationError("Checkpoint sequence latest_checkpoint is invalid")
        checkpoint_id = latest.get("checkpoint_id")
        digest = latest.get("sha256")
        if not isinstance(checkpoint_id, str) or _checkpoint_number(
            checkpoint_id
        ) != high_water_mark:
            raise ValidationError(
                "Checkpoint sequence latest_checkpoint does not match its high-water mark"
            )
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError("Checkpoint sequence latest checkpoint hash is invalid")

    origin = value.get("origin")
    if not isinstance(origin, dict) or set(origin) != _ORIGIN_KEYS:
        raise ValidationError("Checkpoint sequence origin has an invalid shape")
    visible_at_origin = _nonnegative_integer(
        origin.get("visible_checkpoint_count"),
        label="Checkpoint sequence origin visible_checkpoint_count",
    )
    if visible_at_origin > high_water_mark:
        raise ValidationError(
            "Checkpoint sequence high_water_mark is below its origin checkpoint count"
        )
    kind = origin.get("kind")
    assurance = origin.get("pre_migration_deletion_assurance")
    if kind == "native":
        if visible_at_origin != 0 or assurance != _NATIVE_ASSURANCE:
            raise ValidationError("native Checkpoint sequence origin is invalid")
    elif kind == "legacy_migration":
        if assurance != _LEGACY_ASSURANCE:
            raise ValidationError(
                "legacy Checkpoint sequence must record the pre-migration deletion assurance gap"
            )
    else:
        raise ValidationError("Checkpoint sequence origin kind is unsupported")

    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Checkpoint sequence digest is invalid")
    return deepcopy(value)


def load_checkpoint_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.checkpoint_sequence
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError(
            "Checkpoint sequence must be a regular, non-symbolic-link file"
        )
    return validate_checkpoint_sequence_value(paths, load_json(path))


def require_checkpoint_sequence(paths: StudyPaths) -> dict[str, Any]:
    sequence = load_checkpoint_sequence(paths)
    if sequence is None:
        raise ValidationError(
            "Checkpoint sequence is missing; use migrate-checkpoint-sequence only "
            "for an intact pre-sequence Checkpoint history"
        )
    return sequence


def write_checkpoint_sequence(
    paths: StudyPaths,
    value: dict[str, Any],
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    candidate = deepcopy(value)
    candidate["sequence_sha256"] = None
    candidate["sequence_sha256"] = record_digest(candidate, "sequence_sha256")
    normalized = validate_checkpoint_sequence_value(paths, candidate)
    atomic_write_json(
        paths.checkpoint_sequence,
        normalized,
        overwrite=overwrite,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def advance_checkpoint_sequence(
    paths: StudyPaths,
    *,
    checkpoint_id: str,
    checkpoint_sha256: str,
) -> dict[str, Any]:
    sequence = require_checkpoint_sequence(paths)
    expected = int(sequence["high_water_mark"]) + 1
    if _checkpoint_number(checkpoint_id) != expected:
        raise ValidationError(
            f"Checkpoint sequence expected CHECKPOINT-{expected:06d}, got {checkpoint_id}"
        )
    sequence["high_water_mark"] = expected
    sequence["latest_checkpoint"] = {
        "checkpoint_id": checkpoint_id,
        "sha256": checkpoint_sha256,
    }
    return write_checkpoint_sequence(paths, sequence)


def migrate_legacy_checkpoint_sequence(
    paths: StudyPaths,
    checkpoints: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind an explicitly validated, contiguous legacy Checkpoint chain."""

    if load_checkpoint_sequence(paths) is not None:
        raise ValidationError("Checkpoint sequence already exists")
    for index, checkpoint in enumerate(checkpoints, start=1):
        checkpoint_id = checkpoint.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or _checkpoint_number(
            checkpoint_id
        ) != index:
            raise ValidationError(
                "legacy Checkpoint history must be contiguous from CHECKPOINT-000001"
            )
    latest = None
    if checkpoints:
        tail = checkpoints[-1]
        latest = {
            "checkpoint_id": tail["checkpoint_id"],
            "sha256": tail["checkpoint_sha256"],
        }
    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": len(checkpoints),
        "latest_checkpoint": latest,
        "origin": {
            "kind": "legacy_migration",
            "visible_checkpoint_count": len(checkpoints),
            "pre_migration_deletion_assurance": _LEGACY_ASSURANCE,
        },
        "sequence_sha256": None,
    }
    return write_checkpoint_sequence(paths, value, overwrite=False)


def checkpoint_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".CHECKPOINTS.sequence.json.*.tmp"))
