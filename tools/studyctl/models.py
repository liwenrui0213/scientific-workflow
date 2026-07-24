from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterable


SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 3
EVIDENCE_SCHEMA_VERSION = 5
CLAIMS_SCHEMA_VERSION = 3
CHECKPOINT_SCHEMA_VERSION = 5
COMPACTION_PLAN_SCHEMA_VERSION = 2
EXPERIMENT_INTENT_SCHEMA_VERSION = 2
CONTROL_GRAPH_SCHEMA_VERSION = 2
VERDICT_SCHEMA_VERSION = 2

ID_PATTERNS = {
    "study": re.compile(r"^SC-[0-9]{4,}$"),
    "run": re.compile(r"^RUN-[0-9]{6}$"),
    "observation": re.compile(r"^OBS-[0-9]{4,}$"),
    "evidence": re.compile(r"^EVID-[0-9]{4,}$"),
    "claim": re.compile(r"^CLAIM-[0-9]{4,}$"),
    "confirmation": re.compile(r"^CONF-[0-9]{4,}$"),
    "experiment_intent": re.compile(r"^INTENT-[0-9]{4,}$"),
    "control_graph": re.compile(r"^CG-[0-9]{4,}$"),
    "evidence_gap": re.compile(r"^GAP-[0-9]{4,}$"),
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
    study_root: str = "studies"

    def _safe_repository_path(self, relative: Path, *, label: str) -> Path:
        """Return a repository path after rejecting escapes and symlink traversal."""

        if relative.is_absolute() or ".." in relative.parts or "\x00" in str(relative):
            raise ValidationError(f"{label} must be a safe repository-relative path")
        repository = self.root.resolve()
        candidate = repository / relative
        try:
            candidate.relative_to(repository)
        except ValueError as exc:
            raise ValidationError(f"{label} must stay inside the repository") from exc
        _assert_no_symlink_components(repository, candidate, label=label)
        _assert_resolves_within(candidate, repository, label=label)
        return candidate

    def _safe_study_path(self, relative: Path, *, label: str) -> Path:
        study = self.study
        candidate = study / relative
        try:
            candidate.relative_to(study)
        except ValueError as exc:
            raise ValidationError(f"{label} must stay inside Study {self.study_id}") from exc
        _assert_no_symlink_components(self.study_root_path, candidate, label=label)
        _assert_resolves_within(candidate, self.study_root_path, label=label)
        return candidate

    @property
    def study_root_path(self) -> Path:
        return self._safe_repository_path(Path(self.study_root), label="configured Study root")

    @property
    def study(self) -> Path:
        candidate = self.study_root_path / self.study_id
        _assert_no_symlink_components(self.study_root_path, candidate, label="Study directory")
        _assert_resolves_within(candidate, self.study_root_path, label="Study directory")
        return candidate

    @property
    def brief(self) -> Path:
        return self._safe_study_path(Path("BRIEF.md"), label="BRIEF.md")

    @property
    def brief_approval(self) -> Path:
        return self._safe_study_path(
            Path("BRIEF.approval.json"), label="BRIEF.approval.json"
        )

    @property
    def claims(self) -> Path:
        return self._safe_study_path(Path("CLAIMS.json"), label="CLAIMS.json")

    @property
    def verdict(self) -> Path:
        return self._safe_study_path(Path("VERDICT.json"), label="VERDICT.json")

    @property
    def formal(self) -> Path:
        return self._safe_study_path(Path("formal"), label="formal directory")

    @property
    def confirmations(self) -> Path:
        return self._safe_study_path(
            Path("formal/confirmations"), label="confirmation directory"
        )

    @property
    def experiment_intents(self) -> Path:
        return self._safe_study_path(
            Path("intents"), label="Experiment Intent directory"
        )

    @property
    def control_graphs(self) -> Path:
        return self._safe_study_path(
            Path("control-plans"), label="Control Graph directory"
        )

    @property
    def work(self) -> Path:
        return self._safe_study_path(Path("work"), label="work directory")

    @property
    def runs(self) -> Path:
        return self._safe_study_path(Path("runs"), label="runs directory")

    @property
    def evidence(self) -> Path:
        return self._safe_study_path(Path("evidence"), label="evidence directory")

    @property
    def observations(self) -> Path:
        return self._safe_study_path(
            Path("observations"), label="observations directory"
        )

    @property
    def observation_sequence(self) -> Path:
        return self._safe_study_path(
            Path("OBSERVATIONS.sequence.json"),
            label="OBSERVATIONS.sequence.json",
        )

    @property
    def evidence_sequence(self) -> Path:
        return self._safe_study_path(
            Path("EVIDENCE.sequence.json"), label="EVIDENCE.sequence.json"
        )

    @property
    def checkpoint_sequence(self) -> Path:
        return self._safe_study_path(
            Path("CHECKPOINTS.sequence.json"), label="CHECKPOINTS.sequence.json"
        )

    @property
    def graph_record_sequence(self) -> Path:
        return self._safe_study_path(
            Path("GRAPH_RECORDS.sequence.json"),
            label="GRAPH_RECORDS.sequence.json",
        )

    @property
    def checkpoints(self) -> Path:
        return self._safe_study_path(Path("checkpoints"), label="checkpoints directory")

    @property
    def generated(self) -> Path:
        return self._safe_study_path(Path("generated"), label="generated directory")

    @property
    def brief_history(self) -> Path:
        return self._safe_study_path(Path("brief-history"), label="brief-history directory")

    @property
    def active_work(self) -> Path:
        return self._safe_study_path(Path("work/active"), label="active work directory")

    @property
    def archived_work(self) -> Path:
        return self._safe_study_path(Path("work/archived"), label="archived work directory")

    def assert_safe_layout(self, *, must_exist: bool = True) -> None:
        """Reject a Study layout that can redirect workflow I/O through symlinks.

        Missing leaf paths are safe during initialization.  Every existing
        component below the configured Study root, including dynamic Run,
        Evidence, formal, work, checkpoint, and generated files, is checked
        without following symbolic links.
        """

        repository = self.root.resolve()
        study_root = self.study_root_path
        if study_root.exists() and not study_root.is_dir():
            raise ValidationError("configured Study root must be a directory")
        study = self.study
        if not study.exists():
            if must_exist:
                raise WorkflowError(f"study does not exist: {self.study_id}")
            return
        if not study.is_dir():
            raise ValidationError(f"Study path must be a directory: {study}")

        _assert_resolves_within(study_root, repository, label="configured Study root")
        _assert_managed_tree_safe(study, study_root=study_root, repository=repository)

        for directory in (
            self.formal,
            self.experiment_intents,
            self.control_graphs,
            self.work,
            self.runs,
            self.observations,
            self.evidence,
            self.checkpoints,
            self.generated,
            self.brief_history,
        ):
            if directory.exists() and not directory.is_dir():
                raise ValidationError(f"managed Study directory is not a directory: {directory}")
        # Resolve nested managed directories only after their parents have
        # been proved to be directories.  Eager property evaluation would
        # otherwise turn a clear parent-type violation into an incidental
        # ENOTDIR while inspecting the child path.
        nested_directories: list[Path] = []
        if self.formal.is_dir():
            nested_directories.append(self.confirmations)
        if self.work.is_dir():
            nested_directories.extend((self.active_work, self.archived_work))
        for directory in nested_directories:
            if directory.exists() and not directory.is_dir():
                raise ValidationError(
                    f"managed Study directory is not a directory: {directory}"
                )
        for artifact in (
            self.brief,
            self.brief_approval,
            self.claims,
            self.observation_sequence,
            self.evidence_sequence,
            self.checkpoint_sequence,
            self.graph_record_sequence,
            self.verdict,
        ):
            if artifact.exists() and not artifact.is_file():
                raise ValidationError(f"managed Study artifact is not a regular file: {artifact}")


def _assert_no_symlink_components(anchor: Path, path: Path, *, label: str) -> None:
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ValidationError(f"{label} must stay inside {anchor}") from exc
    current = anchor
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # A descendant cannot exist before its parent exists.  The lexical
            # containment check still protects the not-yet-created tail.
            break
        except OSError as exc:
            raise ValidationError(f"cannot inspect {label} path {current}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(f"{label} must not use a symbolic link: {current}")


def _assert_resolves_within(path: Path, boundary: Path, *, label: str) -> None:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"cannot resolve {label} path {path}: {exc}") from exc
    try:
        resolved.relative_to(boundary.resolve())
    except ValueError as exc:
        raise ValidationError(f"{label} resolves outside {boundary}: {path}") from exc


def _assert_managed_tree_safe(study: Path, *, study_root: Path, repository: Path) -> None:
    pending = [study]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ValidationError(f"cannot inspect managed Study directory {directory}: {exc}") from exc
        for entry in entries:
            candidate = Path(entry.path)
            try:
                if entry.is_symlink():
                    raise ValidationError(
                        f"managed Study paths must not use symbolic links: {candidate}"
                    )
                _assert_resolves_within(candidate, study_root, label="managed Study path")
                _assert_resolves_within(candidate, repository, label="managed Study path")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(candidate)
                elif not entry.is_file(follow_symlinks=False):
                    raise ValidationError(
                        f"managed Study entry must be a regular file or directory: {candidate}"
                    )
            except OSError as exc:
                raise ValidationError(f"cannot inspect managed Study path {candidate}: {exc}") from exc


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
    resolved_root = root.resolve()
    configured_root = "studies"
    profile = resolved_root / "scientific-workflow" / "repository-profile.json"
    if profile.is_symlink():
        raise ValidationError("repository profile must not be a symbolic link")
    if profile.exists():
        if not profile.is_file():
            raise ValidationError("repository profile must be a regular file")
        try:
            value = json.loads(profile.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read repository profile {profile}: {exc}") from exc
        raw_root = value.get("study_root") if isinstance(value, dict) else None
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValidationError("repository profile study_root must be a non-empty string")
        candidate = Path(raw_root)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError("repository profile study_root must stay inside the repository")
        if candidate.as_posix().rstrip("/") in {"", "."}:
            raise ValidationError("repository profile study_root must not be the repository root")
        configured_path = resolved_root / candidate
        _assert_no_symlink_components(
            resolved_root, configured_path, label="repository profile study_root"
        )
        _assert_resolves_within(
            configured_path, resolved_root, label="repository profile study_root"
        )
        configured_root = candidate.as_posix().rstrip("/")
    paths = StudyPaths(resolved_root, study_id, configured_root)
    paths.assert_safe_layout(must_exist=must_exist)
    return paths


def errors_only(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    return [issue for issue in issues if issue.level == "ERROR"]
