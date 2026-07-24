from __future__ import annotations

import copy
from pathlib import Path
import stat
from typing import Any

from .active_context import (
    active_selector_bytes,
    active_claims,
    build_active_selector,
    compaction_pressure,
    require_growth_allowed,
    write_active_selector,
    write_compaction_due,
)
from .formalization import check_formalization, collect_formalization_debt
from .git_state import git_diff_metadata, git_state
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    file_record,
    load_json,
    load_json_bytes,
    record_digest,
    sha256_file,
    sha256_bytes,
    sha256_json,
)
from .locking import serialized_study_authority
from .models import (
    REVIEW_PACKET_SCHEMA_VERSION,
    StudyPaths,
    ValidationError,
)
from .observation_triggers import load_current_registry, registry_path
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


def _declared_evidence_mode(item: dict[str, Any]) -> str | None:
    basis = item.get("evidence_basis")
    if not isinstance(basis, dict):
        return None
    mode = basis.get("mode")
    return str(mode) if mode in {"exploratory", "confirmatory", "mixed"} else None


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


def _require_clean_scientific_worktree(
    paths: StudyPaths,
    *,
    require_consistent_authority: bool = True,
    pending_authority_path: Path | None = None,
) -> dict[str, Any]:
    """Require an exact commit while ignoring only Review projection outputs."""

    from .review_verdict_sequence import (
        require_consistent_review_verdict_authority,
    )

    if require_consistent_authority:
        require_consistent_review_verdict_authority(paths)
    repository = git_state(paths.root)
    if not repository.get("available"):
        raise ValidationError(
            "independent Review requires an available Git repository"
        )
    if not repository.get("commit"):
        raise ValidationError(
            "independent Review requires a committed Git revision"
        )
    if not repository.get("dirty"):
        return repository
    allowed_roots = (
        paths.generated.relative_to(paths.root).as_posix(),
        (paths.study / "review-history").relative_to(paths.root).as_posix(),
    )
    generated_root, history_root = allowed_roots
    sequence_path = paths.review_verdict_sequence.relative_to(
        paths.root
    ).as_posix()
    pending_path = (
        pending_authority_path.relative_to(paths.root).as_posix()
        if pending_authority_path is not None
        else None
    )
    disallowed: list[str] = []
    for line in repository.get("status", []):
        rendered = str(line)
        raw_path = rendered[3:] if len(rendered) > 3 else ""
        status_code = rendered[:2]
        in_generated = raw_path == generated_root or raw_path.startswith(
            generated_root + "/"
        )
        in_history = raw_path == history_root or raw_path.startswith(
            history_root + "/"
        )
        history_addition = in_history and status_code in {"??", "A ", " A"}
        sequence_change = (
            raw_path == sequence_path and "D" not in status_code
        )
        pending_authority_addition = (
            pending_path is not None
            and raw_path == pending_path
            and status_code in {"??", "A ", " A"}
        )
        if (
            " -> " in raw_path
            or (
                not in_generated
                and not history_addition
                and not sequence_change
                and not pending_authority_addition
            )
        ):
            disallowed.append(rendered)
    if disallowed:
        raise ValidationError(
            "independent Review requires a clean scientific worktree; only "
            "generated Review projections, immutable review-history additions, "
            "and the consistent Review/Verdict sequence may remain uncommitted. "
            "Unexpected changes: "
            + ", ".join(disallowed[:8])
        )
    return repository


def _review_git_diff_metadata(paths: StudyPaths, base_ref: str) -> dict[str, Any]:
    """Describe committed history while normalizing allowed Review projections."""

    value = git_diff_metadata(paths.root, base_ref)
    if value.get("available"):
        value = dict(value)
        value.update(
            {
                "dirty": False,
                "dirty_status": [],
                "dirty_status_sha256": sha256_bytes(b""),
                "dirty_diff_sha256": sha256_bytes(b""),
            }
        )
    return value


def _review_change_scope(paths: StudyPaths, *, write_projection: bool) -> dict[str, Any]:
    """Remove mutable Study projections from the host-code change summary."""

    value = evaluate_changes(paths, write_projection=write_projection)
    normalized = copy.deepcopy(value)
    changed = normalized.get("changed_paths")
    if isinstance(changed, list):
        normalized["changed_paths"] = [
            item
            for item in changed
            if isinstance(item, dict)
            and item.get("classification") not in {"study_state", "other_study"}
        ]
    normalized["advisories"] = [
        item
        for item in normalized.get("advisories", [])
        if isinstance(item, str)
        and "unrelated Study state is present" not in item
    ]
    return normalized


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
        "decisive_observation_count": len(
            value.get("decisive_observations", [])
        ),
    }


def _evidence_inventory_records(
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
    paths: StudyPaths,
) -> list[dict[str, Any]]:
    return [
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


def _finalized_evidence_refs(
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for (evidence_id, version), (_, item) in sorted(evidence.items()):
        digest = item.get("record_sha256")
        if (
            item.get("status") == "finalized"
            and isinstance(digest, str)
            and len(digest) == 64
        ):
            refs.append(
                {
                    "evidence_id": evidence_id,
                    "version": version,
                    "sha256": digest,
                }
            )
    return refs


def _active_context_binding(
    paths: StudyPaths,
    selector: dict[str, Any],
) -> dict[str, Any]:
    payload = canonical_json_bytes(selector) + b"\n"
    return {
        "path": (
            paths.generated / "ACTIVE_CONTEXT.json"
        ).relative_to(paths.root).as_posix(),
        "size": active_selector_bytes(selector),
        "sha256": sha256_bytes(payload),
        "selector_sha256": selector["selector_sha256"],
    }


def _checkpoint_ref(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "checkpoint_id": source.get("checkpoint_id"),
        "sha256": source.get("checkpoint_sha256"),
    }


def _review_scope(
    paths: StudyPaths,
    *,
    claims: dict[str, Any],
    evidence: dict[tuple[str, int], tuple[Path, dict[str, Any]]],
    selector: dict[str, Any],
    commit: str | None,
    brief_sha256: str,
    latest_checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    selected_claims = active_claims(claims)
    inventory = _evidence_inventory_records(evidence, paths)
    return {
        "commit": commit,
        "brief_sha256": brief_sha256,
        "checkpoint": _checkpoint_ref(latest_checkpoint),
        "claims": [
            {
                "claim_id": str(claim.get("claim_id")),
                "sha256": sha256_json(claim),
            }
            for claim in selected_claims
        ],
        "evidence": _finalized_evidence_refs(evidence),
        "evidence_inventory_sha256": sha256_json(inventory),
        "active_context": _active_context_binding(paths, selector),
    }


def current_review_scope(paths: StudyPaths) -> dict[str, Any]:
    """Return the exact current state that an independent Review may endorse."""

    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must contain an object")
    selector = build_active_selector(paths, claims_data=claims)
    return _review_scope(
        paths,
        claims=claims,
        evidence=evidence_index(paths),
        selector=selector,
        commit=git_state(paths.root).get("commit"),
        brief_sha256=sha256_file(paths.brief),
        latest_checkpoint=_latest_checkpoint_source(paths),
    )


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
                "evidence_basis": _declared_evidence_mode(item),
                "record_sha256": item.get("record_sha256"),
                "observation_ref": item.get("observation_ref"),
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
                    "evidence_basis": _declared_evidence_mode(item),
                    "summary": summary,
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
    """Index registrations, abandonment records, and confirmatory attempts.

    The selected list is bounded for reviewer context, while the inventory hash
    commits to the complete set.  Reviewers must use the count and ``truncated``
    flag instead of treating an omitted locator as a missing attempt.
    """

    from .confirmation import load_confirmation_campaign_abandonment
    from .confirmation_sequence import (
        require_consistent_confirmation_authority,
    )

    confirmation_sequence = require_consistent_confirmation_authority(paths)
    abandonment_sources: list[dict[str, Any]] = []
    abandonments_by_campaign: dict[str, dict[str, Any]] = {}
    if paths.confirmations.is_dir():
        for path in sorted(
            paths.confirmations.glob("CAMP-*.abandonment.json")
        ):
            campaign_id = path.name.removesuffix(".abandonment.json")
            abandonment = load_confirmation_campaign_abandonment(
                paths, campaign_id
            )
            if abandonment is None:  # pragma: no cover - file was just listed
                continue
            authorization = abandonment.get("authorization")
            locator = {
                "record_type": "confirmation_campaign_abandonment",
                "campaign_id": campaign_id,
                "status": "abandoned",
                "path": path.relative_to(paths.root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "record_sha256": abandonment.get("record_sha256"),
                "abandoned_at": abandonment.get("abandoned_at"),
                "claim_version_count": len(
                    abandonment.get("claim_versions", [])
                ),
                "confirmation_record_count": len(
                    abandonment.get("confirmation_records", [])
                ),
                "planned_slot_count": len(
                    abandonment.get("planned_slot_ids", [])
                ),
                "rationale_preview": str(
                    abandonment.get("rationale", "")
                )[:240],
                "authorization": (
                    {
                        "mode": authorization.get("mode"),
                        "source": authorization.get("source"),
                        "assurance": authorization.get("assurance"),
                        "instruction_sha256": authorization.get(
                            "instruction_sha256"
                        ),
                    }
                    if isinstance(authorization, dict)
                    else None
                ),
            }
            abandonment_sources.append(locator)
            abandonments_by_campaign[campaign_id] = locator

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
            campaign = value.get("campaign")
            campaign_id = (
                str(campaign.get("campaign_id"))
                if isinstance(campaign, dict)
                else ""
            )
            abandonment_locator = abandonments_by_campaign.get(campaign_id)
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
                    "campaign_status": (
                        "abandoned"
                        if abandonment_locator is not None
                        else "active"
                    ),
                    "abandonment": copy.deepcopy(abandonment_locator),
                    "record_sha256": value.get("record_sha256"),
                    "campaign": copy.deepcopy(campaign),
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
    selected_abandonments = abandonment_sources[
        :MAX_REVIEW_CONFIRMATION_RECORDS
    ]
    return selected_sources, {
        "authority": {
            **file_record(paths.confirmation_sequence, paths.root),
            "high_water_mark": confirmation_sequence["high_water_mark"],
            "inventory_sha256": confirmation_sequence["inventory_sha256"],
            "sequence_sha256": confirmation_sequence["sequence_sha256"],
        },
        "confirmation_record_count": len(confirmation_sources),
        "selected_confirmation_record_count": len(selected_sources),
        "confirmation_records_truncated": len(selected_sources)
        != len(confirmation_sources),
        "confirmation_records_inventory_sha256": sha256_json(
            confirmation_sources
        ),
        "campaign_abandonments": selected_abandonments,
        "total_campaign_abandonment_count": len(abandonment_sources),
        "selected_campaign_abandonment_count": len(selected_abandonments),
        "campaign_abandonments_truncated": len(selected_abandonments)
        != len(abandonment_sources),
        "campaign_abandonments_inventory_sha256": sha256_json(
            abandonment_sources
        ),
        "attempts": selected_attempts,
        "total_attempt_count": len(attempts),
        "selected_attempt_count": len(selected_attempts),
        "truncated": len(selected_attempts) != len(attempts),
        "inventory_sha256": sha256_json(attempts),
    }


def _build_review_packet(
    paths: StudyPaths,
    *,
    effective_base_ref: str,
    pressure: dict[str, Any],
    materialize_projections: bool,
    allow_unindexed_review_verdict_authority: bool = False,
) -> dict[str, Any]:
    """Build the deterministic packet content from current authority."""

    validation_issues = validate_study(paths)
    if allow_unindexed_review_verdict_authority:
        # A recovery caller has already proved that the visible authority is
        # exactly one valid record ahead of the durable sequence.  Ignore only
        # that expected sequence diagnostic while replaying the packet; every
        # other Study validation issue remains authoritative.
        sequence_path = str(paths.review_verdict_sequence)
        validation_issues = [
            item for item in validation_issues if item.path != sequence_path
        ]
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
    evidence_inventory_records = _evidence_inventory_records(evidence, paths)
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
    git = _review_git_diff_metadata(paths, effective_base_ref)
    repository_profile = profile_summary(paths.root)
    change_scope = _review_change_scope(
        paths,
        write_projection=materialize_projections,
    )
    deviations: list[str] = []
    if validation_errors:
        deviations.append(
            f"authoritative Study validation failed with {len(validation_errors)} error(s); no Evidence was labeled decisive"
        )
    if not git.get("available"):
        deviations.append(str(git.get("deviation") or "Git diff unavailable"))
    if change_scope.get("outcome") != "PASS":
        deviations.append(
            f"current repository change scope is {change_scope.get('outcome')}"
        )
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
    if materialize_projections:
        write_active_selector(paths, claims_data=claims)
        write_compaction_due(paths, pressure)
    decisive_run_sources = [
        item for item in run_sources if "decisive" in item["roles"]
    ]
    contradictory_run_sources = [
        item for item in run_sources if "contradictory" in item["roles"]
    ]
    trigger_registry = load_current_registry(paths.root)
    trigger_registry_path = registry_path(
        paths.root, int(trigger_registry["registry_version"])
    )
    latest_checkpoint = _latest_checkpoint_source(paths)
    active_context_binding = _active_context_binding(paths, selector)
    packet = {
        "schema_version": REVIEW_PACKET_SCHEMA_VERSION,
        "study_id": paths.study_id,
        "brief": {
            "path": paths.brief.relative_to(paths.root).as_posix(),
            "sha256": current_brief_hash,
            "approval": approval,
        },
        "active_context": active_context_binding,
        "active_formal_artifacts": selector["active_formal_artifacts"][
            "sources"
        ],
        "graph_records": selector["graph_records"],
        "claims": active_claims_data,
        "observations": selector["decisive_observations"],
        "observation_trigger_registry": {
            "path": trigger_registry_path.relative_to(paths.root).as_posix(),
            "version": trigger_registry["registry_version"],
            "sha256": trigger_registry["registry_sha256"],
            "size": trigger_registry_path.stat().st_size,
        },
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
            "finalized_refs": _finalized_evidence_refs(evidence),
        },
        "decisive_evidence": decisive_refs,
        "contradictory_evidence": contradictory_refs,
        "other_evidence": other_refs,
        "decisive_run_sources": decisive_run_sources,
        "contradictory_run_sources": contradictory_run_sources,
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
        "latest_checkpoint": latest_checkpoint,
        "compaction_pressure": pressure,
        "review_scope": _review_scope(
            paths,
            claims=claims,
            evidence=evidence,
            selector=selector,
            commit=git.get("head"),
            brief_sha256=current_brief_hash,
            latest_checkpoint=latest_checkpoint,
        ),
        "packet_sha256": "",
    }
    packet["packet_sha256"] = record_digest(packet, "packet_sha256")
    return packet


@serialized_study_authority
def create_review_packet(paths: StudyPaths, base_ref: str | None = None) -> Path:
    pressure = require_growth_allowed(paths, "scientific review")
    _require_clean_scientific_worktree(paths)
    effective_base_ref = base_ref or str(
        load_repository_profile(paths.root)["git"]["base_ref"]
    )
    packet = _build_review_packet(
        paths,
        effective_base_ref=effective_base_ref,
        pressure=pressure,
        materialize_projections=True,
    )
    output = paths.generated / "REVIEW_PACKET.json"
    packet_issues = object_schema_issues(
        paths.root, "review_packet", output, packet
    )
    if packet_issues:
        raise ValidationError(
            "generated REVIEW_PACKET.json is invalid:\n"
            + "\n".join(item.render() for item in packet_issues)
        )
    atomic_write_json(output, packet)
    return output


def _packet_scope_from_summary(packet: dict[str, Any]) -> dict[str, Any]:
    claims = packet.get("claims")
    claim_items = claims.get("claims", []) if isinstance(claims, dict) else []
    inventory = packet.get("evidence_inventory")
    evidence_refs = (
        inventory.get("finalized_refs", []) if isinstance(inventory, dict) else []
    )
    git = packet.get("git_diff_metadata")
    brief = packet.get("brief")
    return {
        "commit": git.get("head") if isinstance(git, dict) else None,
        "brief_sha256": (
            brief.get("sha256") if isinstance(brief, dict) else None
        ),
        "checkpoint": _checkpoint_ref(packet.get("latest_checkpoint")),
        "claims": [
            {
                "claim_id": str(claim.get("claim_id")),
                "sha256": sha256_json(claim),
            }
            for claim in claim_items
            if isinstance(claim, dict)
        ],
        "evidence": evidence_refs,
        "evidence_inventory_sha256": (
            inventory.get("inventory_sha256")
            if isinstance(inventory, dict)
            else None
        ),
        "active_context": packet.get("active_context"),
    }


def _validate_packet_file_binding(
    paths: StudyPaths,
    value: Any,
    *,
    label: str,
    expected_path: Path | None = None,
) -> Path:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} binding must be an object")
    raw_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValidationError(f"{label} binding requires a path")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValidationError(f"{label} binding requires a SHA-256 digest")
    candidate = paths.root / raw_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(paths.root.resolve())
    except (FileNotFoundError, ValueError) as exc:
        raise ValidationError(
            f"{label} binding does not resolve to a repository file"
        ) from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ValidationError(f"{label} binding must identify a regular file")
    if expected_path is not None and resolved != expected_path.resolve(strict=True):
        raise ValidationError(f"{label} binding identifies the wrong path")
    if sha256_file(candidate) != digest:
        raise ValidationError(f"{label} binding has a stale SHA-256 digest")
    size = value.get("size")
    if size is not None and size != candidate.stat().st_size:
        raise ValidationError(f"{label} binding has a stale size")
    return candidate


def _validate_review_packet(
    paths: StudyPaths,
    packet_path: Path,
    *,
    require_current: bool,
    allow_unindexed_review_verdict_authority: bool = False,
    pending_authority_path: Path | None = None,
) -> dict[str, Any]:
    packet = load_json(packet_path)
    if not isinstance(packet, dict):
        raise ValidationError("REVIEW_PACKET.json must contain an object")
    issues = object_schema_issues(
        paths.root, "review_packet", packet_path, packet
    )
    if issues:
        raise ValidationError(
            "invalid REVIEW_PACKET.json:\n"
            + "\n".join(item.render() for item in issues)
        )
    if packet.get("study_id") != paths.study_id:
        raise ValidationError("REVIEW_PACKET.json study_id does not match Study")
    if packet.get("packet_sha256") != record_digest(packet, "packet_sha256"):
        raise ValidationError("REVIEW_PACKET.json packet_sha256 is invalid")
    summarized_scope = _packet_scope_from_summary(packet)
    if packet.get("review_scope") != summarized_scope:
        raise ValidationError(
            "REVIEW_PACKET.json review_scope does not match its packet summary"
        )
    if not require_current:
        return packet

    _require_clean_scientific_worktree(
        paths,
        require_consistent_authority=(
            not allow_unindexed_review_verdict_authority
        ),
        pending_authority_path=pending_authority_path,
    )
    current_scope = current_review_scope(paths)
    if packet["review_scope"] != current_scope:
        raise ValidationError(
            "REVIEW_PACKET.json no longer matches the current Review scope"
        )
    packet_git = packet.get("git_diff_metadata")
    effective_base_ref = (
        packet_git.get("base_ref") if isinstance(packet_git, dict) else None
    )
    if not isinstance(effective_base_ref, str) or not effective_base_ref:
        raise ValidationError(
            "REVIEW_PACKET.json Git summary requires a non-empty base_ref"
        )
    rebuilt = _build_review_packet(
        paths,
        effective_base_ref=effective_base_ref,
        pressure=compaction_pressure(paths),
        materialize_projections=False,
        allow_unindexed_review_verdict_authority=(
            allow_unindexed_review_verdict_authority
        ),
    )
    if packet != rebuilt:
        raise ValidationError(
            "REVIEW_PACKET.json does not replay from current authoritative "
            "sources and derived summaries"
        )

    brief = packet["brief"]
    _validate_packet_file_binding(
        paths,
        brief,
        label="Review packet Brief",
        expected_path=paths.brief,
    )
    current_approval = (
        load_json(paths.brief_approval)
        if paths.brief_approval.is_file()
        else None
    )
    if brief.get("approval") != current_approval:
        raise ValidationError(
            "REVIEW_PACKET.json does not embed the current Brief approval"
        )

    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must contain an object")
    active_claims_data = dict(claims)
    active_claims_data["claims"] = active_claims(claims)
    if packet.get("claims") != active_claims_data:
        raise ValidationError(
            "REVIEW_PACKET.json does not contain the full current active Claim inventory"
        )

    selector = build_active_selector(paths, claims_data=claims)
    if packet.get("active_context") != _active_context_binding(paths, selector):
        raise ValidationError(
            "REVIEW_PACKET.json ACTIVE_CONTEXT identity is stale"
        )
    selector_path = _validate_packet_file_binding(
        paths,
        packet["active_context"],
        label="Review packet ACTIVE_CONTEXT",
        expected_path=paths.generated / "ACTIVE_CONTEXT.json",
    )
    stored_selector = load_json(selector_path)
    if stored_selector != selector:
        raise ValidationError(
            "REVIEW_PACKET.json ACTIVE_CONTEXT bytes do not match current state"
        )
    if stored_selector.get("selector_sha256") != record_digest(
        stored_selector, "selector_sha256"
    ):
        raise ValidationError(
            "REVIEW_PACKET.json ACTIVE_CONTEXT selector digest is invalid"
        )

    evidence = evidence_index(paths)
    inventory_records = _evidence_inventory_records(evidence, paths)
    inventory = packet.get("evidence_inventory")
    if (
        not isinstance(inventory, dict)
        or inventory.get("total_record_count") != len(evidence)
        or inventory.get("inventory_sha256") != sha256_json(inventory_records)
        or inventory.get("finalized_refs") != _finalized_evidence_refs(evidence)
    ):
        raise ValidationError(
            "REVIEW_PACKET.json Evidence inventory summary is stale"
        )

    for index, source in enumerate(packet.get("evidence", [])):
        _validate_packet_file_binding(
            paths,
            source,
            label=f"Review packet Evidence source {index}",
        )
    for role in ("decisive", "contradictory", "other"):
        for index, source in enumerate(packet.get(f"{role}_run_sources", [])):
            _validate_packet_file_binding(
                paths,
                source,
                label=f"Review packet {role} Run source {index}",
            )
    for index, source in enumerate(packet.get("confirmation_records", [])):
        _validate_packet_file_binding(
            paths,
            source,
            label=f"Review packet Confirmation source {index}",
        )
    latest = packet.get("latest_checkpoint")
    if latest is not None:
        _validate_packet_file_binding(
            paths,
            latest,
            label="Review packet latest Checkpoint",
        )
    if latest != _latest_checkpoint_source(paths):
        raise ValidationError(
            "REVIEW_PACKET.json latest Checkpoint summary is stale"
        )
    return packet


@serialized_study_authority
def import_and_render_review(paths: StudyPaths, source: Path) -> Path:
    from .review_verdict_sequence import (
        advance_review_verdict_sequence,
        require_consistent_review_verdict_authority,
        review_verdict_authority_inventory,
    )

    try:
        source_path = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"cannot resolve structured Review source: {source}") from exc
    if not source_path.is_file():
        raise ValidationError("structured Review source must be a regular file")
    review_bytes = source_path.read_bytes()
    review = load_json_bytes(review_bytes, label=str(source_path))
    if not isinstance(review, dict):
        raise ValidationError("structured review must be a JSON object")
    issues = object_schema_issues(paths.root, "review", source_path, review)
    if issues:
        raise ValidationError("invalid structured review:\n" + "\n".join(item.render() for item in issues))
    if review.get("study_id") != paths.study_id:
        raise ValidationError("review study_id does not match Study")
    packet = paths.generated / "REVIEW_PACKET.json"
    if not packet.is_file():
        raise ValidationError("generate REVIEW_PACKET.json before importing a review")
    packet_digest = sha256_file(packet)
    _validate_review_packet(paths, packet, require_current=True)
    packet_bytes = packet.read_bytes()
    if sha256_bytes(packet_bytes) != packet_digest:
        raise ValidationError(
            "REVIEW_PACKET.json changed while the Review was being imported"
        )
    if review.get("review_packet_sha256") != packet_digest:
        raise ValidationError("review does not reference the current REVIEW_PACKET.json")
    previous_inventory = review_verdict_authority_inventory(paths)
    require_consistent_review_verdict_authority(
        paths,
        previous_inventory,
    )
    structured_output = paths.generated / "REVIEW.json"
    markdown_output = paths.generated / "REVIEW.md"
    atomic_write_bytes(structured_output, review_bytes)
    atomic_write_bytes(markdown_output, render_review_markdown(review))
    _archive_review_basis(
        paths,
        review_bytes=review_bytes,
        packet_bytes=packet_bytes,
    )
    current_inventory = review_verdict_authority_inventory(paths)
    if current_inventory != previous_inventory:
        advance_review_verdict_sequence(
            paths,
            previous_inventory=previous_inventory,
        )
    return markdown_output


def _review_history(paths: StudyPaths) -> Path:
    return paths.study / "review-history"


def _archived_review_path(paths: StudyPaths, digest: str) -> Path:
    return _review_history(paths) / f"REVIEW-{digest}.json"


def _archived_packet_path(paths: StudyPaths, digest: str) -> Path:
    return _review_history(paths) / f"REVIEW_PACKET-{digest}.json"


def _archive_exact_bytes(payload: bytes, destination: Path, digest: str) -> None:
    if destination.exists():
        if (
            destination.is_symlink()
            or not destination.is_file()
            or destination.stat().st_nlink != 1
            or sha256_file(destination) != digest
        ):
            raise ValidationError(
                f"immutable Review archive conflicts with expected digest: {destination}"
            )
        destination.chmod(0o444)
        return
    atomic_write_bytes(
        destination,
        payload,
        overwrite=False,
        mode=0o444,
    )


def _archive_review_basis(
    paths: StudyPaths,
    *,
    review_bytes: bytes,
    packet_bytes: bytes,
) -> dict[str, Any]:
    review_digest = sha256_bytes(review_bytes)
    packet_digest = sha256_bytes(packet_bytes)
    history = _review_history(paths)
    history.mkdir(parents=True, exist_ok=True)
    archived_review = _archived_review_path(paths, review_digest)
    archived_packet = _archived_packet_path(paths, packet_digest)
    # Publish the packet bytes first and the Review archive last.  The
    # Review archive is the pair's inventory-visible commit marker; a crash
    # after only the packet remains safely retryable and creates no Review
    # occurrence.
    _archive_exact_bytes(packet_bytes, archived_packet, packet_digest)
    _archive_exact_bytes(review_bytes, archived_review, review_digest)
    return {
        "mode": "reviewed",
        "review": file_record(archived_review, paths.root),
        "review_packet": file_record(archived_packet, paths.root),
    }


def _authorized_review_waiver(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "reason",
        "source",
        "authorization_text",
    }:
        raise ValidationError(
            "Review waiver must contain exactly reason, source, and authorization_text"
        )
    reason = value.get("reason")
    source = value.get("source")
    text = value.get("authorization_text")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("Review waiver requires a non-empty reason")
    if source not in {
        "interactive_human_confirmation",
        "explicit_user_instruction",
    }:
        raise ValidationError("Review waiver has an invalid authorization source")
    if not isinstance(text, str) or not text.strip():
        raise ValidationError(
            "Review waiver requires the exact non-empty human authorization text"
        )
    normalized_text = text.strip()
    return {
        "mode": "waived",
        "reason": reason.strip(),
        "authorization": {
            "source": source,
            "text": normalized_text,
            "text_sha256": sha256_json(normalized_text),
        },
    }


def current_review_basis(
    paths: StudyPaths,
    *,
    judged_scope: dict[str, Any],
    waiver: Any = None,
) -> dict[str, Any]:
    """Bind the current Review or a separately and explicitly authorized waiver."""

    from .review_verdict_sequence import (
        require_consistent_review_verdict_authority,
    )

    require_consistent_review_verdict_authority(paths)
    review_path = paths.generated / "REVIEW.json"
    packet_path = paths.generated / "REVIEW_PACKET.json"
    if waiver is not None:
        basis = _authorized_review_waiver(waiver)
        validate_review_basis(
            paths,
            basis,
            judged_scope=judged_scope,
            require_current=True,
        )
        return basis
    if not review_path.is_file():
        raise ValidationError(
            "no imported independent Review is available; an explicit "
            "human Review waiver with exact authorization text and reason "
            "is required"
        )
    if not packet_path.is_file():
        raise ValidationError(
            "generated REVIEW.json exists without REVIEW_PACKET.json; regenerate "
            "and re-import the independent Review before Verdict"
        )
    review = load_json(review_path)
    if not isinstance(review, dict):
        raise ValidationError("generated REVIEW.json must contain an object")
    issues = object_schema_issues(paths.root, "review", review_path, review)
    if issues:
        raise ValidationError(
            "generated REVIEW.json is invalid:\n"
            + "\n".join(item.render() for item in issues)
        )
    if review.get("study_id") != paths.study_id:
        raise ValidationError("generated REVIEW.json belongs to a different Study")
    _validate_review_packet(paths, packet_path, require_current=True)
    packet_digest = sha256_file(packet_path)
    if review.get("review_packet_sha256") != packet_digest:
        raise ValidationError(
            "generated REVIEW.json does not bind the current REVIEW_PACKET.json"
        )
    review_digest = sha256_file(review_path)
    archived_review = _archived_review_path(paths, review_digest)
    archived_packet = _archived_packet_path(paths, packet_digest)
    if not archived_review.is_file() or not archived_packet.is_file():
        raise ValidationError(
            "import the independent Review again to create its immutable digest archive"
        )
    basis = {
        "mode": "reviewed",
        "review": file_record(archived_review, paths.root),
        "review_packet": file_record(archived_packet, paths.root),
    }
    validate_review_basis(
        paths,
        basis,
        judged_scope=judged_scope,
        require_current=True,
    )
    return basis


def _validate_archived_file(
    paths: StudyPaths,
    value: Any,
    *,
    expected_prefix: str,
) -> Path:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise ValidationError("Review basis file reference has an invalid structure")
    raw_path = value.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValidationError("Review basis file reference requires a path")
    candidate = paths.root / raw_path
    history = _review_history(paths).resolve(strict=False)
    try:
        candidate.resolve(strict=True).relative_to(history)
    except (FileNotFoundError, ValueError) as exc:
        raise ValidationError(
            "Review basis must reference an immutable review-history file"
        ) from exc
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.stat().st_nlink != 1
        or candidate.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValidationError(
            f"Review basis archive must be regular, single-link, and read-only: {raw_path}"
        )
    digest = value.get("sha256")
    if (
        not candidate.name.startswith(expected_prefix)
        or not isinstance(digest, str)
        or candidate.name != f"{expected_prefix}{digest}.json"
        or value != file_record(candidate, paths.root)
    ):
        raise ValidationError(f"Review basis archive binding is stale: {raw_path}")
    return candidate


def _validate_scope_against_review(
    judged_scope: Any,
    review_scope: dict[str, Any],
) -> None:
    if not isinstance(judged_scope, dict):
        raise ValidationError("Verdict judged_scope must be an object")
    for field in ("commit", "brief_sha256", "checkpoint", "active_context"):
        if judged_scope.get(field) != review_scope.get(field):
            raise ValidationError(
                f"Verdict judged_scope.{field} does not match the Review scope"
            )
    for field, identity in (
        ("claims", ("claim_id", "sha256")),
        ("evidence", ("evidence_id", "version", "sha256")),
    ):
        selected = judged_scope.get(field)
        available = review_scope.get(field)
        if not isinstance(selected, list) or not isinstance(available, list):
            raise ValidationError(
                f"Verdict and Review {field} scopes must be arrays"
            )
        available_keys = {
            tuple(item.get(key) for key in identity)
            for item in available
            if isinstance(item, dict)
        }
        for item in selected:
            if (
                not isinstance(item, dict)
                or tuple(item.get(key) for key in identity) not in available_keys
            ):
                raise ValidationError(
                    f"Verdict judged_scope contains {field} not reviewed by "
                    "the bound REVIEW_PACKET"
                )


def validate_legacy_review_basis(paths: StudyPaths, basis: Any) -> None:
    """Replay the immutable subset promised by historical Verdict v2 records."""

    if not isinstance(basis, dict):
        raise ValidationError("legacy Verdict review_basis must be an object")
    mode = basis.get("mode")
    if mode == "waived":
        if set(basis) != {"mode", "reason"}:
            raise ValidationError(
                "legacy waived Review basis has an invalid structure"
            )
        reason = basis.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                "legacy Review waiver requires a non-empty reason"
            )
        return
    if mode != "reviewed" or set(basis) != {
        "mode",
        "review",
        "review_packet",
    }:
        raise ValidationError(
            "legacy reviewed Review basis has an invalid structure"
        )
    review_path = _validate_archived_file(
        paths, basis.get("review"), expected_prefix="REVIEW-"
    )
    packet_path = _validate_archived_file(
        paths,
        basis.get("review_packet"),
        expected_prefix="REVIEW_PACKET-",
    )
    review = load_json(review_path)
    if not isinstance(review, dict):
        raise ValidationError("legacy archived Review must contain an object")
    issues = object_schema_issues(paths.root, "review", review_path, review)
    if issues:
        raise ValidationError(
            "legacy archived Review is invalid:\n"
            + "\n".join(item.render() for item in issues)
        )
    if review.get("study_id") != paths.study_id:
        raise ValidationError(
            "legacy archived Review belongs to a different Study"
        )
    if review.get("review_packet_sha256") != sha256_file(packet_path):
        raise ValidationError(
            "legacy archived Review does not bind its archived REVIEW_PACKET"
        )


def validate_review_basis(
    paths: StudyPaths,
    basis: Any,
    *,
    judged_scope: dict[str, Any] | None = None,
    require_current: bool = False,
    allow_unindexed_review_verdict_authority: bool = False,
    pending_authority_path: Path | None = None,
) -> None:
    """Replay a Verdict's immutable independent-Review binding or waiver."""

    if not isinstance(basis, dict):
        raise ValidationError("Verdict review_basis must be an object")
    mode = basis.get("mode")
    if mode == "waived":
        if set(basis) != {"mode", "reason", "authorization"}:
            raise ValidationError("waived Review basis has an invalid structure")
        reason = basis.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("Review waiver requires a non-empty reason")
        authorization = basis.get("authorization")
        if not isinstance(authorization, dict) or set(authorization) != {
            "source",
            "text",
            "text_sha256",
        }:
            raise ValidationError(
                "Review waiver requires an explicit authorization record"
            )
        if authorization.get("source") not in {
            "interactive_human_confirmation",
            "explicit_user_instruction",
        }:
            raise ValidationError("Review waiver authorization source is invalid")
        text = authorization.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("Review waiver authorization text is empty")
        if authorization.get("text_sha256") != sha256_json(text):
            raise ValidationError(
                "Review waiver authorization text digest is invalid"
            )
        return
    if mode != "reviewed" or set(basis) != {
        "mode",
        "review",
        "review_packet",
    }:
        raise ValidationError("reviewed Review basis has an invalid structure")
    review_path = _validate_archived_file(
        paths, basis.get("review"), expected_prefix="REVIEW-"
    )
    packet_path = _validate_archived_file(
        paths,
        basis.get("review_packet"),
        expected_prefix="REVIEW_PACKET-",
    )
    review = load_json(review_path)
    if not isinstance(review, dict):
        raise ValidationError("archived Review must contain an object")
    issues = object_schema_issues(paths.root, "review", review_path, review)
    if issues:
        raise ValidationError(
            "archived Review is invalid:\n"
            + "\n".join(item.render() for item in issues)
        )
    if review.get("study_id") != paths.study_id:
        raise ValidationError("archived Review belongs to a different Study")
    if review.get("review_packet_sha256") != sha256_file(packet_path):
        raise ValidationError(
            "archived Review does not bind its archived REVIEW_PACKET"
        )
    packet = _validate_review_packet(
        paths,
        packet_path,
        require_current=require_current,
        allow_unindexed_review_verdict_authority=(
            allow_unindexed_review_verdict_authority
        ),
        pending_authority_path=pending_authority_path,
    )
    if judged_scope is None:
        raise ValidationError(
            "reviewed Verdict basis requires the exact judged_scope"
        )
    _validate_scope_against_review(judged_scope, packet["review_scope"])
