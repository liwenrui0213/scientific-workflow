from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .hashing import load_json
from .models import (
    CLAIMS_SCHEMA_VERSION,
    ID_PATTERNS,
    ValidationError,
    WorkflowError,
    require_id,
    study_paths,
)
from .validation import validate_study
from .workspace import load_repository_profile


_MISSING_APPROVAL = "Brief has not been approved"
_EDITABLE_BRIEF_ERRORS = {"Brief still contains replacement placeholders"}
_ROUTE_SKILLS = {
    "draft": "start-scientific-study",
    "approved": "scientific-study",
}


@dataclass(frozen=True)
class StudyCandidate:
    """A deterministic routing view of one Study-shaped directory."""

    study_id: str
    phase: str
    detail: str

    @property
    def skill(self) -> str | None:
        return _ROUTE_SKILLS.get(self.phase)

    def route_record(self) -> dict[str, str]:
        if self.skill is None:
            raise WorkflowError(f"invalid Study cannot be routed: {self.study_id}")
        return {
            "study_id": self.study_id,
            "phase": self.phase,
            "skill": self.skill,
        }


def _invalid_candidate(study_id: str, detail: str) -> StudyCandidate:
    return StudyCandidate(study_id=study_id, phase="invalid", detail=detail)


def _classify_safe_candidate(root: Path, study_id: str) -> StudyCandidate:
    try:
        paths = study_paths(root, study_id)
        errors = [issue for issue in validate_study(paths) if issue.level == "ERROR"]
    except (OSError, UnicodeError, ValidationError, WorkflowError) as exc:
        return _invalid_candidate(study_id, str(exc))

    try:
        claims = load_json(paths.claims)
    except (OSError, UnicodeError, ValidationError) as exc:
        claims = None
        if not errors:
            return _invalid_candidate(study_id, str(exc))
    if (
        isinstance(claims, dict)
        and claims.get("schema_version") != CLAIMS_SCHEMA_VERSION
    ):
        return _invalid_candidate(
            study_id,
            "legacy Claims schema is historical-validation-only; migrate it "
            f"to bounded schema_version {CLAIMS_SCHEMA_VERSION} before resuming",
        )

    if not errors:
        return StudyCandidate(
            study_id=study_id,
            phase="approved",
            detail="active Brief approval is fresh",
        )
    missing_approval = any(
        issue.path == str(paths.brief_approval)
        and issue.message == _MISSING_APPROVAL
        for issue in errors
    )
    non_approval_errors = [
        issue
        for issue in errors
        if not (
            issue.path == str(paths.brief_approval)
            and issue.message == _MISSING_APPROVAL
        )
    ]
    # A newly initialized Study and ``brief-new-version`` deliberately leave
    # an unapproved, editable BRIEF.md which may still contain placeholders or
    # other Brief-local validation errors.  Those are intake work for the
    # start Skill, not evidence that the Study is damaged.  Any error outside
    # the active Brief still fails closed.
    if missing_approval and all(
        issue.path == str(paths.brief)
        and issue.message in _EDITABLE_BRIEF_ERRORS
        for issue in non_approval_errors
    ):
        detail = (
            "editable Brief draft awaiting completion and human approval"
            if non_approval_errors
            else "structurally valid and awaiting human Brief approval"
        )
        return StudyCandidate(
            study_id=study_id,
            phase="draft",
            detail=detail,
        )
    diagnostic = next(
        (
            issue
            for issue in errors
            if issue.path not in {str(paths.brief), str(paths.brief_approval)}
        ),
        errors[0],
    )
    first = f"{Path(diagnostic.path).name}: {diagnostic.message}"
    suffix = "" if len(errors) == 1 else f" (+{len(errors) - 1} more)"
    return _invalid_candidate(study_id, f"{first}{suffix}")


def classify_study_dirs(root: Path) -> tuple[StudyCandidate, ...]:
    """Classify direct, valid-ID entries under the configured Study root.

    The scan never follows a Study-directory symbolic link. Non-Study entries
    are ignored, while Study-shaped files, links, and unsafe trees are retained
    as invalid candidates so an ID-less continuation cannot silently bypass
    ambiguous or damaged state.
    """

    repository = root.resolve()
    profile = load_repository_profile(repository)
    configured = Path(str(profile["study_root"]))
    study_root = repository / configured
    if not study_root.exists():
        return ()
    if study_root.is_symlink() or not study_root.is_dir():
        raise ValidationError(
            f"configured Study root must be a non-symbolic-link directory: {study_root}"
        )

    candidates: list[StudyCandidate] = []
    try:
        entries = sorted(os.scandir(study_root), key=lambda item: item.name)
    except OSError as exc:
        raise WorkflowError(f"cannot inspect configured Study root {study_root}: {exc}") from exc

    for entry in entries:
        if ID_PATTERNS["study"].fullmatch(entry.name) is None:
            continue
        try:
            if entry.is_symlink():
                candidates.append(
                    _invalid_candidate(entry.name, "Study directory must not be a symbolic link")
                )
                continue
            if not entry.is_dir(follow_symlinks=False):
                candidates.append(
                    _invalid_candidate(entry.name, "Study path must be a directory")
                )
                continue
        except OSError as exc:
            candidates.append(_invalid_candidate(entry.name, f"cannot inspect Study path: {exc}"))
            continue
        candidates.append(_classify_safe_candidate(repository, entry.name))
    return tuple(candidates)


def _candidate_summary(candidates: tuple[StudyCandidate, ...], *, limit: int = 8) -> str:
    if not candidates:
        return "none"
    rendered = [
        f"{candidate.study_id}[{candidate.phase}]"
        + (f": {candidate.detail}" if candidate.phase == "invalid" else "")
        for candidate in candidates[:limit]
    ]
    if len(candidates) > limit:
        rendered.append(f"... (+{len(candidates) - limit} more)")
    return "; ".join(rendered)


def resolve_study(root: Path, requested_id: str | None = None) -> StudyCandidate:
    """Resolve a continuation target without creating or modifying a Study."""

    candidates = classify_study_dirs(root)
    if requested_id is not None:
        require_id("study", requested_id)
        selected = [item for item in candidates if item.study_id == requested_id]
        if not selected:
            raise WorkflowError(
                f"requested Study does not exist: {requested_id}; "
                "do not initialize a replacement Study"
            )
        candidate = selected[0]
        if candidate.phase == "invalid":
            raise WorkflowError(
                f"requested Study is invalid: {_candidate_summary(tuple(selected))}; "
                "repair it instead of initializing a replacement Study"
            )
        return candidate

    summary = _candidate_summary(candidates)
    if len(candidates) == 0:
        raise WorkflowError(
            "cannot resolve an ID-less continuation; candidates: none. "
            "Ask the user once for an existing Study ID or an explicit request to start "
            "a new persistent Study; do not initialize a Study automatically"
        )
    if len(candidates) > 1:
        raise WorkflowError(
            f"cannot resolve an ID-less continuation; candidates: {summary}. "
            "Ask the user once to name the intended Study; do not initialize a new Study"
        )

    candidate = candidates[0]
    if candidate.phase == "invalid":
        raise WorkflowError(
            f"cannot resolve an ID-less continuation; candidate: {summary}. "
            "Ask the user once whether to repair this Study or name another; "
            "do not initialize a new Study"
        )
    return candidate
