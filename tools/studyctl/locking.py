from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import os
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - scientific hosts are normally POSIX
    fcntl = None

from .models import StudyPaths, ValidationError, WorkflowError


@contextmanager
def study_authority_lock(paths: StudyPaths) -> Iterator[None]:
    """Serialize protected Study authority and Run-budget transitions.

    The Study directory inode is the lock domain, so replacing a managed child
    such as ``runs/`` cannot create a second authority domain.  POSIX releases
    the advisory lock automatically if the holder crashes.
    """

    if fcntl is None:
        raise WorkflowError(
            "atomic Study authority transitions require POSIX file locking on this host"
        )
    if paths.study.is_symlink() or not paths.study.is_dir():
        raise ValidationError(
            "Study directory is missing or is not a regular directory"
        )
    descriptor = os.open(
        paths.study,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def serialized_study_authority(function: Callable[..., Any]) -> Callable[..., Any]:
    """Run a StudyPaths-first operation in the shared authority lock domain."""

    @wraps(function)
    def wrapped(paths: StudyPaths, *args: Any, **kwargs: Any) -> Any:
        with study_authority_lock(paths):
            return function(paths, *args, **kwargs)

    return wrapped
