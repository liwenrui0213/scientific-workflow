from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import re
from typing import Any, Iterator

from .active_context import active_claims, require_growth_allowed
from .evidence_sequence import (
    evidence_sequence_temporary_paths,
    load_evidence_sequence,
    migrate_legacy_evidence_sequence,
    reserve_evidence_creation,
)
from .formalization import check_formalization
from .hashing import (
    atomic_write_json,
    load_json,
    nested_record_digest,
    record_digest,
    sha256_file,
    sha256_json,
)
from .locking import serialized_study_authority
from .models import (
    SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
    WorkflowError,
    require_id,
    utc_now,
)
from .run_ledger import require_consistent_ledger
from .run_registry import confirmation_binding, effective_run_mode
from .validation import (
    brief_approval_issues,
    brief_content_issues,
    checkpoint_paths,
    evidence_paths,
    errors_only,
    object_schema_issues,
    run_index,
    run_dependency_integrity_issues,
    sealed_run_evidence_eligible,
)


_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "interrupted"}
_CONFIRMATION_TERMINAL_RUN_STATUSES = _TERMINAL_RUN_STATUSES | {"incomplete"}


def effective_evidence_mode(item: dict[str, Any]) -> str:
    """Return an Evidence record's effective epistemic mode.

    Finalized Evidence created before ``evidence_basis`` became first-class is
    historical exploratory Evidence.  Missing metadata can therefore never
    upgrade an old record to confirmatory status.
    """

    basis = item.get("evidence_basis")
    if not isinstance(basis, dict):
        return "exploratory"
    mode = basis.get("mode")
    return mode if mode in {"exploratory", "confirmatory", "mixed"} else "exploratory"


def _invalid_object_message(kind: str, issues: Sequence[Any]) -> str:
    return f"invalid {kind}:\n" + "\n".join(issue.render() for issue in issues)


def _normalize_ids(kind: str, values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{kind} IDs must be supplied as a sequence")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValidationError(f"{kind} ID must be a string: {value!r}")
        require_id(kind, value)
        if value in seen:
            raise ValidationError(f"duplicate {kind} ID: {value}")
        seen.add(value)
        normalized.append(value)
    if not normalized:
        raise ValidationError(f"at least one {kind} ID is required")
    return normalized


def _claims_object(paths: StudyPaths) -> dict[str, Any]:
    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must be an object")
    if claims.get("study_id") != paths.study_id:
        raise ValidationError("CLAIMS.json study_id does not match Study")
    return claims


def _validate_claim_references(paths: StudyPaths, claim_ids: Sequence[str]) -> None:
    requested = _normalize_ids("claim", claim_ids)
    claims = _claims_object(paths)
    available: set[str] = set()
    for claim in claims.get("claims", []):
        if not isinstance(claim, dict):
            raise ValidationError("CLAIMS.json contains a non-object Claim")
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str):
            raise ValidationError("CLAIMS.json contains a Claim without a string claim_id")
        require_id("claim", claim_id)
        if claim_id in available:
            raise ValidationError(f"CLAIMS.json contains duplicate Claim ID: {claim_id}")
        available.add(claim_id)
    missing = sorted(set(requested) - available)
    if missing:
        raise ValidationError(f"Evidence references missing Claim(s): {', '.join(missing)}")
    active_ids = {str(claim.get("claim_id")) for claim in active_claims(claims)}
    inactive = sorted(set(requested) - active_ids)
    if inactive:
        raise ValidationError(
            "new Evidence may address only active Frontier Claim(s): "
            + ", ".join(inactive)
        )


def _terminal_run(paths: StudyPaths, run_id: str) -> dict[str, Any]:
    require_id("run", run_id)
    manifest_path = paths.runs / run_id / "manifest.json"
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValidationError(f"Run manifest must be an object: {manifest_path}")
    schema_issues = object_schema_issues(paths.root, "run", manifest_path, manifest)
    if schema_issues:
        raise ValidationError(_invalid_object_message(f"Run manifest {run_id}", schema_issues))
    if manifest.get("study_id") != paths.study_id:
        raise ValidationError(f"Run {run_id} belongs to a different Study")
    if manifest.get("run_id") != run_id:
        raise ValidationError(f"Run manifest identity does not match reference: {run_id}")
    status = manifest.get("status")
    if status not in _TERMINAL_RUN_STATUSES:
        raise ValidationError(f"Evidence may reference only terminal Runs: {run_id} is {status!r}")
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise ValidationError(f"Run {run_id} has no integrity record")
    manifest_digest = integrity.get("manifest_sha256")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        raise ValidationError(f"terminal Run {run_id} has no manifest_sha256")
    expected_digest = nested_record_digest(manifest, "integrity", "manifest_sha256")
    if manifest_digest != expected_digest:
        raise ValidationError(f"Run manifest integrity check failed: {run_id}")
    if not str(integrity.get("sealed_at") or "").strip():
        raise ValidationError(f"terminal Run {run_id} has no sealed_at timestamp")
    code_state = manifest.get("code_state", {})
    expected_code_change = code_state.get("before") != code_state.get("after")
    if code_state.get("changed_during_run") != expected_code_change:
        raise ValidationError(f"Run tracked-code change flag is invalid: {run_id}")
    if expected_code_change:
        raise ValidationError(
            f"Evidence cannot use a Run whose tracked code changed during execution: {run_id}"
        )
    cohort = manifest.get("cohort")
    if not isinstance(cohort, dict) or not isinstance(cohort.get("fields"), dict):
        raise ValidationError(f"Run {run_id} has no valid Cohort fields")
    fingerprint = cohort.get("fingerprint_sha256")
    if fingerprint != sha256_json(cohort["fields"]):
        raise ValidationError(f"Run Cohort fingerprint is invalid: {run_id}")
    dependency_issues = errors_only(
        run_dependency_integrity_issues(paths, manifest, for_evidence=True)
    )
    if dependency_issues:
        raise ValidationError(
            _invalid_object_message(
                f"Run dependency integrity for Evidence {run_id}",
                dependency_issues,
            )
        )
    expected_eligibility = sealed_run_evidence_eligible(manifest)
    recorded_eligibility = manifest.get("change_scope", {}).get("evidence_eligible")
    if recorded_eligibility is not expected_eligibility:
        raise ValidationError(
            f"Run sealed Evidence eligibility is internally inconsistent: {run_id}"
        )
    if not expected_eligibility:
        raise ValidationError(
            f"Evidence cannot use a Run with unverifiable or blocked change scope: {run_id}"
        )
    return manifest


def _changed_cohort_fields(manifests: Sequence[dict[str, Any]]) -> list[str]:
    fields = [manifest["cohort"]["fields"] for manifest in manifests]
    keys = sorted({str(key) for item in fields for key in item})
    changed: list[str] = []
    for key in keys:
        signatures = [
            ("value", sha256_json(item[key])) if key in item else ("missing", "")
            for item in fields
        ]
        if any(signature != signatures[0] for signature in signatures[1:]):
            changed.append(key)
    return changed


def _exploratory_evidence_basis(run_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "mode": "exploratory",
        "exploratory_run_ids": list(run_ids),
        "confirmatory_run_ids": [],
        "confirmation": None,
        "planned_slot_ids": [],
        "included_slot_ids": [],
        "missing_slot_ids": [],
        "excluded_confirmatory_runs": [],
        "held_out": {
            "status": "not_held_out",
            "freshness": "unknown",
            "workflow_observed_prior_run_count": 0,
        },
    }


def _confirmation_record(
    paths: StudyPaths,
    confirmation_id: str,
    confirmation_sha256: str,
) -> dict[str, Any]:
    # Confirmation is imported lazily so the long-standing exploratory path
    # remains independent of confirmatory registration machinery.
    from .confirmation import load_final_confirmation

    confirmation = load_final_confirmation(paths, confirmation_id)
    if not isinstance(confirmation, dict):
        raise WorkflowError("confirmation loader did not return an object")
    if confirmation.get("confirmation_id") != confirmation_id:
        raise ValidationError(
            f"Confirmation identity does not match Run binding: {confirmation_id}"
        )
    if confirmation.get("record_sha256") != confirmation_sha256:
        raise ValidationError(
            f"confirmatory Runs have a stale Confirmation reference: {confirmation_id}"
        )
    return confirmation


def _confirmation_claim_issues(
    paths: StudyPaths,
    claim_ids: Sequence[str],
    confirmation: dict[str, Any],
) -> None:
    from .confirmation import claim_spec_sha256

    frozen_claims = confirmation.get("claims")
    if not isinstance(frozen_claims, list):
        raise ValidationError("Confirmation claims must be a list")
    frozen_ids = [
        str(claim.get("claim_id"))
        for claim in frozen_claims
        if isinstance(claim, dict)
    ]
    if len(frozen_ids) != len(frozen_claims) or set(frozen_ids) != set(claim_ids):
        raise ValidationError(
            "confirmatory or mixed Evidence addresses.claim_ids must exactly match "
            "the frozen Confirmation claims"
        )

    current_claims = _claims_object(paths).get("claims", [])
    current_by_id = {
        str(claim.get("claim_id")): claim
        for claim in current_claims
        if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
    }
    for frozen in frozen_claims:
        assert isinstance(frozen, dict)  # checked above
        claim_id = str(frozen["claim_id"])
        current = current_by_id.get(claim_id)
        if current is None:
            raise ValidationError(
                f"Confirmation references missing current Claim: {claim_id}"
            )
        current_digest = claim_spec_sha256(current)
        if current_digest != frozen.get("spec_sha256"):
            raise ValidationError(
                f"Claim statement or scope changed after Confirmation was frozen: {claim_id}"
            )


def _sealed_confirmation_manifest_digest(
    manifest: dict[str, Any],
    run_id: str,
) -> str:
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise ValidationError(f"confirmatory Run {run_id} has no integrity record")
    digest = integrity.get("manifest_sha256")
    if not isinstance(digest, str) or not digest:
        raise ValidationError(f"confirmatory Run {run_id} has no manifest_sha256")
    if digest != nested_record_digest(manifest, "integrity", "manifest_sha256"):
        raise ValidationError(f"confirmatory Run manifest integrity check failed: {run_id}")
    if not str(integrity.get("sealed_at") or "").strip():
        raise ValidationError(f"terminal confirmatory Run {run_id} has no sealed_at timestamp")
    return digest


def _ineligible_confirmation_run_reason(manifest: dict[str, Any]) -> str:
    status = str(manifest.get("status"))
    if status == "incomplete":
        return (
            "The sealed Run is incomplete and therefore Evidence-ineligible; "
            "the consumed Confirmation slot remains auditable."
        )
    return (
        "The sealed Run is Evidence-ineligible under its immutable change-scope, "
        "input, output, or formal-artifact integrity checks."
    )


def _derive_evidence_basis(
    paths: StudyPaths,
    claim_ids: Sequence[str],
    manifests: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    exploratory_run_ids: list[str] = []
    selected_confirmatory: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    for manifest in manifests:
        run_id = str(manifest.get("run_id"))
        if effective_run_mode(manifest) == "exploratory":
            exploratory_run_ids.append(run_id)
            continue
        binding = confirmation_binding(manifest)
        if binding is None:
            raise ValidationError(
                f"confirmatory Run has no complete immutable Confirmation binding: {run_id}"
            )
        selected_confirmatory.append((run_id, manifest, binding))

    if not selected_confirmatory:
        return _exploratory_evidence_basis(exploratory_run_ids), None

    registrations = {
        (binding["confirmation_id"], binding["confirmation_sha256"])
        for _, _, binding in selected_confirmatory
    }
    if len(registrations) != 1:
        raise ValidationError(
            "Evidence cannot combine confirmatory Runs from multiple Confirmation registrations"
        )
    confirmation_id, confirmation_sha256 = next(iter(registrations))
    confirmation = _confirmation_record(paths, confirmation_id, confirmation_sha256)
    _confirmation_claim_issues(paths, claim_ids, confirmation)

    raw_slots = confirmation.get("run_slots")
    if not isinstance(raw_slots, list):
        raise ValidationError("Confirmation run_slots must be a list")
    planned_slot_ids = [
        str(slot.get("slot_id"))
        for slot in raw_slots
        if isinstance(slot, dict) and isinstance(slot.get("slot_id"), str)
    ]
    if len(planned_slot_ids) != len(raw_slots) or len(planned_slot_ids) != len(
        set(planned_slot_ids)
    ):
        raise ValidationError("Confirmation run_slots must have unique string slot_id values")
    planned_slots = set(planned_slot_ids)

    runs = run_index(paths)
    require_consistent_ledger(paths, runs)
    attempts_by_slot: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    terminals_by_slot: dict[str, tuple[str, dict[str, Any]]] = {}
    for run_id, (_, manifest) in sorted(runs.items()):
        binding = confirmation_binding(manifest)
        if binding is None or binding["confirmation_id"] != confirmation_id:
            continue
        if binding["confirmation_sha256"] != confirmation_sha256:
            raise ValidationError(
                f"Runs bound to {confirmation_id} do not share one immutable registration hash"
            )
        from .confirmation import validate_confirmation_run

        validate_confirmation_run(paths, confirmation, manifest)
        slot_id = binding["slot_id"]
        if slot_id not in planned_slots:
            raise ValidationError(
                f"confirmatory Run {run_id} uses an unplanned Confirmation slot: {slot_id}"
            )
        attempts_by_slot.setdefault(slot_id, []).append((run_id, manifest))
        if manifest.get("status") not in _CONFIRMATION_TERMINAL_RUN_STATUSES:
            continue
        if slot_id in terminals_by_slot:
            other_run_id, _ = terminals_by_slot[slot_id]
            raise ValidationError(
                f"Confirmation slot {slot_id} has multiple terminal Runs: "
                f"{other_run_id}, {run_id}"
            )
        terminals_by_slot[slot_id] = (run_id, manifest)

    duplicate_attempt = next(
        (
            (slot_id, [run_id for run_id, _ in attempts])
            for slot_id, attempts in sorted(attempts_by_slot.items())
            if len(attempts) > 1
        ),
        None,
    )
    if duplicate_attempt is not None:
        slot_id, run_ids = duplicate_attempt
        raise ValidationError(
            f"Confirmation slot {slot_id} is consumed by multiple Runs: "
            + ", ".join(run_ids)
        )

    selected_ids = {run_id for run_id, _, _ in selected_confirmatory}
    selected_slots = {
        binding["slot_id"] for _, _, binding in selected_confirmatory
    }
    eligible_terminal_ids: set[str] = set()
    excluded_by_slot: dict[str, dict[str, Any]] = {}
    for slot_id, (run_id, manifest) in terminals_by_slot.items():
        manifest_sha256 = _sealed_confirmation_manifest_digest(manifest, run_id)
        expected_eligibility = sealed_run_evidence_eligible(manifest)
        recorded_eligibility = manifest.get("change_scope", {}).get("evidence_eligible")
        if recorded_eligibility is not expected_eligibility:
            raise ValidationError(
                f"confirmatory Run sealed Evidence eligibility is inconsistent: {run_id}"
            )
        if expected_eligibility:
            # Reuse the full ordinary Evidence admission checks for every Run
            # that is scientifically included.  A retained dependency can
            # become unavailable after sealing; that consumes the slot but is
            # now an explicit exclusion rather than a silently omitted attempt.
            try:
                _terminal_run(paths, run_id)
            except ValidationError as exc:
                detail = " ".join(str(exc).split())
                excluded_by_slot[slot_id] = {
                    "run_id": run_id,
                    "manifest_sha256": manifest_sha256,
                    "slot_id": slot_id,
                    "reason": (
                        "The sealed Run cannot enter Evidence under current "
                        f"integrity checks: {detail}"
                    )[:2048],
                }
            else:
                eligible_terminal_ids.add(run_id)
        if not expected_eligibility:
            excluded_by_slot[slot_id] = {
                "run_id": run_id,
                "manifest_sha256": manifest_sha256,
                "slot_id": slot_id,
                "reason": _ineligible_confirmation_run_reason(manifest),
            }

    omitted = sorted(eligible_terminal_ids - selected_ids)
    if omitted:
        raise ValidationError(
            "confirmatory Evidence must include every Evidence-eligible terminal Run; "
            "omitted: " + ", ".join(omitted)
        )
    unexpected = sorted(selected_ids - eligible_terminal_ids)
    if unexpected:
        raise ValidationError(
            "confirmatory Evidence includes Runs that are not eligible terminal "
            "Confirmation attempts: " + ", ".join(unexpected)
        )

    held_out = confirmation.get("held_out")
    if not isinstance(held_out, dict):
        raise ValidationError("Confirmation held_out must be an object")
    held_out_summary = {
        "status": held_out.get("status"),
        "freshness": held_out.get("freshness"),
        "workflow_observed_prior_run_count": held_out.get(
            "workflow_observed_prior_run_count"
        ),
    }
    missing_slot_ids = [
        slot_id for slot_id in planned_slot_ids if slot_id not in terminals_by_slot
    ]
    included_slot_ids = [
        slot_id for slot_id in planned_slot_ids if slot_id in selected_slots
    ]
    excluded_confirmatory_runs = [
        excluded_by_slot[slot_id]
        for slot_id in planned_slot_ids
        if slot_id in excluded_by_slot
    ]
    confirmatory_run_ids = [
        run_id for run_id, _, _ in selected_confirmatory
    ]
    mode = "mixed" if exploratory_run_ids else "confirmatory"
    return (
        {
            "mode": mode,
            "exploratory_run_ids": exploratory_run_ids,
            "confirmatory_run_ids": confirmatory_run_ids,
            "confirmation": {
                "confirmation_id": confirmation_id,
                "sha256": confirmation_sha256,
            },
            "planned_slot_ids": planned_slot_ids,
            "included_slot_ids": included_slot_ids,
            "missing_slot_ids": missing_slot_ids,
            "excluded_confirmatory_runs": excluded_confirmatory_runs,
            "held_out": held_out_summary,
        },
        confirmation,
    )


def _run_references_for_draft(
    paths: StudyPaths, run_ids: Sequence[str]
) -> tuple[list[dict[str, str]], list[str], list[str], list[dict[str, Any]]]:
    normalized = _normalize_ids("run", run_ids)
    require_consistent_ledger(paths, run_index(paths))
    manifests = [_terminal_run(paths, run_id) for run_id in normalized]
    references = [
        {
            "run_id": run_id,
            "manifest_sha256": str(manifest["integrity"]["manifest_sha256"]),
            "role": "context",
        }
        for run_id, manifest in zip(normalized, manifests, strict=True)
    ]
    fingerprints = sorted(
        {str(manifest["cohort"]["fingerprint_sha256"]) for manifest in manifests}
    )
    changed_fields = _changed_cohort_fields(manifests) if len(fingerprints) > 1 else []
    return references, fingerprints, changed_fields, manifests


@contextmanager
def _evidence_lock(paths: StudyPaths, evidence_id: str) -> Iterator[None]:
    paths.evidence.mkdir(parents=True, exist_ok=True)
    lock_path = paths.evidence / f".{evidence_id}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkflowError(f"another operation is active for Evidence {evidence_id}") from exc
    os.close(lock_fd)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _versions(paths: StudyPaths, evidence_id: str) -> list[tuple[int, Path, dict[str, Any]]]:
    pattern = re.compile(rf"^{re.escape(evidence_id)}\.v([0-9]{{4,}})\.json$")
    versions: list[tuple[int, Path, dict[str, Any]]] = []
    for path in sorted(paths.evidence.glob(f"{evidence_id}.v*.json")):
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ValidationError(f"malformed Evidence version filename: {path.name}")
        version = int(match.group(1))
        if version < 1 or path.name != f"{evidence_id}.v{version:04d}.json":
            raise ValidationError(f"non-canonical Evidence version filename: {path.name}")
        item = load_json(path)
        if not isinstance(item, dict):
            raise ValidationError(f"Evidence must be an object: {path}")
        if item.get("study_id") != paths.study_id:
            raise ValidationError(f"Evidence belongs to a different Study: {path}")
        if item.get("evidence_id") != evidence_id or item.get("version") != version:
            raise ValidationError(f"Evidence identity/version does not match filename: {path}")
        if item.get("status") not in {"draft", "finalized"}:
            raise ValidationError(f"Evidence has invalid status: {path}")
        versions.append((version, path, item))
    return versions


@serialized_study_authority
def migrate_evidence_sequence(paths: StudyPaths) -> Path:
    """Explicitly establish a counter for one intact legacy Evidence history."""

    if load_evidence_sequence(paths) is not None:
        raise ValidationError("Evidence sequence already exists")
    sequence_temps = evidence_sequence_temporary_paths(paths)
    if sequence_temps:
        raise ValidationError(
            "legacy Evidence migration refuses unfinished sequence temporary state: "
            + ", ".join(path.name for path in sequence_temps)
        )

    canonical = re.compile(r"^(EVID-[0-9]{4,})\.v([0-9]{4,})\.json$")
    versions_by_id: dict[str, list[tuple[int, str]]] = {}
    for path in evidence_paths(paths):
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"legacy Evidence must be a regular, non-symbolic-link file: {path}"
            )
        match = canonical.fullmatch(path.name)
        if match is None:
            raise ValidationError(
                f"legacy Evidence history contains a non-canonical filename: {path.name}"
            )
        evidence_id = match.group(1)
        version = int(match.group(2))
        require_id("evidence", evidence_id)
        if version < 1 or path.name != f"{evidence_id}.v{version:04d}.json":
            raise ValidationError(
                f"legacy Evidence history contains a non-canonical filename: {path.name}"
            )
        item = load_json(path)
        if not isinstance(item, dict):
            raise ValidationError(f"legacy Evidence must be an object: {path}")
        schema_issues = errors_only(
            object_schema_issues(paths.root, "evidence", path, item)
        )
        if schema_issues:
            raise ValidationError(
                _invalid_object_message(f"legacy Evidence {path.name}", schema_issues)
            )
        if item.get("study_id") != paths.study_id:
            raise ValidationError(f"legacy Evidence belongs to another Study: {path}")
        if item.get("evidence_id") != evidence_id or item.get("version") != version:
            raise ValidationError(
                f"legacy Evidence identity/version does not match filename: {path}"
            )
        status = item.get("status")
        if status == "finalized":
            if item.get("record_sha256") != record_digest(item, "record_sha256"):
                raise ValidationError(f"legacy Evidence digest is invalid: {path}")
        elif status == "draft":
            if item.get("record_sha256") is not None:
                raise ValidationError(f"legacy draft Evidence is already sealed: {path}")
        else:  # schema validation normally reports this first
            raise ValidationError(f"legacy Evidence has invalid status: {path}")
        versions_by_id.setdefault(evidence_id, []).append((version, str(status)))

    for evidence_id, version_records in sorted(versions_by_id.items()):
        numbers = sorted(number for number, _ in version_records)
        expected = list(range(1, numbers[-1] + 1))
        if numbers != expected:
            raise ValidationError(
                f"legacy Evidence history is not intact: {evidence_id} has a version gap"
            )
        drafts = [number for number, status in version_records if status == "draft"]
        if len(drafts) > 1 or (drafts and drafts[0] != numbers[-1]):
            raise ValidationError(
                f"legacy Evidence history is not intact: {evidence_id} has an invalid draft sequence"
            )

    if paths.evidence.is_dir():
        ambiguous = sorted(
            path
            for path in paths.evidence.iterdir()
            if path.name.startswith(".EVID-")
            and (path.name.endswith(".tmp") or path.name.endswith(".lock"))
        )
        if ambiguous:
            raise ValidationError(
                "legacy Evidence migration refuses unfinished lock/temporary state: "
                + ", ".join(path.name for path in ambiguous)
            )

    checkpoint_high_water_mark = 0
    for checkpoint_path in checkpoint_paths(paths):
        if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
            raise ValidationError(
                f"legacy Checkpoint must be a regular, non-symbolic-link file: {checkpoint_path}"
            )
        checkpoint = load_json(checkpoint_path)
        if not isinstance(checkpoint, dict):
            raise ValidationError(f"legacy Checkpoint must be an object: {checkpoint_path}")
        if checkpoint.get("study_id") != paths.study_id:
            raise ValidationError(
                f"legacy Checkpoint belongs to another Study: {checkpoint_path}"
            )
        watermarks = checkpoint.get("active_context_watermarks")
        if watermarks is None:
            continue
        if not isinstance(watermarks, dict):
            raise ValidationError(
                f"legacy Checkpoint has invalid active-context watermarks: {checkpoint_path}"
            )
        raw_watermark = watermarks.get("evidence_record_count")
        if (
            isinstance(raw_watermark, bool)
            or not isinstance(raw_watermark, int)
            or raw_watermark < 0
        ):
            raise ValidationError(
                f"legacy Checkpoint has an invalid Evidence watermark: {checkpoint_path}"
            )
        checkpoint_high_water_mark = max(
            checkpoint_high_water_mark, raw_watermark
        )

    migrate_legacy_evidence_sequence(
        paths,
        visible_record_count=sum(len(items) for items in versions_by_id.values()),
        checkpoint_high_water_mark=checkpoint_high_water_mark,
    )
    return paths.evidence_sequence


@serialized_study_authority
def create_evidence_draft(
    paths: StudyPaths,
    evidence_id: str,
    claim_ids: Sequence[str],
    run_ids: Sequence[str],
) -> Path:
    """Create the next editable version of an Evidence record."""
    require_id("evidence", evidence_id)
    normalized_claim_ids = _normalize_ids("claim", claim_ids)
    _validate_claim_references(paths, normalized_claim_ids)
    run_refs, fingerprints, changed_fields, manifests = _run_references_for_draft(
        paths, run_ids
    )
    evidence_basis, confirmation = _derive_evidence_basis(
        paths, normalized_claim_ids, manifests
    )
    frozen_analysis: dict[str, Any] = {"method": None}
    if confirmation is not None:
        analysis_plan = confirmation.get("analysis_plan")
        if not isinstance(analysis_plan, dict):
            raise ValidationError("Confirmation analysis_plan must be an object")
        frozen_analysis = {
            key: copy.deepcopy(analysis_plan.get(key))
            for key in (
                "method",
                "primary_outcomes",
                "decision_rule",
                "stopping_rule",
                "exclusion_rule",
            )
        }
    with _evidence_lock(paths, evidence_id):
        versions = _versions(paths, evidence_id)
        drafts = [path for _, path, item in versions if item.get("status") == "draft"]
        if drafts:
            raise WorkflowError(
                f"Evidence {evidence_id} already has an open draft: {drafts[0]}"
            )
        version = max((number for number, _, _ in versions), default=0) + 1
        timestamp = utc_now()
        draft: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "study_id": paths.study_id,
            "evidence_id": evidence_id,
            "version": version,
            "status": "draft",
            "created_at": timestamp,
            "updated_at": timestamp,
            "addresses": {
                "claim_ids": normalized_claim_ids,
                "question": None,
            },
            "evidence_basis": evidence_basis,
            "runs": run_refs,
            "analysis": {
                **frozen_analysis,
                "comparison": {
                    "mode": "single_cohort" if len(fingerprints) == 1 else "compatible_cohorts",
                    "cohort_fingerprints": fingerprints,
                    "changed_fields": changed_fields,
                    "compatibility_justification": None,
                },
            },
            "result": None,
            "scope": None,
            "uncertainty": None,
            "limitations": [],
            "assessment": None,
            "related_evidence": {
                "supporting": [],
                "contradictory": [],
            },
            "record_sha256": None,
        }
        destination = paths.evidence / f"{evidence_id}.v{version:04d}.json"
        schema_issues = object_schema_issues(paths.root, "evidence", destination, draft)
        if schema_issues:
            raise ValidationError(_invalid_object_message("generated Evidence draft", schema_issues))
        require_growth_allowed(paths, "new Evidence")
        reserve_evidence_creation(paths)
        atomic_write_json(destination, draft, overwrite=False)
        return destination


def _validate_run_references(
    paths: StudyPaths, item: dict[str, Any]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    require_consistent_ledger(paths, run_index(paths))
    seen: set[str] = set()
    fingerprints: set[str] = set()
    manifests: list[dict[str, Any]] = []
    for run_ref in item.get("runs", []):
        run_id = str(run_ref.get("run_id"))
        if run_id in seen:
            raise ValidationError(f"Evidence repeats Run reference: {run_id}")
        seen.add(run_id)
        manifest = _terminal_run(paths, run_id)
        recorded_digest = manifest["integrity"]["manifest_sha256"]
        if run_ref.get("manifest_sha256") != recorded_digest:
            raise ValidationError(f"Evidence has a stale Run manifest reference: {run_id}")
        if any(record.get("changed_during_run") for record in manifest.get("inputs", [])):
            raise ValidationError(
                f"finalized Evidence cannot use a Run whose input changed: {run_id}"
            )
        if manifest.get("code_state", {}).get("changed_during_run"):
            raise ValidationError(
                f"finalized Evidence cannot use a Run whose tracked code changed: {run_id}"
            )
        if not manifest.get("change_scope", {}).get("evidence_eligible", False):
            raise ValidationError(
                f"finalized Evidence cannot use a Run with unverifiable or blocked change scope: {run_id}"
            )
        manifests.append(manifest)
        fingerprints.add(str(manifest["cohort"]["fingerprint_sha256"]))
    declared = item.get("analysis", {}).get("comparison", {}).get(
        "cohort_fingerprints", []
    )
    if len(declared) != len(set(declared)) or set(declared) != fingerprints:
        raise ValidationError("declared Cohort fingerprints do not exactly match referenced Runs")
    changed_fields = _changed_cohort_fields(manifests) if len(fingerprints) > 1 else []
    return sorted(fingerprints), changed_fields, manifests


def _validate_related_evidence(paths: StudyPaths, item: dict[str, Any]) -> None:
    current_key = (str(item["evidence_id"]), int(item["version"]))
    seen: dict[tuple[str, int], str] = {}
    related = item.get("related_evidence", {})
    for role in ("supporting", "contradictory"):
        for reference in related.get(role, []):
            evidence_id = str(reference.get("evidence_id"))
            version = int(reference.get("version", 0))
            key = (evidence_id, version)
            if key == current_key:
                raise ValidationError("Evidence cannot reference itself as related Evidence")
            previous_role = seen.setdefault(key, role)
            if previous_role != role:
                raise ValidationError(
                    f"related Evidence {evidence_id} v{version} appears in conflicting roles"
                )
            elif sum(
                1
                for candidate in related.get(role, [])
                if (
                    str(candidate.get("evidence_id")),
                    int(candidate.get("version", 0)),
                )
                == key
            ) > 1:
                raise ValidationError(
                    f"related Evidence reference is duplicated: {evidence_id} v{version}"
                )
            target = paths.evidence / f"{evidence_id}.v{version:04d}.json"
            existing = load_json(target)
            if not isinstance(existing, dict):
                raise ValidationError(f"related Evidence must be an object: {target}")
            if existing.get("study_id") != paths.study_id:
                raise ValidationError(f"related Evidence belongs to a different Study: {target}")
            if existing.get("evidence_id") != evidence_id or existing.get("version") != version:
                raise ValidationError(f"related Evidence identity does not match filename: {target}")
            if existing.get("status") != "finalized":
                raise ValidationError(f"related Evidence is not finalized: {target}")
            digest = existing.get("record_sha256")
            if digest != record_digest(existing, "record_sha256"):
                raise ValidationError(f"related Evidence integrity check failed: {target}")
            if reference.get("sha256") != digest:
                raise ValidationError(f"related Evidence reference is stale: {target}")


def _require_nonblank(name: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"finalized Evidence requires explicit {name}")


def _require_evidence_formalization(
    paths: StudyPaths,
    item: dict[str, Any] | None = None,
) -> None:
    changed_paths: list[str] = []
    scientific_critical = False
    shared_across_runs = False
    if item is not None:
        for run_ref in item.get("runs", []):
            manifest = _terminal_run(paths, str(run_ref.get("run_id")))
            context = manifest.get("formalization", {})
            for path in context.get("changed_paths", []):
                if path not in changed_paths:
                    changed_paths.append(path)
            scientific_critical = scientific_critical or bool(
                context.get("scientific_critical")
            )
            shared_across_runs = shared_across_runs or bool(
                context.get("shared_across_runs")
            )
    formalization = check_formalization(
        paths,
        {
            "for_evidence": True,
            "changed_path": changed_paths,
            "scientific_critical": scientific_critical,
            "shared_across_runs": shared_across_runs,
        },
    )
    if formalization.blocked:
        details = "\n".join(
            f"- {requirement['level']}: {requirement['artifact']}: {requirement['reason']}"
            for requirement in formalization.requirements
        )
        raise ValidationError(f"formalization gate blocked Evidence finalization:\n{details}")


def _validate_final_content(
    item: dict[str, Any],
    fingerprints: Sequence[str],
    expected_changed_fields: Sequence[str],
) -> None:
    analysis = item.get("analysis", {})
    _require_nonblank("analysis.method", analysis.get("method"))
    _require_nonblank("result", item.get("result"))
    _require_nonblank("scope", item.get("scope"))
    _require_nonblank("uncertainty", item.get("uncertainty"))
    _require_nonblank("assessment", item.get("assessment"))
    comparison = analysis.get("comparison", {})
    if len(fingerprints) > 1:
        if comparison.get("mode") != "compatible_cohorts":
            raise ValidationError(
                "multi-Cohort Evidence requires comparison.mode=compatible_cohorts"
            )
        changed_fields = comparison.get("changed_fields")
        if (
            not isinstance(changed_fields, list)
            or not changed_fields
            or any(not isinstance(field, str) or not field.strip() for field in changed_fields)
            or len(changed_fields) != len(set(changed_fields))
        ):
            raise ValidationError(
                "multi-Cohort Evidence requires explicit, unique changed_fields"
            )
        if changed_fields != list(expected_changed_fields):
            raise ValidationError(
                "analysis.comparison.changed_fields does not exactly match the Run Cohorts"
            )
        _require_nonblank(
            "analysis.comparison.compatibility_justification",
            comparison.get("compatibility_justification"),
        )
    else:
        if comparison.get("mode") != "single_cohort":
            raise ValidationError("single-Cohort Evidence requires comparison.mode=single_cohort")
        if comparison.get("changed_fields") != []:
            raise ValidationError("single-Cohort Evidence must have no changed_fields")


def _validate_final_evidence_basis(
    paths: StudyPaths,
    item: dict[str, Any],
    manifests: Sequence[dict[str, Any]],
) -> None:
    actual = item.get("evidence_basis")
    if not isinstance(actual, dict):
        raise ValidationError("Evidence draft requires an explicit evidence_basis object")
    expected, confirmation = _derive_evidence_basis(
        paths,
        item.get("addresses", {}).get("claim_ids", []),
        manifests,
    )
    if actual != expected:
        changed = sorted(
            key
            for key in set(actual) | set(expected)
            if actual.get(key) != expected.get(key)
        )
        raise ValidationError(
            "Evidence evidence_basis does not match the deterministic Run and "
            "Confirmation audit; differing fields: "
            + ", ".join(changed)
        )
    mode = str(expected["mode"])
    missing_slots = expected["missing_slot_ids"]
    if mode in {"confirmatory", "mixed"} and missing_slots:
        raise ValidationError(
            f"{mode} Evidence cannot finalize with missing Confirmation slots: "
            + ", ".join(missing_slots)
        )
    if confirmation is not None:
        analysis_plan = confirmation.get("analysis_plan")
        if not isinstance(analysis_plan, dict):
            raise ValidationError("Confirmation analysis_plan must be an object")
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            raise ValidationError("Evidence analysis must be an object")
        frozen_fields = (
            "method",
            "primary_outcomes",
            "decision_rule",
            "stopping_rule",
            "exclusion_rule",
        )
        changed_fields = [
            field
            for field in frozen_fields
            if analysis.get(field) != analysis_plan.get(field)
        ]
        if changed_fields:
            raise ValidationError(
                "confirmatory or mixed Evidence analysis fields must exactly match "
                "the frozen Confirmation analysis_plan: "
                + ", ".join(changed_fields)
            )


def validate_evidence_basis(paths: StudyPaths, item: dict[str, Any]) -> None:
    """Revalidate the epistemic projection of a draft or finalized Evidence.

    This is intentionally callable through a local import from full-Study
    validation, avoiding a module-level ``validation``/``evidence`` cycle.
    Historical finalized records without ``evidence_basis`` remain valid but
    have exploratory effective mode via :func:`effective_evidence_mode`.
    """

    if "evidence_basis" not in item:
        if item.get("status") == "finalized":
            return
        raise ValidationError("Evidence draft requires an explicit evidence_basis object")
    runs = item.get("runs")
    if not isinstance(runs, list):
        raise ValidationError("Evidence runs must be a list")
    manifests: list[dict[str, Any]] = []
    for run_ref in runs:
        if not isinstance(run_ref, dict):
            raise ValidationError("Evidence Run reference must be an object")
        run_id = run_ref.get("run_id")
        if not isinstance(run_id, str):
            raise ValidationError("Evidence Run reference requires a string run_id")
        manifests.append(_terminal_run(paths, run_id))
    _validate_final_evidence_basis(paths, item, manifests)


def finalize_evidence(paths: StudyPaths, source_path: Path) -> Path:
    """Validate an edited draft and atomically replace it with an immutable record."""
    brief_issues = errors_only(brief_content_issues(paths) + brief_approval_issues(paths))
    if brief_issues:
        details = "\n".join(issue.render() for issue in brief_issues)
        raise ValidationError(
            f"a fresh approved Brief is required before Evidence finalization:\n{details}"
        )
    _require_evidence_formalization(paths)
    source = source_path.resolve()
    item = load_json(source)
    if not isinstance(item, dict):
        raise ValidationError("Evidence source must be a JSON object")
    schema_issues = object_schema_issues(paths.root, "evidence", source, item)
    if schema_issues:
        raise ValidationError(_invalid_object_message("Evidence source", schema_issues))
    if item.get("study_id") != paths.study_id:
        raise ValidationError("Evidence source study_id does not match Study")
    evidence_id = str(item.get("evidence_id"))
    require_id("evidence", evidence_id)
    version = item.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError("Evidence version must be a positive integer")
    if item.get("status") != "draft":
        raise ValidationError("evidence-finalize accepts only a draft Evidence source")
    if item.get("record_sha256") is not None:
        raise ValidationError("draft Evidence record_sha256 must be null")
    _require_evidence_formalization(paths, item)

    destination = paths.evidence / f"{evidence_id}.v{version:04d}.json"
    with _evidence_lock(paths, evidence_id):
        versions = _versions(paths, evidence_id)
        drafts = [(number, path, value) for number, path, value in versions if value["status"] == "draft"]
        if len(drafts) > 1:
            raise ValidationError(f"Evidence {evidence_id} has more than one open draft")
        current = next(
            (value for number, path, value in versions if number == version and path == destination),
            None,
        )
        if current is None:
            raise ValidationError(f"no authoritative draft exists at {destination}")
        if current.get("status") == "finalized":
            raise WorkflowError(f"refusing to overwrite finalized Evidence: {destination}")
        if current.get("status") != "draft":
            raise ValidationError(f"authoritative Evidence is not a draft: {destination}")
        if item.get("created_at") != current.get("created_at"):
            raise ValidationError("Evidence source does not match the authoritative draft")

        claim_ids = item.get("addresses", {}).get("claim_ids", [])
        _validate_claim_references(paths, claim_ids)
        fingerprints, changed_fields, manifests = _validate_run_references(paths, item)
        _validate_related_evidence(paths, item)
        _validate_final_content(item, fingerprints, changed_fields)
        _validate_final_evidence_basis(paths, item, manifests)

        finalized = dict(item)
        finalized["status"] = "finalized"
        finalized["updated_at"] = utc_now()
        finalized["record_sha256"] = record_digest(finalized, "record_sha256")
        final_schema_issues = object_schema_issues(
            paths.root, "evidence", destination, finalized
        )
        if final_schema_issues:
            raise ValidationError(
                _invalid_object_message("finalized Evidence", final_schema_issues)
            )

        initial_digest = sha256_file(destination)

        def _ensure_draft_unchanged(_temporary_path: Path) -> None:
            latest = load_json(destination)
            if not isinstance(latest, dict) or latest.get("status") != "draft":
                raise WorkflowError(
                    f"refusing to overwrite non-draft Evidence: {destination}"
                )
            if sha256_file(destination) != initial_digest:
                raise WorkflowError(f"Evidence draft changed during finalization: {destination}")

        atomic_write_json(
            destination,
            finalized,
            overwrite=True,
            mode=0o444,
            before_replace=_ensure_draft_unchanged,
        )
        return destination
