from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from .formalization import artifact_ready, collect_formalization_debt, load_policy
from .hashing import atomic_write_bytes, load_json, sha256_file
from .models import StudyPaths, ValidationError, WorkflowError
from .validation import (
    brief_approval_issues,
    evidence_index,
    parse_brief_metadata,
    run_index,
    validate_study,
)


def _generic_formal_artifact_active(path: Path) -> bool:
    if path.suffix.lower() == ".json":
        try:
            value = load_json(path)
        except ValidationError:
            return False
        return isinstance(value, dict) and value.get("status") in {"active", "finalized"}
    if path.suffix.lower() == ".md":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        return (
            re.search(
                r"^\s*status\s*:\s*(?:active|finalized)\s*$",
                text,
                flags=re.IGNORECASE | re.MULTILINE,
            )
            is not None
        )
    return False


def active_formal_artifacts(paths: StudyPaths) -> list[dict[str, Any]]:
    policy = load_policy(paths)
    known = {
        (paths.study / relative).resolve(): kind
        for kind, relative in policy["formal_artifacts"].items()
    }
    records: list[dict[str, Any]] = []
    if not paths.formal.is_dir():
        return records
    for path in sorted(paths.formal.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(paths.formal)
        if relative.parts and relative.parts[0] == "changeset-history":
            # Superseded CHANGESET records remain historical provenance, not
            # active scientific/formal context.
            continue
        kind = known.get(path.resolve())
        if kind is None:
            kind = relative.with_suffix("").as_posix().upper()
        records.append(
            {
                "kind": kind,
                "path": path.relative_to(paths.root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "active": (
                    artifact_ready(paths, kind)
                    if path.resolve() in known
                    else _generic_formal_artifact_active(path)
                ),
            }
        )
    return records


def _evidence_ref_key(ref: dict[str, Any]) -> tuple[str, int]:
    return str(ref.get("evidence_id")), int(ref.get("version", 0))


def _load_claims(paths: StudyPaths) -> dict[str, Any]:
    try:
        value = load_json(paths.claims)
    except ValidationError:
        return {
            "claims": [],
            "frontier": {
                "summary": None,
                "claim_ids": [],
                "open_questions": [],
                "next_actions": [],
                "human_decisions_required": [],
            },
            "formalization_debt": [],
        }
    if not isinstance(value, dict):
        return {"claims": [], "frontier": {}, "formalization_debt": []}
    if not isinstance(value.get("claims"), list):
        value["claims"] = []
    if not isinstance(value.get("frontier"), dict):
        value["frontier"] = {}
    if not isinstance(value.get("formalization_debt"), list):
        value["formalization_debt"] = []
    return value


def _latest_checkpoint(paths: StudyPaths) -> dict[str, Any] | None:
    files = sorted(paths.checkpoints.glob("CHECKPOINT-*.json")) if paths.checkpoints.is_dir() else []
    if not files:
        return None
    value = load_json(files[-1])
    return value if isinstance(value, dict) else None


def _budget_state(paths: StudyPaths, runs: dict[str, tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    gpu = sum(float(item.get("budget", {}).get("estimated_gpu_hours", 0.0)) for _, item in runs.values())
    cpu = sum(float(item.get("budget", {}).get("estimated_cpu_hours", 0.0)) for _, item in runs.values())
    hard_budget: dict[str, Any] = {}
    if paths.brief.is_file():
        try:
            metadata = parse_brief_metadata(paths.brief.read_text(encoding="utf-8"))
            hard_budget = metadata.get("hard_budget", {})
        except (OSError, ValidationError):
            pass
    return {
        "estimated_gpu_hours_recorded": gpu,
        "estimated_cpu_hours_recorded": cpu,
        "hard_budget": hard_budget,
    }


def _append_items(lines: list[str], items: Iterable[str], empty: str = "None recorded.") -> None:
    material = [str(item) for item in items]
    if not material:
        lines.append(empty)
        return
    for item in material:
        lines.append(f"- {item}")


def render_status(paths: StudyPaths) -> Path:
    validation_issues = validate_study(paths)
    authority_errors = [item for item in validation_issues if item.level == "ERROR"]
    authority_warnings = [item for item in validation_issues if item.level == "WARNING"]
    claims_data = _load_claims(paths)
    claims = claims_data.get("claims", [])
    frontier = claims_data.get("frontier", {})
    evidence_load_error: str | None = None
    try:
        evidence = evidence_index(paths)
    except ValidationError as exc:
        evidence = {}
        evidence_load_error = str(exc)
    run_load_error: str | None = None
    try:
        runs = run_index(paths)
    except ValidationError as exc:
        runs = {}
        run_load_error = str(exc)
    approval_problems = brief_approval_issues(paths)
    approval_valid = not any(issue.level == "ERROR" for issue in approval_problems)
    awaiting_initial_approval = (
        not paths.brief_approval.exists()
        and len(authority_errors) == 1
        and authority_errors[0].path == str(paths.brief_approval)
        and authority_errors[0].message == "Brief has not been approved"
    )
    brief_hash = sha256_file(paths.brief) if paths.brief.is_file() else None

    supporting_keys: list[tuple[str, int]] = []
    contradictory_keys: list[tuple[str, int]] = []
    scientific_chain_valid = not any(
        str(item.path).startswith(str(paths.claims))
        or f"{paths.study / 'evidence'}/" in str(item.path)
        or f"{paths.study / 'runs'}/" in str(item.path)
        for item in authority_errors
    ) and evidence_load_error is None and run_load_error is None
    if scientific_chain_valid:
        for claim in claims:
            supporting_keys.extend(_evidence_ref_key(ref) for ref in claim.get("supporting_evidence", []))
            contradictory_keys.extend(_evidence_ref_key(ref) for ref in claim.get("contradictory_evidence", []))
    supporting_keys = list(dict.fromkeys(supporting_keys))
    contradictory_keys = list(dict.fromkeys(contradictory_keys))

    debt = collect_formalization_debt(paths)
    budget = _budget_state(paths, runs)
    checkpoint = _latest_checkpoint(paths)
    try:
        from .workspace import evaluate_changes, repository_profile_path

        change_scope = evaluate_changes(paths)
        profile_path = repository_profile_path(paths.root).relative_to(paths.root).as_posix()
        change_scope_error = None
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        change_scope = {
            "outcome": "INVALID",
            "changed_paths": [],
            "violations": [],
            "advisories": [],
        }
        profile_path = "scientific-workflow/repository-profile.json"
        change_scope_error = str(exc)
    lines = [
        f"# Study Status: {paths.study_id}",
        "",
        "> Generated deterministically from authoritative Study files. This file is a projection, not a source of truth.",
        "",
        "## Approved Brief",
        "",
        f"- Path: `{paths.brief.relative_to(paths.root).as_posix()}`",
        f"- SHA-256: `{brief_hash or 'unavailable'}`",
        f"- Approval: **{'current' if approval_valid else 'missing or stale'}**",
    ]
    if approval_problems:
        for issue in approval_problems:
            lines.append(f"- {issue.message}")

    lines.extend(["", "## Authority Validation", ""])
    if awaiting_initial_approval and evidence_load_error is None and run_load_error is None:
        lines.append(
            "**DRAFT — structurally valid, awaiting human Brief approval. "
            "Scientific research and execution are not yet authorized.**"
        )
        for issue in authority_warnings:
            lines.append(f"- {issue.render()}")
    elif authority_errors or evidence_load_error or run_load_error:
        lines.append("**INVALID — scientific Evidence/Claim summaries below are not trusted where their source chain failed.**")
        for issue in authority_errors:
            lines.append(f"- {issue.render()}")
        for issue in authority_warnings:
            lines.append(f"- {issue.render()}")
        if evidence_load_error:
            lines.append(f"- Evidence index error: {evidence_load_error}")
        if run_load_error:
            lines.append(f"- Run index error: {run_load_error}")
    elif authority_warnings:
        lines.append(
            "**PASS WITH WARNINGS — authoritative records are structurally valid, "
            "but reproducibility or availability needs attention.**"
        )
        for issue in authority_warnings:
            lines.append(f"- {issue.render()}")
    else:
        lines.append("Authoritative records and references pass deterministic validation.")

    lines.extend(
        [
            "",
            "## Repository Adaptation and Change Scope",
            "",
            f"- Profile: `{profile_path}`",
            f"- Current scope outcome: **{change_scope['outcome']}**",
        ]
    )
    changeset = change_scope.get("changeset")
    lines.append(
        f"- Active CHANGESET: `{changeset['path']}`"
        if isinstance(changeset, dict)
        else "- Active CHANGESET: none"
    )
    validation_proof = change_scope.get("validation")
    lines.append(
        f"- Validation proof: `{validation_proof['path']}` "
        f"(passed={validation_proof.get('passed')})"
        if isinstance(validation_proof, dict)
        else "- Validation proof: none"
    )
    if change_scope_error:
        lines.append(f"- Profile/scope error: {change_scope_error}")
    for record in change_scope.get("changed_paths", []):
        states = ", ".join(record.get("states", [])) or "unknown"
        lines.append(
            f"- `{record.get('classification')}` / {states}: `{record.get('path')}`"
        )
    for violation in change_scope.get("violations", []):
        target = violation.get("path") or "<repository>"
        lines.append(
            f"- BLOCKED `{violation.get('rule')}` at `{target}`: {violation.get('reason')}"
        )
    for advisory in change_scope.get("advisories", []):
        lines.append(f"- Advisory: {advisory}")

    lines.extend(["", "## Current Claims", ""])
    if claims:
        lines.extend(["| Claim | State | Statement |", "|---|---|---|"])
        for claim in claims:
            statement = str(claim.get("statement", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{claim.get('claim_id')}` | `{claim.get('state')}` | {statement} |")
    else:
        lines.append("No Claims recorded.")

    lines.extend(["", "## Current Frontier", ""])
    lines.append(str(frontier.get("summary") or "No Frontier summary recorded."))
    if frontier.get("claim_ids"):
        lines.append("")
        lines.append("Active Claim IDs: " + ", ".join(f"`{item}`" for item in frontier["claim_ids"]))

    lines.extend(["", "## Recent Decisive Evidence", ""])
    decisive_rows: list[str] = []
    for key in supporting_keys[-10:]:
        record = evidence.get(key)
        if record:
            _, item = record
            decisive_rows.append(f"`{key[0]}` v{key[1]} — {item.get('assessment')} — scope: {item.get('scope')}")
    _append_items(lines, decisive_rows)

    lines.extend(["", "## Contradictory Evidence", ""])
    contradictory_rows: list[str] = []
    for key in contradictory_keys:
        record = evidence.get(key)
        if record:
            _, item = record
            contradictory_rows.append(f"`{key[0]}` v{key[1]} — {item.get('assessment')} — scope: {item.get('scope')}")
    _append_items(lines, contradictory_rows)

    lines.extend(["", "## Open Questions", ""])
    _append_items(lines, frontier.get("open_questions", []))

    lines.extend(["", "## Formalization Debt", ""])
    _append_items(
        lines,
        [f"`{item.get('level')}` — {item.get('artifact')}: {item.get('reason')}" for item in debt],
    )

    lines.extend(
        [
            "",
            "## Budget",
            "",
            f"- Recorded estimated GPU hours: {budget['estimated_gpu_hours_recorded']:.6g}",
            f"- Recorded estimated CPU hours: {budget['estimated_cpu_hours_recorded']:.6g}",
            f"- Hard budget from Brief metadata: `{budget['hard_budget']}`",
            "",
            "## Latest Checkpoint",
            "",
        ]
    )
    if checkpoint:
        lines.append(f"- `{checkpoint.get('checkpoint_id')}` — `{checkpoint.get('checkpoint_sha256')}`")
    else:
        lines.append("No Checkpoint exists.")

    lines.extend(["", "## Human Attention Required", ""])
    attention: list[str] = list(frontier.get("human_decisions_required", []))
    if not approval_valid:
        attention.append("Approve or restore the active Brief.")
    if contradictory_keys:
        attention.append("Review preserved contradictory Evidence before interpreting Claims.")
    if any(item.get("level") in {"blocking_now", "required_before_review"} for item in debt):
        attention.append("Resolve blocking formalization debt before review.")
    if (authority_errors and not awaiting_initial_approval) or evidence_load_error or run_load_error:
        attention.append("Repair deterministic validation errors before relying on Claims or Evidence summaries.")
    if authority_warnings:
        attention.append("Review deterministic validation warnings before claiming reproducibility.")
    if change_scope.get("outcome") in {"BLOCKED", "INVALID"}:
        attention.append(
            "Resolve repository change-scope violations before recording an Evidence-eligible Run."
        )
    elif change_scope.get("outcome") == "ADVISORY":
        attention.append(
            "Git change scope is not fully verifiable; resulting Runs cannot enter formal Evidence."
        )
    _append_items(lines, attention)

    lines.extend(["", "## Next Actions", ""])
    _append_items(lines, frontier.get("next_actions", []))
    output = paths.generated / "STATUS.md"
    atomic_write_bytes(output, ("\n".join(lines) + "\n").encode("utf-8"))
    return output


REVIEW_SECTIONS = (
    ("requirement_coverage", "Requirement Coverage"),
    ("implementation_findings", "Implementation Findings"),
    ("protected_condition_findings", "Protected-condition Findings"),
    ("cohort_findings", "Cohort Findings"),
    ("reproducibility_findings", "Reproducibility Findings"),
    ("scientific_claim_findings", "Scientific-claim Findings"),
    ("contradictory_evidence_findings", "Contradictory-evidence Findings"),
    ("formalization_findings", "Formalization Findings"),
)


def _render_source(source: dict[str, Any]) -> str:
    fields = []
    for key in ("path", "symbol", "line", "commit", "run_id", "evidence_id", "claim_id", "checkpoint_id", "note"):
        if key in source:
            fields.append(f"{key}={source[key]}")
    return f"{source.get('kind')}: " + ", ".join(fields)


def render_review_markdown(review: dict[str, Any]) -> bytes:
    lines = [
        f"# Independent Scientific Review: {review.get('study_id')}",
        "",
        "> **Non-authoritative projection.** This Markdown is derived from structured review JSON. The human Verdict remains authoritative.",
        "",
        "## Summary",
        "",
        str(review.get("summary", "")),
    ]
    for key, title in REVIEW_SECTIONS:
        lines.extend(["", f"## {title}", ""])
        findings = review.get(key, [])
        if not findings:
            lines.append("No findings recorded in this category.")
            continue
        for finding in findings:
            lines.extend(
                [
                    f"### [{str(finding.get('severity', '')).upper()}] {finding.get('finding_id')}: {finding.get('title')}",
                    "",
                    str(finding.get("description", "")),
                    "",
                    f"Recommendation: {finding.get('recommendation', '')}",
                    "",
                    "Sources:",
                ]
            )
            for source in finding.get("sources", []):
                lines.append(f"- {_render_source(source)}")
    for key, title in (("open_questions", "Open Questions"), ("recommended_human_checks", "Recommended Human Checks")):
        lines.extend(["", f"## {title}", ""])
        _append_items(lines, review.get(key, []))
    return ("\n".join(lines) + "\n").encode("utf-8")
