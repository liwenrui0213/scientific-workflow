from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import math
from pathlib import Path
import re
from typing import Any, Mapping

from .budget import manifest_budget_commitment
from .hashing import atomic_write_json, load_json, record_digest, sha256_file
from .models import StudyPaths, ValidationError, WorkflowError


_LEDGER_SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^RUN-([0-9]{6})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_STATES = {
    "reserved",
    "aborted",
    "running",
    "succeeded",
    "failed",
    "interrupted",
    "incomplete",
}
_TERMINAL_STATES = {
    "succeeded",
    "failed",
    "interrupted",
    "incomplete",
}
_RESOURCE_KEYS = {"gpu_hours", "cpu_hours", "storage_gb"}


def ledger_path(paths: StudyPaths) -> Path:
    # Keep the high-water record outside runs/. Moving or recreating the
    # entire Run directory must not create a fresh budget/identity namespace.
    return paths.study / "RUNS.ledger.json"


def empty_ledger(paths: StudyPaths) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": _LEDGER_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "high_water_mark": 0,
        "runs": {},
        "ledger_sha256": None,
    }
    value["ledger_sha256"] = record_digest(value, "ledger_sha256")
    return value


def _normalized_commitment(value: Any, *, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != _RESOURCE_KEYS:
        raise ValidationError(
            f"{label} must contain exactly gpu_hours, cpu_hours, and storage_gb"
        )
    normalized: dict[str, float] = {}
    for key in sorted(_RESOURCE_KEYS):
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValidationError(f"{label} {key} must be a non-negative finite number")
        try:
            number = float(raw)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValidationError(
                f"{label} {key} must be a non-negative finite number"
            ) from exc
        if number < 0 or not math.isfinite(number):
            raise ValidationError(f"{label} {key} must be a non-negative finite number")
        normalized[key] = number
    return normalized


def validate_ledger_value(paths: StudyPaths, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Run ledger must be a JSON object")
    expected_keys = {
        "schema_version",
        "study_id",
        "high_water_mark",
        "runs",
        "ledger_sha256",
    }
    if set(value) != expected_keys:
        raise ValidationError("Run ledger has missing or unsupported fields")
    if value.get("schema_version") != _LEDGER_SCHEMA_VERSION:
        raise ValidationError("Run ledger schema_version is unsupported")
    if value.get("study_id") != paths.study_id:
        raise ValidationError("Run ledger study_id does not match the Study")
    high_water = value.get("high_water_mark")
    if isinstance(high_water, bool) or not isinstance(high_water, int):
        raise ValidationError("Run ledger high_water_mark must be an integer")
    if high_water < 0 or high_water > 999_999:
        raise ValidationError("Run ledger high_water_mark is outside the Run ID range")
    runs = value.get("runs")
    if not isinstance(runs, dict):
        raise ValidationError("Run ledger runs must be an object")
    highest_entry = 0
    normalized_runs: dict[str, Any] = {}
    for run_id, entry in runs.items():
        match = _RUN_ID.fullmatch(run_id) if isinstance(run_id, str) else None
        if match is None:
            raise ValidationError(f"Run ledger contains an invalid Run ID: {run_id!r}")
        highest_entry = max(highest_entry, int(match.group(1)))
        if not isinstance(entry, dict) or set(entry) != {
            "status",
            "commitment",
            "manifest_sha256",
        }:
            raise ValidationError(f"Run ledger entry {run_id} has an invalid shape")
        status = entry.get("status")
        if status not in _RUN_STATES:
            raise ValidationError(f"Run ledger entry {run_id} has an invalid status")
        manifest_digest = entry.get("manifest_sha256")
        if manifest_digest is not None and (
            not isinstance(manifest_digest, str)
            or _SHA256.fullmatch(manifest_digest) is None
        ):
            raise ValidationError(
                f"Run ledger entry {run_id} has an invalid Manifest digest"
            )
        if status == "aborted" and manifest_digest is not None:
            raise ValidationError(
                f"aborted Run ledger entry {run_id} cannot reference a Manifest"
            )
        normalized_runs[run_id] = {
            "status": status,
            "commitment": _normalized_commitment(
                entry.get("commitment"), label=f"Run ledger entry {run_id} commitment"
            ),
            "manifest_sha256": manifest_digest,
        }
    if highest_entry > high_water:
        raise ValidationError("Run ledger high_water_mark is below an allocated Run ID")
    expected_ids = {f"RUN-{number:06d}" for number in range(1, high_water + 1)}
    if set(normalized_runs) != expected_ids:
        raise ValidationError(
            "Run ledger must account for every allocated ID through high_water_mark"
        )
    if value.get("ledger_sha256") != record_digest(value, "ledger_sha256"):
        raise ValidationError("Run ledger digest is invalid")
    normalized = deepcopy(value)
    normalized["runs"] = normalized_runs
    return normalized


def load_ledger(paths: StudyPaths) -> dict[str, Any] | None:
    path = ledger_path(paths)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError("Run ledger must be a regular, non-symbolic-link file")
    return validate_ledger_value(paths, load_json(path))


def write_ledger(paths: StudyPaths, value: dict[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(value)
    candidate["ledger_sha256"] = None
    candidate["ledger_sha256"] = record_digest(candidate, "ledger_sha256")
    normalized = validate_ledger_value(paths, candidate)
    atomic_write_json(
        ledger_path(paths),
        normalized,
        overwrite=True,
        mode=0o444,
        require_parent_fsync=True,
    )
    return normalized


def _manifest_entry(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": manifest["status"],
        "commitment": manifest_budget_commitment(manifest),
        "manifest_sha256": sha256_file(path),
    }


def bootstrap_or_reconcile_ledger(
    paths: StudyPaths,
    manifests: Mapping[str, tuple[Path, dict[str, Any]]],
    *,
    write: bool,
) -> dict[str, Any]:
    """Load the durable Run ledger and reconcile crash-safe forward transitions.

    Once present, the ledger is the durable high-water and budget-history
    index. Ordinary admission never reconstructs a missing ledger because a
    reconstruction cannot distinguish legacy state from deleted history.
    """

    ledger = load_ledger(paths)
    if ledger is None:
        raise ValidationError(
            "Run ledger is missing; use the explicit legacy-ledger migration "
            "for an intact pre-ledger Study"
        )

    ledger_runs = ledger["runs"]
    missing_from_ledger = sorted(set(manifests) - set(ledger_runs))
    if missing_from_ledger:
        raise ValidationError(
            "Run ledger is missing visible Run(s): " + ", ".join(missing_from_ledger)
        )
    missing_manifests = sorted(
        run_id
        for run_id, entry in ledger_runs.items()
        if entry["status"] != "aborted" and run_id not in manifests
    )
    if missing_manifests:
        raise ValidationError(
            "Run ledger references missing Run Manifest(s): "
            + ", ".join(missing_manifests)
        )

    changed = False
    for run_id, (path, manifest) in sorted(manifests.items()):
        actual = _manifest_entry(path, manifest)
        recorded = ledger_runs[run_id]
        if recorded == actual:
            continue
        forward_transition = (
            recorded["status"] == "reserved"
            and actual["status"] in {"running", *_TERMINAL_STATES}
        ) or (
            recorded["status"] == "running"
            and actual["status"] in _TERMINAL_STATES
        )
        if not forward_transition:
            raise ValidationError(
                f"Run ledger entry {run_id} does not match its immutable Manifest"
            )
        ledger_runs[run_id] = actual
        changed = True

    if changed and write:
        ledger = write_ledger(paths, ledger)
    return ledger


def require_consistent_ledger(
    paths: StudyPaths,
    manifests: Mapping[str, tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    """Require the durable ledger to match every visible immutable Manifest.

    Read-only consumers such as Evidence may not treat an in-memory forward
    reconciliation as durable state.  A later locked Run registration can
    persist a crash-safe ``running -> terminal`` transition; until then the
    scientific chain remains explicitly stale and Evidence admission fails.
    """

    current = load_ledger(paths)
    reconciled = bootstrap_or_reconcile_ledger(paths, manifests, write=False)
    if current is None:  # bootstrap_or_reconcile_ledger already rejects this
        raise ValidationError("Run ledger is missing")
    if reconciled != current:
        raise ValidationError(
            "Run ledger is stale relative to visible immutable Manifests"
        )
    return current


def migrate_legacy_ledger(
    paths: StudyPaths,
    manifests: Mapping[str, tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    """Explicitly index one intact, contiguous pre-V3 Run history."""

    if load_ledger(paths) is not None:
        raise ValidationError("Run ledger already exists")
    if not manifests:
        raise ValidationError("legacy Run-ledger migration requires visible Runs")
    if any(
        manifest.get("schema_version") not in {1, 2}
        for _, manifest in manifests.values()
    ):
        raise ValidationError(
            "legacy Run-ledger migration accepts only pre-ledger V1/V2 Runs"
        )
    expected = {
        f"RUN-{number:06d}" for number in range(1, len(manifests) + 1)
    }
    if set(manifests) != expected:
        raise ValidationError(
            "legacy Run-ledger migration refuses a Run-ID gap"
        )
    ledger = empty_ledger(paths)
    ledger["high_water_mark"] = len(manifests)
    for run_id, (path, manifest) in sorted(manifests.items()):
        ledger["runs"][run_id] = _manifest_entry(path, manifest)
    return write_ledger(paths, ledger)


def ledger_commitment_totals(
    ledger: dict[str, Any], *, exclude_run_id: str | None = None
) -> dict[str, float]:
    totals = {key: Decimal("0") for key in _RESOURCE_KEYS}
    for run_id, entry in ledger["runs"].items():
        if run_id == exclude_run_id or entry["status"] == "aborted":
            continue
        for key in _RESOURCE_KEYS:
            totals[key] += Decimal(str(entry["commitment"][key]))
    return {key: float(value) for key, value in totals.items()}


def reserve_run_id(
    paths: StudyPaths,
    ledger: dict[str, Any],
    commitment: dict[str, float],
) -> tuple[dict[str, Any], str]:
    value = deepcopy(ledger)
    number = int(value["high_water_mark"]) + 1
    if number > 999_999:
        raise WorkflowError("Run ID space is exhausted")
    run_id = f"RUN-{number:06d}"
    run_directory = paths.runs / run_id
    if run_directory.exists() or run_directory.is_symlink():
        raise ValidationError(f"Run ID {run_id} collides with an existing path")
    value["high_water_mark"] = number
    value["runs"][run_id] = {
        "status": "reserved",
        "commitment": _normalized_commitment(
            commitment, label=f"Run ledger entry {run_id} commitment"
        ),
        "manifest_sha256": None,
    }
    return write_ledger(paths, value), run_id


def mark_registration_aborted(
    paths: StudyPaths, ledger: dict[str, Any], run_id: str
) -> dict[str, Any]:
    value = deepcopy(ledger)
    entry = value["runs"].get(run_id)
    if not isinstance(entry, dict) or entry.get("status") != "reserved":
        raise ValidationError(f"Run ledger entry {run_id} is not reserved")
    entry["status"] = "aborted"
    entry["commitment"] = {
        "gpu_hours": 0.0,
        "cpu_hours": 0.0,
        "storage_gb": 0.0,
    }
    entry["manifest_sha256"] = None
    return write_ledger(paths, value)


def record_manifest_in_ledger(
    paths: StudyPaths,
    ledger: dict[str, Any],
    run_id: str,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    value = deepcopy(ledger)
    recorded = value["runs"].get(run_id)
    if not isinstance(recorded, dict):
        raise ValidationError(f"Run ledger has no reservation for {run_id}")
    actual = _manifest_entry(manifest_path, manifest)
    allowed = (
        recorded["status"] == "reserved" and actual["status"] == "running"
    ) or (
        recorded["status"] == "running"
        and actual["status"] in _TERMINAL_STATES
    )
    if not allowed and recorded != actual:
        raise ValidationError(
            f"Run ledger entry {run_id} cannot transition from "
            f"{recorded['status']} to {actual['status']}"
        )
    value["runs"][run_id] = actual
    return write_ledger(paths, value)
