from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from .formalization import load_policy
from .hashing import (
    atomic_write_json,
    file_record,
    load_json,
    record_digest,
    sha256_bytes,
    sha256_json,
)
from .models import (
    StudyPaths,
    ValidationError,
    ValidationIssue,
    WorkflowError,
    require_id,
    utc_now,
)
from .locking import serialized_study_authority


_SLOT_PATTERN = re.compile(r"^SLOT-[0-9]{3,}$")
_CANDIDATE_PATTERN = re.compile(r"^CAND-[0-9]{3,}$")
_ACTIVE_FORMAL_STATUSES = {"active", "finalized"}


def claim_spec_sha256(claim: dict[str, Any]) -> str:
    """Hash only the Claim statement and scope frozen by confirmation."""

    if not isinstance(claim, dict):
        raise ValidationError("Claim specification must be an object")
    statement = claim.get("statement")
    scope = claim.get("scope")
    if not isinstance(statement, str) or not statement.strip():
        raise ValidationError("Confirmation Claim statement must be non-empty")
    if scope is not None and not isinstance(scope, str):
        raise ValidationError("Confirmation Claim scope must be a string or null")
    return sha256_json({"statement": statement, "scope": scope})


def _load_claims(paths: StudyPaths) -> dict[str, dict[str, Any]]:
    value = load_json(paths.claims)
    if not isinstance(value, dict) or not isinstance(value.get("claims"), list):
        raise ValidationError("CLAIMS.json must contain a claims array")
    result: dict[str, dict[str, Any]] = {}
    for item in value["claims"]:
        if not isinstance(item, dict) or not isinstance(item.get("claim_id"), str):
            raise ValidationError("CLAIMS.json contains an invalid Claim")
        claim_id = str(item["claim_id"])
        require_id("claim", claim_id)
        if claim_id in result:
            raise ValidationError(f"duplicate Claim ID in CLAIMS.json: {claim_id}")
        result[claim_id] = item
    return result


def _claim_binding(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": str(claim["claim_id"]),
        "statement": str(claim["statement"]),
        "scope": claim.get("scope"),
        "spec_sha256": claim_spec_sha256(claim),
    }


def _safe_source_path(paths: StudyPaths, source: Path) -> Path:
    paths.assert_safe_layout()
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"Confirmation draft must be a regular file: {source}")
    resolved = source.resolve(strict=True)
    try:
        resolved.relative_to(paths.study.resolve())
    except ValueError as exc:
        raise ValidationError("Confirmation draft must stay inside its Study") from exc
    try:
        resolved.relative_to(paths.confirmations.resolve(strict=False))
    except ValueError:
        pass
    else:
        raise ValidationError("finalized Confirmation Records cannot be used as drafts")
    return resolved


def _user_file(paths: StudyPaths, raw: str, *, repository_only: bool) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValidationError("Confirmation file paths must be non-empty strings")
    path = Path(raw)
    if repository_only and (path.is_absolute() or ".." in path.parts):
        raise ValidationError("candidate paths must be safe repository-relative paths")
    candidate = path if path.is_absolute() else paths.root / path
    if candidate.is_symlink() or not candidate.is_file():
        raise ValidationError(f"Confirmation binding is not a regular file: {raw}")
    resolved = candidate.resolve(strict=True)
    if repository_only:
        try:
            resolved.relative_to(paths.root.resolve())
        except ValueError as exc:
            raise ValidationError("candidate paths must stay inside the repository") from exc
        current = paths.root.resolve()
        relative = candidate.absolute().relative_to(paths.root.absolute())
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValidationError(
                    f"candidate paths must not traverse symbolic links: {raw}"
                )
    return candidate


def _bind_paths(
    paths: StudyPaths,
    raw_paths: Sequence[str],
    *,
    repository_only: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not isinstance(raw_paths, list):
        raise ValidationError("Confirmation paths must be an array")
    normalized: list[str] = []
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_paths:
        candidate = _user_file(paths, raw, repository_only=repository_only)
        binding = file_record(candidate, paths.root)
        canonical = str(binding["path"])
        if canonical in seen:
            raise ValidationError(f"duplicate Confirmation path: {canonical}")
        seen.add(canonical)
        normalized.append(canonical)
        bindings.append(binding)
    return normalized, bindings


def _git(args: list[str], root: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def _candidate_code_state(paths: StudyPaths, candidate_paths: Sequence[str]) -> dict[str, Any]:
    probe = _git(["rev-parse", "--show-toplevel"], paths.root)
    if probe.returncode != 0:
        return {"available": False, "commit": None, "diff_sha256": None}
    commit = _git(["rev-parse", "HEAD"], paths.root)
    diff = _git(["diff", "--binary", "HEAD", "--", *candidate_paths], paths.root)
    if commit.returncode != 0 or diff.returncode != 0:
        return {"available": False, "commit": None, "diff_sha256": None}
    return {
        "available": True,
        "commit": commit.stdout.decode("utf-8", errors="replace").strip(),
        "diff_sha256": sha256_bytes(diff.stdout),
    }


def _formal_binding(paths: StudyPaths, filename: str) -> dict[str, Any]:
    path = paths.formal / filename
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"confirmatory work requires formal/{filename}")
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValidationError(f"formal/{filename} must be a JSON object")
    if value.get("status") not in _ACTIVE_FORMAL_STATUSES:
        raise ValidationError(
            f"formal/{filename} must have active or finalized status before confirmation"
        )
    return file_record(path, paths.root)


def _input_matches(binding: dict[str, Any], raw_input: Any) -> bool:
    if not isinstance(raw_input, dict):
        return False
    return (
        raw_input.get("path") == binding.get("path")
        or (
            isinstance(binding.get("sha256"), str)
            and raw_input.get("sha256_before") == binding.get("sha256")
        )
    )


def _prior_held_out_run_count(
    manifests: Mapping[str, tuple[Path, dict[str, Any]]],
    bindings: Sequence[dict[str, Any]],
    *,
    max_run_number: int | None = None,
) -> int:
    if not bindings:
        return 0
    count = 0
    for run_id, (_, manifest) in sorted(manifests.items()):
        match = re.fullmatch(r"RUN-([0-9]{6})", run_id)
        if match is None:
            raise ValidationError(f"invalid Run ID in authoritative index: {run_id}")
        if max_run_number is not None and int(match.group(1)) > max_run_number:
            continue
        inputs = manifest.get("inputs")
        if isinstance(inputs, list) and any(
            _input_matches(binding, raw_input)
            for binding in bindings
            for raw_input in inputs
        ):
            count += 1
    return count


def _authoritative_run_history(
    paths: StudyPaths,
) -> tuple[dict[str, tuple[Path, dict[str, Any]]], int]:
    from .run_ledger import require_consistent_ledger
    from .validation import run_index

    manifests = run_index(paths)
    ledger = require_consistent_ledger(paths, manifests)
    high_water = ledger.get("high_water_mark")
    if isinstance(high_water, bool) or not isinstance(high_water, int):
        raise ValidationError("Run ledger high_water_mark is invalid")
    return manifests, high_water


def _require_fresh_brief(paths: StudyPaths) -> None:
    from .validation import brief_approval_issues, brief_content_issues, errors_only

    issues = errors_only(brief_content_issues(paths) + brief_approval_issues(paths))
    if issues:
        details = "\n".join(issue.render() for issue in issues)
        raise ValidationError(
            "a fresh human-approved Brief is required before confirmation:\n" + details
        )


def _cohort_fields_for_slot(
    paths: StudyPaths,
    raw_fields: dict[str, Any],
    hardware_class: str,
    precision: str,
) -> dict[str, Any]:
    """Expand author-supplied custom fields to the Run's full fingerprint fields."""

    from .run_registry import _cohort_record, _load_protocol, _selected_runtime_environment

    encoded = [
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        for key, value in sorted(raw_fields.items())
    ]
    protocol_path, protocol = _load_protocol(paths)
    cohort, _, _ = _cohort_record(
        paths,
        load_policy(paths),
        protocol_path,
        protocol,
        None,
        hardware_class,
        precision,
        encoded,
        _selected_runtime_environment(),
    )
    return copy.deepcopy(cohort["fields"])


@serialized_study_authority
def create_confirmation_draft(
    paths: StudyPaths,
    confirmation_id: str,
    claim_ids: Sequence[str],
) -> Path:
    """Create one editable, non-authoritative pre-confirmatory-Run draft."""

    paths.assert_safe_layout()
    _require_fresh_brief(paths)
    require_id("confirmation", confirmation_id)
    if not isinstance(claim_ids, (list, tuple)) or not claim_ids:
        raise ValidationError("at least one Claim ID is required")
    selected_ids = [require_id("claim", str(value)) for value in claim_ids]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValidationError("Confirmation Claim IDs must be unique")
    current = _load_claims(paths)
    missing = [claim_id for claim_id in selected_ids if claim_id not in current]
    if missing:
        raise ValidationError(
            "Confirmation references missing current Claim(s): " + ", ".join(missing)
        )
    final_path = paths.confirmations / f"{confirmation_id}.json"
    if final_path.exists():
        raise WorkflowError(f"Confirmation Record already exists: {final_path}")
    draft_path = paths.active_work / f"{confirmation_id}.confirmation.draft.json"
    if draft_path.exists():
        raise WorkflowError(f"Confirmation draft already exists: {draft_path}")
    template = load_json(paths.root / "scientific-workflow" / "templates" / "CONFIRMATION.json")
    if not isinstance(template, dict):
        raise ValidationError("CONFIRMATION template must be a JSON object")
    draft = copy.deepcopy(template)
    draft.update(
        {
            "study_id": paths.study_id,
            "confirmation_id": confirmation_id,
            "status": "draft",
            "claims": [_claim_binding(current[claim_id]) for claim_id in selected_ids],
            "created_at": utc_now(),
        }
    )
    atomic_write_json(draft_path, draft, overwrite=False)
    return draft_path


def _require_nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Confirmation {label} must be non-empty")
    return value


def _finalize_claims(paths: StudyPaths, raw_claims: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValidationError("Confirmation requires at least one Claim")
    current = _load_claims(paths)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_claims:
        if not isinstance(raw, dict):
            raise ValidationError("Confirmation claims must be objects")
        claim_id = require_id("claim", str(raw.get("claim_id", "")))
        if claim_id in seen:
            raise ValidationError(f"duplicate Confirmation Claim: {claim_id}")
        seen.add(claim_id)
        claim = current.get(claim_id)
        if claim is None:
            raise ValidationError(f"Confirmation references missing current Claim: {claim_id}")
        expected = _claim_binding(claim)
        if raw != expected:
            raise ValidationError(
                f"Claim statement or scope changed after Confirmation draft creation: {claim_id}"
            )
        result.append(expected)
    return result


def _finalize_candidates(paths: StudyPaths, raw_candidates: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValidationError("Confirmation requires at least one candidate")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise ValidationError("Confirmation candidates must be objects")
        candidate_id = str(raw.get("candidate_id", ""))
        if _CANDIDATE_PATTERN.fullmatch(candidate_id) is None:
            raise ValidationError(f"invalid candidate ID: {candidate_id!r}")
        if candidate_id in seen:
            raise ValidationError(f"duplicate Confirmation candidate: {candidate_id}")
        seen.add(candidate_id)
        description = _require_nonempty_text(raw.get("description"), "candidate description")
        normalized, bindings = _bind_paths(
            paths,
            raw.get("paths"),
            repository_only=True,
        )
        if not normalized:
            raise ValidationError(f"candidate {candidate_id} requires at least one bound path")
        result.append(
            {
                "candidate_id": candidate_id,
                "description": description,
                "paths": normalized,
                "bindings": bindings,
                "code_state": _candidate_code_state(paths, normalized),
            }
        )
    return result


def _finalize_held_out(
    paths: StudyPaths,
    raw: Any,
    manifests: Mapping[str, tuple[Path, dict[str, Any]]],
    run_high_water_mark: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("Confirmation held_out must be an object")
    status = raw.get("status")
    if status not in {"held_out", "not_held_out", "not_applicable"}:
        raise ValidationError("Confirmation held_out.status is invalid")
    description = _require_nonempty_text(raw.get("description"), "held-out description")
    normalized, bindings = _bind_paths(paths, raw.get("paths"), repository_only=False)
    if status == "held_out" and not normalized:
        raise ValidationError("held_out status requires at least one bound path")
    if status == "not_applicable" and normalized:
        raise ValidationError("not_applicable held-out status cannot bind paths")
    count = _prior_held_out_run_count(
        manifests,
        bindings,
        max_run_number=run_high_water_mark,
    )
    if status == "not_applicable":
        freshness = "not_applicable"
        count = 0
    elif status == "held_out":
        freshness = "fresh" if count == 0 else "reused"
    else:
        freshness = "reused" if count else "unknown"
    return {
        "status": status,
        "description": description,
        "paths": normalized,
        "bindings": bindings,
        "workflow_observed_prior_run_count": count,
        "workflow_observed_run_high_water_mark": run_high_water_mark,
        "freshness": freshness,
    }


def _finalize_analysis_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("Confirmation analysis_plan must be an object")
    outcomes = raw.get("primary_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        raise ValidationError("Confirmation analysis plan requires primary outcomes")
    if any(not isinstance(item, str) or not item.strip() for item in outcomes):
        raise ValidationError("Confirmation primary outcomes must be non-empty strings")
    if len(outcomes) != len(set(outcomes)):
        raise ValidationError("Confirmation primary outcomes must be unique")
    return {
        "method": _require_nonempty_text(raw.get("method"), "analysis method"),
        "primary_outcomes": outcomes,
        "decision_rule": _require_nonempty_text(raw.get("decision_rule"), "decision rule"),
        "stopping_rule": _require_nonempty_text(raw.get("stopping_rule"), "stopping rule"),
        "exclusion_rule": _require_nonempty_text(raw.get("exclusion_rule"), "exclusion rule"),
    }


def _finalize_slots(
    paths: StudyPaths,
    raw_slots: Any,
    candidates: Sequence[dict[str, Any]],
    held_out: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw_slots, list) or not raw_slots:
        raise ValidationError("Confirmation requires at least one planned slot")
    candidate_ids = {str(item["candidate_id"]) for item in candidates}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_slots:
        if not isinstance(raw, dict):
            raise ValidationError("Confirmation run slots must be objects")
        slot_id = str(raw.get("slot_id", ""))
        if _SLOT_PATTERN.fullmatch(slot_id) is None:
            raise ValidationError(f"invalid Confirmation slot ID: {slot_id!r}")
        if slot_id in seen:
            raise ValidationError(f"duplicate Confirmation slot: {slot_id}")
        seen.add(slot_id)
        candidate_id = str(raw.get("candidate_id", ""))
        if candidate_id not in candidate_ids:
            raise ValidationError(f"Confirmation slot {slot_id} references unknown candidate")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValidationError(f"Confirmation slot {slot_id} argv must be non-empty strings")
        seed = raw.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, (int, str, type(None))):
            raise ValidationError(f"Confirmation slot {slot_id} seed is invalid")
        hardware = _require_nonempty_text(raw.get("hardware_class"), "slot hardware_class")
        precision = _require_nonempty_text(raw.get("precision"), "slot precision")
        custom_fields = raw.get("cohort_fields")
        if not isinstance(custom_fields, dict):
            raise ValidationError(f"Confirmation slot {slot_id} cohort_fields must be an object")
        input_paths, input_bindings = _bind_paths(
            paths,
            raw.get("input_paths"),
            repository_only=False,
        )
        if held_out["status"] == "held_out" and not set(held_out["paths"]).issubset(input_paths):
            raise ValidationError(
                f"Confirmation slot {slot_id} must declare every held-out path as an input"
            )
        result.append(
            {
                "slot_id": slot_id,
                "candidate_id": candidate_id,
                "argv": argv,
                "seed": seed,
                "hardware_class": hardware,
                "precision": precision,
                "cohort_fields": _cohort_fields_for_slot(
                    paths, custom_fields, hardware, precision
                ),
                "input_paths": input_paths,
                "input_bindings": input_bindings,
            }
        )
    return result


@serialized_study_authority
def finalize_confirmation(paths: StudyPaths, source: Path) -> Path:
    """Freeze an editable draft as one immutable pre-run Confirmation Record."""

    _require_fresh_brief(paths)
    draft_path = _safe_source_path(paths, source)
    draft = load_json(draft_path)
    if not isinstance(draft, dict):
        raise ValidationError("Confirmation draft must be a JSON object")
    if draft.get("status") != "draft" or draft.get("record_sha256") is not None:
        raise ValidationError("only an unsealed Confirmation draft can be finalized")
    if draft.get("study_id") != paths.study_id:
        raise ValidationError("Confirmation draft study_id does not match Study")
    confirmation_id = require_id("confirmation", str(draft.get("confirmation_id", "")))
    expected_name = f"{confirmation_id}.confirmation.draft.json"
    if draft_path.name != expected_name:
        raise ValidationError(f"Confirmation draft filename must be {expected_name}")
    created_at = _require_nonempty_text(draft.get("created_at"), "created_at")
    frozen_at = utc_now()
    claims = _finalize_claims(paths, draft.get("claims"))
    candidates = _finalize_candidates(paths, draft.get("candidates"))
    protocol = _formal_binding(paths, "PROTOCOL.json")
    evaluator = _formal_binding(paths, "EVALUATOR.json")
    manifests, run_high_water_mark = _authoritative_run_history(paths)
    held_out = _finalize_held_out(
        paths,
        draft.get("held_out"),
        manifests,
        run_high_water_mark,
    )
    analysis_plan = _finalize_analysis_plan(draft.get("analysis_plan"))
    run_slots = _finalize_slots(paths, draft.get("run_slots"), candidates, held_out)
    finalized: dict[str, Any] = {
        "schema_version": 1,
        "study_id": paths.study_id,
        "confirmation_id": confirmation_id,
        "status": "finalized",
        "claims": claims,
        "candidates": candidates,
        "formal_artifacts": {"protocol": protocol, "evaluator": evaluator},
        "held_out": held_out,
        "analysis_plan": analysis_plan,
        "run_slots": run_slots,
        "created_at": created_at,
        "frozen_at": frozen_at,
        "record_sha256": None,
    }
    finalized["record_sha256"] = record_digest(finalized, "record_sha256")
    target = paths.confirmations / f"{confirmation_id}.json"
    errors = _static_record_errors(paths, target, finalized)
    if errors:
        raise ValidationError(
            "Confirmation Record does not satisfy its frozen contract: "
            + "; ".join(errors)
        )
    atomic_write_json(
        target,
        finalized,
        overwrite=False,
        mode=0o444,
        require_parent_fsync=True,
    )
    return target


def _static_record_errors(paths: StudyPaths, path: Path, value: Any) -> list[str]:
    errors: list[str] = []
    from .validation import errors_only, object_schema_issues

    for issue in errors_only(object_schema_issues(paths.root, "confirmation", path, value)):
        errors.append(issue.message)
    if not isinstance(value, dict):
        return errors or ["Confirmation Record must be an object"]
    confirmation_id = value.get("confirmation_id")
    if value.get("study_id") != paths.study_id:
        errors.append("Confirmation study_id does not match Study directory")
    if not isinstance(confirmation_id, str) or path.name != f"{confirmation_id}.json":
        errors.append("Confirmation identity does not match filename")
    if value.get("status") != "finalized":
        errors.append("Confirmation Record is not finalized")
    frozen_at = value.get("frozen_at")
    if not isinstance(frozen_at, str) or not frozen_at.strip():
        errors.append("finalized Confirmation Record requires a non-empty frozen_at")
    if value.get("record_sha256") != record_digest(value, "record_sha256"):
        errors.append("Confirmation record_sha256 does not match record")
    if path.exists():
        metadata = path.stat()
        if metadata.st_mode & 0o222:
            errors.append("finalized Confirmation Record must be read-only")
        if metadata.st_nlink != 1:
            errors.append("finalized Confirmation Record must not have hard links")
    claim_ids = [
        item.get("claim_id") for item in value.get("claims", []) if isinstance(item, dict)
    ]
    if len(claim_ids) != len(set(claim_ids)):
        errors.append("Confirmation Claim IDs must be unique")
    candidate_ids = [
        item.get("candidate_id")
        for item in value.get("candidates", [])
        if isinstance(item, dict)
    ]
    if len(candidate_ids) != len(set(candidate_ids)):
        errors.append("Confirmation candidate IDs must be unique")
    slot_ids = [
        item.get("slot_id") for item in value.get("run_slots", []) if isinstance(item, dict)
    ]
    if len(slot_ids) != len(set(slot_ids)):
        errors.append("Confirmation slot IDs must be unique")
    formal = value.get("formal_artifacts")
    if not isinstance(formal, dict) or not all(
        isinstance(formal.get(name), dict) for name in ("protocol", "evaluator")
    ):
        errors.append("finalized Confirmation requires frozen PROTOCOL and EVALUATOR")
    for claim in value.get("claims", []):
        if isinstance(claim, dict):
            try:
                expected = claim_spec_sha256(claim)
            except ValidationError as exc:
                errors.append(str(exc))
            else:
                if claim.get("spec_sha256") != expected:
                    errors.append(
                        f"Confirmation Claim digest is invalid: {claim.get('claim_id')}"
                    )
    for candidate in value.get("candidates", []):
        if isinstance(candidate, dict) and (
            candidate.get("paths") != [
                binding.get("path")
                for binding in candidate.get("bindings", [])
                if isinstance(binding, dict)
            ]
        ):
            errors.append(
                f"Confirmation candidate path bindings are inconsistent: "
                f"{candidate.get('candidate_id')}"
            )
    for slot in value.get("run_slots", []):
        if isinstance(slot, dict) and (
            slot.get("input_paths") != [
                binding.get("path")
                for binding in slot.get("input_bindings", [])
                if isinstance(binding, dict)
            ]
        ):
            errors.append(
                f"Confirmation slot input bindings are inconsistent: {slot.get('slot_id')}"
            )
    return errors


def load_final_confirmation(paths: StudyPaths, confirmation_id: str) -> dict[str, Any]:
    require_id("confirmation", confirmation_id)
    path = paths.confirmations / f"{confirmation_id}.json"
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"finalized Confirmation Record does not exist: {confirmation_id}")
    value = load_json(path)
    errors = _static_record_errors(paths, path, value)
    if errors:
        raise ValidationError("invalid finalized Confirmation Record: " + "; ".join(errors))
    assert isinstance(value, dict)
    return value


def _require_binding_current(
    paths: StudyPaths,
    binding: Any,
    *,
    label: str,
    repository_only: bool = False,
) -> None:
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
        raise ValidationError(f"Confirmation {label} binding is invalid")
    _, current = _bind_paths(
        paths,
        [str(binding["path"])],
        repository_only=repository_only,
    )
    if current[0] != binding:
        raise ValidationError(f"Confirmation {label} binding hash or size changed")


def validate_confirmation_current(
    paths: StudyPaths,
    confirmation: dict[str, Any],
) -> None:
    """Fail closed when a frozen registration no longer matches current inputs."""

    confirmation_id = str(confirmation.get("confirmation_id", ""))
    loaded = load_final_confirmation(paths, confirmation_id)
    if loaded != confirmation:
        raise ValidationError(f"Confirmation object is stale: {confirmation_id}")
    current_claims = _load_claims(paths)
    for frozen in confirmation["claims"]:
        claim_id = str(frozen["claim_id"])
        current = current_claims.get(claim_id)
        if current is None or claim_spec_sha256(current) != frozen.get("spec_sha256"):
            raise ValidationError(
                f"Claim statement or scope changed after Confirmation was frozen: {claim_id}"
            )
    for candidate in confirmation["candidates"]:
        for binding in candidate["bindings"]:
            _require_binding_current(
                paths, binding, label="candidate", repository_only=True
            )
        expected_state = _candidate_code_state(paths, candidate["paths"])
        if candidate.get("code_state") != expected_state:
            raise ValidationError("Confirmation candidate code state changed")
    expected_formal = {
        "protocol": _formal_binding(paths, "PROTOCOL.json"),
        "evaluator": _formal_binding(paths, "EVALUATOR.json"),
    }
    if confirmation.get("formal_artifacts") != expected_formal:
        raise ValidationError("Confirmation PROTOCOL or EVALUATOR binding changed")
    held_out = confirmation["held_out"]
    for binding in held_out["bindings"]:
        _require_binding_current(paths, binding, label="held-out")
    high_water = held_out["workflow_observed_run_high_water_mark"]
    manifests, current_high_water = _authoritative_run_history(paths)
    if current_high_water < high_water:
        raise ValidationError("Run ledger rolled back below Confirmation high-water mark")
    expected_prior_count = _prior_held_out_run_count(
        manifests,
        held_out["bindings"],
        max_run_number=high_water,
    )
    if expected_prior_count != held_out["workflow_observed_prior_run_count"]:
        raise ValidationError("Confirmation held-out prior-use count is inconsistent")
    if held_out["status"] == "held_out":
        leaked_runs: list[str] = []
        for run_id, (_, manifest) in sorted(manifests.items()):
            match = re.fullmatch(r"RUN-([0-9]{6})", run_id)
            if match is None or int(match.group(1)) <= high_water:
                continue
            inputs = manifest.get("inputs")
            if not isinstance(inputs, list) or not any(
                _input_matches(binding, raw_input)
                for binding in held_out["bindings"]
                for raw_input in inputs
            ):
                continue
            role = manifest.get("epistemic_role")
            if not (
                manifest.get("schema_version") == 4
                and isinstance(role, dict)
                and role.get("mode") == "confirmatory"
                and role.get("confirmation_id") == confirmation_id
                and role.get("confirmation_sha256")
                == confirmation.get("record_sha256")
            ):
                leaked_runs.append(run_id)
        if leaked_runs:
            raise ValidationError(
                "held-out inputs were used after Confirmation freeze outside its "
                "registered Runs: " + ", ".join(leaked_runs)
            )
    for slot in confirmation["run_slots"]:
        for binding in slot.get("input_bindings", []):
            _require_binding_current(paths, binding, label="slot input")


def validate_confirmation_run(
    paths: StudyPaths,
    confirmation: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Validate an immutable Run against its pre-confirmatory frozen record.

    This deliberately does not consult the current working tree.  Historical
    confirmatory evidence remains meaningful after later development because
    the terminal Run carries immutable snapshots of the relevant protocol,
    evaluator, inputs, execution conditions, and Confirmation digest.
    """

    loaded = load_final_confirmation(paths, str(confirmation.get("confirmation_id", "")))
    if loaded != confirmation:
        raise ValidationError("Confirmation object does not match its immutable record")
    if manifest.get("schema_version") != 4:
        raise ValidationError("only Run schema V4 can represent confirmatory execution")
    role = manifest.get("epistemic_role")
    if not isinstance(role, dict) or role.get("mode") != "confirmatory":
        raise ValidationError("Run is not immutably marked confirmatory")
    if role.get("confirmation_id") != confirmation.get("confirmation_id") or role.get(
        "confirmation_sha256"
    ) != confirmation.get("record_sha256"):
        raise ValidationError("Run Confirmation binding does not match frozen record")
    run_id = manifest.get("run_id")
    match = re.fullmatch(r"RUN-([0-9]{6})", str(run_id))
    if match is None:
        raise ValidationError("confirmatory Run has an invalid Run ID")
    frozen_high_water = confirmation["held_out"][
        "workflow_observed_run_high_water_mark"
    ]
    if int(match.group(1)) <= frozen_high_water:
        raise ValidationError(
            f"confirmatory Run {run_id} predates its Confirmation Record"
        )
    slot_id = role.get("slot_id")
    slots = [slot for slot in confirmation["run_slots"] if slot.get("slot_id") == slot_id]
    if len(slots) != 1:
        raise ValidationError(f"Run references an unknown Confirmation slot: {slot_id}")
    slot = slots[0]
    execution = manifest.get("execution")
    environment = manifest.get("environment")
    cohort = manifest.get("cohort")
    if not isinstance(execution, dict) or not isinstance(environment, dict) or not isinstance(cohort, dict):
        raise ValidationError("confirmatory Run lacks execution, environment, or Cohort data")
    expected = {
        "argv": slot["argv"],
        "seed": slot["seed"],
        "hardware_class": slot["hardware_class"],
        "precision": slot["precision"],
        "cohort_fields": slot["cohort_fields"],
    }
    actual = {
        "argv": execution.get("argv"),
        "seed": execution.get("seed"),
        "hardware_class": environment.get("hardware_class"),
        "precision": environment.get("precision"),
        "cohort_fields": cohort.get("fields"),
    }
    differing = [key for key in expected if expected[key] != actual[key]]
    planned_inputs = slot.get("input_bindings", [])
    run_inputs = manifest.get("inputs")
    normalized_run_inputs = (
        [
            {
                "path": item.get("path"),
                "size": item.get("size"),
                "sha256": item.get("sha256_before"),
            }
            for item in run_inputs
            if isinstance(item, dict)
        ]
        if isinstance(run_inputs, list)
        else None
    )
    if normalized_run_inputs != planned_inputs:
        differing.append("input_bindings")
    formal_by_kind = {
        item.get("kind"): item
        for item in manifest.get("formal_artifacts", [])
        if isinstance(item, dict)
    }
    for name, kind in (("protocol", "PROTOCOL"), ("evaluator", "EVALUATOR")):
        frozen = confirmation["formal_artifacts"][name]
        captured = formal_by_kind.get(kind)
        if not isinstance(captured, dict) or any(
            captured.get(field) != frozen.get(field) for field in ("size", "sha256")
        ):
            differing.append(name)
    candidate = next(
        item
        for item in confirmation["candidates"]
        if item["candidate_id"] == slot["candidate_id"]
    )
    code_state = candidate.get("code_state")
    if isinstance(code_state, dict) and code_state.get("available"):
        if manifest.get("git", {}).get("commit") != code_state.get("commit"):
            differing.append("candidate_commit")
    if differing:
        raise ValidationError(
            "confirmatory Run differs from frozen Confirmation slot: "
            + ", ".join(dict.fromkeys(differing))
        )


def _slot_consumed(paths: StudyPaths, confirmation_id: str, slot_id: str) -> str | None:
    for manifest_path in sorted(paths.runs.glob("RUN-*/manifest.json")):
        try:
            manifest = load_json(manifest_path)
        except ValidationError:
            continue
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 4:
            continue
        role = manifest.get("epistemic_role")
        if (
            isinstance(role, dict)
            and role.get("mode") == "confirmatory"
            and role.get("confirmation_id") == confirmation_id
            and role.get("slot_id") == slot_id
        ):
            return str(manifest.get("run_id") or manifest_path.parent.name)
    return None


def validate_confirmation_slot(
    paths: StudyPaths,
    confirmation_id: str,
    slot_id: str,
    *,
    argv: Sequence[str],
    seed: int | str | None,
    hardware_class: str,
    precision: str,
    cohort_fields: dict[str, Any],
    input_paths: Sequence[str | os.PathLike[str]] | None,
) -> dict[str, str]:
    """Validate exact current execution conditions for one unused frozen slot."""

    require_id("confirmation", confirmation_id)
    if _SLOT_PATTERN.fullmatch(slot_id) is None:
        raise ValidationError(f"invalid confirmation slot identifier: {slot_id!r}")
    confirmation = load_final_confirmation(paths, confirmation_id)
    validate_confirmation_current(paths, confirmation)
    slots = [item for item in confirmation["run_slots"] if item.get("slot_id") == slot_id]
    if len(slots) != 1:
        raise ValidationError(f"Confirmation slot does not exist: {confirmation_id}/{slot_id}")
    slot = slots[0]
    input_raw = [str(value) for value in (input_paths or [])]
    actual_paths, actual_bindings = _bind_paths(paths, input_raw, repository_only=False)
    comparisons = {
        "argv": list(argv),
        "seed": seed,
        "hardware_class": hardware_class,
        "precision": precision,
        "cohort_fields": cohort_fields,
        "input_paths": actual_paths,
        "input_bindings": actual_bindings,
    }
    differing = [key for key, value in comparisons.items() if slot.get(key) != value]
    if differing:
        raise ValidationError(
            f"confirmatory Run does not match frozen slot {slot_id}: "
            + ", ".join(differing)
        )
    consumed_by = _slot_consumed(paths, confirmation_id, slot_id)
    if consumed_by is not None:
        raise ValidationError(
            f"confirmation slot {confirmation_id}/{slot_id} is already consumed by {consumed_by}"
        )
    return {
        "confirmation_id": confirmation_id,
        "confirmation_sha256": str(confirmation["record_sha256"]),
        "slot_id": slot_id,
        "candidate_id": str(slot["candidate_id"]),
    }


def confirmation_record_issues(paths: StudyPaths) -> list[ValidationIssue]:
    """Return static integrity issues for immutable Confirmation Records."""

    issues: list[ValidationIssue] = []
    if not paths.confirmations.exists():
        return issues
    if paths.confirmations.is_symlink() or not paths.confirmations.is_dir():
        return [
            ValidationIssue(
                "ERROR", str(paths.confirmations), "Confirmation directory is invalid"
            )
        ]
    for entry in sorted(paths.confirmations.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            issues.append(
                ValidationIssue(
                    "ERROR", str(entry), "Confirmation entry must be a regular JSON file"
                )
            )
            continue
        if re.fullmatch(r"CONF-[0-9]{4,}\.json", entry.name) is None:
            issues.append(
                ValidationIssue("ERROR", str(entry), "unexpected Confirmation filename")
            )
            continue
        try:
            value = load_json(entry)
            errors = _static_record_errors(paths, entry, value)
        except (OSError, ValidationError, WorkflowError) as exc:
            errors = [str(exc)]
        issues.extend(ValidationIssue("ERROR", str(entry), error) for error in errors)
    return issues


def confirmation_run_issues(
    paths: StudyPaths,
    runs: Mapping[str, tuple[Path, dict[str, Any]]],
) -> list[ValidationIssue]:
    """Replay every V4 confirmatory binding, including unreferenced attempts."""

    issues: list[ValidationIssue] = []
    consumed: dict[tuple[str, str], str] = {}
    loaded: dict[str, dict[str, Any]] = {}
    for run_id, (manifest_path, manifest) in sorted(runs.items()):
        if manifest.get("schema_version") != 4:
            continue
        role = manifest.get("epistemic_role")
        if not isinstance(role, dict) or role.get("mode") != "confirmatory":
            continue
        confirmation_id = role.get("confirmation_id")
        slot_id = role.get("slot_id")
        if not isinstance(confirmation_id, str) or not isinstance(slot_id, str):
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(manifest_path),
                    "confirmatory Run has an incomplete Confirmation binding",
                )
            )
            continue
        key = (confirmation_id, slot_id)
        previous = consumed.get(key)
        if previous is not None:
            issues.append(
                ValidationIssue(
                    "ERROR",
                    str(manifest_path),
                    f"Confirmation slot {confirmation_id}/{slot_id} is consumed "
                    f"by multiple Runs: {previous}, {run_id}",
                )
            )
        else:
            consumed[key] = run_id
        try:
            confirmation = loaded.get(confirmation_id)
            if confirmation is None:
                confirmation = load_final_confirmation(paths, confirmation_id)
                loaded[confirmation_id] = confirmation
            validate_confirmation_run(paths, confirmation, manifest)
        except (OSError, ValidationError, WorkflowError) as exc:
            issues.append(ValidationIssue("ERROR", str(manifest_path), str(exc)))
    return issues
