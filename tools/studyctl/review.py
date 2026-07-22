from __future__ import annotations

from pathlib import Path
from typing import Any

from .active_context import (
    active_claims,
    build_active_selector,
    require_growth_allowed,
    write_active_selector,
    write_compaction_due,
)
from .formalization import check_formalization, collect_formalization_debt
from .git_state import git_diff_metadata, git_state
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    load_json,
    record_digest,
    sha256_file,
    sha256_json,
)
from .models import SCHEMA_VERSION, StudyPaths, ValidationError
from .rendering import render_review_markdown
from .run_registry import confirmation_binding, effective_run_mode
from .validation import (
    brief_approval_issues,
    checkpoint_paths,
    evidence_index,
    object_schema_issues,
    protected_artifact_snapshot,
    run_index,
    validate_study,
)
from .workspace import evaluate_changes, load_repository_profile, profile_summary


def _evidence_key(ref: dict[str, Any]) -> tuple[str, int]:
    return str(ref.get("evidence_id")), int(ref.get("version", 0))


def _claim_refs(claims: dict[str, Any], field: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for claim in claims.get("claims", []):
        for ref in claim.get(field, []):
            key = _evidence_key(ref)
            if key not in seen:
                refs.append(ref)
                seen.add(key)
    return refs


MAX_REVIEW_EVIDENCE_PER_ROLE = 128
MAX_REVIEW_RUN_SOURCES = 512
MAX_REVIEW_CONFIRMATION_RECORDS = 128
MAX_REVIEW_CONFIRMATION_ATTEMPTS = 512


def _bounded_refs(
    claims: dict[str, Any], field: str
) -> tuple[list[dict[str, Any]], int]:
    refs = _claim_refs(claims, field)
    return refs[:MAX_REVIEW_EVIDENCE_PER_ROLE], len(refs)


def _manifests_for_evidence(
    refs: list[dict[str, Any]],
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    run_ids: list[str] = []
    for ref in refs:
        record = evidence.get(_evidence_key(ref))
        if not record:
            continue
        for run_ref in record[1].get("runs", []):
            run_id = str(run_ref.get("run_id"))
            if run_id not in run_ids:
                run_ids.append(run_id)
    return [runs[run_id][1] for run_id in run_ids if run_id in runs]


def _latest_checkpoint(paths: StudyPaths) -> dict[str, Any] | None:
    files = checkpoint_paths(paths)
    if not files:
        return None
    value = load_json(files[-1])
    return value if isinstance(value, dict) else None


def _latest_checkpoint_source(paths: StudyPaths) -> dict[str, Any] | None:
    files = checkpoint_paths(paths)
    if not files:
        return None
    path = files[-1]
    value = load_json(path)
    if not isinstance(value, dict):
        return None
    return {
        "checkpoint_id": value.get("checkpoint_id"),
        "checkpoint_sha256": value.get("checkpoint_sha256"),
        "path": path.relative_to(paths.root).as_posix(),
        "sha256": sha256_file(path),
        "created_at": value.get("created_at"),
        "active_claim_count": len(value.get("claims_snapshot", [])),
        "decisive_evidence_count": len(value.get("decisive_evidence", [])),
        "contradictory_evidence_count": len(
            value.get("contradictory_evidence", [])
        ),
    }


def _evidence_source_index(
    refs_by_role: list[tuple[str, list[dict[str, Any]]]],
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
    paths: StudyPaths,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for role, refs in refs_by_role:
        for ref in refs:
            key = _evidence_key(ref)
            role_key = (*key, role)
            if role_key in seen:
                continue
            seen.add(role_key)
            source = evidence.get(key)
            if source is None:
                continue
            path, item = source
            inference = item.get("inference")
            if not isinstance(inference, dict):
                inference = {}

            def _inference_count(field: str) -> int:
                values = inference.get(field)
                return len(values) if isinstance(values, list) else 0

            summary = {
                "schema_version": item.get("schema_version"),
                "evidence_id": key[0],
                "version": key[1],
                "status": item.get("status"),
                "assessment": item.get("assessment"),
                "evidence_basis": item.get("evidence_basis", {}).get(
                    "mode", "exploratory"
                ),
                "record_sha256": item.get("record_sha256"),
                "addressed_claim_ids": item.get("addresses", {}).get(
                    "claim_ids", []
                ),
                "run_count": len(item.get("runs", [])),
                "inference": {
                    "observation_to_claim_present": bool(
                        str(inference.get("observation_to_claim") or "").strip()
                    ),
                    "auxiliary_assumption_count": _inference_count(
                        "auxiliary_assumptions"
                    ),
                    "competing_explanation_count": _inference_count(
                        "competing_explanations"
                    ),
                    "falsification_condition_count": _inference_count(
                        "falsification_conditions"
                    ),
                },
            }
            records.append(
                {
                    "path": path.relative_to(paths.root).as_posix(),
                    "sha256": sha256_file(path),
                    "evidence_id": key[0],
                    "version": key[1],
                    "role": role,
                    "status": item.get("status"),
                    "assessment": item.get("assessment"),
                    "evidence_basis": item.get("evidence_basis", {}).get(
                        "mode", "exploratory"
                    ),
                    # Compatibility key: this is deliberately a bounded source
                    # summary, never the authoritative Evidence object.
                    "object": summary,
                }
            )
    return records


def _run_source_index(
    evidence_sources: list[dict[str, Any]],
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
    runs: dict[str, tuple[Path, dict[str, Any]]],
    paths: StudyPaths,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_run: dict[str, dict[str, Any]] = {}
    all_index_records: list[dict[str, Any]] = []
    for source in evidence_sources:
        key = (str(source["evidence_id"]), int(source["version"]))
        evidence_record = evidence.get(key)
        if evidence_record is None:
            continue
        for run_ref in evidence_record[1].get("runs", []):
            run_id = str(run_ref.get("run_id"))
            run_source = runs.get(run_id)
            if run_source is None:
                continue
            path, manifest = run_source
            inventory_record = {
                "run_id": run_id,
                "path": path.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(path),
                "status": manifest.get("status"),
                "epistemic_role": manifest.get("epistemic_role", {}).get(
                    "mode", "exploratory"
                ),
                "manifest_sha256": manifest.get("integrity", {}).get(
                    "manifest_sha256"
                ),
            }
            all_index_records.append(inventory_record)
            current = by_run.get(run_id)
            if current is None:
                current = {
                    **inventory_record,
                    "roles": [],
                    "assessments": [],
                    "evidence_ids": [],
                    "cohort_fingerprint_sha256": manifest.get("cohort", {}).get(
                        "fingerprint_sha256"
                    ),
                }
                by_run[run_id] = current
            for field, value in (
                ("roles", source["role"]),
                ("assessments", source.get("assessment")),
                ("evidence_ids", source["evidence_id"]),
            ):
                if value not in current[field]:
                    current[field].append(value)
    ordered = [by_run[key] for key in sorted(by_run)]
    selected = ordered[:MAX_REVIEW_RUN_SOURCES]
    inventory = {
        "total_record_count": len(ordered),
        "selected_record_count": len(selected),
        "truncated": len(selected) != len(ordered),
        "inventory_sha256": sha256_json(
            sorted(all_index_records, key=lambda item: (item["run_id"], item["path"]))
        ),
    }
    return selected, inventory


def _confirmation_source_index(
    paths: StudyPaths,
    runs: dict[str, tuple[Path, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Index frozen registrations and every workflow-visible confirmatory attempt.

    The selected list is bounded for reviewer context, while the inventory hash
    commits to the complete set.  Reviewers must use the count and ``truncated``
    flag instead of treating an omitted locator as a missing attempt.
    """

    confirmation_sources: list[dict[str, Any]] = []
    if paths.confirmations.is_dir():
        for path in sorted(paths.confirmations.glob("CONF-*.json")):
            value = load_json(path)
            if not isinstance(value, dict):
                continue
            claim_ids = [
                item.get("claim_id")
                for item in value.get("claims", [])
                if isinstance(item, dict)
            ]
            slot_ids = [
                item.get("slot_id")
                for item in value.get("run_slots", [])
                if isinstance(item, dict)
            ]
            held_out = value.get("held_out")
            held_out_summary = (
                {
                    "status": held_out.get("status"),
                    "freshness": held_out.get("freshness"),
                    "description_preview": str(held_out.get("description", ""))[:240],
                    "path_count": len(held_out.get("paths", [])),
                    "bindings_sha256": sha256_json(held_out.get("bindings", [])),
                    "workflow_observed_prior_run_count": held_out.get(
                        "workflow_observed_prior_run_count"
                    ),
                }
                if isinstance(held_out, dict)
                else None
            )
            confirmation_sources.append(
                {
                    "confirmation_id": value.get("confirmation_id"),
                    "status": value.get("status"),
                    "record_sha256": value.get("record_sha256"),
                    "path": path.relative_to(paths.root).as_posix(),
                    "sha256": sha256_file(path),
                    "claim_ids": claim_ids,
                    "planned_slot_count": len(slot_ids),
                    "planned_slot_ids_sha256": sha256_json(slot_ids),
                    "analysis_plan_sha256": sha256_json(value.get("analysis_plan")),
                    "held_out": held_out_summary,
                }
            )

    attempts: list[dict[str, Any]] = []
    for run_id, (path, manifest) in sorted(runs.items()):
        if effective_run_mode(manifest) != "confirmatory":
            continue
        binding = confirmation_binding(manifest)
        attempts.append(
            {
                "run_id": run_id,
                "status": manifest.get("status"),
                "path": path.relative_to(paths.root).as_posix(),
                "sha256": sha256_file(path),
                "manifest_sha256": manifest.get("integrity", {}).get(
                    "manifest_sha256"
                ),
                "confirmation_id": (
                    binding.get("confirmation_id") if binding else None
                ),
                "confirmation_sha256": (
                    binding.get("confirmation_sha256") if binding else None
                ),
                "slot_id": binding.get("slot_id") if binding else None,
            }
        )

    selected_sources = confirmation_sources[:MAX_REVIEW_CONFIRMATION_RECORDS]
    selected_attempts = attempts[:MAX_REVIEW_CONFIRMATION_ATTEMPTS]
    return selected_sources, {
        "confirmation_record_count": len(confirmation_sources),
        "selected_confirmation_record_count": len(selected_sources),
        "confirmation_records_truncated": len(selected_sources)
        != len(confirmation_sources),
        "confirmation_records_inventory_sha256": sha256_json(
            confirmation_sources
        ),
        "attempts": selected_attempts,
        "total_attempt_count": len(attempts),
        "selected_attempt_count": len(selected_attempts),
        "truncated": len(selected_attempts) != len(attempts),
        "inventory_sha256": sha256_json(attempts),
    }


def create_review_packet(paths: StudyPaths, base_ref: str | None = None) -> Path:
    pressure = require_growth_allowed(paths, "scientific review")
    effective_base_ref = base_ref or str(
        load_repository_profile(paths.root)["git"]["base_ref"]
    )
    validation_issues = validate_study(paths)
    validation_errors = [item for item in validation_issues if item.level == "ERROR"]
    approval = load_json(paths.brief_approval) if paths.brief_approval.is_file() else None
    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must be an object")
    active_claims_data = dict(claims)
    active_claims_data["claims"] = active_claims(claims)
    evidence = evidence_index(paths)
    runs = run_index(paths)
    if validation_errors:
        decisive_refs = []
        contradictory_refs = []
        other_refs = []
        decisive_total = 0
        contradictory_total = 0
        other_total = 0
    else:
        decisive_refs, decisive_total = _bounded_refs(
            active_claims_data, "supporting_evidence"
        )
        contradictory_refs, contradictory_total = _bounded_refs(
            active_claims_data, "contradictory_evidence"
        )
        other_refs, other_total = _bounded_refs(
            active_claims_data, "other_evidence"
        )
    evidence_sources = _evidence_source_index(
        [
            ("decisive", decisive_refs),
            ("contradictory", contradictory_refs),
            ("other", other_refs),
        ],
        evidence,
        paths,
    )
    evidence_inventory_records = [
        {
            "path": path.relative_to(paths.root).as_posix(),
            "sha256": sha256_file(path),
            "evidence_id": key[0],
            "version": key[1],
            "status": item.get("status"),
            "assessment": item.get("assessment"),
        }
        for key, (path, item) in sorted(evidence.items())
    ]
    decisive_manifests = _manifests_for_evidence(decisive_refs, evidence, runs)
    contradictory_manifests = _manifests_for_evidence(contradictory_refs, evidence, runs)
    all_critical_manifests: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    for manifest in [*decisive_manifests, *contradictory_manifests]:
        if manifest["run_id"] not in seen_runs:
            all_critical_manifests.append(manifest)
            seen_runs.add(manifest["run_id"])
    run_sources, run_inventory = _run_source_index(
        evidence_sources,
        evidence,
        runs,
        paths,
    )
    confirmation_sources, confirmation_attempt_inventory = (
        _confirmation_source_index(paths, runs)
    )

    approval_issues = brief_approval_issues(paths)
    current_brief_hash = sha256_file(paths.brief)
    current_protected = protected_artifact_snapshot(paths)
    protected_checks = {
        "brief_current_hash": current_brief_hash,
        "approval_present": approval is not None,
        "approval_current": not any(item.level == "ERROR" for item in approval_issues),
        "approval_brief_hash": approval.get("brief", {}).get("sha256") if isinstance(approval, dict) else None,
        "current_protected_artifacts": current_protected,
        "approved_protected_artifacts": approval.get("protected_artifacts") if isinstance(approval, dict) else None,
        "issues": [item.render() for item in approval_issues],
        "critical_run_checks": [
            {
                "run_id": manifest["run_id"],
                "brief_hash_matches_active": manifest.get("brief", {}).get("sha256") == current_brief_hash,
                "approval_hash_matches_active": (
                    isinstance(approval, dict)
                    and manifest.get("brief", {}).get("approval_sha256") == approval.get("approval_sha256")
                ),
            }
            for manifest in all_critical_manifests
        ],
    }
    cohort_fingerprints = {
        item["run_id"]: item.get("cohort_fingerprint_sha256")
        for item in run_sources
    }
    git = git_diff_metadata(paths.root, effective_base_ref)
    repository_profile = profile_summary(paths.root)
    change_scope = evaluate_changes(paths, write_projection=True)
    deviations: list[str] = []
    if validation_errors:
        deviations.append(
            f"authoritative Study validation failed with {len(validation_errors)} error(s); no Evidence was labeled decisive"
        )
    for source in evidence_sources:
        if source.get("object", {}).get("schema_version") == 1:
            deviations.append(
                f"active Evidence {source['evidence_id']} v{source['version']} uses "
                "legacy schema V1 without the required V2 inference argument"
            )
    if not git.get("available"):
        deviations.append(str(git.get("deviation") or "Git diff unavailable"))
    if change_scope.get("outcome") != "PASS":
        deviations.append(
            f"current repository change scope is {change_scope.get('outcome')}"
        )
    if git_state(paths.root).get("dirty"):
        deviations.append("working tree is dirty")
    for manifest in all_critical_manifests:
        if manifest.get("status") != "succeeded":
            deviations.append(f"critical Run {manifest['run_id']} has status {manifest.get('status')}")
        if manifest.get("brief", {}).get("sha256") != current_brief_hash:
            deviations.append(
                f"critical Run {manifest['run_id']} used a different Brief hash"
            )
        if not isinstance(approval, dict) or manifest.get("brief", {}).get(
            "approval_sha256"
        ) != approval.get("approval_sha256"):
            deviations.append(
                f"critical Run {manifest['run_id']} used a different Brief approval"
            )
        if manifest.get("git", {}).get("dirty"):
            deviations.append(f"critical Run {manifest['run_id']} used a dirty working tree")
        if manifest.get("code_state", {}).get("changed_during_run"):
            deviations.append(f"critical Run {manifest['run_id']} changed tracked code during execution")
        if any(record.get("changed_during_run") for record in manifest.get("inputs", [])):
            deviations.append(f"critical Run {manifest['run_id']} had an input change during execution")
    formalization = check_formalization(paths, {"for_review": True})
    if formalization.blocked:
        deviations.append("progressive-formalization review gate is BLOCKED")
    selector = build_active_selector(paths, claims_data=claims)
    selector_path = write_active_selector(paths, claims_data=claims)
    write_compaction_due(paths, pressure)
    decisive_run_sources = [
        item for item in run_sources if "decisive" in item["roles"]
    ]
    contradictory_run_sources = [
        item for item in run_sources if "contradictory" in item["roles"]
    ]
    packet = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "brief": {
            "path": paths.brief.relative_to(paths.root).as_posix(),
            "sha256": current_brief_hash,
            "approval": approval,
        },
        "active_context": {
            "path": selector_path.relative_to(paths.root).as_posix(),
            "size": selector_path.stat().st_size,
            "sha256": sha256_file(selector_path),
            "selector_sha256": selector["selector_sha256"],
        },
        "active_formal_artifacts": selector["active_formal_artifacts"][
            "sources"
        ],
        "claims": active_claims_data,
        "evidence": evidence_sources,
        "evidence_inventory": {
            "total_record_count": len(evidence),
            "active_referenced_record_count": len(
                {
                    (item["evidence_id"], item["version"])
                    for item in evidence_sources
                }
            ),
            "selected_source_count": len(evidence_sources),
            "selected_by_role": {
                "decisive": len(decisive_refs),
                "contradictory": len(contradictory_refs),
                "other": len(other_refs),
            },
            "total_by_role": {
                "decisive": decisive_total,
                "contradictory": contradictory_total,
                "other": other_total,
            },
            "truncated": any(
                selected < total
                for selected, total in (
                    (len(decisive_refs), decisive_total),
                    (len(contradictory_refs), contradictory_total),
                    (len(other_refs), other_total),
                )
            ),
            "inventory_sha256": sha256_json(evidence_inventory_records),
        },
        "decisive_evidence": decisive_refs,
        "contradictory_evidence": contradictory_refs,
        "other_evidence": other_refs,
        # Compatibility names now contain bounded source indexes, not full
        # authoritative Run manifests.
        "decisive_run_manifests": decisive_run_sources,
        "contradictory_run_manifests": contradictory_run_sources,
        "other_run_sources": [
            item for item in run_sources if "other" in item["roles"]
        ],
        "run_inventory": run_inventory,
        "confirmation_records": confirmation_sources,
        "confirmation_attempt_inventory": confirmation_attempt_inventory,
        "cohort_fingerprints": cohort_fingerprints,
        "protected_condition_hash_checks": protected_checks,
        "git_diff_metadata": git,
        "repository_profile": repository_profile,
        "change_scope": change_scope,
        "unresolved_formalization_debt": collect_formalization_debt(paths),
        "formalization_check": {
            "outcome": formalization.outcome,
            "requirements": formalization.requirements,
        },
        "reproducibility_commands": [
            {
                "run_id": item["run_id"],
                "manifest_path": item["path"],
                "manifest_sha256": item.get("manifest_sha256"),
                "instruction": "Read the immutable Run manifest at manifest_path.",
            }
            for item in run_sources
        ],
        "known_deviations": sorted(set(deviations)),
        "authority_validation": {
            "passed": not validation_errors,
            "errors": [item.render() for item in validation_errors],
            "warnings": [item.render() for item in validation_issues if item.level == "WARNING"],
        },
        "latest_checkpoint": _latest_checkpoint_source(paths),
        "compaction_pressure": pressure,
        "packet_sha256": "",
    }
    packet["packet_sha256"] = record_digest(packet, "packet_sha256")
    output = paths.generated / "REVIEW_PACKET.json"
    atomic_write_json(output, packet)
    return output


def import_and_render_review(paths: StudyPaths, source: Path) -> Path:
    review = load_json(source.resolve())
    if not isinstance(review, dict):
        raise ValidationError("structured review must be a JSON object")
    issues = object_schema_issues(paths.root, "review", source, review)
    if issues:
        raise ValidationError("invalid structured review:\n" + "\n".join(item.render() for item in issues))
    if review.get("study_id") != paths.study_id:
        raise ValidationError("review study_id does not match Study")
    packet = paths.generated / "REVIEW_PACKET.json"
    if not packet.is_file():
        raise ValidationError("generate REVIEW_PACKET.json before importing a review")
    if review.get("review_packet_sha256") != sha256_file(packet):
        raise ValidationError("review does not reference the current REVIEW_PACKET.json")
    structured_output = paths.generated / "REVIEW.json"
    markdown_output = paths.generated / "REVIEW.md"
    atomic_write_json(structured_output, review)
    atomic_write_bytes(markdown_output, render_review_markdown(review))
    return markdown_output
