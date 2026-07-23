from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, TextIO

from .budget import format_brief_hard_budget_block, normalize_hard_budget
from .git_state import git_state, reviewer_identity
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    load_json,
    load_json_bytes,
    record_digest,
    sha256_file,
    sha256_json,
)
from .locking import serialized_study_authority
from .models import (
    HUMAN_SCIENTIFIC_VERDICTS,
    SCHEMA_VERSION,
    HumanGateError,
    StudyPaths,
    ValidationError,
    WorkflowError,
    require_id,
    utc_now,
)
from .validation import (
    REQUIRED_BRIEF_HEADINGS,
    brief_approval_issues,
    brief_content_issues,
    checkpoint_paths,
    evidence_index,
    object_schema_issues,
    parse_brief_metadata,
    protected_artifact_snapshot,
)


_BRIEF_VERSION_LINE = re.compile(r"^Brief version:\s*([0-9]+)\s*$", re.MULTILINE)
_BRIEF_METADATA_BLOCK = re.compile(
    r"<!--\s*STUDYCTL-METADATA-BEGIN\s*(\{.*?\})\s*STUDYCTL-METADATA-END\s*-->",
    re.DOTALL,
)
_REPLACEMENT_PLACEHOLDER = re.compile(r"\[REPLACE(?:[^\]]*)\]")
_VERDICT_VERSION = re.compile(r"^VERDICT\.v([0-9]+)\.json$")
_VERDICT_ID = re.compile(r"^VERDICT-([0-9]+)$")
_IMPLEMENTATION_VERDICTS = {"accepted", "rejected", "requires_changes"}
_RESOURCE_BUDGET_SECTION = re.compile(
    r"^##\s+Resource Budget\s*$.*?(?=^##\s+|<!--\s*STUDYCTL-METADATA-BEGIN|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _require_human_tty(stdin: TextIO, stdout: TextIO) -> None:
    try:
        interactive = bool(stdin.isatty()) and bool(stdout.isatty())
    except (AttributeError, OSError):
        interactive = False
    if not interactive:
        raise HumanGateError("this human confirmation command requires an interactive TTY")


def _confirmation(stdin: TextIO, stdout: TextIO, phrase: str) -> None:
    print("Type exactly the following confirmation phrase:", file=stdout)
    print(phrase, file=stdout)
    stdout.flush()
    typed = stdin.readline().rstrip("\r\n")
    if typed != phrase:
        raise HumanGateError("confirmation did not exactly match the required phrase")


def _raise_validation_issues(label: str, issues: list[Any]) -> None:
    errors = [issue for issue in issues if issue.level == "ERROR"]
    if errors:
        rendered = "\n".join(issue.render() for issue in errors)
        raise ValidationError(f"{label}:\n{rendered}")


def _brief_relative_path(paths: StudyPaths) -> str:
    return paths.brief.relative_to(paths.root).as_posix()


def _validate_approvable_brief(paths: StudyPaths) -> tuple[str, int]:
    _raise_validation_issues("Brief is not approvable", brief_content_issues(paths))
    if paths.brief.is_symlink() or not paths.brief.is_file():
        raise ValidationError("Brief must be a regular, non-symbolic-link file")
    text = paths.brief.read_text(encoding="utf-8")
    if _REPLACEMENT_PLACEHOLDER.search(text) or re.search(r"\{\{[^{}]+\}\}", text):
        raise ValidationError("Brief still contains a replacement placeholder")
    metadata = parse_brief_metadata(text)
    version = metadata["brief_version"]
    version_match = _BRIEF_VERSION_LINE.search(text)
    if version_match is None:
        raise ValidationError("Brief is missing its visible 'Brief version' line")
    if int(version_match.group(1)) != version:
        raise ValidationError("visible Brief version does not match Brief metadata")
    return text, version


def _load_existing_approval(paths: StudyPaths) -> dict[str, Any]:
    if paths.brief_approval.is_symlink():
        raise ValidationError("BRIEF.approval.json must not be a symbolic link")
    approval = load_json(paths.brief_approval)
    if not isinstance(approval, dict):
        raise ValidationError("BRIEF.approval.json must contain a JSON object")
    _raise_validation_issues(
        "existing Brief approval is invalid",
        object_schema_issues(paths.root, "brief_approval", paths.brief_approval, approval),
    )
    if approval.get("study_id") != paths.study_id:
        raise ValidationError("existing Brief approval has the wrong study_id")
    if approval.get("approval_sha256") != record_digest(approval, "approval_sha256"):
        raise ValidationError("existing Brief approval has an invalid approval_sha256")
    return approval


def _next_reapproval_archive(paths: StudyPaths, brief_version: int) -> Path:
    history = paths.study / "brief-history"
    prefix = f"BRIEF.approval.v{brief_version:04d}.r"
    highest = 0
    if history.is_dir():
        for path in history.glob(f"{prefix}*.json"):
            match = re.fullmatch(rf"{re.escape(prefix)}([0-9]+)\.json", path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return history / f"{prefix}{highest + 1:04d}.json"


@serialized_study_authority
def approve_brief(
    paths: StudyPaths,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> Path:
    """Create a procedural human approval for the exact active Brief state."""

    _require_human_tty(stdin, stdout)
    _, brief_version = _validate_approvable_brief(paths)
    brief_hash = sha256_file(paths.brief)
    brief_path = _brief_relative_path(paths)
    protected = protected_artifact_snapshot(paths)
    if any(value is not None for value in protected.values()):
        from .formalization import artifact_ready

        if not artifact_ready(paths, "EVALUATOR"):
            raise ValidationError(
                "protected evaluator, data-split, or acceptance-criteria artifacts "
                "require an active formal/EVALUATOR.json before approval"
            )

    previous: dict[str, Any] | None = None
    previous_file_hash: str | None = None
    archive_path: Path | None = None
    if paths.brief_approval.exists():
        previous = _load_existing_approval(paths)
        previous_file_hash = sha256_file(paths.brief_approval)
        if previous.get("brief", {}).get("sha256") != brief_hash:
            raise WorkflowError(
                "refusing to approve a changed Brief while BRIEF.approval.json exists; "
                "start a new Brief version first"
            )
        if previous.get("brief", {}).get("path") != brief_path:
            raise ValidationError("existing Brief approval records the wrong Brief path")
        if previous.get("protected_artifacts") == protected:
            raise WorkflowError("the active Brief and protected artifacts are already approved")
        archive_path = _next_reapproval_archive(paths, brief_version)

    print(f"Study: {paths.study_id}", file=stdout)
    print(f"Brief path: {brief_path}", file=stdout)
    print(f"Brief SHA-256: {brief_hash}", file=stdout)
    if previous is not None:
        print("Protected-artifact snapshot changed since the previous approval.", file=stdout)
        print(json.dumps(protected, ensure_ascii=False, sort_keys=True), file=stdout)
    phrase = f"APPROVE {paths.study_id} {brief_hash}"
    _confirmation(stdin, stdout, phrase)

    # Do not bind a human confirmation to state other than the state displayed.
    if sha256_file(paths.brief) != brief_hash:
        raise WorkflowError("Brief changed during confirmation; approval was not recorded")
    if protected_artifact_snapshot(paths) != protected:
        raise WorkflowError("protected artifacts changed during confirmation; approval was not recorded")
    if previous_file_hash is not None:
        if not paths.brief_approval.is_file() or sha256_file(paths.brief_approval) != previous_file_hash:
            raise WorkflowError("existing Brief approval changed during confirmation")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "brief": {"path": brief_path, "sha256": brief_hash},
        "protected_artifacts": protected,
        "approved_at": utc_now(),
        "reviewer": reviewer_identity(paths.root),
        "repository": git_state(paths.root),
        "approval_sha256": "",
    }
    record["approval_sha256"] = record_digest(record, "approval_sha256")
    _raise_validation_issues(
        "generated Brief approval is invalid",
        object_schema_issues(paths.root, "brief_approval", paths.brief_approval, record),
    )

    if previous is None:
        atomic_write_json(paths.brief_approval, record, overwrite=False, mode=0o444)
    else:
        if archive_path is None:
            raise WorkflowError("internal error: missing Brief reapproval archive path")
        atomic_write_bytes(
            archive_path,
            paths.brief_approval.read_bytes(),
            overwrite=False,
            mode=0o444,
        )
        atomic_write_json(paths.brief_approval, record, overwrite=True, mode=0o444)
    print(f"Recorded immutable Brief approval: {paths.brief_approval}", file=stdout)
    return paths.brief_approval


def _legacy_approved_brief(
    paths: StudyPaths,
) -> tuple[str, int, dict[str, float | None]]:
    """Validate an approved pre-visible-budget Brief for one-way migration."""

    if paths.brief.is_symlink() or not paths.brief.is_file():
        raise ValidationError("Brief must be a regular, non-symbolic-link file")
    text = paths.brief.read_text(encoding="utf-8")
    if "STUDYCTL-HARD-BUDGET-BEGIN" in text or "STUDYCTL-HARD-BUDGET-END" in text:
        raise ValidationError("legacy Brief migration requires no visible budget block")
    if _REPLACEMENT_PLACEHOLDER.search(text) or re.search(r"\{\{[^{}]+\}\}", text):
        raise ValidationError("legacy Brief still contains a replacement placeholder")
    for heading in REQUIRED_BRIEF_HEADINGS:
        if re.search(
            rf"^##\s+{re.escape(heading)}\s*$", text, flags=re.MULTILINE
        ) is None:
            raise ValidationError(f"legacy Brief is missing heading: {heading}")
    if len(re.findall(r"^##\s+Resource Budget\s*$", text, re.MULTILINE)) != 1:
        raise ValidationError("legacy Brief must contain exactly one Resource Budget heading")
    metadata = parse_brief_metadata(text, allow_legacy_hard_budget=True)
    if "hard_budget" not in metadata:
        raise ValidationError("Brief is not a recognized legacy hard-budget format")
    limits = normalize_hard_budget(
        metadata["hard_budget"], label="legacy Brief hard_budget"
    )
    version = metadata["brief_version"]
    if version < 1:
        raise ValidationError("legacy Brief brief_version must be at least 1")
    version_match = _BRIEF_VERSION_LINE.search(text)
    if version_match is None or int(version_match.group(1)) != version:
        raise ValidationError("visible Brief version does not match legacy Brief metadata")
    # The exact legacy Brief must still be the approved byte sequence.  A
    # protected artifact may already have drifted; that must not deadlock the
    # one-way migration because opening the new draft revokes the approval.
    approval = _load_existing_approval(paths)
    approved_brief = approval.get("brief")
    if not isinstance(approved_brief, dict):
        raise ValidationError("legacy Brief approval has no Brief binding")
    if approved_brief.get("path") != _brief_relative_path(paths):
        raise ValidationError("legacy Brief approval records the wrong Brief path")
    if approved_brief.get("sha256") != sha256_file(paths.brief):
        raise ValidationError("legacy Brief changed after approval")
    return text, version, limits


def _new_brief_draft(
    text: str,
    current_version: int,
    *,
    legacy_budget: dict[str, float | None] | None = None,
) -> bytes:
    next_version = current_version + 1
    version_match = _BRIEF_VERSION_LINE.search(text)
    if version_match is None or int(version_match.group(1)) != current_version:
        raise ValidationError("visible Brief version does not match Brief metadata")
    metadata_match = _BRIEF_METADATA_BLOCK.search(text)
    if metadata_match is None:
        raise ValidationError("Brief is missing the STUDYCTL-METADATA block")
    metadata = parse_brief_metadata(
        text, allow_legacy_hard_budget=legacy_budget is not None
    )
    if legacy_budget is not None:
        metadata.pop("hard_budget", None)
    metadata["brief_version"] = next_version
    metadata_block = (
        "<!-- STUDYCTL-METADATA-BEGIN\n"
        + json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\nSTUDYCTL-METADATA-END -->"
    )
    revised = text[: metadata_match.start()] + metadata_block + text[metadata_match.end() :]
    if legacy_budget is not None:
        if _RESOURCE_BUDGET_SECTION.search(revised) is None:
            raise ValidationError("legacy Brief is missing its Resource Budget section")
        budget_section = (
            "## Resource Budget\n\n"
            "The JSON block below is the single machine-enforced source for "
            "lifetime Study hard limits. A numeric zero authorizes no positive "
            "use; `null` leaves positive use unauthorized until a new Brief "
            "version supplies a numeric limit. Storage uses decimal gigabytes "
            "(`1 GB = 10^9 bytes`).\n\n"
            + format_brief_hard_budget_block(legacy_budget)
            + "\n\n"
            "[REPLACE: Review the migrated hard limits and state advisory "
            "allocation or calendar guidance only.]\n\n"
        )
        revised = _RESOURCE_BUDGET_SECTION.sub(budget_section, revised, count=1)
    version_match = _BRIEF_VERSION_LINE.search(revised)
    if version_match is None:
        raise ValidationError("new Brief draft lost its visible version line")
    placeholder = (
        f"Brief version: {next_version}\n\n"
        f"[REPLACE: Review and update every affected section for Brief version {next_version}.]"
    )
    revised = revised[: version_match.start()] + placeholder + revised[version_match.end() :]
    if not revised.endswith("\n"):
        revised += "\n"
    return revised.encode("utf-8")


@serialized_study_authority
def begin_brief_revision(paths: StudyPaths) -> Path:
    """Archive a fresh approved Brief and open the next editable draft."""

    legacy_budget: dict[str, float | None] | None = None
    try:
        text, version = _validate_approvable_brief(paths)
    except ValidationError as current_error:
        try:
            text, version, legacy_budget = _legacy_approved_brief(paths)
        except (ValidationError, WorkflowError) as legacy_error:
            try:
                raw_text = paths.brief.read_text(encoding="utf-8")
                raw_metadata = parse_brief_metadata(
                    raw_text, allow_legacy_hard_budget=True
                )
            except (OSError, ValidationError):
                raise current_error
            if "hard_budget" in raw_metadata:
                raise legacy_error
            raise current_error
    else:
        _raise_validation_issues(
            "a new Brief version requires a fresh approved Brief",
            brief_approval_issues(paths),
        )
        _load_existing_approval(paths)

    history = paths.study / "brief-history"
    archived_brief = history / f"BRIEF.v{version:04d}.md"
    archived_approval = history / f"BRIEF.approval.v{version:04d}.json"
    old_brief = paths.brief.read_bytes()
    old_approval = paths.brief_approval.read_bytes()
    try:
        captured_text = old_brief.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("captured Brief is not valid UTF-8") from exc
    if captured_text != text:
        raise WorkflowError("Brief changed while opening a new version")
    approval = _load_existing_approval(paths)
    captured_approval = load_json_bytes(
        old_approval, label=str(paths.brief_approval)
    )
    if captured_approval != approval:
        raise WorkflowError("Brief approval changed while opening a new version")
    approved_brief = approval.get("brief")
    if not isinstance(approved_brief, dict) or (
        approved_brief.get("path") != _brief_relative_path(paths)
        or approved_brief.get("sha256")
        != hashlib.sha256(old_brief).hexdigest()
    ):
        raise ValidationError("captured Brief is not authorized by its approval")
    new_brief = _new_brief_draft(
        text, version, legacy_budget=legacy_budget
    )
    approved_brief_mode = paths.brief.stat().st_mode & 0o777
    draft_brief_mode = approved_brief_mode | 0o200

    def ensure_exact_archive(destination: Path, payload: bytes) -> None:
        if destination.exists() or destination.is_symlink():
            if (
                destination.is_symlink()
                or not destination.is_file()
                or destination.read_bytes() != payload
            ):
                raise WorkflowError(
                    f"Brief history conflicts with captured authority: {destination}"
                )
            return
        atomic_write_bytes(destination, payload, overwrite=False, mode=0o444)

    ensure_exact_archive(archived_brief, old_brief)
    ensure_exact_archive(archived_approval, old_approval)
    try:
        atomic_write_bytes(paths.brief, new_brief, overwrite=True, mode=draft_brief_mode)
        paths.brief_approval.unlink()
    except Exception:
        # Keep the active pair coherent if the local filesystem rejects either
        # half of the transition. Immutable history remains as recovery data.
        atomic_write_bytes(paths.brief, old_brief, overwrite=True, mode=approved_brief_mode)
        if not paths.brief_approval.exists():
            atomic_write_bytes(paths.brief_approval, old_approval, overwrite=False, mode=0o444)
        raise
    return paths.brief


def _contains_placeholder(value: str) -> bool:
    upper = value.upper()
    return "[REPLACE" in upper or "[FILLED BY STUDYCTL]" in upper or "{{" in value


def _validate_verdict_branches(verdict: dict[str, Any]) -> None:
    created_at = verdict.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip() or _contains_placeholder(created_at):
        raise ValidationError("Verdict created_at must be populated")

    implementation = verdict.get("implementation_verdict")
    if not isinstance(implementation, dict):
        raise ValidationError("implementation_verdict must be a separate object")
    if implementation.get("decision") not in _IMPLEMENTATION_VERDICTS:
        raise ValidationError("invalid implementation verdict decision")
    implementation_rationale = implementation.get("rationale")
    if not isinstance(implementation_rationale, str) or not implementation_rationale.strip():
        raise ValidationError("implementation verdict requires a non-empty rationale")
    if _contains_placeholder(implementation_rationale):
        raise ValidationError("implementation verdict rationale still contains a placeholder")
    for condition in implementation.get("conditions", []):
        if not condition.strip() or _contains_placeholder(condition):
            raise ValidationError("implementation verdict conditions must be completed")

    scientific = verdict.get("scientific_verdict")
    if not isinstance(scientific, dict):
        raise ValidationError("scientific_verdict must be a separate object")
    if scientific.get("decision") not in HUMAN_SCIENTIFIC_VERDICTS:
        raise ValidationError("invalid scientific verdict decision")
    for field in ("rationale", "scope"):
        value = scientific.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"scientific verdict requires a non-empty {field}")
        if _contains_placeholder(value):
            raise ValidationError(f"scientific verdict {field} still contains a placeholder")
    for condition in scientific.get("conditions", []):
        if not condition.strip() or _contains_placeholder(condition):
            raise ValidationError("scientific verdict conditions must be completed")

    reviewer = verdict.get("reviewer")
    if not isinstance(reviewer, dict):
        raise ValidationError("Verdict reviewer must be an object")
    for field in ("identity", "source"):
        value = reviewer.get(field)
        if not isinstance(value, str) or not value.strip() or _contains_placeholder(value):
            raise ValidationError(f"Verdict reviewer.{field} must be populated")

    authorization = verdict.get("authorization")
    if authorization is not None:
        if not isinstance(authorization, dict) or set(authorization) != {
            "mode",
            "source",
            "assurance",
            "instruction",
            "instruction_sha256",
        }:
            raise ValidationError("Verdict authorization has an invalid structure")
        if authorization.get("mode") != "agent_initiated":
            raise ValidationError("Verdict authorization mode must be agent_initiated")
        if authorization.get("source") != "explicit_user_instruction":
            raise ValidationError(
                "Agent-initiated Verdict authorization must come from an explicit user instruction"
            )
        if authorization.get("assurance") != "cooperative":
            raise ValidationError(
                "Agent-initiated Verdict authorization assurance must be cooperative"
            )
        instruction = authorization.get("instruction")
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or _contains_placeholder(instruction)
        ):
            raise ValidationError(
                "Agent-initiated Verdict authorization requires the explicit user instruction"
            )
        if authorization.get("instruction_sha256") != sha256_json(instruction):
            raise ValidationError("Verdict authorization instruction_sha256 does not match")


def _validate_verdict_claim_decision(verdict: dict[str, Any]) -> None:
    """Apply the Claim/scope epistemic invariant to every Verdict input path."""

    scope = verdict.get("judged_scope")
    scientific = verdict.get("scientific_verdict")
    if not isinstance(scope, dict) or not isinstance(scientific, dict):
        return
    claims = scope.get("claims")
    if isinstance(claims, list) and not claims and scientific.get("decision") != "requires_more_evidence":
        raise ValidationError(
            "a Verdict without selected Claims must use scientific decision "
            "requires_more_evidence"
        )


def _next_verdict_id(paths: StudyPaths) -> str:
    """Allocate a human-readable Verdict ID without asking the human to manage it."""

    highest = 0
    for path in sorted(paths.study.glob("VERDICT*.json")):
        try:
            existing = load_json(path)
        except ValidationError as exc:
            raise ValidationError(f"cannot allocate Verdict ID while {path} is invalid: {exc}") from exc
        if not isinstance(existing, dict):
            raise ValidationError(f"cannot allocate Verdict ID from non-object Verdict: {path}")
        raw_id = existing.get("verdict_id")
        match = _VERDICT_ID.fullmatch(str(raw_id))
        if match is None:
            raise ValidationError(f"cannot allocate Verdict ID from invalid identity {raw_id!r}: {path}")
        highest = max(highest, int(match.group(1)))
    return f"VERDICT-{highest + 1:04d}"


def _checkpoint_claims(paths: StudyPaths) -> list[dict[str, Any]]:
    latest = _latest_checkpoint(paths)
    if latest is None:
        return []
    claims = latest.get("claims_snapshot")
    if not isinstance(claims, list) or any(not isinstance(item, dict) for item in claims):
        raise ValidationError("latest Checkpoint has an invalid claims_snapshot")
    return claims


def _prompt_line(
    stdin: TextIO,
    stdout: TextIO,
    prompt: str,
    *,
    allow_empty: bool = False,
) -> str:
    print(prompt, end="", file=stdout)
    stdout.flush()
    value = stdin.readline().rstrip("\r\n").strip()
    if not value and not allow_empty:
        raise HumanGateError(f"a non-empty response is required for: {prompt.rstrip()}")
    return value


def _prompt_decision(
    stdin: TextIO,
    stdout: TextIO,
    label: str,
    allowed: set[str],
) -> str:
    rendered = "/".join(sorted(allowed))
    decision = _prompt_line(stdin, stdout, f"{label} decision [{rendered}]: ")
    if decision not in allowed:
        raise HumanGateError(f"invalid {label.lower()} decision: {decision!r}")
    return decision


def _parse_claim_selection(raw: str, available: list[dict[str, Any]]) -> list[str]:
    available_ids = [str(item.get("claim_id")) for item in available]
    if not available_ids:
        if raw and raw.lower() not in {"none", "no"}:
            raise HumanGateError("no Checkpoint Claims are available for scientific judgment")
        return []
    if not raw or raw.lower() == "all":
        return available_ids
    if raw.lower() in {"none", "no"}:
        return []
    selected = [item for item in re.split(r"[\s,]+", raw) if item]
    if len(selected) != len(set(selected)):
        raise HumanGateError("Claim selection contains a duplicate ID")
    unknown = [claim_id for claim_id in selected if claim_id not in available_ids]
    if unknown:
        raise HumanGateError("Claim selection contains unavailable IDs: " + ", ".join(unknown))
    return selected


def _interactive_verdict_choices(
    paths: StudyPaths,
    stdin: TextIO,
    stdout: TextIO,
) -> dict[str, Any]:
    # Fail before asking for human judgment when the mechanical scope is stale.
    _current_judged_scope(paths, [])
    available = _checkpoint_claims(paths)
    if available:
        print("Claims frozen by the latest Checkpoint:", file=stdout)
        for claim in available:
            statement = str(claim.get("statement", "")).strip()
            preview = statement if len(statement) <= 160 else statement[:157] + "..."
            print(f"- {claim.get('claim_id')}: {preview}", file=stdout)
        raw_claims = _prompt_line(
            stdin,
            stdout,
            "Claim IDs to judge [all; comma-separated; or none]: ",
            allow_empty=True,
        )
    else:
        print(
            "No Checkpoint Claims are available; this can record an implementation-only Verdict.",
            file=stdout,
        )
        raw_claims = "none"
    claim_ids = _parse_claim_selection(raw_claims, available)
    implementation_decision = _prompt_decision(
        stdin, stdout, "Implementation", _IMPLEMENTATION_VERDICTS
    )
    implementation_rationale = _prompt_line(
        stdin, stdout, "Implementation rationale: "
    )
    scientific_decision = _prompt_decision(
        stdin, stdout, "Scientific", HUMAN_SCIENTIFIC_VERDICTS
    )
    scientific_rationale = _prompt_line(stdin, stdout, "Scientific rationale: ")
    scientific_scope = _prompt_line(
        stdin,
        stdout,
        "Scientific scope accepted or rejected by this decision: ",
    )
    return {
        "input_version": 1,
        "claim_ids": claim_ids,
        "implementation_verdict": {
            "decision": implementation_decision,
            "rationale": implementation_rationale,
            "conditions": [],
        },
        "scientific_verdict": {
            "decision": scientific_decision,
            "rationale": scientific_rationale,
            "scope": scientific_scope,
            "conditions": [],
        },
    }


def _validate_verdict_choices(
    choices: dict[str, Any],
    *,
    agent_initiated: bool,
) -> None:
    required = {
        "input_version",
        "claim_ids",
        "implementation_verdict",
        "scientific_verdict",
    }
    expected_version = 2 if agent_initiated else 1
    if agent_initiated:
        required.add("authorization")
    if set(choices) != required or choices.get("input_version") != expected_version:
        raise ValidationError(
            "Verdict decision input must contain exactly "
            f"input_version={expected_version}, claim_ids, implementation_verdict, and "
            "scientific_verdict"
            + (", plus authorization" if agent_initiated else "")
        )
    if agent_initiated:
        authorization = choices.get("authorization")
        if not isinstance(authorization, dict) or set(authorization) != {
            "source",
            "instruction",
        }:
            raise ValidationError(
                "Agent-initiated Verdict input authorization must contain exactly "
                "source and instruction"
            )
        if authorization.get("source") != "explicit_user_instruction":
            raise ValidationError(
                "Agent-initiated Verdict input must cite explicit_user_instruction"
            )
        instruction = authorization.get("instruction")
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or _contains_placeholder(instruction)
        ):
            raise ValidationError(
                "Agent-initiated Verdict input requires a non-placeholder user instruction"
            )
    claim_ids = choices.get("claim_ids")
    if not isinstance(claim_ids, list) or any(not isinstance(item, str) for item in claim_ids):
        raise ValidationError("Verdict decision input claim_ids must be an array of strings")
    if len(claim_ids) != len(set(claim_ids)):
        raise ValidationError("Verdict decision input repeats a Claim ID")
    for label, allowed in (
        ("implementation_verdict", _IMPLEMENTATION_VERDICTS),
        ("scientific_verdict", HUMAN_SCIENTIFIC_VERDICTS),
    ):
        branch = choices.get(label)
        required_branch = {"decision", "rationale"}
        if label == "scientific_verdict":
            required_branch.add("scope")
        allowed_branch = required_branch | {"conditions"}
        if (
            not isinstance(branch, dict)
            or not required_branch.issubset(branch)
            or not set(branch).issubset(allowed_branch)
        ):
            raise ValidationError(
                f"{label} must contain decision and rationale"
                + (", plus the authorized scope" if label == "scientific_verdict" else "")
                + "; conditions are optional"
            )
        if branch.get("decision") not in allowed:
            raise ValidationError(f"invalid {label} decision")
        rationale = branch.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or _contains_placeholder(rationale):
            raise ValidationError(f"{label} requires a completed rationale")
        if label == "scientific_verdict":
            scope = branch.get("scope")
            if not isinstance(scope, str) or not scope.strip() or _contains_placeholder(scope):
                raise ValidationError("scientific_verdict requires an authorized scope")
        conditions = branch.get("conditions", [])
        if not isinstance(conditions, list) or any(
            not isinstance(item, str) or not item.strip() or _contains_placeholder(item)
            for item in conditions
        ):
            raise ValidationError(f"{label}.conditions must contain completed strings")


def _current_verdict_authority(
    paths: StudyPaths,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Validate authority that every new Verdict format must share."""

    _raise_validation_issues(
        "Verdict requires a fresh approved Brief",
        brief_content_issues(paths) + brief_approval_issues(paths),
    )
    approval = _load_existing_approval(paths)
    latest = _latest_checkpoint(paths)
    if latest is not None and latest.get("brief") != {
        "sha256": approval["brief"]["sha256"],
        "approval_sha256": approval["approval_sha256"],
    }:
        raise ValidationError(
            "latest Checkpoint does not bind the current Brief approval; compact before Verdict"
        )
    repository = git_state(paths.root)
    if repository.get("available") and repository.get("dirty"):
        raise ValidationError(
            "Verdict requires a clean Git worktree; commit the reviewed state first"
        )
    return approval, latest, repository


def _current_judged_scope(paths: StudyPaths, claim_ids: list[str]) -> dict[str, Any]:
    """Bind human choices to the exact current authority and Evidence graph."""

    _, latest, repository = _current_verdict_authority(paths)
    current_claims = load_json(paths.claims)
    if not isinstance(current_claims, dict):
        raise ValidationError("CLAIMS.json must contain an object")
    current_active = [
        item
        for item in current_claims.get("claims", [])
        if isinstance(item, dict) and item.get("lifecycle") == "active"
    ]
    if latest is None and current_active:
        raise ValidationError(
            "active Claims require a fresh Checkpoint before a scientific Verdict"
        )
    if latest is not None and latest.get("claims_file_sha256") != sha256_file(paths.claims):
        raise ValidationError(
            "latest Checkpoint does not bind the current CLAIMS.json; compact before Verdict"
        )
    checkpoint_claims = _checkpoint_claims(paths)
    by_id = {str(item.get("claim_id")): item for item in checkpoint_claims}
    unknown = [claim_id for claim_id in claim_ids if claim_id not in by_id]
    if unknown:
        raise ValidationError(
            "selected Claims are not frozen by the latest Checkpoint: " + ", ".join(unknown)
        )
    claim_refs = [
        {"claim_id": claim_id, "sha256": sha256_json(by_id[claim_id])}
        for claim_id in claim_ids
    ]
    evidence_refs: list[dict[str, Any]] = []
    seen_evidence: set[tuple[str, int]] = set()
    for claim_id in claim_ids:
        claim = by_id[claim_id]
        for field in ("supporting_evidence", "contradictory_evidence", "other_evidence"):
            refs = claim.get(field, [])
            if not isinstance(refs, list):
                raise ValidationError(f"Checkpoint Claim {claim_id} has invalid {field}")
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValidationError(f"Checkpoint Claim {claim_id} has an invalid Evidence ref")
                key = (str(ref.get("evidence_id")), int(ref.get("version", 0)))
                if key not in seen_evidence:
                    evidence_refs.append(dict(ref))
                    seen_evidence.add(key)
    checkpoint_ref = None
    if latest is not None:
        checkpoint_ref = {
            "checkpoint_id": latest["checkpoint_id"],
            "sha256": latest["checkpoint_sha256"],
        }
    return {
        "commit": repository.get("commit"),
        "brief_sha256": sha256_file(paths.brief),
        "checkpoint": checkpoint_ref,
        "claims": claim_refs,
        "evidence": evidence_refs,
    }


def _build_generated_verdict(
    paths: StudyPaths,
    choices: dict[str, Any],
    *,
    agent_initiated: bool = False,
) -> dict[str, Any]:
    _validate_verdict_choices(choices, agent_initiated=agent_initiated)
    claim_ids = list(choices["claim_ids"])
    scientific_decision = choices["scientific_verdict"]["decision"]
    if not claim_ids and scientific_decision != "requires_more_evidence":
        raise ValidationError(
            "a Verdict without selected Claims must use scientific decision "
            "requires_more_evidence"
        )
    verdict = {
        "schema_version": SCHEMA_VERSION,
        "study_id": paths.study_id,
        "verdict_id": _next_verdict_id(paths),
        "created_at": utc_now(),
        "reviewer": reviewer_identity(paths.root),
        "judged_scope": _current_judged_scope(paths, claim_ids),
        "implementation_verdict": {
            **choices["implementation_verdict"],
            "conditions": list(choices["implementation_verdict"].get("conditions", [])),
        },
        "scientific_verdict": {
            **choices["scientific_verdict"],
            "conditions": list(choices["scientific_verdict"].get("conditions", [])),
        },
        "confirmation": {
            "typed_text": "[FILLED BY STUDYCTL]",
            "confirmed_at": "[FILLED BY STUDYCTL]",
        },
        "verdict_sha256": None,
    }
    if agent_initiated:
        instruction = choices["authorization"]["instruction"].strip()
        verdict["authorization"] = {
            "mode": "agent_initiated",
            "source": "explicit_user_instruction",
            "assurance": "cooperative",
            "instruction": instruction,
            "instruction_sha256": sha256_json(instruction),
        }
    return verdict


def _validate_claim_references(paths: StudyPaths, refs: Any) -> None:
    if not isinstance(refs, list):
        raise ValidationError("judged_scope.claims must be an array")
    claims = load_json(paths.claims)
    if not isinstance(claims, dict):
        raise ValidationError("CLAIMS.json must contain an object")
    _raise_validation_issues(
        "current CLAIMS.json is invalid",
        object_schema_issues(paths.root, "claims", paths.claims, claims),
    )
    if claims.get("study_id") != paths.study_id:
        raise ValidationError("current CLAIMS.json has the wrong study_id")
    current = {str(item.get("claim_id")): item for item in claims.get("claims", [])}
    if len(current) != len(claims.get("claims", [])):
        raise ValidationError("current CLAIMS.json repeats a Claim ID")
    seen: set[str] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise ValidationError(f"judged_scope.claims[{index}] must be an object")
        if set(ref) != {"claim_id", "sha256"}:
            raise ValidationError(
                f"judged_scope.claims[{index}] must contain exactly claim_id and sha256"
            )
        claim_id = ref.get("claim_id")
        if not isinstance(claim_id, str) or claim_id not in current:
            raise ValidationError(f"judged_scope contains a missing Claim reference: {claim_id!r}")
        if claim_id in seen:
            raise ValidationError(f"judged_scope repeats Claim reference {claim_id}")
        seen.add(claim_id)
        claim = current[claim_id]
        if ref["sha256"] != sha256_json(claim):
            raise ValidationError(f"Claim reference {claim_id} has a stale sha256")


def _validate_evidence_references(paths: StudyPaths, refs: Any) -> None:
    if not isinstance(refs, list):
        raise ValidationError("judged_scope.evidence must be an array")
    evidence = evidence_index(paths)
    seen: set[tuple[str, int]] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise ValidationError(f"judged_scope.evidence[{index}] must be an object")
        evidence_id = ref.get("evidence_id")
        version = ref.get("version")
        if not isinstance(evidence_id, str) or not isinstance(version, int) or isinstance(version, bool):
            raise ValidationError(f"judged_scope.evidence[{index}] is not a valid Evidence reference")
        key = (evidence_id, version)
        if key in seen:
            raise ValidationError(f"judged_scope repeats Evidence reference {key}")
        seen.add(key)
        current = evidence.get(key)
        if current is None:
            raise ValidationError(f"judged_scope contains missing Evidence reference {key}")
        evidence_path, item = current
        _raise_validation_issues(
            f"Evidence reference {key} is invalid",
            object_schema_issues(paths.root, "evidence", evidence_path, item),
        )
        if item.get("status") != "finalized":
            raise ValidationError(f"Verdict may reference only finalized Evidence: {key}")
        digest = item.get("record_sha256")
        if digest != record_digest(item, "record_sha256"):
            raise ValidationError(f"finalized Evidence has an invalid digest: {key}")
        if ref.get("sha256") != digest:
            raise ValidationError(f"judged_scope contains a stale Evidence hash: {key}")
        allowed = {"evidence_id", "version", "sha256"}
        if set(ref) != allowed:
            raise ValidationError(
                f"Evidence reference {key} must contain exactly evidence_id, version, and sha256"
            )


def _latest_checkpoint(paths: StudyPaths) -> dict[str, Any] | None:
    files = checkpoint_paths(paths)
    if not files:
        return None
    value = load_json(files[-1])
    if not isinstance(value, dict):
        raise ValidationError("latest Checkpoint must contain an object")
    _raise_validation_issues(
        "latest Checkpoint is invalid",
        object_schema_issues(paths.root, "checkpoint", files[-1], value),
    )
    if value.get("checkpoint_sha256") != record_digest(value, "checkpoint_sha256"):
        raise ValidationError("latest Checkpoint has an invalid checkpoint_sha256")
    return value


def _validate_checkpoint_reference(paths: StudyPaths, ref: Any) -> None:
    latest = _latest_checkpoint(paths)
    if latest is None:
        if ref is not None:
            raise ValidationError("judged_scope.checkpoint must be null because no Checkpoint exists")
        return
    if not isinstance(ref, dict):
        raise ValidationError("judged_scope.checkpoint must reference the latest Checkpoint")
    if ref == latest:
        return
    expected_id = latest.get("checkpoint_id")
    expected_hash = latest.get("checkpoint_sha256")
    if ref.get("checkpoint_id") != expected_id:
        raise ValidationError("judged_scope.checkpoint does not reference the latest Checkpoint")
    supplied_hash = ref.get("sha256", ref.get("checkpoint_sha256"))
    if supplied_hash != expected_hash:
        raise ValidationError("judged_scope.checkpoint has a stale Checkpoint hash")
    allowed = {"checkpoint_id", "sha256"}
    alternate_allowed = {"checkpoint_id", "checkpoint_sha256"}
    if set(ref) not in (allowed, alternate_allowed):
        raise ValidationError("judged_scope.checkpoint is not an exact Checkpoint reference")


def _validate_current_verdict_scope(paths: StudyPaths, verdict: dict[str, Any]) -> None:
    _current_verdict_authority(paths)
    scope = verdict.get("judged_scope")
    if not isinstance(scope, dict):
        raise ValidationError("judged_scope must be an object")
    current_git = git_state(paths.root)
    if scope.get("commit") != current_git.get("commit"):
        raise ValidationError("judged_scope.commit does not match the current Git commit")
    if scope.get("brief_sha256") != sha256_file(paths.brief):
        raise ValidationError("judged_scope.brief_sha256 does not match the active Brief")
    _validate_claim_references(paths, scope.get("claims"))
    _validate_evidence_references(paths, scope.get("evidence"))
    _validate_checkpoint_reference(paths, scope.get("checkpoint"))
    if scope.get("claims") and scope.get("checkpoint") is None:
        raise ValidationError(
            "Verdict Claim references require the latest Checkpoint snapshot"
        )


def _next_verdict_path(paths: StudyPaths) -> Path:
    if not paths.verdict.exists():
        return paths.verdict
    highest = 1
    for path in paths.study.glob("VERDICT.v*.json"):
        match = _VERDICT_VERSION.fullmatch(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return paths.study / f"VERDICT.v{highest + 1:04d}.json"


def _reject_duplicate_verdict_id(paths: StudyPaths, verdict_id: str) -> None:
    for path in sorted(paths.study.glob("VERDICT*.json")):
        try:
            existing = load_json(path)
        except ValidationError as exc:
            raise ValidationError(f"cannot verify existing Verdict identity in {path}: {exc}") from exc
        if isinstance(existing, dict) and existing.get("verdict_id") == verdict_id:
            raise WorkflowError(f"Verdict ID already exists: {verdict_id}")


@serialized_study_authority
def record_verdict(
    paths: StudyPaths,
    source_path: Path | None = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    *,
    agent_initiated: bool = False,
) -> Path:
    """Record human-owned decisions in a mechanically scoped immutable Verdict.

    Interactive mode collects and confirms the decisions directly. Agent-initiated
    mode accepts only a version-2 decision input that preserves the explicit user
    instruction authorizing those decisions; it cannot import a complete Verdict.
    """

    if agent_initiated:
        if source_path is None:
            raise HumanGateError(
                "agent-initiated Verdict requires a decision-only --file with explicit user authorization"
            )
    else:
        _require_human_tty(stdin, stdout)
    generated_claim_ids: list[str] | None = None
    if source_path is None:
        choices = _interactive_verdict_choices(paths, stdin, stdout)
        verdict = _build_generated_verdict(paths, choices)
        generated_claim_ids = list(choices["claim_ids"])
        source = paths.verdict
    else:
        source = source_path.resolve()
        supplied = load_json(source)
        if not isinstance(supplied, dict):
            raise ValidationError("Verdict source must contain a JSON object")
        if "input_version" in supplied:
            verdict = _build_generated_verdict(
                paths,
                supplied,
                agent_initiated=agent_initiated,
            )
            generated_claim_ids = list(supplied["claim_ids"])
        else:
            if agent_initiated:
                raise HumanGateError(
                    "agent-initiated Verdict accepts only decision input, not a complete Verdict record"
                )
            # Compatibility path for previously prepared full Verdict sources.
            verdict = supplied
    if not agent_initiated and verdict.get("authorization") is not None:
        raise ValidationError(
            "Verdict authorization is reserved for --agent-initiated decision input"
        )
    _raise_validation_issues(
        "Verdict source does not match the Verdict schema",
        object_schema_issues(paths.root, "verdict", source, verdict),
    )
    if verdict.get("study_id") != paths.study_id:
        raise ValidationError("Verdict study_id does not match the Study")
    verdict_id = require_id("verdict", str(verdict.get("verdict_id", "")))
    if verdict.get("verdict_sha256") is not None:
        raise ValidationError("unrecorded Verdict source must have verdict_sha256 null")
    _validate_verdict_branches(verdict)
    _validate_verdict_claim_decision(verdict)
    _validate_current_verdict_scope(paths, verdict)
    _reject_duplicate_verdict_id(paths, verdict_id)
    destination = _next_verdict_path(paths)

    implementation = verdict["implementation_verdict"]
    scientific = verdict["scientific_verdict"]
    scope = verdict["judged_scope"]
    print(f"Study: {paths.study_id}", file=stdout)
    print(f"Verdict ID: {verdict_id}", file=stdout)
    print(f"Judged commit: {scope['commit'] or 'unavailable'}", file=stdout)
    print(f"Judged Brief SHA-256: {scope['brief_sha256']}", file=stdout)
    checkpoint = scope["checkpoint"]
    checkpoint_id = checkpoint["checkpoint_id"] if checkpoint else "none"
    print(f"Judged Checkpoint: {checkpoint_id}", file=stdout)
    claim_ids = [ref["claim_id"] for ref in scope["claims"]]
    evidence_ids = [
        f"{ref['evidence_id']}@v{ref['version']}" for ref in scope["evidence"]
    ]
    claim_preview = claim_ids[:24]
    claim_suffix = f", +{len(claim_ids) - 24} more" if len(claim_ids) > 24 else ""
    print(
        f"Judged Claims ({len(claim_ids)}): {', '.join(claim_preview) or 'none'}{claim_suffix}",
        file=stdout,
    )
    evidence_preview = evidence_ids[:24]
    evidence_suffix = (
        f", +{len(evidence_ids) - 24} more" if len(evidence_ids) > 24 else ""
    )
    print(
        f"Judged Evidence ({len(evidence_ids)}): "
        f"{', '.join(evidence_preview) or 'none'}{evidence_suffix}",
        file=stdout,
    )
    print(f"Mechanical scope SHA-256: {sha256_json(scope)}", file=stdout)
    print(f"Implementation verdict: {implementation['decision']}", file=stdout)
    print(f"Scientific verdict: {scientific['decision']}", file=stdout)
    print(f"Scientific scope: {scientific['scope']}", file=stdout)
    print(
        "Reviewer: " + json.dumps(verdict["reviewer"], ensure_ascii=False, sort_keys=True),
        file=stdout,
    )
    if agent_initiated:
        authorization = verdict["authorization"]
        print(
            "Authorization: explicit user instruction (SHA-256 "
            f"{authorization['instruction_sha256']})",
            file=stdout,
        )
    else:
        phrase = f"RECORD VERDICT {paths.study_id} {verdict_id}"
        _confirmation(stdin, stdout, phrase)

    # Recheck every mutable scope anchor after the human has confirmed it.
    _validate_current_verdict_scope(paths, verdict)
    if generated_claim_ids is not None:
        regenerated_scope = _current_judged_scope(paths, generated_claim_ids)
        if regenerated_scope != scope:
            raise WorkflowError(
                "Verdict scope changed during confirmation; no Verdict was recorded"
            )
    if destination.exists():
        raise WorkflowError(f"refusing to overwrite existing Verdict: {destination}")
    recorded_at = utc_now()
    if agent_initiated:
        verdict["confirmation"] = {
            "mode": "agent_initiated",
            "recorded_at": recorded_at,
        }
    else:
        verdict["confirmation"] = {
            "typed_text": phrase,
            "confirmed_at": recorded_at,
        }
    verdict["verdict_sha256"] = record_digest(verdict, "verdict_sha256")
    _raise_validation_issues(
        "confirmed Verdict does not match the Verdict schema",
        object_schema_issues(paths.root, "verdict", destination, verdict),
    )
    atomic_write_json(destination, verdict, overwrite=False, mode=0o444)
    print(f"Recorded immutable Verdict: {destination}", file=stdout)
    return destination
