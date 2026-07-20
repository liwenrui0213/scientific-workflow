#!/usr/bin/env python3
"""Small fail-closed PreToolUse guardrail for human gates and sealed records."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any


_MUTATION = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:rm|unlink|mv|cp|install|truncate|tee|sed\s+-i|perl\s+-i)\b"
    r"|(?:>>?|\.write_(?:text|bytes)|\.unlink\(|\.touch\(|os\.remove|os\.replace)"
    r"|(?:\bopen\s*\(\s*[^,]+,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+][^'\"]*['\"])"
    r"|(?:\.open\s*\(\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+][^'\"]*['\"])"
    r"|(?:\bPath\.open\s*\(\s*[^,]+,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+][^'\"]*['\"])"
    r"|(?:\bos\.open\s*\([^)]*\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)\b)",
    re.IGNORECASE,
)
_HUMAN_COMMAND = re.compile(
    r"(?:studyctl|tools\.studyctl).*\b(?:approve-brief|verdict)\b",
    re.IGNORECASE | re.DOTALL,
)
def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }


def _tool_payload(event: dict[str, Any]) -> tuple[str, str]:
    tool_name = str(event.get("tool_name") or event.get("toolName") or "")
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return tool_name, str(tool_input)
    for key in ("command", "cmd", "patch", "input"):
        if isinstance(tool_input.get(key), str):
            return tool_name, tool_input[key]
    return tool_name, json.dumps(tool_input, ensure_ascii=False, sort_keys=True)


def _direct_targets(event: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract file targets used by Codex Edit/Write-style tools."""

    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return []
    targets: list[tuple[str, str]] = []
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            # A Write tool may overwrite an existing path, so conservatively
            # classify every direct file target as an update.
            targets.append(("update", value.strip()))
    return targets


def _patch_targets(payload: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for match in re.finditer(r"^\*\*\* (Add|Update|Delete) File:\s*(.+?)\s*$", payload, re.MULTILINE):
        targets.append((match.group(1).lower(), match.group(2).strip()))
    return targets


def _workflow_context(cwd: Path) -> tuple[Path, str]:
    fallback_root: Path | None = None
    for root in (cwd, *cwd.parents):
        if fallback_root is None and (
            (root / "scientific-workflow" / "policy.json").is_file()
            or (root / ".git").exists()
        ):
            fallback_root = root
        profile_path = root / "scientific-workflow" / "repository-profile.json"
        if not profile_path.is_file():
            continue
        if profile_path.is_symlink():
            raise ValueError("repository profile must not be a symbolic link")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        raw_study_root = profile.get("study_root") if isinstance(profile, dict) else None
        if not isinstance(raw_study_root, str) or not raw_study_root.strip():
            raise ValueError("repository profile has no valid study_root")
        study_root = raw_study_root.strip().replace("\\", "/").rstrip("/")
        parts = Path(study_root).parts
        if Path(study_root).is_absolute() or ".." in parts or study_root in {"", "."}:
            raise ValueError("repository profile study_root escapes or equals the repository root")
        resolved = (root / study_root).resolve(strict=False)
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("repository profile study_root resolves outside the repository") from exc
        return root, study_root
    # Compatibility fallback for a partially installed V1 repository. The
    # deterministic CLI still rejects a missing profile; the hook remains able
    # to protect the original default paths during migration.
    return fallback_root or cwd, "studies"


def _study_regex(study_root: str, suffix: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(study_root)}/(SC-[0-9]{{4,}})/{suffix}",
        re.IGNORECASE,
    )


def _referenced_evidence(
    repository_root: Path,
    study_root: str,
    study_id: str,
) -> set[tuple[str, int]] | None:
    claims_path = repository_root / study_root / study_id / "CLAIMS.json"
    if not claims_path.is_file():
        return None
    try:
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("claims"), list):
        return None
    refs: set[tuple[str, int]] = set()
    for claim in claims.get("claims", []):
        if not isinstance(claim, dict):
            return None
        for field in ("supporting_evidence", "contradictory_evidence", "other_evidence"):
            field_refs = claim.get(field, [])
            if not isinstance(field_refs, list):
                return None
            for ref in field_refs:
                if not isinstance(ref, dict):
                    return None
                try:
                    refs.add((str(ref["evidence_id"]), int(ref["version"])))
                except (KeyError, TypeError, ValueError):
                    return None
    return refs


def _approval_exists(repository_root: Path, study_root: str, study_id: str) -> bool:
    return (
        repository_root / study_root / study_id / "BRIEF.approval.json"
    ).is_file()


def decide(event: dict[str, Any]) -> str | None:
    tool_name, payload = _tool_payload(event)
    cwd = Path(str(event.get("cwd") or Path.cwd())).resolve()
    repository_root, study_root = _workflow_context(cwd)
    brief_pattern = _study_regex(study_root, r"BRIEF\.md(?:\b|$)")
    approval_pattern = _study_regex(study_root, r"BRIEF\.approval\.json(?:\b|$)")
    verdict_pattern = _study_regex(study_root, r"VERDICT(?:\.v[0-9]+)?\.json(?:\b|$)")
    run_pattern = _study_regex(
        study_root,
        r"runs(?:\b|/RUN-[0-9]{6}(?:\b|/manifest\.json(?:\b|$)))",
    )
    evidence_pattern = _study_regex(
        study_root,
        r"evidence/(EVID-[0-9]{4,})\.v([0-9]{4,})\.json(?:\b|$)",
    )
    evidence_directory_pattern = _study_regex(study_root, r"evidence(?:\s|/|$)")
    changeset_pattern = _study_regex(
        study_root, r"formal/CHANGESET\.json(?:\b|$)"
    )
    validation_pattern = _study_regex(
        study_root, r"formal/VALIDATION\.json(?:\b|$)"
    )
    if tool_name == "Bash" or "bash" in tool_name.lower():
        if _HUMAN_COMMAND.search(payload):
            return "Codex must not invoke the human-only approve-brief or verdict command."
        if not _MUTATION.search(payload):
            return None
        lowered = payload.lower()
        if approval_pattern.search(lowered):
            return "Brief approval records may be written only by the interactive studyctl gate."
        if verdict_pattern.search(lowered):
            return "Finalized Verdict records must not be directly created, changed, or removed."
        if changeset_pattern.search(lowered):
            return "CHANGESET records may be written only by studyctl changeset-new."
        if validation_pattern.search(lowered):
            return "Validation proofs may be written only by studyctl validate-changes."
        if run_pattern.search(lowered):
            return "Run manifests are sealed execution records and must not be changed or removed."
        brief_match = brief_pattern.search(lowered)
        if brief_match and _approval_exists(
            repository_root, study_root, brief_match.group(1).upper()
        ):
            return "An approved Brief must be revised through studyctl brief-new-version."
        for evidence_match in evidence_pattern.finditer(lowered):
            study_id = evidence_match.group(1).upper()
            key = (evidence_match.group(2).upper(), int(evidence_match.group(3)))
            referenced = _referenced_evidence(repository_root, study_root, study_id)
            if referenced is None:
                return "Cannot safely verify Claim references; repair CLAIMS.json before changing Evidence."
            if key in referenced:
                return "Evidence referenced by a Claim is immutable; create a new Evidence version."
        evidence_directory = evidence_directory_pattern.search(lowered)
        if evidence_directory:
            referenced = _referenced_evidence(
                repository_root,
                study_root,
                evidence_directory.group(1).upper(),
            )
            if referenced is None:
                return "Cannot safely verify Claim references; repair CLAIMS.json before changing Evidence."
            if referenced:
                return "Evidence referenced by a Claim is immutable; create a new Evidence version."
        return None

    for action, raw_path in [*_patch_targets(payload), *_direct_targets(event)]:
        normalized = raw_path.replace("\\", "/").lstrip("./")
        lowered = normalized.lower()
        if approval_pattern.search(lowered):
            return "Brief approval records may be written only by the interactive studyctl gate."
        if verdict_pattern.search(lowered):
            return "Verdict records may be written only by the interactive studyctl gate."
        if changeset_pattern.search(lowered):
            return "CHANGESET records may be written only by studyctl changeset-new."
        if validation_pattern.search(lowered):
            return "Validation proofs may be written only by studyctl validate-changes."
        if run_pattern.search(lowered) and action in {"add", "update", "delete"}:
            return "Run manifests are sealed execution records and must not be changed or removed."
        brief_match = brief_pattern.search(normalized)
        if brief_match and _approval_exists(
            repository_root, study_root, brief_match.group(1).upper()
        ):
            return "An approved Brief must be revised through studyctl brief-new-version."
        evidence_match = evidence_pattern.search(normalized)
        if evidence_match and action in {"update", "delete"}:
            key = (evidence_match.group(2).upper(), int(evidence_match.group(3)))
            referenced = _referenced_evidence(
                repository_root,
                study_root,
                evidence_match.group(1).upper(),
            )
            if referenced is None:
                return "Cannot safely verify Claim references; repair CLAIMS.json before changing Evidence."
            if key in referenced:
                return "Evidence referenced by a Claim is immutable; create a new Evidence version."
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook input must be a JSON object")
        reason = decide(event)
    except Exception as exc:
        reason = f"Scientific workflow hook could not safely inspect this tool call: {exc}"
    if reason:
        print(json.dumps(_deny(reason), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
