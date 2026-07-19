from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable


SCHEMA_VERSION = 1

ID_PATTERNS = {
    "study": re.compile(r"^SC-[0-9]{4,}$"),
    "run": re.compile(r"^RUN-[0-9]{6}$"),
    "evidence": re.compile(r"^EVID-[0-9]{4,}$"),
    "claim": re.compile(r"^CLAIM-[0-9]{4,}$"),
    "cohort": re.compile(r"^COHORT-[0-9]{3,}$"),
    "checkpoint": re.compile(r"^CHECKPOINT-[0-9]{6}$"),
    "verdict": re.compile(r"^VERDICT-[0-9]{4,}$"),
}

CLAIM_STATES = {
    "proposed",
    "under_test",
    "partially_supported",
    "numerically_supported",
    "contradicted",
    "inconclusive",
}

HUMAN_SCIENTIFIC_VERDICTS = {
    "accepted_within_scope",
    "rejected",
    "requires_more_evidence",
}

FORMALIZATION_LEVELS = {
    "advisory",
    "required_before_expensive_run",
    "required_before_evidence",
    "required_before_review",
    "blocking_now",
}


class WorkflowError(RuntimeError):
    """Expected workflow failure suitable for a concise CLI message."""


class ValidationError(WorkflowError):
    """Raised when authoritative input fails deterministic validation."""


class HumanGateError(WorkflowError):
    """Raised when a human-only command is not run interactively."""


class RunInterrupted(WorkflowError):
    """Raised after an interrupted Run has been sealed."""


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    path: str
    message: str

    def render(self) -> str:
        return f"{self.level}: {self.path}: {self.message}"


@dataclass
class FormalizationResult:
    outcome: str
    requirements: list[dict[str, str]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.outcome == "BLOCKED"


@dataclass(frozen=True)
class StudyPaths:
    root: Path
    study_id: str

    @property
    def study(self) -> Path:
        return self.root / "studies" / self.study_id

    @property
    def brief(self) -> Path:
        return self.study / "BRIEF.md"

    @property
    def brief_approval(self) -> Path:
        return self.study / "BRIEF.approval.json"

    @property
    def claims(self) -> Path:
        return self.study / "CLAIMS.json"

    @property
    def verdict(self) -> Path:
        return self.study / "VERDICT.json"

    @property
    def formal(self) -> Path:
        return self.study / "formal"

    @property
    def runs(self) -> Path:
        return self.study / "runs"

    @property
    def evidence(self) -> Path:
        return self.study / "evidence"

    @property
    def checkpoints(self) -> Path:
        return self.study / "checkpoints"

    @property
    def generated(self) -> Path:
        return self.study / "generated"

    @property
    def active_work(self) -> Path:
        return self.study / "work" / "active"

    @property
    def archived_work(self) -> Path:
        return self.study / "work" / "archived"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_id(kind: str, value: str) -> str:
    pattern = ID_PATTERNS[kind]
    if not pattern.fullmatch(value):
        raise ValidationError(f"invalid {kind} ID: {value!r}; expected {pattern.pattern}")
    return value


def get_repo_root(start: Path | None = None) -> Path:
    """Find this workflow root without requiring Git.

    AGENTS.md and scientific-workflow/policy.json are durable project markers.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "scientific-workflow" / "policy.json").is_file():
            return candidate
        if (candidate / ".git").exists() and (candidate / "tools" / "studyctl").is_dir():
            return candidate
    raise WorkflowError("could not locate repository root containing scientific-workflow/policy.json")


def study_paths(root: Path, study_id: str, *, must_exist: bool = True) -> StudyPaths:
    require_id("study", study_id)
    paths = StudyPaths(root.resolve(), study_id)
    if must_exist and not paths.study.is_dir():
        raise WorkflowError(f"study does not exist: {study_id}")
    return paths


def errors_only(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    return [issue for issue in issues if issue.level == "ERROR"]


def as_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    return value
