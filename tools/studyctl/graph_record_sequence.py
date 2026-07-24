from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import stat
from typing import Any, Sequence

from .hashing import atomic_write_json, load_json, record_digest, sha256_json
from .models import StudyPaths, ValidationError, require_id


_SEQUENCE_SCHEMA_VERSION = 1
_SEQUENCE_KEYS = {
    "schema_version",
    "study_id",
    "high_water_mark",
    "inventory_sha256",
    "sequence_sha256",
}
_INVENTORY_KEYS = {
    "kind",
    "id",
    "version",
    "record_sha256",
    "file_sha256",
}


def _inventory_key(item: dict[str, Any]) -> tuple[str, str, int]:
    return str(item["kind"]), str(item["id"]), int(item["version"])


def normalize_graph_record_inventory(
    value: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError("Graph-record inventory must be a sequence")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != _INVENTORY_KEYS:
            raise ValidationError("Graph-record inventory entry has an invalid shape")
        kind = raw.get("kind")
        if kind == "experiment_intent":
            record_id = require_id("experiment_intent", str(raw.get("id", "")))
        elif kind == "control_graph":
            record_id = require_id("control_graph", str(raw.get("id", "")))
        elif kind == "plan_lifecycle":
            record_id = str(raw.get("id", ""))
            if (
                len(record_id) < len("PLAN-EVENT-000001")
                or not record_id.startswith("PLAN-EVENT-")
                or not record_id.removeprefix("PLAN-EVENT-").isdigit()
            ):
                raise ValidationError(
                    "Graph-record plan_lifecycle ID is invalid"
                )
        else:
            raise ValidationError("Graph-record inventory kind is unsupported")
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValidationError(
                "Graph-record inventory version must be a positive integer"
            )
        entry = {
            "kind": kind,
            "id": record_id,
            "version": version,
            "record_sha256": _digest(
                raw.get("record_sha256"),
                label="Graph-record inventory record_sha256",
            ),
            "file_sha256": _digest(
                raw.get("file_sha256"),
                label="Graph-record inventory file_sha256",
            ),
        }
        key = _inventory_key(entry)
        if key in seen:
            raise ValidationError("Graph-record inventory contains a duplicate entry")
        seen.add(key)
        normalized.append(entry)
    normalized.sort(key=_inventory_key)
    return normalized


def _digest(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def empty_graph_record_sequence(paths: StudyPaths) -> dict[str, Any]:
    empty_hash = sha256_json([])
    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "inventory_sha256": empty_hash,
        "sequence_sha256": None,
    }
    value["sequence_sha256"] = record_digest(value, "sequence_sha256")
    return value


def validate_graph_record_sequence_value(
    paths: StudyPaths, value: Any
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Graph-record sequence must be a JSON object")
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError(
            "Graph-record sequence has missing or unsupported fields"
        )
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError("Graph-record sequence schema_version is unsupported")
    if value.get("study_id") != paths.study_id:
        raise ValidationError("Graph-record sequence study_id does not match Study")
    _nonnegative_integer(
        value.get("high_water_mark"),
        label="Graph-record sequence high_water_mark",
    )
    _digest(
        value.get("inventory_sha256"),
        label="Graph-record sequence inventory_sha256",
    )
    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Graph-record sequence digest is invalid")
    return deepcopy(value)


def load_graph_record_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.graph_record_sequence
    if not path.exists() and not path.is_symlink():
        return None
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValidationError(
            "Graph-record sequence must be a regular, non-linked file"
        )
    if metadata.st_mode & 0o222:
        raise ValidationError("Graph-record sequence must be sealed read-only")
    return validate_graph_record_sequence_value(paths, load_json(path))


def require_graph_record_sequence(paths: StudyPaths) -> dict[str, Any]:
    value = load_graph_record_sequence(paths)
    if value is None:
        raise ValidationError("Graph-record sequence is missing")
    return value


def write_graph_record_sequence(
    paths: StudyPaths,
    value: dict[str, Any],
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    candidate = deepcopy(value)
    candidate["sequence_sha256"] = None
    candidate["sequence_sha256"] = record_digest(candidate, "sequence_sha256")
    normalized = validate_graph_record_sequence_value(paths, candidate)
    atomic_write_json(
        paths.graph_record_sequence,
        normalized,
        overwrite=overwrite,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def require_consistent_graph_record_sequence(
    paths: StudyPaths,
    inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    sequence = require_graph_record_sequence(paths)
    normalized = normalize_graph_record_inventory(inventory)
    if int(sequence["high_water_mark"]) != len(normalized):
        raise ValidationError(
            "visible graph records do not match the monotone sequence count; "
            "a record may be missing, rolled back, or unindexed"
        )
    if sequence["inventory_sha256"] != sha256_json(normalized):
        raise ValidationError(
            "visible graph records do not match the monotone sequence inventory"
        )
    return sequence


def advance_graph_record_sequence(
    paths: StudyPaths,
    *,
    previous_inventory: Sequence[dict[str, Any]],
    current_inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    sequence = require_consistent_graph_record_sequence(
        paths, previous_inventory
    )
    previous = normalize_graph_record_inventory(previous_inventory)
    current = normalize_graph_record_inventory(current_inventory)
    if len(current) != len(previous) + 1:
        raise ValidationError(
            "Graph-record sequence can advance by exactly one record"
        )
    previous_keys = {_inventory_key(item) for item in previous}
    additions = [
        item for item in current if _inventory_key(item) not in previous_keys
    ]
    if len(additions) != 1:
        raise ValidationError(
            "Graph-record sequence advance does not contain one new identity"
        )
    retained = [
        item for item in current if _inventory_key(item) in previous_keys
    ]
    if retained != previous:
        raise ValidationError(
            "Graph-record sequence advance changed an existing record binding"
        )
    sequence["high_water_mark"] = int(sequence["high_water_mark"]) + 1
    sequence["inventory_sha256"] = sha256_json(current)
    return write_graph_record_sequence(paths, sequence)


def recover_unindexed_graph_record(
    paths: StudyPaths,
    inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Advance across exactly one durable record left by an interrupted publish."""

    sequence = require_graph_record_sequence(paths)
    current = normalize_graph_record_inventory(inventory)
    expected_count = int(sequence["high_water_mark"]) + 1
    if len(current) != expected_count:
        raise ValidationError(
            "Graph-record recovery requires exactly one unindexed visible record"
        )
    candidates: list[list[dict[str, Any]]] = []
    for index in range(len(current)):
        prior = current[:index] + current[index + 1 :]
        if sha256_json(prior) == sequence["inventory_sha256"]:
            candidates.append(prior)
    if len(candidates) != 1:
        raise ValidationError(
            "Graph-record recovery cannot uniquely reconstruct the prior inventory"
        )
    sequence["high_water_mark"] = expected_count
    sequence["inventory_sha256"] = sha256_json(current)
    return write_graph_record_sequence(paths, sequence)


def graph_record_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".GRAPH_RECORDS.sequence.json.*.tmp"))
