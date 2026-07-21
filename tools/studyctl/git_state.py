from __future__ import annotations

import getpass
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence

from .hashing import sha256_bytes


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def git_state(root: Path) -> dict[str, Any]:
    probe = _git(root, ["rev-parse", "--show-toplevel"])
    if probe.returncode != 0:
        return {
            "available": False,
            "commit": None,
            "dirty": None,
            "status": [],
            "status_sha256": None,
        }
    commit = _git(root, ["rev-parse", "HEAD"])
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    lines = status.stdout.splitlines() if status.returncode == 0 else []
    return {
        "available": True,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(lines),
        "status": lines,
        "status_sha256": sha256_bytes(status.stdout.encode("utf-8")) if status.returncode == 0 else None,
    }


def git_tracked_state(
    root: Path, *, exclude_paths: Sequence[str] = ()
) -> dict[str, Any]:
    """Fingerprint the commit and tracked worktree bytes, excluding Run outputs."""

    probe = _git(root, ["rev-parse", "--show-toplevel"])
    if probe.returncode != 0:
        return {"available": False, "commit": None, "diff_sha256": None}
    commit = _git(root, ["rev-parse", "HEAD"])
    pathspecs = ["."]
    for raw in exclude_paths:
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts or not raw.strip():
            raise ValueError("tracked-state exclusions must be safe repository-relative paths")
        pathspecs.append(f":(top,exclude){candidate.as_posix()}")
    diff = _git(root, ["diff", "--binary", "HEAD", "--", *pathspecs])
    return {
        "available": True,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "diff_sha256": (
            sha256_bytes(diff.stdout.encode("utf-8")) if diff.returncode == 0 else None
        ),
    }


def git_diff_metadata(root: Path, base_ref: str) -> dict[str, Any]:
    state = git_state(root)
    if not state["available"]:
        return {
            "available": False,
            "base_ref": base_ref,
            "head": None,
            "merge_base": None,
            "name_status": [],
            "diff_sha256": None,
            "deviation": "repository is not a Git worktree",
        }
    base = _git(root, ["rev-parse", "--verify", base_ref])
    if base.returncode != 0:
        return {
            "available": False,
            "base_ref": base_ref,
            "head": state["commit"],
            "merge_base": None,
            "name_status": [],
            "diff_sha256": None,
            "deviation": f"base ref {base_ref!r} is unavailable",
        }
    merge_base = _git(root, ["merge-base", base_ref, "HEAD"])
    anchor = merge_base.stdout.strip() if merge_base.returncode == 0 else base.stdout.strip()
    diff = _git(root, ["diff", "--binary", anchor, "HEAD"])
    names = _git(root, ["diff", "--name-status", anchor, "HEAD"])
    dirty_diff = _git(root, ["diff", "--binary", "HEAD"])
    return {
        "available": diff.returncode == 0,
        "base_ref": base_ref,
        "head": state["commit"],
        "merge_base": anchor,
        "name_status": names.stdout.splitlines() if names.returncode == 0 else [],
        "diff_sha256": sha256_bytes(diff.stdout.encode("utf-8")) if diff.returncode == 0 else None,
        "dirty": state["dirty"],
        "dirty_status": state["status"],
        "dirty_status_sha256": state["status_sha256"],
        "dirty_diff_sha256": (
            sha256_bytes(dirty_diff.stdout.encode("utf-8"))
            if dirty_diff.returncode == 0
            else None
        ),
        "deviation": None,
    }


def reviewer_identity(root: Path) -> dict[str, str]:
    explicit = os.environ.get("STUDYCTL_REVIEWER", "").strip()
    if explicit:
        return {"identity": explicit, "source": "STUDYCTL_REVIEWER"}
    name = _git(root, ["config", "--get", "user.name"])
    email = _git(root, ["config", "--get", "user.email"])
    if name.returncode == 0 and name.stdout.strip():
        identity = name.stdout.strip()
        if email.returncode == 0 and email.stdout.strip():
            identity += f" <{email.stdout.strip()}>"
        return {"identity": identity, "source": "git_config"}
    user = os.environ.get("USER", "").strip() or getpass.getuser()
    return {"identity": user, "source": "local_account"}
