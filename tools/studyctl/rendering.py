from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from .active_context import (
    active_claims,
    claim_lifecycle,
    compaction_pressure,
    refresh_active_projection,
    write_active_selector,
)
from .budget import (
    budget_projection,
    budget_totals_from_manifests,
    format_budget_violation,
    parse_brief_hard_budget,
)
from .formalization import artifact_ready, collect_formalization_debt, load_policy
from .hashing import atomic_write_bytes, load_json, sha256_file
from .models import CLAIMS_SCHEMA_VERSION, StudyPaths, ValidationError, WorkflowError
from .run_ledger import (
    bootstrap_or_reconcile_ledger,
    ledger_commitment_totals,
    ledger_path,
    load_ledger,
)
from .validation import (
    brief_approval_issues,
    effective_run_epistemic_mode,
    evidence_index,
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
        if relative.parts and relative.parts[0] in {
            "changeset-history",
            "confirmations",
        }:
            # Superseded CHANGESETs and finalized Confirmation registrations
            # remain historical provenance, not globally active formal context.
            # Confirmation bindings are reached through Runs and Evidence.
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


def _declared_evidence_mode(item: dict[str, Any]) -> str | None:
    basis = item.get("evidence_basis")
    if not isinstance(basis, dict):
        return None
    mode = basis.get("mode")
    return str(mode) if mode in {"exploratory", "confirmatory", "mixed"} else None


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
    """Project resource charges without hiding the authority used.

    Current Studies charge from the durable Run ledger.  In particular, a
    missing Run directory must make validation fail without making already
    reserved/consumed budget disappear from STATUS.  Pre-ledger V1/V2 history
    may still be displayed from immutable Manifests, but that lower-assurance
    fallback is explicitly labelled and is never presented as ledger-backed.
    """

    committed: dict[str, float] | None = None
    authority = "unavailable"
    authority_path = ledger_path(paths).relative_to(paths.root).as_posix()
    authority_error: str | None = None
    authority_warning: str | None = None
    try:
        ledger = load_ledger(paths)
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        authority_error = f"durable Run ledger is invalid: {exc}"
    else:
        if ledger is not None:
            committed = ledger_commitment_totals(ledger)
            authority = "durable Run ledger (authoritative)"
            try:
                reconciled = bootstrap_or_reconcile_ledger(
                    paths,
                    runs,
                    write=False,
                )
                if reconciled != ledger:
                    # A terminal Manifest may have reached durable storage
                    # immediately before the corresponding ledger update.
                    # Show the conservative reconciled charge while keeping
                    # STATUS invalid until that transition is persisted.
                    committed = ledger_commitment_totals(reconciled)
                    authority_error = (
                        "durable Run ledger is stale relative to visible immutable "
                        "Manifests"
                    )
            except (ValidationError, WorkflowError, OSError, ValueError) as exc:
                authority_error = str(exc)
        elif runs and all(
            manifest.get("schema_version") in {1, 2}
            for _, manifest in runs.values()
        ):
            committed = budget_totals_from_manifests(
                item for _, item in runs.values()
            )
            authority = "legacy Manifest fallback (unindexed, lower assurance)"
            authority_warning = (
                "durable Run ledger is absent; migrate the intact legacy Run "
                "history before treating budget history as authoritative"
            )
        else:
            authority_error = (
                "durable Run ledger is missing; resource charges are unavailable"
            )

    hard_budget: dict[str, Any] = {}
    projection: dict[str, Any] | None = None
    if paths.brief.is_file():
        try:
            hard_budget = parse_brief_hard_budget(
                paths.brief.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            message = f"visible Brief hard budget is invalid: {exc}"
            authority_error = (
                f"{authority_error}; {message}" if authority_error else message
            )
    if committed is not None and hard_budget:
        projection = budget_projection(
            hard_budget,
            committed,
            {"gpu_hours": 0.0, "cpu_hours": 0.0, "storage_gb": 0.0},
        )
        if projection["violations"]:
            message = "current approved hard budget is already exceeded: " + "; ".join(
                format_budget_violation(item)
                for item in projection["violations"]
            )
            authority_error = (
                f"{authority_error}; {message}" if authority_error else message
            )
    return {
        "estimated_gpu_hours_recorded": (
            committed["gpu_hours"] if committed is not None else None
        ),
        "estimated_cpu_hours_recorded": (
            committed["cpu_hours"] if committed is not None else None
        ),
        "charged_storage_gb_recorded": (
            committed["storage_gb"] if committed is not None else None
        ),
        "hard_budget": hard_budget,
        "projection": projection,
        "violations": projection["violations"] if projection else [],
        "authority": authority,
        "authority_path": authority_path,
        "authority_error": authority_error,
        "authority_warning": authority_warning,
    }


def _format_budget_charge(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unavailable"
    return f"{float(value):.6g}"


def _append_items(lines: list[str], items: Iterable[str], empty: str = "None recorded.") -> None:
    material = [str(item) for item in items]
    if not material:
        lines.append(empty)
        return
    for item in material:
        lines.append(f"- {item}")


def _render_unsupported_claims_status(
    paths: StudyPaths,
    claims_data: dict[str, Any],
    validation_issues: list[Any],
) -> Path:
    """Render a bounded fail-closed notice for any unsupported Claims version."""

    error_count = sum(item.level == "ERROR" for item in validation_issues)
    warning_count = sum(item.level == "WARNING" for item in validation_issues)
    lines = [
        f"# Study Status: {paths.study_id}",
        "",
        "## Unsupported Claims State",
        "",
        f"- Observed Claims schema version: `{claims_data.get('schema_version')!r}`",
        f"- Required current schema version: `{CLAIMS_SCHEMA_VERSION}`",
        f"- Authoritative Claims path: `{paths.claims.relative_to(paths.root).as_posix()}`",
        f"- Authoritative Claims bytes: {paths.claims.stat().st_size}",
        f"- Authoritative Claims SHA-256: `{sha256_file(paths.claims)}`",
        f"- Validation summary: {error_count} error(s), {warning_count} warning(s)",
        "",
        "CLAIMS.json uses an unsupported schema_version. Status rendering fails "
        "closed without projecting Claim content; repair the authoritative file "
        "to the canonical current schema before resuming active operations.",
    ]
    output = paths.generated / "STATUS.md"
    atomic_write_bytes(output, ("\n".join(lines) + "\n").encode("utf-8"))
    return output


def render_status(paths: StudyPaths) -> Path:
    validation_issues = validate_study(paths)
    authority_errors = [item for item in validation_issues if item.level == "ERROR"]
    authority_warnings = [item for item in validation_issues if item.level == "WARNING"]
    claims_data = _load_claims(paths)
    if claims_data.get("schema_version") != CLAIMS_SCHEMA_VERSION:
        return _render_unsupported_claims_status(
            paths,
            claims_data,
            validation_issues,
        )
    all_claims = claims_data.get("claims", [])
    claims = active_claims(claims_data)
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
    other_keys: list[tuple[str, int]] = []
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
            other_keys.extend(
                _evidence_ref_key(ref) for ref in claim.get("other_evidence", [])
            )
    supporting_keys = list(dict.fromkeys(supporting_keys))
    contradictory_keys = list(dict.fromkeys(contradictory_keys))
    other_keys = list(dict.fromkeys(other_keys))

    debt = collect_formalization_debt(paths)
    budget = _budget_state(paths, runs)
    checkpoint = _latest_checkpoint(paths)
    pressure_error: str | None = None
    try:
        pressure = compaction_pressure(
            paths,
            claims_data=claims_data,
            runs=runs,
            evidence=evidence,
        )
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        pressure = {
            "level": "invalid",
            "compaction_due": False,
            "growth_blocked": False,
            "metrics": [],
            "reasons": [],
        }
        pressure_error = str(exc)
    selector_error: str | None = None
    confirmation_projection: dict[str, Any] | None = None
    try:
        if pressure_error is None:
            selector_path, compaction_due_path = refresh_active_projection(
                paths,
                claims_data=claims_data,
                pressure=pressure,
            )
        else:
            selector_path = write_active_selector(paths, claims_data=claims_data)
            compaction_due_path = paths.generated / "COMPACTION_DUE.json"
        selector_data = load_json(selector_path)
        raw_confirmations = (
            selector_data.get("confirmations")
            if isinstance(selector_data, dict)
            else None
        )
        if not isinstance(raw_confirmations, dict):
            raise ValidationError(
                "ACTIVE_CONTEXT.json lacks the bounded Confirmation index"
            )
        confirmation_projection = raw_confirmations
    except (ValidationError, WorkflowError, OSError, ValueError) as exc:
        selector_path = paths.generated / "ACTIVE_CONTEXT.json"
        compaction_due_path = paths.generated / "COMPACTION_DUE.json"
        selector_error = str(exc)
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
    elif (
        authority_errors
        or evidence_load_error
        or run_load_error
        or budget["authority_error"]
    ):
        lines.append("**INVALID — scientific Evidence/Claim summaries below are not trusted where their source chain failed.**")
        for issue in authority_errors:
            lines.append(f"- {issue.render()}")
        for issue in authority_warnings:
            lines.append(f"- {issue.render()}")
        if evidence_load_error:
            lines.append(f"- Evidence index error: {evidence_load_error}")
        if run_load_error:
            lines.append(f"- Run index error: {run_load_error}")
        if budget["authority_error"]:
            lines.append(f"- Budget authority error: {budget['authority_error']}")
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
        lines.extend(
            [
                "| Claim | Lifecycle | State | Evidence basis | Statement |",
                "|---|---|---|---|---|",
            ]
        )
        for claim in claims:
            statement = str(claim.get("statement", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| `{claim.get('claim_id')}` | "
                f"`{claim_lifecycle(claim)}` | "
                f"`{claim.get('state')}` | "
                f"`{claim.get('evidence_basis')}` | {statement} |"
            )
    else:
        lines.append("No Frontier-selected Claims recorded.")
    lines.append(
        f"- Frontier-selected Claims: {len(claims)}; "
        f"non-Frontier Claims retained by authority: {max(len(all_claims) - len(claims), 0)}"
    )

    lines.extend(["", "## Current Frontier", ""])
    lines.append(str(frontier.get("summary") or "No Frontier summary recorded."))
    if frontier.get("claim_ids"):
        lines.append("")
        lines.append("Active Claim IDs: " + ", ".join(f"`{item}`" for item in frontier["claim_ids"]))

    epistemic_counts: dict[str, int] = {"exploratory": 0, "confirmatory": 0}
    for _, manifest in runs.values():
        mode = effective_run_epistemic_mode(manifest)
        epistemic_counts[mode] = epistemic_counts.get(mode, 0) + 1
    lines.extend(
        [
            "",
            "## Run Epistemic Roles",
            "",
            f"- Exploratory Runs: {epistemic_counts['exploratory']}",
            f"- Confirmatory Runs: {epistemic_counts['confirmatory']}",
            "- V1–V3 Runs are conservatively counted as exploratory.",
        ]
    )

    lines.extend(["", "## Pending Confirmation Work", ""])
    if confirmation_projection is None:
        lines.append("Confirmation locator index is unavailable because active context is invalid.")
    else:
        drafts = confirmation_projection["drafts"]
        pending_confirmations = confirmation_projection["pending_finalized"]
        in_progress_confirmations = confirmation_projection["in_progress"]
        awaiting_evidence = confirmation_projection["awaiting_evidence"]
        history = confirmation_projection["history"]
        lines.extend(
            [
                f"- Editable Confirmation drafts: {drafts['total_count']}",
                f"- Finalized Confirmation records: {history['total_count']}",
                f"- Finalized records with pending slots: {history['pending_count']}",
                f"- Finalized records with running slots: {history['in_progress_count']}",
                "- Finalized records awaiting Evidence: "
                f"{history['awaiting_evidence_count']}",
                f"- Finalized records represented in Evidence: {history['completed_count']}",
                f"- Full-history inventory SHA-256: `{history['inventory_sha256']}`",
            ]
        )
        for draft in drafts["items"]:
            lines.append(
                f"- Draft `{draft['confirmation_id']}`: `{draft['path']}` "
                f"(SHA-256 `{draft['sha256']}`)"
            )
        if drafts["truncated"]:
            lines.append(
                f"- Draft locator list truncated to {drafts['selected_count']} of "
                f"{drafts['total_count']}; inventory SHA-256 "
                f"`{drafts['inventory_sha256']}`"
            )
        for record in pending_confirmations["items"]:
            slots = record["pending_slot_ids"]
            visible_slots = ", ".join(f"`{item}`" for item in slots["items"])
            lines.append(
                f"- `{record['confirmation_id']}`: {record['pending_slot_count']} "
                f"pending slot(s) at `{record['path']}`; visible slots: "
                f"{visible_slots or 'none'}"
            )
            if slots["truncated"]:
                lines.append(
                    f"  - Slot locator list truncated to {slots['selected_count']} of "
                    f"{slots['total_count']}; inventory SHA-256 "
                    f"`{slots['inventory_sha256']}`"
                )
        if pending_confirmations["truncated"]:
            lines.append(
                "- Pending Confirmation locator list truncated to "
                f"{pending_confirmations['selected_count']} of "
                f"{pending_confirmations['total_count']}; inventory SHA-256 "
                f"`{pending_confirmations['inventory_sha256']}`"
            )
        for record in in_progress_confirmations["items"]:
            running_slots = record["running_slots"]
            visible = ", ".join(
                f"`{item['slot_id']}` as `{item['run_id']}` at `{item['path']}`"
                for item in running_slots["items"]
            )
            lines.append(
                f"- `{record['confirmation_id']}` has "
                f"{record['running_slot_count']} running slot(s): "
                f"{visible or 'none visible'}; do not start a replacement Run."
            )
            if running_slots["truncated"]:
                lines.append(
                    "  - Running-slot locator list truncated to "
                    f"{running_slots['selected_count']} of "
                    f"{running_slots['total_count']}; inventory SHA-256 "
                    f"`{running_slots['inventory_sha256']}`"
                )
        for record in awaiting_evidence["items"]:
            drafts_for_record = record["evidence_drafts"]
            if drafts_for_record["total_count"]:
                visible = ", ".join(
                    f"`{item['evidence_id']}` v{item['version']} at `{item['path']}`"
                    for item in drafts_for_record["items"]
                )
                lines.append(
                    f"- `{record['confirmation_id']}` has confirmatory Evidence "
                    f"draft(s) to resume: {visible}."
                )
            else:
                lines.append(
                    f"- `{record['confirmation_id']}` consumed all planned slots and "
                    "now awaits a confirmatory Evidence draft."
                )
        if in_progress_confirmations["truncated"]:
            lines.append(
                "- In-progress Confirmation locator list is truncated; inventory "
                f"SHA-256 `{in_progress_confirmations['inventory_sha256']}`"
            )
        if awaiting_evidence["truncated"]:
            lines.append(
                "- Awaiting-Evidence Confirmation locator list is truncated; "
                f"inventory SHA-256 `{awaiting_evidence['inventory_sha256']}`"
            )
        if not pending_confirmations["items"]:
            lines.append("No finalized Confirmation has an unconsumed Run slot.")

    lines.extend(["", "## Recent Decisive Evidence", ""])
    decisive_rows: list[str] = []
    for key in supporting_keys[-10:]:
        record = evidence.get(key)
        if record:
            source, item = record
            basis = _declared_evidence_mode(item)
            decisive_rows.append(
                f"`{key[0]}` v{key[1]} — `{item.get('assessment')}` / `{basis}` — "
                f"source: `{source.relative_to(paths.root).as_posix()}` — "
                f"SHA-256: `{sha256_file(source)}`"
            )
    _append_items(lines, decisive_rows)

    lines.extend(["", "## Contradictory Evidence", ""])
    contradictory_rows: list[str] = []
    for key in contradictory_keys[-10:]:
        record = evidence.get(key)
        if record:
            source, item = record
            basis = _declared_evidence_mode(item)
            contradictory_rows.append(
                f"`{key[0]}` v{key[1]} — `{item.get('assessment')}` / `{basis}` — "
                f"source: `{source.relative_to(paths.root).as_posix()}` — "
                f"SHA-256: `{sha256_file(source)}`"
            )
    _append_items(lines, contradictory_rows)

    lines.extend(["", "## Other Active Evidence", ""])
    other_rows: list[str] = []
    for key in other_keys[-10:]:
        record = evidence.get(key)
        if record:
            source, item = record
            basis = _declared_evidence_mode(item)
            other_rows.append(
                f"`{key[0]}` v{key[1]} — `{item.get('assessment')}` / `{basis}` — "
                f"source: `{source.relative_to(paths.root).as_posix()}` — "
                f"SHA-256: `{sha256_file(source)}`"
            )
    _append_items(lines, other_rows)

    lines.extend(["", "## Open Questions", ""])
    _append_items(lines, frontier.get("open_questions", []))

    lines.extend(["", "## Formalization Debt", ""])
    _append_items(
        lines,
        [f"`{item.get('level')}` — {item.get('artifact')}: {item.get('reason')}" for item in debt],
    )

    lines.extend(["", "## Active Context and Compaction Pressure", ""])
    if selector_error:
        lines.append(f"- Active selector: **INVALID** — {selector_error}")
    else:
        lines.extend(
            [
                f"- Active selector: `{selector_path.relative_to(paths.root).as_posix()}`",
                f"- Active selector bytes: {selector_path.stat().st_size}",
                f"- Compaction advisory: `{compaction_due_path.relative_to(paths.root).as_posix()}`",
            ]
        )
    if pressure_error:
        lines.append(f"- Pressure state: **INVALID** — {pressure_error}")
    else:
        lines.append(f"- Pressure level: **{str(pressure['level']).upper()}**")
        lines.append(
            "- Growth gate: **BLOCKED** for the next Run, new Evidence, and review"
            if pressure["growth_blocked"]
            else "- Growth gate: open"
        )
        lines.extend(
            [
                "",
                "| Metric | Observed | Soft | Hard | Level |",
                "|---|---:|---:|---:|---|",
            ]
        )
        for metric in pressure["metrics"]:
            lines.append(
                f"| `{metric['name']}` | {metric['observed']} | "
                f"{metric['soft']} | {metric['hard']} | `{metric['level']}` |"
            )
        if pressure["reasons"]:
            lines.append("")
            _append_items(lines, pressure["reasons"])

    lines.extend(
        [
            "",
            "## Budget",
            "",
            f"- Charge authority: **{budget['authority']}**",
            f"- Authority path: `{budget['authority_path']}`",
            "- Recorded estimated GPU hours: "
            f"{_format_budget_charge(budget['estimated_gpu_hours_recorded'])}",
            "- Recorded estimated CPU hours: "
            f"{_format_budget_charge(budget['estimated_cpu_hours_recorded'])}",
            "- Recorded charged storage (decimal GB): "
            f"{_format_budget_charge(budget['charged_storage_gb_recorded'])}",
            f"- Hard budget from visible Brief block: `{budget['hard_budget']}`",
            (
                f"- Authority validation: **INVALID** — {budget['authority_error']}"
                if budget["authority_error"]
                else (
                    "- Authority validation: **LEGACY FALLBACK** — "
                    f"{budget['authority_warning']}"
                    if budget["authority_warning"]
                    else "- Authority validation: current and internally consistent"
                )
            ),
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
    if budget["authority_error"]:
        attention.append(
            "Resolve the hard-budget or Run-ledger authority error before further execution."
        )
    if authority_warnings:
        attention.append("Review deterministic validation warnings before claiming reproducibility.")
    if pressure_error:
        attention.append("Repair the compaction-pressure policy or source state before further growth.")
    elif pressure["growth_blocked"]:
        attention.append(
            "Compaction pressure is HARD: perform semantic compaction "
            "before the next Run, new Evidence, or scientific review."
        )
    elif pressure["compaction_due"]:
        attention.append(
            "Compaction pressure is SOFT: plan semantic compaction before a hard growth gate."
        )
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
