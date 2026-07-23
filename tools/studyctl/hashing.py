from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from .models import ValidationError, WorkflowError


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_json_bytes(data: bytes, *, label: str = "<bytes>") -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {label}: {exc}") from exc


def load_json(path: Path) -> Any:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    return load_json_bytes(data, label=str(path))


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"value is not valid JSON: {exc}") from exc
    return (text + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise WorkflowError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def record_digest(value: dict[str, Any], field: str) -> str:
    stripped = dict(value)
    stripped.pop(field, None)
    return sha256_json(stripped)


def nested_record_digest(value: dict[str, Any], section: str, field: str) -> str:
    stripped = json.loads(canonical_json_bytes(value).decode("utf-8"))
    nested = stripped.get(section)
    if isinstance(nested, dict):
        nested.pop(field, None)
    return sha256_json(stripped)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    overwrite: bool = True,
    mode: int | None = None,
    before_replace: Callable[[Path], None] | None = None,
    require_parent_fsync: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and path.exists():
        raise WorkflowError(f"refusing to overwrite existing file: {path}")
    temp_path: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        if before_replace is not None:
            before_replace(temp_path)
        if overwrite:
            os.replace(temp_path, path)
            temp_path = None
        else:
            # Linking a fully fsynced same-directory temporary file gives us an
            # atomic create-if-absent operation; unlike a second exists() check,
            # it cannot overwrite a concurrently created authoritative record.
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise WorkflowError(f"refusing to overwrite existing file: {path}") from exc
            temp_path.unlink()
            temp_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if require_parent_fsync:
                raise WorkflowError(
                    f"cannot durably sync parent directory for {path}: {exc}"
                ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    overwrite: bool = True,
    mode: int | None = None,
    before_replace: Callable[[Path], None] | None = None,
    require_parent_fsync: bool = False,
) -> None:
    atomic_write_bytes(
        path,
        pretty_json_bytes(value),
        overwrite=overwrite,
        mode=mode,
        before_replace=before_replace,
        require_parent_fsync=require_parent_fsync,
    )


def file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or path.is_symlink():
        raise ValidationError(f"symbolic links are not accepted as artifacts: {path}")
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = str(resolved)
    if not resolved.is_file():
        raise ValidationError(f"only regular files are supported: {path}")
    return {
        "path": relative,
        "size": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }
