from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
import re
from typing import Any, Iterable

from .hashing import nested_record_digest
from .models import ValidationError


_HARD_BUDGET_BLOCK = re.compile(
    r"<!--\s*STUDYCTL-HARD-BUDGET-BEGIN\s*-->\s*"
    r"```json\s*(\{.*?\})\s*```\s*"
    r"<!--\s*STUDYCTL-HARD-BUDGET-END\s*-->",
    flags=re.DOTALL,
)
_HARD_BUDGET_KEYS = ("gpu_hours", "cpu_hours", "storage_gb")
_MANIFEST_ESTIMATE_KEYS = {
    "gpu_hours": "estimated_gpu_hours",
    "cpu_hours": "estimated_cpu_hours",
    "storage_gb": "estimated_storage_gb",
}


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite hard-budget number is not allowed: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate hard-budget key: {key!r}")
        result[key] = value
    return result


def _budget_number(name: str, value: Any, *, nullable: bool) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        expected = "a non-negative finite number or null" if nullable else "a non-negative finite number"
        raise ValidationError(f"{name} must be {expected}")
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        expected = (
            "a non-negative finite number or null"
            if nullable
            else "a non-negative finite number"
        )
        raise ValidationError(f"{name} must be {expected}") from exc
    if not math.isfinite(numeric) or numeric < 0:
        expected = "a non-negative finite number or null" if nullable else "finite and non-negative"
        raise ValidationError(f"{name} must be {expected}")
    return numeric


def parse_brief_hard_budget(text: str) -> dict[str, float | None]:
    """Parse the single visible, machine-enforced hard-budget block."""

    if (
        text.count("STUDYCTL-HARD-BUDGET-BEGIN") != 1
        or text.count("STUDYCTL-HARD-BUDGET-END") != 1
    ):
        raise ValidationError(
            "Brief must contain exactly one visible STUDYCTL-HARD-BUDGET JSON block"
        )
    matches = list(_HARD_BUDGET_BLOCK.finditer(text))
    if not matches:
        raise ValidationError(
            "Brief is missing the visible STUDYCTL-HARD-BUDGET JSON block"
        )
    if len(matches) != 1:
        raise ValidationError(
            "Brief must contain exactly one visible STUDYCTL-HARD-BUDGET JSON block"
        )
    match = matches[0]
    try:
        value = json.loads(
            match.group(1),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Brief hard-budget JSON is invalid: {exc}") from exc
    return normalize_hard_budget(value, label="Brief hard budget")


def normalize_hard_budget(
    value: Any, *, label: str = "hard budget"
) -> dict[str, float | None]:
    """Validate and normalize one complete hard-budget object."""

    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    missing = [key for key in _HARD_BUDGET_KEYS if key not in value]
    extra = sorted(set(value) - set(_HARD_BUDGET_KEYS))
    if missing:
        raise ValidationError(
            f"{label} is missing field(s): " + ", ".join(missing)
        )
    if extra:
        raise ValidationError(
            f"{label} has unsupported field(s): " + ", ".join(extra)
        )
    return {
        key: _budget_number(f"{label} {key}", value[key], nullable=True)
        for key in _HARD_BUDGET_KEYS
    }


def format_brief_hard_budget_block(
    value: dict[str, Any],
) -> str:
    """Render the one visible machine-enforced Brief budget block."""

    normalized = normalize_hard_budget(value, label="Brief hard budget")
    return (
        "<!-- STUDYCTL-HARD-BUDGET-BEGIN -->\n"
        "```json\n"
        + json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n```\n"
        "<!-- STUDYCTL-HARD-BUDGET-END -->"
    )


def replace_brief_hard_budget(
    text: str,
    *,
    gpu_hours: float | int | None,
    cpu_hours: float | int | None,
    storage_gb: float | int | None,
) -> str:
    """Replace the canonical Brief budget block without creating a second source."""

    parse_brief_hard_budget(text)
    value = {
        "gpu_hours": _budget_number("hard budget gpu_hours", gpu_hours, nullable=True),
        "cpu_hours": _budget_number("hard budget cpu_hours", cpu_hours, nullable=True),
        "storage_gb": _budget_number("hard budget storage_gb", storage_gb, nullable=True),
    }
    replacement = format_brief_hard_budget_block(value)
    return _HARD_BUDGET_BLOCK.sub(replacement, text, count=1)


def requested_budget(
    *,
    gpu_hours: Any = 0.0,
    cpu_hours: Any = 0.0,
    storage_gb: Any = 0.0,
) -> dict[str, float]:
    return {
        "gpu_hours": float(
            _budget_number("estimated GPU hours", gpu_hours, nullable=False)
        ),
        "cpu_hours": float(
            _budget_number("estimated CPU hours", cpu_hours, nullable=False)
        ),
        "storage_gb": float(
            _budget_number("estimated storage GB", storage_gb, nullable=False)
        ),
    }


def _decimal(name: str, value: Any) -> Decimal:
    numeric = _budget_number(name, value, nullable=False)
    try:
        return Decimal(str(numeric))
    except InvalidOperation as exc:  # pragma: no cover - guarded by _budget_number
        raise ValidationError(f"{name} is not a decimal-compatible number") from exc


def manifest_budget_commitment(manifest: dict[str, Any]) -> dict[str, float]:
    run_id = manifest.get("run_id", "<unknown>")
    budget = manifest.get("budget")
    if not isinstance(budget, dict):
        raise ValidationError(f"Run {run_id} has no valid budget record")

    if manifest.get("schema_version") in {3, 4, 5}:
        status = manifest.get("status")
        if status not in {
            "running",
            "succeeded",
            "failed",
            "interrupted",
            "incomplete",
        }:
            raise ValidationError(f"Run {run_id} has an invalid lifecycle status")

        integrity = manifest.get("integrity")
        if not isinstance(integrity, dict):
            raise ValidationError(f"Run {run_id} has no valid integrity record")
        if status == "running":
            if any(
                integrity.get(key) is not None
                for key in ("sealed_at", "manifest_sha256")
            ):
                raise ValidationError(
                    f"Run {run_id} has an invalid running integrity record"
                )
        elif integrity.get("manifest_sha256") != nested_record_digest(
            manifest, "integrity", "manifest_sha256"
        ):
            raise ValidationError(
                f"Run {run_id} has an invalid terminal manifest digest"
            )

        requested = budget.get("requested")
        if not isinstance(requested, dict):
            raise ValidationError(
                f"Run {run_id} has no valid requested-budget reservation"
            )
        commitment = {
            key: _decimal(
                f"Run {run_id} requested {key}",
                requested.get(key),
            )
            for key in _HARD_BUDGET_KEYS
        }
        estimates = {
            key: _decimal(
                f"Run {run_id} {manifest_key}",
                budget.get(manifest_key),
            )
            for key, manifest_key in _MANIFEST_ESTIMATE_KEYS.items()
        }
        actual_storage_raw = budget.get("actual_output_storage_gb")
        expected_storage = estimates["storage_gb"]
        if actual_storage_raw is not None:
            expected_storage = max(
                expected_storage,
                _decimal(
                    f"Run {run_id} actual_output_storage_gb",
                    actual_storage_raw,
                ),
            )
        expected = {
            "gpu_hours": estimates["gpu_hours"],
            "cpu_hours": estimates["cpu_hours"],
            "storage_gb": expected_storage,
        }
        if commitment != expected:
            raise ValidationError(
                f"Run {run_id} requested-budget reservation is inconsistent "
                "with its estimates and declared-output storage"
            )
        return {key: float(value) for key, value in commitment.items()}

    # V1 Runs remain historical and Evidence-ineligible; pre-budget V2 Runs
    # retain their earlier Evidence semantics. Both versions' declared
    # estimates conservatively consume a later Brief's lifetime budget.
    integrity = manifest.get("integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("manifest_sha256")
        != nested_record_digest(manifest, "integrity", "manifest_sha256")
    ):
        raise ValidationError(
            f"legacy Run {run_id} has an invalid terminal manifest digest"
        )
    estimates = {
        key: _decimal(
            f"Run {run_id} {manifest_key}",
            budget.get(manifest_key, 0.0),
        )
        for key, manifest_key in _MANIFEST_ESTIMATE_KEYS.items()
    }
    actual_storage = budget.get("actual_output_storage_gb")
    if actual_storage is not None:
        estimates["storage_gb"] = max(
            estimates["storage_gb"],
            _decimal(
                f"Run {run_id} actual_output_storage_gb",
                actual_storage,
            ),
        )
    # Pre-budget manifests did not carry an authoritative storage reservation,
    # but their immutable output records still expose the bytes retained by the
    # Study.  Charge those bytes conservatively so a later hard limit cannot be
    # bypassed merely because the Run predates schema V3.
    declared_output_bytes = Decimal("0")
    outputs = manifest.get("outputs", [])
    if not isinstance(outputs, list):
        raise ValidationError(f"legacy Run {run_id} has invalid output records")
    for index, record in enumerate(outputs):
        if not isinstance(record, dict):
            raise ValidationError(
                f"legacy Run {run_id} output[{index}] is not an object"
            )
        size = record.get("size")
        if size is None:
            continue
        declared_output_bytes += _decimal(
            f"legacy Run {run_id} output[{index}] size",
            size,
        )
    estimates["storage_gb"] = max(
        estimates["storage_gb"],
        declared_output_bytes / Decimal("1000000000"),
    )
    return {key: float(value) for key, value in estimates.items()}


def budget_totals_from_manifests(
    manifests: Iterable[dict[str, Any]],
) -> dict[str, float]:
    totals = {key: Decimal("0") for key in _HARD_BUDGET_KEYS}
    for manifest in manifests:
        commitment = manifest_budget_commitment(manifest)
        for key in _HARD_BUDGET_KEYS:
            totals[key] += Decimal(str(commitment[key]))
    return {key: float(value) for key, value in totals.items()}


def budget_projection(
    hard_limits: dict[str, float | None],
    committed: dict[str, float],
    requested: dict[str, float],
) -> dict[str, Any]:
    projected: dict[str, float] = {}
    violations: list[dict[str, Any]] = []
    for key in _HARD_BUDGET_KEYS:
        before = _decimal(f"committed {key}", committed.get(key, 0.0))
        addition = _decimal(f"requested {key}", requested.get(key, 0.0))
        after = before + addition
        projected[key] = float(after)
        limit_value = hard_limits.get(key)
        limit = Decimal("0") if limit_value is None else _decimal(
            f"hard limit {key}", limit_value
        )
        if after > limit:
            violations.append(
                {
                    "resource": key,
                    "committed": float(before),
                    "requested": float(addition),
                    "projected": float(after),
                    "limit": None if limit_value is None else float(limit),
                }
            )
    return {
        "hard_limits": dict(hard_limits),
        "committed_before": dict(committed),
        "requested": dict(requested),
        "committed_after": projected,
        "violations": violations,
    }


def format_budget_violation(violation: dict[str, Any]) -> str:
    resource = str(violation["resource"]).replace("_", " ")
    limit = violation["limit"]
    limit_text = "not authorized (null)" if limit is None else f"limit {limit:g}"
    return (
        f"hard {resource} budget exceeded: committed {violation['committed']:g} "
        f"+ requested {violation['requested']:g} = {violation['projected']:g}, "
        f"{limit_text}"
    )
