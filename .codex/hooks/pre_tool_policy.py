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
    r"|(?:>>?|\.write_(?:text|bytes)|\.unlink\(|os\.remove|os\.replace)",
    re.IGNORECASE,
)
_HUMAN_COMMAND = re.compile(
    r"(?:studyctl|tools\.studyctl).*\b(?:approve-brief|verdict)\b",
    re.IGNORECASE | re.DOTALL,
)
_BRIEF = re.compile(
    r"(?:^|/)studies/(SC-[0-9]{4,})/BRIEF\.md$",
    re.IGNORECASE,
)
_EVIDENCE = re.compile(
    r"(?:^|/)studies/(SC-[0-9]{4,})/evidence/(EVID-[0-9]{4,})\.v([0-9]{4,})\.json$",
    re.IGNORECASE,
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


def _referenced_evidence(cwd: Path, study_id: str) -> set[tuple[str, int]] | None:
    for root in (cwd, *cwd.parents):
        claims_path = root / "studies" / study_id / "CLAIMS.json"
        if not claims_path.is_file():
            continue
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
    return None


def _approval_exists(cwd: Path, study_id: str) -> bool:
    return any((root / "studies" / study_id / "BRIEF.approval.json").is_file() for root in (cwd, *cwd.parents))


def decide(event: dict[str, Any]) -> str | None:
    tool_name, payload = _tool_payload(event)
    cwd = Path(str(event.get("cwd") or Path.cwd())).resolve()
    if tool_name == "Bash" or "bash" in tool_name.lower():
        if _HUMAN_COMMAND.search(payload):
            return "Codex must not invoke the human-only approve-brief or verdict command."
        if not _MUTATION.search(payload):
            return None
        lowered = payload.lower()
        if "brief.approval.json" in lowered:
            return "Brief approval records may be written only by the interactive studyctl gate."
        if re.search(r"verdict(?:\.v[0-9]+)?\.json", lowered):
            return "Finalized Verdict records must not be directly created, changed, or removed."
        if re.search(
            r"studies/sc-[0-9]{4,}/runs(?:\b|/run-[0-9]{6}(?:\b|/manifest\.json))",
            lowered,
        ):
            return "Run manifests are sealed execution records and must not be changed or removed."
        brief_match = re.search(r"studies/(sc-[0-9]{4,})/brief\.md", lowered)
        if brief_match and _approval_exists(cwd, brief_match.group(1).upper()):
            return "An approved Brief must be revised through studyctl brief-new-version."
        for evidence_match in re.finditer(
            r"studies/(sc-[0-9]{4,})/evidence/(evid-[0-9]{4,})\.v([0-9]{4,})\.json",
            lowered,
        ):
            study_id = evidence_match.group(1).upper()
            key = (evidence_match.group(2).upper(), int(evidence_match.group(3)))
            referenced = _referenced_evidence(cwd, study_id)
            if referenced is None:
                return "Cannot safely verify Claim references; repair CLAIMS.json before changing Evidence."
            if key in referenced:
                return "Evidence referenced by a Claim is immutable; create a new Evidence version."
        evidence_directory = re.search(
            r"studies/(sc-[0-9]{4,})/evidence(?:\s|/|$)",
            lowered,
        )
        if evidence_directory:
            referenced = _referenced_evidence(cwd, evidence_directory.group(1).upper())
            if referenced is None:
                return "Cannot safely verify Claim references; repair CLAIMS.json before changing Evidence."
            if referenced:
                return "Evidence referenced by a Claim is immutable; create a new Evidence version."
        return None

    for action, raw_path in [*_patch_targets(payload), *_direct_targets(event)]:
        normalized = raw_path.replace("\\", "/").lstrip("./")
        lowered = normalized.lower()
        if lowered.endswith("brief.approval.json"):
            return "Brief approval records may be written only by the interactive studyctl gate."
        if re.search(r"(?:^|/)verdict(?:\.v[0-9]+)?\.json$", lowered):
            return "Verdict records may be written only by the interactive studyctl gate."
        if re.search(r"(?:^|/)runs/run-[0-9]{6}/manifest\.json$", lowered) and action in {"update", "delete"}:
            return "Run manifests are sealed execution records and must not be changed or removed."
        brief_match = _BRIEF.search(normalized)
        if brief_match and _approval_exists(cwd, brief_match.group(1).upper()):
            return "An approved Brief must be revised through studyctl brief-new-version."
        evidence_match = _EVIDENCE.search(normalized)
        if evidence_match and action in {"update", "delete"}:
            key = (evidence_match.group(2).upper(), int(evidence_match.group(3)))
            referenced = _referenced_evidence(cwd, evidence_match.group(1).upper())
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
