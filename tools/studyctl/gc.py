from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .hashing import nested_record_digest, sha256_file
from .models import StudyPaths
from .validation import authoritative_string_references, evidence_index, run_index
from .workspace import load_repository_profile


def _resolve_recorded_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _manifest_reproducible(paths: StudyPaths, manifest: dict[str, Any]) -> tuple[bool, str]:
    if manifest.get("status") != "succeeded":
        return False, "Run did not succeed"
    if manifest.get("integrity", {}).get("manifest_sha256") != nested_record_digest(
        manifest, "integrity", "manifest_sha256"
    ):
        return False, "Run manifest integrity check failed"
    if not manifest.get("execution", {}).get("argv"):
        return False, "Run command argv is missing"
    code_state = manifest.get("code_state", {})
    if code_state.get("changed_during_run") != (
        code_state.get("before") != code_state.get("after")
    ):
        return False, "Run tracked-code change record is invalid"
    if code_state.get("changed_during_run"):
        return False, "tracked code changed during Run"
    git = manifest.get("git", {})
    if not git.get("available") or not git.get("commit") or git.get("dirty"):
        return False, "Run lacks a clean reproducible Git commit"
    for record in manifest.get("inputs", []):
        if record.get("changed_during_run"):
            return False, f"input changed during Run: {record.get('path')}"
        input_path = _resolve_recorded_path(paths.root, str(record.get("path", "")))
        if not input_path.is_file():
            return False, f"input is unavailable: {record.get('path')}"
        if record.get("sha256_after") != sha256_file(input_path):
            return False, f"input hash changed: {record.get('path')}"
    if not manifest.get("change_scope", {}).get("evidence_eligible", False):
        return False, "Run change scope is not Evidence-eligible"
    return True, "reproducible terminal Run"


def garbage_collection_report(paths: StudyPaths) -> dict[str, Any]:
    runs = run_index(paths)
    evidence = evidence_index(paths)
    authoritative_strings = authoritative_string_references(paths)
    protected_run_ids = {
        str(ref.get("run_id"))
        for _, item in evidence.values()
        for ref in item.get("runs", [])
    }
    for value in authoritative_strings:
        protected_run_ids.update(re.findall(r"RUN-[0-9]{6}", value))

    profile = load_repository_profile(paths.root)
    object_relative = Path(str(profile["object_root"]))
    object_root = (paths.root / object_relative).resolve()
    registered: dict[Path, list[tuple[str, dict[str, Any], dict[str, Any], bool, str]]] = {}
    retained: list[dict[str, str]] = []
    candidates: list[dict[str, Any]] = []
    for run_id, (_, manifest) in runs.items():
        reproducible, reproducibility_reason = _manifest_reproducible(paths, manifest)
        for output in manifest.get("outputs", []):
            if not output.get("present"):
                continue
            path = _resolve_recorded_path(paths.root, str(output.get("path", "")))
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(object_root)
            except (OSError, ValueError):
                continue
            registered.setdefault(resolved, []).append(
                (run_id, manifest, output, reproducible, reproducibility_reason)
            )

    # Classify each resolved object once. Any protection on any registration
    # dominates candidate status, so aliases cannot make protected data appear
    # deletable through a second unreferenced Run.
    for resolved, registrations in sorted(registered.items(), key=lambda item: str(item[0])):
        relative = resolved.relative_to(object_root)
        display = (object_relative / relative).as_posix()
        run_ids = sorted({item[0] for item in registrations})
        reason: str | None = None
        if any(run_id in protected_run_ids for run_id in run_ids):
            reason = "Run is referenced by Evidence, Claim, or Verdict"
        elif any(item[2].get("pinned") for item in registrations):
            reason = "output is pinned"
        elif any(item[2].get("classification") == "baseline" for item in registrations):
            reason = "output is a baseline"
        elif any(item[2].get("classification") == "unique_anomaly" for item in registrations):
            reason = "output is a unique anomaly"
        elif any(display in value or str(resolved) in value for value in authoritative_strings):
            reason = "object path is referenced by an authoritative record"
        elif resolved.is_symlink() or not resolved.is_file():
            reason = "object is not a regular file"
        else:
            actual_hash = sha256_file(resolved)
            if any(item[2].get("sha256") != actual_hash for item in registrations):
                reason = "object hash conflicts with a Run manifest"
            elif not any(item[3] for item in registrations):
                reason = registrations[0][4]
        run_label = ",".join(run_ids)
        if reason:
            retained.append({"path": display, "run_id": run_label, "reason": reason})
        else:
            candidates.append(
                {
                    "path": display,
                    "run_ids": run_ids,
                    "size": resolved.stat().st_size,
                    "sha256": sha256_file(resolved),
                    "reason": "unreferenced ordinary output with a reproducible Run manifest",
                }
            )

    if object_root.is_dir():
        for path in sorted(object_root.rglob("*")):
            if path.name == ".gitignore" or path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved not in registered:
                retained.append(
                    {
                        "path": (object_relative / resolved.relative_to(object_root)).as_posix(),
                        "run_id": "unregistered",
                        "reason": "object has no reproducible Run manifest",
                    }
                )
    return {
        "schema_version": 1,
        "study_id": paths.study_id,
        "mode": "dry-run",
        "deleted": [],
        "candidates": sorted(candidates, key=lambda item: item["path"]),
        "retained": sorted(retained, key=lambda item: (item["path"], item["run_id"])),
    }
