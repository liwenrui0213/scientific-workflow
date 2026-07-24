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
from .models import (
    VERDICT_SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
    WorkflowError,
)


_SEQUENCE_SCHEMA_VERSION = 1
_SEQUENCE_KEYS = {
    "schema_version",
    "study_id",
    "high_water_mark",
    "inventory_sha256",
    "sequence_sha256",
}
_REVIEW_ARCHIVE = re.compile(r"^REVIEW-([0-9a-f]{64})\.json$")
_PACKET_ARCHIVE = re.compile(r"^REVIEW_PACKET-([0-9a-f]{64})\.json$")
_VERDICT_FILE = re.compile(r"^VERDICT(?:\.v[0-9]{4,})?\.json$")


def _require_sealed_regular(path: Path, *, label: str) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValidationError(f"{label} must be a regular, non-linked file")
    if metadata.st_mode & 0o222:
        raise ValidationError(f"{label} must be sealed read-only")


def empty_review_verdict_sequence(paths: StudyPaths) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": _SEQUENCE_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "inventory_sha256": sha256_json([]),
        "sequence_sha256": None,
    }
    value["sequence_sha256"] = record_digest(value, "sequence_sha256")
    return value


def validate_review_verdict_sequence_value(
    paths: StudyPaths,
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Review/Verdict authority sequence must be a JSON object")
    if value.get("schema_version") != _SEQUENCE_SCHEMA_VERSION:
        raise ValidationError(
            "Review/Verdict authority sequence schema_version is unsupported"
        )
    if set(value) != _SEQUENCE_KEYS:
        raise ValidationError(
            "Review/Verdict authority sequence has missing or unsupported fields"
        )
    if value.get("study_id") != paths.study_id:
        raise ValidationError(
            "Review/Verdict authority sequence study_id does not match the Study"
        )
    high_water_mark = value.get("high_water_mark")
    if (
        isinstance(high_water_mark, bool)
        or not isinstance(high_water_mark, int)
        or high_water_mark < 0
    ):
        raise ValidationError(
            "Review/Verdict authority sequence high_water_mark must be a "
            "non-negative integer"
        )
    inventory_sha256 = value.get("inventory_sha256")
    if (
        not isinstance(inventory_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", inventory_sha256) is None
    ):
        raise ValidationError(
            "Review/Verdict authority sequence inventory_sha256 is invalid"
        )
    if value.get("sequence_sha256") != record_digest(value, "sequence_sha256"):
        raise ValidationError("Review/Verdict authority sequence digest is invalid")
    return deepcopy(value)


def load_review_verdict_sequence(paths: StudyPaths) -> dict[str, Any] | None:
    path = paths.review_verdict_sequence
    if not path.exists() and not path.is_symlink():
        return None
    _require_sealed_regular(path, label="Review/Verdict authority sequence")
    return validate_review_verdict_sequence_value(paths, load_json(path))


def require_review_verdict_sequence(paths: StudyPaths) -> dict[str, Any]:
    sequence = load_review_verdict_sequence(paths)
    if sequence is None:
        raise ValidationError(
            "Review/Verdict authority sequence is missing; use the explicit "
            "legacy migration command only for a pre-sequence Study"
        )
    return sequence


def write_review_verdict_sequence(
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
    normalized = validate_review_verdict_sequence_value(paths, candidate)
    atomic_write_json(
        paths.review_verdict_sequence,
        normalized,
        overwrite=overwrite,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def _review_inventory(paths: StudyPaths) -> list[dict[str, Any]]:
    history = paths.study / "review-history"
    if not history.exists() and not history.is_symlink():
        return []
    if history.is_symlink() or not history.is_dir():
        raise ValidationError(
            "Review history must be a regular repository directory"
        )
    reviews: dict[str, Path] = {}
    packets: dict[str, Path] = {}
    for path in sorted(history.iterdir(), key=lambda item: item.name):
        review_match = _REVIEW_ARCHIVE.fullmatch(path.name)
        packet_match = _PACKET_ARCHIVE.fullmatch(path.name)
        if review_match is None and packet_match is None:
            raise ValidationError(
                f"unexpected immutable Review-history entry: {path}"
            )
        _require_sealed_regular(path, label="immutable Review-history entry")
        expected_digest = (
            review_match.group(1)
            if review_match is not None
            else packet_match.group(1)
        )
        if sha256_file(path) != expected_digest:
            raise ValidationError(
                f"immutable Review-history filename digest is stale: {path}"
            )
        if review_match is not None:
            reviews[expected_digest] = path
        else:
            packets[expected_digest] = path

    inventory: list[dict[str, Any]] = []
    for review_digest, review_path in sorted(reviews.items()):
        review = load_json(review_path)
        if not isinstance(review, dict):
            raise ValidationError(
                f"immutable Review archive must contain an object: {review_path}"
            )
        if review.get("study_id") != paths.study_id:
            raise ValidationError(
                f"immutable Review archive belongs to another Study: {review_path}"
            )
        packet_digest = review.get("review_packet_sha256")
        if (
            not isinstance(packet_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", packet_digest) is None
        ):
            raise ValidationError(
                f"immutable Review archive has no valid packet binding: {review_path}"
            )
        packet_path = packets.get(packet_digest)
        if packet_path is None:
            raise ValidationError(
                f"immutable Review archive is missing its packet: {review_path}"
            )
        inventory.append(
            {
                "record_type": "review_import",
                "record_id": review_digest,
                "review_sha256": review_digest,
                "review_packet_sha256": packet_digest,
            }
        )
    # A packet archive is written before its Review archive.  An unreferenced
    # packet is therefore harmless interrupted-publication staging, not a
    # Review occurrence.  Once a Review archive exists, the pair becomes one
    # sequence-bound inventory item and either member may no longer disappear.
    return inventory


def _verdict_inventory(paths: StudyPaths) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(paths.study.glob("VERDICT*.json"), key=lambda item: item.name):
        if _VERDICT_FILE.fullmatch(path.name) is None:
            raise ValidationError(f"unexpected immutable Verdict filename: {path}")
        _require_sealed_regular(path, label="immutable Verdict")
        verdict = load_json(path)
        if not isinstance(verdict, dict):
            raise ValidationError(f"immutable Verdict must contain an object: {path}")
        if verdict.get("study_id") != paths.study_id:
            raise ValidationError(f"immutable Verdict belongs to another Study: {path}")
        verdict_id = verdict.get("verdict_id")
        verdict_sha256 = verdict.get("verdict_sha256")
        if not isinstance(verdict_id, str) or not verdict_id:
            raise ValidationError(f"immutable Verdict identity is invalid: {path}")
        if verdict_sha256 != record_digest(verdict, "verdict_sha256"):
            raise ValidationError(f"immutable Verdict digest is invalid: {path}")
        inventory.append(
            {
                "record_type": "verdict",
                "record_id": verdict_id,
                "path": path.relative_to(paths.root).as_posix(),
                "record_sha256": verdict_sha256,
                "file_sha256": sha256_file(path),
            }
        )
    return inventory


def review_verdict_authority_inventory(
    paths: StudyPaths,
) -> list[dict[str, Any]]:
    inventory = [*_review_inventory(paths), *_verdict_inventory(paths)]
    inventory.sort(
        key=lambda item: (
            str(item["record_type"]),
            str(item["record_id"]),
            str(item.get("path", "")),
        )
    )
    identities = [
        (str(item["record_type"]), str(item["record_id"]))
        for item in inventory
    ]
    if len(identities) != len(set(identities)):
        raise ValidationError(
            "Review/Verdict authority inventory repeats an immutable identity"
        )
    return inventory


def require_consistent_review_verdict_authority(
    paths: StudyPaths,
    inventory: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sequence = require_review_verdict_sequence(paths)
    current = list(
        review_verdict_authority_inventory(paths)
        if inventory is None
        else inventory
    )
    if int(sequence["high_water_mark"]) != len(current):
        raise ValidationError(
            "visible Review/Verdict authority does not match the sequence count; "
            "a record may be missing or left unindexed"
        )
    if sequence["inventory_sha256"] != sha256_json(current):
        raise ValidationError(
            "visible Review/Verdict authority does not match the sequence inventory"
        )
    return sequence


def advance_review_verdict_sequence(
    paths: StudyPaths,
    *,
    previous_inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    sequence = require_consistent_review_verdict_authority(
        paths,
        previous_inventory,
    )
    previous = list(previous_inventory)
    current = review_verdict_authority_inventory(paths)
    if len(current) != len(previous) + 1:
        raise ValidationError(
            "Review/Verdict authority sequence can advance by exactly one record"
        )
    previous_keys = {
        (str(item["record_type"]), str(item["record_id"]))
        for item in previous
    }
    retained = [
        item
        for item in current
        if (str(item["record_type"]), str(item["record_id"])) in previous_keys
    ]
    if retained != previous:
        raise ValidationError(
            "Review/Verdict authority publication changed an existing binding"
        )
    sequence["high_water_mark"] = len(current)
    sequence["inventory_sha256"] = sha256_json(current)
    return write_review_verdict_sequence(paths, sequence)


@serialized_study_authority
def recover_unindexed_review_verdict_authority(
    paths: StudyPaths,
) -> dict[str, Any]:
    temporary_paths = review_verdict_sequence_temporary_paths(paths)
    if temporary_paths:
        raise ValidationError(
            "Review/Verdict authority recovery refuses unfinished sequence "
            "temporary files"
        )
    sequence = require_review_verdict_sequence(paths)
    current = review_verdict_authority_inventory(paths)
    if len(current) != int(sequence["high_water_mark"]) + 1:
        raise ValidationError(
            "Review/Verdict authority recovery requires exactly one unindexed record"
        )
    candidate_indices = [
        index
        for index in range(len(current))
        if sha256_json(current[:index] + current[index + 1 :])
        == sequence["inventory_sha256"]
    ]
    if len(candidate_indices) != 1:
        raise ValidationError(
            "Review/Verdict authority recovery cannot uniquely reconstruct the "
            "prior inventory"
        )
    candidate_index = candidate_indices[0]
    prior_inventory = current[:candidate_index] + current[candidate_index + 1 :]
    recovered = current[candidate_index]
    if recovered["record_type"] == "verdict":
        prior_verdicts = [
            item
            for item in prior_inventory
            if item.get("record_type") == "verdict"
        ]
        prior_numbers: list[int] = []
        prior_versions: list[int] = []
        for item in prior_verdicts:
            verdict_id = str(item.get("record_id", ""))
            match = re.fullmatch(r"VERDICT-([0-9]+)", verdict_id)
            if match is None:
                raise ValidationError(
                    "Review/Verdict authority recovery found an invalid prior "
                    "Verdict identity"
                )
            prior_numbers.append(int(match.group(1)))
            path_name = Path(str(item.get("path", ""))).name
            version_match = re.fullmatch(r"VERDICT\.v([0-9]+)\.json", path_name)
            if version_match is not None:
                prior_versions.append(int(version_match.group(1)))
        expected_id = f"VERDICT-{max(prior_numbers, default=0) + 1:04d}"
        if not prior_verdicts:
            expected_path = paths.verdict.relative_to(paths.root).as_posix()
        else:
            next_version = max([1, *prior_versions]) + 1
            expected_path = (
                paths.study / f"VERDICT.v{next_version:04d}.json"
            ).relative_to(paths.root).as_posix()
        if (
            recovered.get("record_id") != expected_id
            or recovered.get("path") != expected_path
        ):
            raise ValidationError(
                "Review/Verdict authority recovery candidate does not match "
                "the deterministic next Verdict identity and path"
            )
        verdict_path = paths.root / str(recovered["path"])
        verdict = load_json(verdict_path)
        if (
            not isinstance(verdict, dict)
            or verdict.get("schema_version") != VERDICT_SCHEMA_VERSION
        ):
            raise ValidationError(
                "Review/Verdict authority recovery cannot adopt a legacy "
                "Verdict; use explicit pre-sequence migration only for "
                "historical records"
            )
        # Recovery is the completion of the normal current-schema publication
        # path, not an alternate Verdict ingestion API.  The sequence is
        # intentionally one record behind here, so replay every other schema,
        # human-authorization, epistemic, and current-scope invariant before
        # advancing it.
        from .approval import (
            _contains_placeholder,
            _validate_current_verdict_scope,
            _validate_verdict_branches,
            _validate_verdict_claim_decision,
        )
        from .validation import errors_only, object_schema_issues

        verdict_issues = errors_only(
            object_schema_issues(
                paths.root,
                "verdict",
                verdict_path,
                verdict,
            )
        )
        if verdict_issues:
            raise ValidationError(
                "Review/Verdict authority recovery cannot adopt an invalid "
                "Verdict:\n"
                + "\n".join(issue.render() for issue in verdict_issues)
            )
        if (
            verdict.get("study_id") != paths.study_id
            or verdict.get("verdict_sha256")
            != record_digest(verdict, "verdict_sha256")
        ):
            raise ValidationError(
                "Review/Verdict authority recovery Verdict identity or digest "
                "is invalid"
            )
        _validate_verdict_branches(verdict)
        _validate_verdict_claim_decision(verdict)
        confirmation = verdict.get("confirmation")
        if not isinstance(confirmation, dict):
            raise ValidationError(
                "Review/Verdict authority recovery Verdict confirmation is invalid"
            )
        authorization = verdict.get("authorization")
        if authorization is None:
            expected_phrase = (
                f"RECORD VERDICT {paths.study_id} {verdict.get('verdict_id')}"
            )
            confirmed_at = confirmation.get("confirmed_at")
            if (
                confirmation.get("typed_text") != expected_phrase
                or not isinstance(confirmed_at, str)
                or not confirmed_at.strip()
                or _contains_placeholder(confirmed_at)
            ):
                raise ValidationError(
                    "Review/Verdict authority recovery Verdict confirmation "
                    "phrase or timestamp is invalid"
                )
        else:
            recorded_at = confirmation.get("recorded_at")
            if (
                confirmation.get("mode") != "agent_initiated"
                or not isinstance(recorded_at, str)
                or not recorded_at.strip()
                or _contains_placeholder(recorded_at)
            ):
                raise ValidationError(
                    "Review/Verdict authority recovery Agent-initiated Verdict "
                    "confirmation mode or timestamp is invalid"
                )
        _validate_current_verdict_scope(
            paths,
            verdict,
            require_consistent_authority=False,
            pending_authority_path=verdict_path,
        )
    else:
        # Review recovery is only the forward completion of a current import,
        # not a second ingestion path.  Recheck the same structured Review and
        # packet contracts before making the archive pair omission-evident.
        from .review import _validate_review_packet
        from .validation import errors_only, object_schema_issues

        review_digest = str(recovered["review_sha256"])
        packet_digest = str(recovered["review_packet_sha256"])
        history = paths.study / "review-history"
        review_path = history / f"REVIEW-{review_digest}.json"
        packet_path = history / f"REVIEW_PACKET-{packet_digest}.json"
        review = load_json(review_path)
        review_issues = errors_only(
            object_schema_issues(
                paths.root,
                "review",
                review_path,
                review,
            )
        )
        if review_issues:
            raise ValidationError(
                "Review/Verdict authority recovery cannot adopt an invalid "
                "Review archive:\n"
                + "\n".join(issue.render() for issue in review_issues)
            )
        if (
            not isinstance(review, dict)
            or review.get("study_id") != paths.study_id
            or review.get("review_packet_sha256") != sha256_file(packet_path)
        ):
            raise ValidationError(
                "Review/Verdict authority recovery Review and packet binding "
                "is invalid"
            )
        _validate_review_packet(
            paths,
            packet_path,
            require_current=True,
            allow_unindexed_review_verdict_authority=True,
        )
    sequence["high_water_mark"] = len(current)
    sequence["inventory_sha256"] = sha256_json(current)
    return write_review_verdict_sequence(paths, sequence)


def _validate_legacy_authority_for_migration(
    paths: StudyPaths,
    inventory: Sequence[dict[str, Any]],
) -> None:
    """Validate explicit pre-sequence history without admitting current records."""

    from .approval import (
        _contains_placeholder,
        _validate_verdict_branches,
        _validate_verdict_claim_decision,
    )
    from .review import validate_legacy_review_basis
    from .validation import errors_only, object_schema_issues

    history = paths.study / "review-history"
    for record in inventory:
        if record["record_type"] == "review_import":
            review_digest = str(record["review_sha256"])
            packet_digest = str(record["review_packet_sha256"])
            review_path = history / f"REVIEW-{review_digest}.json"
            packet_path = history / f"REVIEW_PACKET-{packet_digest}.json"
            review = load_json(review_path)
            review_issues = errors_only(
                object_schema_issues(
                    paths.root,
                    "review",
                    review_path,
                    review,
                )
            )
            if review_issues:
                raise ValidationError(
                    "legacy Review/Verdict migration found an invalid Review:\n"
                    + "\n".join(issue.render() for issue in review_issues)
                )
            if (
                not isinstance(review, dict)
                or review.get("study_id") != paths.study_id
                or review.get("review_packet_sha256") != sha256_file(packet_path)
            ):
                raise ValidationError(
                    "legacy Review/Verdict migration found an invalid Review "
                    "and packet binding"
                )
            packet = load_json(packet_path)
            if (
                not isinstance(packet, dict)
                or packet.get("schema_version") != 1
                or packet.get("study_id") != paths.study_id
            ):
                raise ValidationError(
                    "legacy Review/Verdict migration accepts only historical "
                    "schema-v1 Review packets; current packets require the "
                    "normal sequence-bound import path"
                )
            continue

        verdict_path = paths.root / str(record["path"])
        verdict = load_json(verdict_path)
        if not isinstance(verdict, dict) or verdict.get("schema_version") not in {
            1,
            2,
        }:
            raise ValidationError(
                "legacy Review/Verdict migration accepts only historical "
                "Verdict schema versions 1 and 2; current Verdicts require "
                "normal publication or one-record recovery"
            )
        verdict_issues = errors_only(
            object_schema_issues(
                paths.root,
                "verdict",
                verdict_path,
                verdict,
            )
        )
        if verdict_issues:
            raise ValidationError(
                "legacy Review/Verdict migration found an invalid Verdict:\n"
                + "\n".join(issue.render() for issue in verdict_issues)
            )
        _validate_verdict_branches(verdict)
        _validate_verdict_claim_decision(verdict)
        confirmation = verdict.get("confirmation")
        expected_phrase = (
            f"RECORD VERDICT {paths.study_id} {verdict.get('verdict_id')}"
        )
        confirmed_at = (
            confirmation.get("confirmed_at")
            if isinstance(confirmation, dict)
            else None
        )
        if (
            not isinstance(confirmation, dict)
            or confirmation.get("typed_text") != expected_phrase
            or not isinstance(confirmed_at, str)
            or not confirmed_at.strip()
            or _contains_placeholder(confirmed_at)
        ):
            raise ValidationError(
                "legacy Review/Verdict migration found an invalid Verdict "
                "confirmation phrase"
            )
        if verdict.get("schema_version") == 2:
            validate_legacy_review_basis(
                paths,
                verdict.get("review_basis"),
            )


@serialized_study_authority
def migrate_legacy_review_verdict_sequence(paths: StudyPaths) -> Path:
    """Create the sequence once for an explicitly selected pre-sequence Study."""

    if paths.review_verdict_sequence.exists() or paths.review_verdict_sequence.is_symlink():
        raise WorkflowError(
            "Review/Verdict authority sequence already exists; migration is "
            "only for a pre-sequence Study"
        )
    temporary_paths = review_verdict_sequence_temporary_paths(paths)
    if temporary_paths:
        raise ValidationError(
            "Review/Verdict authority migration refuses unfinished sequence "
            "temporary files"
        )
    inventory = review_verdict_authority_inventory(paths)
    if not inventory:
        raise ValidationError(
            "legacy Review/Verdict migration requires existing historical "
            "Review or Verdict records"
        )
    _validate_legacy_authority_for_migration(paths, inventory)
    value = empty_review_verdict_sequence(paths)
    value["high_water_mark"] = len(inventory)
    value["inventory_sha256"] = sha256_json(inventory)
    write_review_verdict_sequence(paths, value, overwrite=False)
    require_consistent_review_verdict_authority(paths, inventory)
    return paths.review_verdict_sequence


def review_verdict_sequence_temporary_paths(paths: StudyPaths) -> list[Path]:
    return sorted(paths.study.glob(".REVIEW_VERDICTS.sequence.json.*.tmp"))
