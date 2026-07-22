from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .checkpoint_sequence import require_checkpoint_sequence
from .evidence_sequence import require_evidence_sequence
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from .models import CLAIMS_SCHEMA_VERSION, StudyPaths, ValidationError
from .run_ledger import load_ledger


CLAIM_LIFECYCLES = {"active", "retired", "superseded"}

PRESSURE_METRICS = (
    "active_claims",
    "authoritative_claims",
    "terminal_claims",
    "claims_source_bytes",
    "frontier_open_questions",
    "frontier_next_actions",
    "frontier_human_decisions",
    "active_selector_bytes",
    "runs_since_checkpoint",
    "evidence_records_since_checkpoint",
    "active_work_files",
    "active_work_bytes",
)


ACTIVE_FORMAL_SOURCE_LIMIT = 8
CONFIRMATION_SOURCE_LIMIT = 8
CONFIRMATION_SLOT_LOCATOR_LIMIT = 16
CONFIRMATION_CLAIM_LOCATOR_LIMIT = 8
ACTIVE_CONTEXT_TEXT_PREVIEW_BYTES = 256
ACTIVE_CONTEXT_FRONTIER_ITEM_LIMIT = 8
ACTIVE_CONTEXT_FILENAME = "ACTIVE_CONTEXT.json"
COMPACTION_DUE_FILENAME = "COMPACTION_DUE.json"


def require_bounded_claims_version(
    claims_data: Mapping[str, Any], *, operation: str
) -> None:
    """Require the bounded current Claims schema for active operations.

    Schema V1 remains readable for historical validation, but its arrays and
    strings were unbounded.  Treating it as active input would recreate the
    very context-growth failure this protocol is meant to prevent.
    """

    version = claims_data.get("schema_version")
    if version != CLAIMS_SCHEMA_VERSION:
        raise ValidationError(
            f"{operation} requires bounded CLAIMS.json schema_version "
            f"{CLAIMS_SCHEMA_VERSION}; schema_version {version!r} is "
            "historical-validation-only and must be semantically compacted "
            "into a bounded current Claims file before active research resumes"
        )


def claim_lifecycle(claim: Mapping[str, Any]) -> str:
    """Return the explicit lifecycle, treating legacy Claims as active."""

    value = claim.get("lifecycle", "active")
    return value if isinstance(value, str) else ""


def active_claims(claims_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select Frontier Claims in Frontier order without inventing content."""

    claims = claims_data.get("claims", [])
    frontier = claims_data.get("frontier", {})
    if not isinstance(claims, list) or not isinstance(frontier, dict):
        return []
    by_id = {
        claim.get("claim_id"): claim
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim_id in frontier.get("claim_ids", []):
        if not isinstance(claim_id, str) or claim_id in seen:
            continue
        claim = by_id.get(claim_id)
        if claim is not None:
            selected.append(claim)
            seen.add(claim_id)
    return selected


def inactive_claim_refs(claims_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return bounded, content-addressed refs for Claims outside the Frontier."""

    active_ids = {
        str(claim.get("claim_id")) for claim in active_claims(claims_data)
    }
    records: list[dict[str, Any]] = []
    claims = claims_data.get("claims", [])
    if not isinstance(claims, list):
        return records
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or claim_id in active_ids:
            continue
        record = {
            "claim_id": claim_id,
            "lifecycle": claim_lifecycle(claim),
            "state": claim.get("state"),
            "sha256": sha256_json(claim),
        }
        superseded_by = claim.get("superseded_by")
        if isinstance(superseded_by, str):
            record["superseded_by"] = superseded_by
        records.append(record)
    return records


def _pressure_thresholds(paths: StudyPaths) -> dict[str, dict[str, int]]:
    policy_path = paths.root / "scientific-workflow" / "policy.json"
    policy = load_json(policy_path)
    if not isinstance(policy, dict):
        raise ValidationError("scientific-workflow/policy.json must be an object")
    active_context = policy.get("active_context")
    if not isinstance(active_context, dict):
        raise ValidationError("policy active_context must be an object")
    pressure = active_context.get("compaction_pressure")
    if not isinstance(pressure, dict):
        raise ValidationError("policy active_context.compaction_pressure must be an object")
    if set(pressure) != set(PRESSURE_METRICS):
        missing = sorted(set(PRESSURE_METRICS) - set(pressure))
        extra = sorted(set(pressure) - set(PRESSURE_METRICS))
        raise ValidationError(
            "policy compaction-pressure metrics mismatch; "
            f"missing={missing}, extra={extra}"
        )
    thresholds: dict[str, dict[str, int]] = {}
    for name in PRESSURE_METRICS:
        value = pressure[name]
        if not isinstance(value, dict) or set(value) != {"soft", "hard"}:
            raise ValidationError(
                f"policy pressure metric {name} must contain exactly soft and hard"
            )
        soft = value["soft"]
        hard = value["hard"]
        if (
            isinstance(soft, bool)
            or isinstance(hard, bool)
            or not isinstance(soft, int)
            or not isinstance(hard, int)
            or soft < 0
            or hard <= soft
        ):
            raise ValidationError(
                f"policy pressure metric {name} requires integers 0 <= soft < hard"
            )
        thresholds[name] = {"soft": soft, "hard": hard}
    return thresholds


def _latest_checkpoint(paths: StudyPaths) -> dict[str, Any] | None:
    source = _latest_checkpoint_source(paths)
    return source[1] if source is not None else None


def _latest_checkpoint_source(
    paths: StudyPaths,
) -> tuple[Path, dict[str, Any]] | None:
    sequence = require_checkpoint_sequence(paths)
    high_water_mark = int(sequence["high_water_mark"])
    candidates = sorted(paths.checkpoints.glob("CHECKPOINT-*.json"))
    expected_names = [
        f"CHECKPOINT-{number:06d}.json"
        for number in range(1, high_water_mark + 1)
    ]
    if [path.name for path in candidates] != expected_names:
        raise ValidationError(
            "visible Checkpoints do not match the monotone Checkpoint sequence"
        )
    if high_water_mark == 0:
        return None
    path = candidates[-1]
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValidationError("latest Checkpoint must be an object")
    latest = sequence.get("latest_checkpoint")
    if not isinstance(latest, dict) or (
        latest.get("checkpoint_id") != value.get("checkpoint_id")
        or latest.get("sha256") != value.get("checkpoint_sha256")
    ):
        raise ValidationError(
            "latest Checkpoint does not match the monotone Checkpoint sequence"
        )
    return path, value


def _active_formal_source_index(paths: StudyPaths) -> dict[str, Any]:
    """Return a bounded index without loading formal-artifact contents.

    The readiness rules remain centralized in ``rendering.active_formal_artifacts``.
    The local import avoids a module-import cycle because rendering itself uses
    the active-context pressure functions.
    """

    from .rendering import active_formal_artifacts

    active = [item for item in active_formal_artifacts(paths) if item["active"]]
    inventory = [
        {
            "kind": item["kind"],
            "path": item["path"],
            "size": item["size"],
            "sha256": item["sha256"],
        }
        for item in active
    ]
    selected = inventory[:ACTIVE_FORMAL_SOURCE_LIMIT]
    return {
        "sources": selected,
        "total_count": len(inventory),
        "selected_count": len(selected),
        "truncated": len(selected) != len(inventory),
        "inventory_sha256": sha256_json(inventory),
    }


def _bounded_locator_index(
    items: list[Any], *, limit: int
) -> dict[str, Any]:
    """Return a finite locator prefix committed to the complete inventory."""

    selected = items[:limit]
    return {
        "items": selected,
        "total_count": len(items),
        "selected_count": len(selected),
        "truncated": len(selected) != len(items),
        "inventory_sha256": sha256_json(items),
    }


def _confirmation_source_index(paths: StudyPaths) -> dict[str, Any]:
    """Index resumable Confirmation work without making it active formal context.

    Finalized records are immutable history.  Their pending slots, running
    attempts, and open Evidence drafts are selected as resumable work, while
    bounded locators and complete inventory hashes make truncation explicit.
    Editable Confirmation drafts are exposed separately so resumption does not
    create a duplicate draft.
    """

    # Local imports avoid cycles: validation and run_registry both use active
    # context helpers during their normal module initialization.
    from .confirmation import load_final_confirmation
    from .run_registry import confirmation_binding
    from .validation import evidence_index, run_index

    attempts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run_id, (manifest_path, manifest) in run_index(paths).items():
        binding = confirmation_binding(manifest)
        if binding is None:
            continue
        key = (
            binding["confirmation_id"],
            binding["confirmation_sha256"],
        )
        attempts.setdefault(key, []).append(
            {
                "slot_id": binding["slot_id"],
                "run_id": run_id,
                "status": str(manifest.get("status", "")),
                "path": manifest_path.relative_to(paths.root).as_posix(),
                "size": manifest_path.stat().st_size,
                "sha256": sha256_file(manifest_path),
            }
        )

    evidence_counts: dict[tuple[str, str], int] = {}
    evidence_drafts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for evidence_path, item in evidence_index(paths).values():
        basis = item.get("evidence_basis")
        confirmation = basis.get("confirmation") if isinstance(basis, dict) else None
        if not isinstance(confirmation, dict):
            continue
        confirmation_id = confirmation.get("confirmation_id")
        record_sha256 = confirmation.get("sha256")
        if not isinstance(confirmation_id, str) or not isinstance(record_sha256, str):
            continue
        key = (confirmation_id, record_sha256)
        if item.get("status") == "finalized":
            evidence_counts[key] = evidence_counts.get(key, 0) + 1
        elif item.get("status") == "draft":
            evidence_drafts.setdefault(key, []).append(
                {
                    "evidence_id": item.get("evidence_id"),
                    "version": item.get("version"),
                    "path": evidence_path.relative_to(paths.root).as_posix(),
                    "size": evidence_path.stat().st_size,
                    "sha256": sha256_file(evidence_path),
                }
            )

    finalized_paths = (
        sorted(
            paths.confirmations.glob("CONF-*.json"),
            key=lambda item: item.name,
        )
        if paths.confirmations.is_dir()
        else []
    )
    finalized_ids = {path.stem for path in finalized_paths}

    drafts: list[dict[str, Any]] = []
    if paths.active_work.is_dir():
        for path in sorted(
            paths.active_work.glob("CONF-*.confirmation.draft.json"),
            key=lambda item: item.name,
        ):
            if path.is_symlink() or not path.is_file():
                continue
            confirmation_id = path.name.removesuffix(
                ".confirmation.draft.json"
            )
            if confirmation_id in finalized_ids:
                # Finalization retains the editable source for provenance; it
                # is no longer resumable once the immutable record exists.
                continue
            drafts.append(
                {
                    "confirmation_id": confirmation_id,
                    "path": path.relative_to(paths.root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )

    history: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    in_progress: list[dict[str, Any]] = []
    awaiting_evidence: list[dict[str, Any]] = []
    if finalized_paths:
        for path in finalized_paths:
            confirmation_id = path.stem
            record = load_final_confirmation(paths, confirmation_id)
            record_sha256 = str(record["record_sha256"])
            slot_ids = [str(item["slot_id"]) for item in record["run_slots"]]
            key = (confirmation_id, record_sha256)
            record_attempts = sorted(
                attempts.get(key, []),
                key=lambda item: (str(item["slot_id"]), str(item["run_id"])),
            )
            consumed_ids = {
                str(item["slot_id"]) for item in record_attempts
            }
            pending_ids = [slot_id for slot_id in slot_ids if slot_id not in consumed_ids]
            claim_ids = [str(item["claim_id"]) for item in record["claims"]]
            finalized_evidence_count = evidence_counts.get(key, 0)
            draft_evidence = sorted(
                evidence_drafts.get(key, []),
                key=lambda item: (str(item["evidence_id"]), int(item["version"])),
            )
            running_slots = [
                item for item in record_attempts if item["status"] == "running"
            ]
            has_running_slot = bool(running_slots)
            history_item = {
                "confirmation_id": confirmation_id,
                "path": path.relative_to(paths.root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "record_sha256": record_sha256,
                "frozen_at": record.get("frozen_at"),
                "planned_slot_count": len(slot_ids),
                "consumed_slot_count": len(slot_ids) - len(pending_ids),
                "pending_slot_count": len(pending_ids),
                "running_slot_count": len(running_slots),
                "finalized_evidence_count": finalized_evidence_count,
                "draft_evidence_count": len(draft_evidence),
            }
            history.append(history_item)
            action_item = {
                **history_item,
                "claim_ids": _bounded_locator_index(
                    claim_ids,
                    limit=CONFIRMATION_CLAIM_LOCATOR_LIMIT,
                ),
                "evidence_drafts": _bounded_locator_index(
                    draft_evidence,
                    limit=CONFIRMATION_SOURCE_LIMIT,
                ),
            }
            if pending_ids:
                pending.append(
                    {
                        **action_item,
                        "pending_slot_ids": _bounded_locator_index(
                            pending_ids,
                            limit=CONFIRMATION_SLOT_LOCATOR_LIMIT,
                        ),
                    }
                )
            if has_running_slot:
                in_progress.append(
                    {
                        **action_item,
                        "running_slots": _bounded_locator_index(
                            running_slots,
                            limit=CONFIRMATION_SLOT_LOCATOR_LIMIT,
                        ),
                    }
                )
            if draft_evidence or (
                not has_running_slot
                and not pending_ids
                and finalized_evidence_count == 0
            ):
                awaiting_evidence.append(action_item)

    completed_count = sum(
        int(item["finalized_evidence_count"]) > 0 for item in history
    )
    return {
        "drafts": _bounded_locator_index(
            drafts,
            limit=CONFIRMATION_SOURCE_LIMIT,
        ),
        "pending_finalized": _bounded_locator_index(
            pending,
            limit=CONFIRMATION_SOURCE_LIMIT,
        ),
        "in_progress": _bounded_locator_index(
            in_progress,
            limit=CONFIRMATION_SOURCE_LIMIT,
        ),
        "awaiting_evidence": _bounded_locator_index(
            awaiting_evidence,
            limit=CONFIRMATION_SOURCE_LIMIT,
        ),
        "history": {
            **_bounded_locator_index(
                history,
                limit=CONFIRMATION_SOURCE_LIMIT,
            ),
            "pending_count": len(pending),
            "in_progress_count": len(in_progress),
            "awaiting_evidence_count": len(awaiting_evidence),
            "completed_count": completed_count,
        },
    }


def _text_preview(
    value: Any, *, byte_limit: int = ACTIVE_CONTEXT_TEXT_PREVIEW_BYTES
) -> dict[str, Any]:
    """Return a prefix bounded by canonical JSON bytes, not code points.

    Schema ``maxLength`` counts Unicode code points.  The serialized selector
    is byte-budgeted, and one code point may consume four UTF-8 bytes or six
    JSON escape bytes.  Binary search is safe because serialized prefix size
    is monotone as code points are appended.
    """

    text = value if isinstance(value, str) else ""
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(canonical_json_bytes(text[:middle])) <= byte_limit:
            low = middle
        else:
            high = middle - 1
    preview = text[:low]
    return {
        "preview": preview,
        "preview_canonical_bytes": len(canonical_json_bytes(preview)),
        "characters": len(text),
        "truncated": len(preview) < len(text),
        "sha256": sha256_json(text),
    }


def _bounded_text_index(value: Any) -> dict[str, Any]:
    items = value if isinstance(value, list) else []
    selected = [
        _text_preview(item)
        for item in items[:ACTIVE_CONTEXT_FRONTIER_ITEM_LIMIT]
    ]
    return {
        "items": selected,
        "total_count": len(items),
        "selected_count": len(selected),
        "truncated": len(selected) != len(items),
        "inventory_sha256": sha256_json(items),
    }


def _claim_selector_ref(claim: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact locator, never a full Claim payload."""

    def count(field: str) -> int:
        value = claim.get(field)
        return len(value) if isinstance(value, list) else 0

    return {
        "claim_id": claim.get("claim_id"),
        "state": claim.get("state"),
        "evidence_basis": claim.get("evidence_basis", "none"),
        "lifecycle": claim_lifecycle(claim),
        "updated_at": claim.get("updated_at"),
        "statement": _text_preview(claim.get("statement")),
        "scope": _text_preview(claim.get("scope")),
        "uncertainty_present": bool(claim.get("uncertainty")),
        "limitations_count": count("limitations"),
        "evidence_counts": {
            "supporting": count("supporting_evidence"),
            "contradictory": count("contradictory_evidence"),
            "other": count("other_evidence"),
        },
        "sha256": sha256_json(claim),
    }


def _frontier_selector(frontier: Mapping[str, Any]) -> dict[str, Any]:
    claim_ids = frontier.get("claim_ids", [])
    if not isinstance(claim_ids, list):
        claim_ids = []
    return {
        "summary": _text_preview(frontier.get("summary")),
        "claim_ids": claim_ids,
        "open_questions": _bounded_text_index(frontier.get("open_questions")),
        "next_actions": _bounded_text_index(frontier.get("next_actions")),
        "human_decisions_required": _bounded_text_index(
            frontier.get("human_decisions_required")
        ),
        "sha256": sha256_json(frontier),
    }


def build_active_selector(
    paths: StudyPaths,
    *,
    claims_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded default context selector, never source-file contents."""

    if claims_data is None:
        loaded_claims = load_json(paths.claims)
        if not isinstance(loaded_claims, dict):
            raise ValidationError("CLAIMS.json must be an object")
        claims_data = loaded_claims
    require_bounded_claims_version(
        claims_data,
        operation="active-context projection",
    )
    frontier = claims_data.get("frontier", {})
    if not isinstance(frontier, dict):
        frontier = {}
    selected_claims = active_claims(claims_data)
    selected_claim_refs = [_claim_selector_ref(claim) for claim in selected_claims]
    all_claims = claims_data.get("claims", [])
    all_claim_count = len(all_claims) if isinstance(all_claims, list) else 0
    formal = _active_formal_source_index(paths)

    checkpoint_source = _latest_checkpoint_source(paths)
    checkpoint_summary: dict[str, Any] | None = None
    if checkpoint_source is not None:
        checkpoint_path, checkpoint = checkpoint_source
        checkpoint_summary = {
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "path": checkpoint_path.relative_to(paths.root).as_posix(),
            "size": checkpoint_path.stat().st_size,
            "sha256": sha256_file(checkpoint_path),
            "record_sha256": checkpoint.get("checkpoint_sha256"),
            "created_at": checkpoint.get("created_at"),
            "active_claim_count": len(checkpoint.get("claims_snapshot", [])),
            "decisive_evidence_count": len(
                checkpoint.get("decisive_evidence", [])
            ),
            "contradictory_evidence_count": len(
                checkpoint.get("contradictory_evidence", [])
            ),
        }

    selector = {
        "schema_version": 1,
        "study_id": paths.study_id,
        "brief": {
            "path": paths.brief.relative_to(paths.root).as_posix(),
            "size": paths.brief.stat().st_size,
            "sha256": sha256_file(paths.brief),
        },
        "claims_source": {
            "path": paths.claims.relative_to(paths.root).as_posix(),
            "size": paths.claims.stat().st_size,
            "sha256": sha256_file(paths.claims),
            "revision": claims_data.get("revision"),
            "authoritative_claim_count": all_claim_count,
            "selected_claim_count": len(selected_claims),
        },
        "frontier": _frontier_selector(frontier),
        "selected_claims": selected_claim_refs,
        "active_formal_artifacts": formal,
        "confirmations": _confirmation_source_index(paths),
        "latest_checkpoint": checkpoint_summary,
        "selector_sha256": "",
    }
    selector["selector_sha256"] = record_digest(selector, "selector_sha256")
    return selector


def active_selector_bytes(selector: Mapping[str, Any]) -> int:
    """Return bytes written by :func:`write_active_selector` exactly."""

    return len(canonical_json_bytes(selector)) + 1


def write_active_selector(
    paths: StudyPaths,
    *,
    claims_data: Mapping[str, Any] | None = None,
) -> Path:
    selector = build_active_selector(paths, claims_data=claims_data)
    size = active_selector_bytes(selector)
    hard_limit = _pressure_thresholds(paths)["active_selector_bytes"]["hard"]
    if size >= hard_limit:
        raise ValidationError(
            "active-context selector would exceed its structural byte budget: "
            f"{size} bytes reaches hard limit {hard_limit}; validate the bounded "
            "Claims schema and compact the Frontier before loading active context"
        )
    output = paths.generated / ACTIVE_CONTEXT_FILENAME
    atomic_write_bytes(output, canonical_json_bytes(selector) + b"\n")
    return output


def _projected_pressure(
    pressure: Mapping[str, Any], operation: str | None
) -> dict[str, Any]:
    """Project the single monotone counter changed by a growth preflight."""

    projected = deepcopy(dict(pressure))
    increments: dict[str, int] = {}
    if operation and "Run" in operation:
        increments["runs_since_checkpoint"] = 1
    elif operation and "Evidence" in operation:
        increments["evidence_records_since_checkpoint"] = 1
    if not increments:
        return projected

    overall = "normal"
    reasons: list[str] = []
    for metric in projected.get("metrics", []):
        name = metric.get("name")
        observed = int(metric.get("observed", 0)) + increments.get(name, 0)
        metric["observed"] = observed
        level = "normal"
        if observed >= int(metric["hard"]):
            level = "hard"
            overall = "hard"
        elif observed >= int(metric["soft"]):
            level = "soft"
            if overall == "normal":
                overall = "soft"
        metric["level"] = level
        if level != "normal":
            reasons.append(
                f"{name}={observed} reached {level} threshold {metric[level]}"
            )
    projected["level"] = overall
    projected["compaction_due"] = overall in {"soft", "hard"}
    projected["growth_blocked"] = overall == "hard"
    projected["reasons"] = reasons
    return projected


def write_compaction_due(
    paths: StudyPaths,
    pressure: Mapping[str, Any],
    *,
    operation: str | None = None,
    output: Path | None = None,
    include_active_context: bool = True,
) -> Path:
    """Persist a deterministic advisory; it is generated, never authority."""

    projected = _projected_pressure(pressure, operation)
    selector_path = paths.generated / ACTIVE_CONTEXT_FILENAME
    selector_binding = (
        {
            "path": selector_path.relative_to(paths.root).as_posix(),
            "size": selector_path.stat().st_size,
            "sha256": sha256_file(selector_path),
        }
        if include_active_context and selector_path.is_file()
        else None
    )
    advisory = {
        "schema_version": 1,
        "study_id": paths.study_id,
        "generated_projection": True,
        "operation": operation,
        "active_context": selector_binding,
        "current_level": pressure.get("level"),
        "projected_level": projected.get("level"),
        "compaction_due": bool(projected.get("compaction_due")),
        "growth_blocked_now": bool(pressure.get("growth_blocked")),
        "reasons": projected.get("reasons", []),
    }
    destination = output or (paths.generated / COMPACTION_DUE_FILENAME)
    atomic_write_json(destination, advisory)
    return destination


def runtime_compaction_due_path(paths: StudyPaths) -> Path:
    """Return an ignored local-runtime path that cannot dirty the repository."""

    configured = os.environ.get("STUDYCTL_RUNTIME_DIR", "").strip()
    base = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    repository_key = hashlib.sha256(
        str(paths.root.resolve()).encode("utf-8")
    ).hexdigest()[:24]
    return (
        base
        / "studyctl-runtime"
        / repository_key
        / paths.study_id
        / COMPACTION_DUE_FILENAME
    )


def refresh_active_projection(
    paths: StudyPaths,
    *,
    claims_data: Mapping[str, Any] | None = None,
    pressure: Mapping[str, Any] | None = None,
    operation: str | None = None,
) -> tuple[Path, Path]:
    selector = write_active_selector(paths, claims_data=claims_data)
    effective_pressure = (
        dict(pressure)
        if pressure is not None
        else compaction_pressure(paths, claims_data=claims_data)
    )
    advisory = write_compaction_due(paths, effective_pressure, operation=operation)
    return selector, advisory


def _run_records(paths: StudyPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.runs.is_dir():
        return records
    for manifest_path in sorted(paths.runs.glob("RUN-*/manifest.json")):
        value = load_json(manifest_path)
        if not isinstance(value, dict):
            raise ValidationError(f"Run manifest must be an object: {manifest_path}")
        records.append(value)
    return records


def _evidence_records(paths: StudyPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.evidence.is_dir():
        return records
    for evidence_path in sorted(paths.evidence.glob("EVID-*.v*.json")):
        value = load_json(evidence_path)
        if not isinstance(value, dict):
            raise ValidationError(f"Evidence must be an object: {evidence_path}")
        records.append(value)
    return records


def _active_work_size(paths: StudyPaths) -> tuple[int, int]:
    count = 0
    size = 0
    if not paths.active_work.is_dir():
        return count, size
    for path in sorted(paths.active_work.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        count += 1
        size += path.stat().st_size
    return count, size


def pressure_watermarks(
    *, run_count: int, evidence_high_water_mark: int
) -> dict[str, int]:
    """Capture the finite counters reset by a newly finalized Checkpoint."""

    return {
        "run_count": run_count,
        "evidence_record_count": evidence_high_water_mark,
    }


def compaction_pressure(
    paths: StudyPaths,
    *,
    claims_data: Mapping[str, Any] | None = None,
    runs: Mapping[str, tuple[Path, dict[str, Any]]] | None = None,
    evidence: Mapping[tuple[str, int], tuple[Path, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compute deterministic soft/hard compaction pressure from policy."""

    thresholds = _pressure_thresholds(paths)
    if claims_data is None:
        loaded_claims = load_json(paths.claims)
        if not isinstance(loaded_claims, dict):
            raise ValidationError("CLAIMS.json must be an object")
        claims_data = loaded_claims
    require_bounded_claims_version(
        claims_data,
        operation="active-context pressure",
    )
    run_records = (
        [item for _, item in runs.values()]
        if runs is not None
        else _run_records(paths)
    )
    evidence_records = (
        [item for _, item in evidence.values()]
        if evidence is not None
        else _evidence_records(paths)
    )
    checkpoint = _latest_checkpoint(paths)
    ledger = load_ledger(paths)
    # New Studies always have a monotone ledger.  Frozen pre-ledger V1/V2
    # history remains readable for explicit migration and compaction; its
    # visible count is necessarily lower assurance because prior deletion
    # cannot be reconstructed from local files.
    run_count = (
        int(ledger["high_water_mark"])
        if ledger is not None
        else len(run_records)
    )
    if ledger is not None and run_count < len(run_records):
        raise ValidationError(
            "Run ledger high_water_mark is below the visible Run record count"
        )
    visible_evidence_record_count = len(evidence_records)
    sequence = require_evidence_sequence(paths)
    evidence_high_water_mark = int(sequence["high_water_mark"])
    if evidence_high_water_mark < visible_evidence_record_count:
        raise ValidationError(
            "Evidence sequence high_water_mark is below the visible Evidence record count"
        )
    run_watermark = 0
    evidence_watermark = 0
    if checkpoint is not None:
        watermarks = checkpoint.get("active_context_watermarks")
        if isinstance(watermarks, dict):
            raw_run = watermarks.get("run_count", 0)
            raw_evidence = watermarks.get("evidence_record_count", 0)
            if (
                isinstance(raw_run, bool)
                or not isinstance(raw_run, int)
                or raw_run < 0
            ):
                raise ValidationError(
                    "latest Checkpoint Run watermark must be a non-negative integer"
                )
            run_watermark = raw_run
            if (
                isinstance(raw_evidence, bool)
                or not isinstance(raw_evidence, int)
                or raw_evidence < 0
            ):
                raise ValidationError(
                    "latest Checkpoint Evidence watermark must be a non-negative integer"
                )
            evidence_watermark = raw_evidence
        else:
            # A legacy Checkpoint has no authoritative Evidence sequence
            # baseline.  Zero is conservative: it can overstate pressure but
            # cannot hide creation history through timestamp reconstruction.
            legacy_run = checkpoint.get("budget_state", {}).get("run_count", 0)
            if (
                isinstance(legacy_run, int)
                and not isinstance(legacy_run, bool)
                and legacy_run >= 0
            ):
                run_watermark = legacy_run
    if run_watermark > run_count:
        raise ValidationError(
            "Run ledger high_water_mark is below the latest Checkpoint watermark"
        )
    if evidence_watermark > evidence_high_water_mark:
        raise ValidationError(
            "Evidence sequence high_water_mark is below the latest Checkpoint watermark"
        )

    frontier = claims_data.get("frontier", {})
    if not isinstance(frontier, dict):
        frontier = {}
    selected_claims = active_claims(claims_data)
    all_claim_records = claims_data.get("claims", [])
    if not isinstance(all_claim_records, list):
        all_claim_records = []
    terminal_claim_count = sum(
        isinstance(claim, dict)
        and claim_lifecycle(claim) in {"retired", "superseded"}
        for claim in all_claim_records
    )
    selector = build_active_selector(paths, claims_data=claims_data)
    active_work_files, active_work_bytes = _active_work_size(paths)
    observations = {
        "active_claims": len(selected_claims),
        "authoritative_claims": len(all_claim_records),
        "terminal_claims": terminal_claim_count,
        "claims_source_bytes": paths.claims.stat().st_size,
        "frontier_open_questions": len(frontier.get("open_questions", [])),
        "frontier_next_actions": len(frontier.get("next_actions", [])),
        "frontier_human_decisions": len(
            frontier.get("human_decisions_required", [])
        ),
        "active_selector_bytes": active_selector_bytes(selector),
        "runs_since_checkpoint": run_count - run_watermark,
        "evidence_records_since_checkpoint": (
            evidence_high_water_mark - evidence_watermark
        ),
        "active_work_files": active_work_files,
        "active_work_bytes": active_work_bytes,
    }
    metrics: list[dict[str, Any]] = []
    reasons: list[str] = []
    overall = "normal"
    for name in PRESSURE_METRICS:
        observed = observations[name]
        threshold = thresholds[name]
        level = "normal"
        if observed >= threshold["hard"]:
            level = "hard"
            overall = "hard"
        elif observed >= threshold["soft"]:
            level = "soft"
            if overall == "normal":
                overall = "soft"
        metric = {
            "name": name,
            "observed": observed,
            "soft": threshold["soft"],
            "hard": threshold["hard"],
            "level": level,
        }
        metrics.append(metric)
        if level != "normal":
            reasons.append(
                f"{name}={observed} reached {level} threshold "
                f"{threshold[level]}"
            )
    return {
        "level": overall,
        "compaction_due": overall in {"soft", "hard"},
        "growth_blocked": overall == "hard",
        "latest_checkpoint": (
            {
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "sha256": checkpoint.get("checkpoint_sha256"),
            }
            if checkpoint is not None
            else None
        ),
        "watermarks": {
            "run_count": run_watermark,
            "evidence_record_count": evidence_watermark,
        },
        "metrics": metrics,
        "reasons": reasons,
    }


def require_growth_allowed(paths: StudyPaths, operation: str) -> dict[str, Any]:
    """Block growth at hard pressure without choosing compaction content."""

    pressure = compaction_pressure(paths)
    # The preflight predicts the one counter this operation would increment.
    # Persist it outside the worktree so observability cannot make an otherwise
    # clean, reproducible Run dirty.  ``status`` and ``context`` also refresh
    # the repository-local bounded selector and advisory projections.
    write_compaction_due(
        paths,
        pressure,
        operation=operation,
        output=runtime_compaction_due_path(paths),
        include_active_context=False,
    )
    if pressure["growth_blocked"]:
        hard_reasons = [
            reason
            for reason in pressure["reasons"]
            if "reached hard threshold" in reason
        ]
        details = "; ".join(hard_reasons)
        raise ValidationError(
            f"compaction pressure hard threshold blocks {operation}: {details}. "
            "Prepare semantic compaction before further growth; the deterministic "
            "CLI will not delete history or choose scientific content automatically."
        )
    return pressure
