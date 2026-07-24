from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
import re
import stat
from typing import Any

from .hashing import (
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from .models import StudyPaths, ValidationError


_SEQUENCE_SCHEMA_VERSION = 3
_SEQUENCE_KEYS = {
    "schema_version",
    "study_id",
    "high_water_mark",
    "finalized_count",
    "finalized_inventory_sha256",
    "sequence_sha256",
}
_FINAL_NAME = re.compile(r"^(EVID-[0-9]{4,})\.v([0-9]{4,})\.json$")


def _require_regular_file(
    path: Path,
    *,
    label: str,
    sealed: bool,
) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValidationError(f"{label} must be a regular, non-linked file")
    if sealed and metadata.st_mode & 0o222:
        raise ValidationError(f"{label} must be sealed read-only")


def empty_evidence_sequence(paths: StudyPaths) -> dict[str, Any]:
    """Return the digest-bound sequence authority for a new Study."""

    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "finalized_count": 0,
        "finalized_inventory_sha256": sha256_json([]),
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
    _nonnegative_integer(
        value.get("finalized_count"),
        label="Evidence sequence finalized_count",
    )
    digest = value.get("finalized_inventory_sha256")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValidationError(
            "Evidence sequence finalized_inventory_sha256 is invalid"
        )

    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Evidence sequence digest is invalid")
    return deepcopy(value)


def load_evidence_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.evidence_sequence
    if not path.exists() and not path.is_symlink():
        return None
    _require_regular_file(path, label="Evidence sequence", sealed=True)
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


def finalized_evidence_inventory(paths: StudyPaths) -> list[dict[str, Any]]:
    """Return exact immutable bindings for all visible finalized Evidence."""

    inventory: list[dict[str, Any]] = []
    for path in sorted(paths.evidence.glob("EVID-*.v*.json")):
        match = _FINAL_NAME.fullmatch(path.name)
        if match is None:
            continue
        _require_regular_file(path, label="Evidence record", sealed=False)
        value = load_json(path)
        if not isinstance(value, dict) or value.get("status") != "finalized":
            continue
        _require_regular_file(path, label="finalized Evidence", sealed=True)
        evidence_id = match.group(1)
        version = int(match.group(2))
        if (
            value.get("evidence_id") != evidence_id
            or value.get("version") != version
        ):
            raise ValidationError(
                f"finalized Evidence identity does not match filename: {path}"
            )
        digest = value.get("record_sha256")
        if digest != record_digest(value, "record_sha256"):
            raise ValidationError(f"finalized Evidence digest is invalid: {path}")
        inventory.append(
            {
                "evidence_id": evidence_id,
                "version": version,
                "record_sha256": digest,
                "file_sha256": sha256_file(path),
            }
        )
    inventory.sort(key=lambda item: (item["evidence_id"], item["version"]))
    return inventory


def require_consistent_evidence_finalizations(
    paths: StudyPaths,
    inventory: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Require the sequence to bind every visible finalized Evidence record."""

    sequence = require_evidence_sequence(paths)
    current = list(
        finalized_evidence_inventory(paths) if inventory is None else inventory
    )
    if int(sequence["finalized_count"]) != len(current):
        raise ValidationError(
            "visible finalized Evidence does not match the sequence count; "
            "a record may be missing or left unindexed"
        )
    if sequence["finalized_inventory_sha256"] != sha256_json(current):
        raise ValidationError(
            "visible finalized Evidence does not match the sequence inventory"
        )
    return sequence


def advance_finalized_evidence_sequence(
    paths: StudyPaths,
    *,
    previous_inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Bind exactly one newly finalized Evidence record."""

    sequence = require_consistent_evidence_finalizations(
        paths, previous_inventory
    )
    previous = list(previous_inventory)
    current = finalized_evidence_inventory(paths)
    if len(current) != len(previous) + 1:
        raise ValidationError(
            "Evidence finalization sequence can advance by exactly one record"
        )
    previous_keys = {
        (item["evidence_id"], item["version"]) for item in previous
    }
    retained = [
        item
        for item in current
        if (item["evidence_id"], item["version"]) in previous_keys
    ]
    if retained != previous:
        raise ValidationError(
            "Evidence finalization changed an existing inventory binding"
        )
    sequence["finalized_count"] = len(current)
    sequence["finalized_inventory_sha256"] = sha256_json(current)
    return write_evidence_sequence(paths, sequence)


def recover_unindexed_evidence_finalization(paths: StudyPaths) -> dict[str, Any]:
    """Bind one valid finalized Evidence record left by an interrupted update."""

    sequence = require_evidence_sequence(paths)
    current = finalized_evidence_inventory(paths)
    if len(current) != int(sequence["finalized_count"]) + 1:
        raise ValidationError(
            "Evidence recovery requires exactly one unindexed finalized record"
        )
    candidates = [
        current[:index] + current[index + 1 :]
        for index in range(len(current))
        if sha256_json(current[:index] + current[index + 1 :])
        == sequence["finalized_inventory_sha256"]
    ]
    if len(candidates) != 1:
        raise ValidationError(
            "Evidence recovery cannot uniquely reconstruct the prior inventory"
        )
    sequence["finalized_count"] = len(current)
    sequence["finalized_inventory_sha256"] = sha256_json(current)
    return write_evidence_sequence(paths, sequence)


def evidence_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".EVIDENCE.sequence.json.*.tmp"))
