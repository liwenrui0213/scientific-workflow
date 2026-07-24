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
from .locking import serialized_study_authority
from .models import StudyPaths, ValidationError, WorkflowError


_SEQUENCE_SCHEMA_VERSION = 1
_SEQUENCE_KEYS = {
    "schema_version",
    "study_id",
    "high_water_mark",
    "inventory_sha256",
    "sequence_sha256",
}
_CONFIRMATION_NAME = re.compile(r"^(CONF-[0-9]{4,})\.json$")
_ABANDONMENT_NAME = re.compile(
    r"^(CAMP-[0-9a-f]{64})\.abandonment\.json$"
)
_LEGACY_CONFIRMATION_SCHEMA_VERSIONS = frozenset({2, 3})


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


def empty_confirmation_sequence(paths: StudyPaths) -> dict[str, Any]:
    """Return the digest-bound authority for immutable Confirmation records."""

    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "inventory_sha256": sha256_json([]),
        "sequence_sha256": None,
    }
    value["sequence_sha256"] = record_digest(value, "sequence_sha256")
    return value


def validate_confirmation_sequence_value(
    paths: StudyPaths,
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Confirmation authority sequence must be a JSON object")
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError(
            "Confirmation authority sequence schema_version is unsupported"
        )
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError(
            "Confirmation authority sequence has missing or unsupported fields"
        )
    if value.get("study_id") != paths.study_id:
        raise ValidationError(
            "Confirmation authority sequence study_id does not match the Study"
        )
    high_water_mark = value.get("high_water_mark")
    if (
        isinstance(high_water_mark, bool)
        or not isinstance(high_water_mark, int)
        or high_water_mark < 0
    ):
        raise ValidationError(
            "Confirmation authority sequence high_water_mark must be a "
            "non-negative integer"
        )
    inventory_sha256 = value.get("inventory_sha256")
    if (
        not isinstance(inventory_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", inventory_sha256) is None
    ):
        raise ValidationError(
            "Confirmation authority sequence inventory_sha256 is invalid"
        )
    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Confirmation authority sequence digest is invalid")
    return deepcopy(value)


def load_confirmation_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.confirmation_sequence
    if not path.exists() and not path.is_symlink():
        return None
    _require_regular_file(
        path,
        label="Confirmation authority sequence",
        sealed=True,
    )
    return validate_confirmation_sequence_value(paths, load_json(path))


def require_confirmation_sequence(paths: StudyPaths) -> dict[str, Any]:
    sequence = load_confirmation_sequence(paths)
    if sequence is None:
        raise ValidationError("Confirmation authority sequence is missing")
    return sequence


def write_confirmation_sequence(
    paths: StudyPaths,
    value: dict[str, Any],
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    candidate = deepcopy(value)
    candidate["sequence_sha256"] = None
    candidate["sequence_sha256"] = record_digest(
        candidate,
        "sequence_sha256",
    )
    normalized = validate_confirmation_sequence_value(paths, candidate)
    atomic_write_json(
        paths.confirmation_sequence,
        normalized,
        overwrite=overwrite,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def confirmation_authority_inventory(
    paths: StudyPaths,
) -> list[dict[str, Any]]:
    """Return exact bindings for all visible immutable Confirmation authority."""

    inventory: list[dict[str, Any]] = []
    if not paths.confirmations.is_dir():
        return inventory
    for path in sorted(paths.confirmations.iterdir(), key=lambda item: item.name):
        confirmation_match = _CONFIRMATION_NAME.fullmatch(path.name)
        abandonment_match = _ABANDONMENT_NAME.fullmatch(path.name)
        if confirmation_match is None and abandonment_match is None:
            continue
        _require_regular_file(
            path,
            label="Confirmation authority record",
            sealed=True,
        )
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValidationError(
                f"Confirmation authority record must be an object: {path}"
            )
        digest = value.get("record_sha256")
        if digest != record_digest(value, "record_sha256"):
            raise ValidationError(
                f"Confirmation authority record digest is invalid: {path}"
            )
        if confirmation_match is not None:
            confirmation_id = confirmation_match.group(1)
            if (
                value.get("confirmation_id") != confirmation_id
                or value.get("status") != "finalized"
            ):
                raise ValidationError(
                    "finalized Confirmation identity or status does not match "
                    f"its filename: {path}"
                )
            binding = {
                "record_type": "confirmation",
                "record_id": confirmation_id,
                "record_sha256": digest,
                "file_sha256": sha256_file(path),
            }
        else:
            assert abandonment_match is not None
            campaign_id = abandonment_match.group(1)
            if (
                value.get("campaign_id") != campaign_id
                or value.get("record_type")
                != "confirmation_campaign_abandonment"
                or value.get("status") != "abandoned"
            ):
                raise ValidationError(
                    "Confirmation campaign abandonment identity or status does "
                    f"not match its filename: {path}"
                )
            binding = {
                "record_type": "campaign_abandonment",
                "record_id": campaign_id,
                "record_sha256": digest,
                "file_sha256": sha256_file(path),
            }
        inventory.append(binding)
    inventory.sort(key=lambda item: (item["record_type"], item["record_id"]))
    return inventory


def require_consistent_confirmation_authority(
    paths: StudyPaths,
    inventory: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Require the sequence to bind every visible immutable authority record."""

    sequence = require_confirmation_sequence(paths)
    current = list(
        confirmation_authority_inventory(paths)
        if inventory is None
        else inventory
    )
    if int(sequence["high_water_mark"]) != len(current):
        raise ValidationError(
            "visible Confirmation authority does not match the sequence count; "
            "a record may be missing or left unindexed"
        )
    if sequence["inventory_sha256"] != sha256_json(current):
        raise ValidationError(
            "visible Confirmation authority does not match the sequence inventory"
        )
    return sequence


def advance_confirmation_sequence(
    paths: StudyPaths,
    *,
    previous_inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Bind exactly one newly published Confirmation authority record."""

    sequence = require_consistent_confirmation_authority(
        paths,
        previous_inventory,
    )
    previous = list(previous_inventory)
    current = confirmation_authority_inventory(paths)
    if len(current) != len(previous) + 1:
        raise ValidationError(
            "Confirmation authority sequence can advance by exactly one record"
        )
    previous_keys = {
        (item["record_type"], item["record_id"]) for item in previous
    }
    retained = [
        item
        for item in current
        if (item["record_type"], item["record_id"]) in previous_keys
    ]
    if retained != previous:
        raise ValidationError(
            "Confirmation authority finalization changed an existing binding"
        )
    sequence["high_water_mark"] = len(current)
    sequence["inventory_sha256"] = sha256_json(current)
    return write_confirmation_sequence(paths, sequence)


def recover_unindexed_confirmation_authority(
    paths: StudyPaths,
) -> dict[str, Any]:
    """Bind one valid authority record left by an interrupted finalization."""

    sequence = require_confirmation_sequence(paths)
    current = confirmation_authority_inventory(paths)
    if len(current) != int(sequence["high_water_mark"]) + 1:
        raise ValidationError(
            "Confirmation authority recovery requires exactly one unindexed record"
        )
    candidates = [
        current[:index] + current[index + 1 :]
        for index in range(len(current))
        if sha256_json(current[:index] + current[index + 1 :])
        == sequence["inventory_sha256"]
    ]
    if len(candidates) != 1:
        raise ValidationError(
            "Confirmation authority recovery cannot uniquely reconstruct the "
            "prior inventory"
        )
    sequence["high_water_mark"] = len(current)
    sequence["inventory_sha256"] = sha256_json(current)
    return write_confirmation_sequence(paths, sequence)


@serialized_study_authority
def migrate_legacy_confirmation_sequence(paths: StudyPaths) -> Path:
    """Bind one complete, valid pre-sequence v2/v3 Confirmation history.

    This is an explicit one-time compatibility boundary, not a replacement
    for normal sequence publication or interrupted-publication recovery.
    Current-schema v4 records and campaign abandonments were introduced with
    sequence authority and therefore cannot enter through this path.
    """

    paths.assert_safe_layout()
    if paths.confirmation_sequence.exists() or paths.confirmation_sequence.is_symlink():
        raise WorkflowError(
            "Confirmation authority sequence already exists; migration is "
            "only for a genuinely pre-sequence Study"
        )
    temporary_paths = confirmation_sequence_temporary_paths(paths)
    if temporary_paths:
        raise ValidationError(
            "Confirmation authority migration refuses unfinished sequence "
            "temporary files"
        )
    if paths.confirmations.is_symlink() or not paths.confirmations.is_dir():
        raise ValidationError(
            "legacy Confirmation registry must be a regular directory"
        )

    entries = sorted(paths.confirmations.iterdir(), key=lambda item: item.name)
    if not entries:
        raise ValidationError(
            "legacy Confirmation migration requires a non-empty finalized history"
        )
    unexpected = [
        path
        for path in entries
        if _CONFIRMATION_NAME.fullmatch(path.name) is None
    ]
    if unexpected:
        raise ValidationError(
            "legacy Confirmation migration accepts only finalized v2/v3 "
            "Confirmation records; unexpected entry: "
            + str(unexpected[0])
        )

    # Import locally to avoid the normal confirmation module's dependency on
    # this sequence module. These validators replay the same immutable-record
    # contract used after migration without requiring the not-yet-created
    # sequence.
    from .confirmation import (
        _campaign_sequence_errors,
        _claim_version_key,
        _static_record_errors,
    )

    records: list[dict[str, Any]] = []
    campaign_groups: dict[str, list[dict[str, Any]]] = {}
    claim_sets: set[tuple[tuple[str, str], ...]] = set()
    for path in entries:
        _require_regular_file(
            path,
            label="legacy finalized Confirmation",
            sealed=True,
        )
        value = load_json(path)
        schema_version = (
            value.get("schema_version")
            if isinstance(value, dict)
            else None
        )
        if schema_version not in _LEGACY_CONFIRMATION_SCHEMA_VERSIONS:
            raise ValidationError(
                "legacy Confirmation migration accepts only schema v2/v3 "
                f"records: {path}"
            )
        errors = _static_record_errors(paths, path, value)
        if errors:
            raise ValidationError(
                "legacy Confirmation migration found an invalid finalized "
                f"record {path}: " + "; ".join(errors)
            )
        assert isinstance(value, dict)
        records.append(value)
        campaign = value.get("campaign")
        assert isinstance(campaign, dict)
        campaign_id = str(campaign["campaign_id"])
        campaign_groups.setdefault(campaign_id, []).append(value)
        claim_sets.add(_claim_version_key(value["claims"]))

    for campaign_id, campaign_records in campaign_groups.items():
        errors = _campaign_sequence_errors(campaign_records)
        if errors:
            raise ValidationError(
                "legacy Confirmation migration found an invalid campaign "
                f"{campaign_id}: " + "; ".join(errors)
            )

    ordered_claim_sets = sorted(claim_sets)
    for index, left in enumerate(ordered_claim_sets):
        for right in ordered_claim_sets[index + 1 :]:
            overlap = set(left) & set(right)
            if overlap and left != right:
                claim_id = sorted(overlap)[0][0]
                raise ValidationError(
                    "legacy Confirmation migration found one Claim version in "
                    f"incompatible campaign Claim sets: {claim_id}"
                )

    inventory = confirmation_authority_inventory(paths)
    if len(inventory) != len(records) or any(
        item["record_type"] != "confirmation" for item in inventory
    ):
        raise ValidationError(
            "legacy Confirmation inventory does not exactly match the "
            "validated finalized history"
        )
    value = empty_confirmation_sequence(paths)
    value["high_water_mark"] = len(inventory)
    value["inventory_sha256"] = sha256_json(inventory)
    write_confirmation_sequence(paths, value, overwrite=False)
    require_consistent_confirmation_authority(paths, inventory)
    return paths.confirmation_sequence


def confirmation_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".CONFIRMATIONS.sequence.json.*.tmp"))
