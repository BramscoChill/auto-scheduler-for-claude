"""
Task Registry Writer
=====================

Reads and writes ``auto-scheduler-tasks.json`` on behalf of the popup's task
editor.  This is the only module that writes the task registry, and it only
ever runs in response to an explicit user action in the editor - never from a
poll, a timer, or any background thread.

Split out rather than folded into ``tasks`` (which must stay pure) or
``task_runner`` (which owns processes, state, and logs): the registry is a
user-owned file, so its write path has its own concerns - a stale-file guard,
atomic replacement, and a refusal to ever write content the loader would
reject.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .instance_id import effective_config_dir
from .tasks import TASKS_FILENAME, Task, ValidationError, validate_entries

__all__ = [
    'ERROR_INVALID', 'ERROR_READ', 'ERROR_STALE', 'ERROR_WRITE', 'MISSING_STAMP',
    'read_registry', 'registry_path', 'write_registry',
]

# Error keys returned to the caller, which maps them to localized text.  This
# module stays free of UI strings for the same reason ``task_runner`` does.
ERROR_READ = 'registry_error_read'          # the file exists but cannot be read or parsed
ERROR_STALE = 'registry_error_stale'        # the file changed on disk since it was read
ERROR_INVALID = 'registry_error_invalid'    # the prospective content fails validation
ERROR_WRITE = 'registry_error_write'        # the atomic replace failed

# Stamp reported for a registry file that does not exist yet.
MISSING_STAMP = (0.0, -1)


def registry_path() -> Path:
    """Return the registry path inside the effective config dir."""
    return effective_config_dir() / TASKS_FILENAME


def read_registry() -> tuple[list[dict[str, Any]], tuple[float, int], str | None]:
    """Read the raw registry entries and a stamp identifying the file version.

    Returns the entries as written, not as :class:`~.tasks.Task` objects: the
    editor edits the file's own shape, including the ``prompt`` /
    ``prompt_file`` distinction that ``Task`` has already collapsed.

    A missing file is not an error - it reads as an empty registry with
    :data:`MISSING_STAMP`, which is what lets the editor create the first
    task.

    Returns
    -------
    tuple[list[dict], tuple[float, int], str | None]
        ``(entries, stamp, error_key)``.  On error the entries are empty and
        the stamp is :data:`MISSING_STAMP`.
    """
    path = registry_path()
    if not path.is_file():
        return [], MISSING_STAMP, None

    try:
        stamp = _stamp(path)
        text = path.read_text(encoding='utf-8-sig').strip()
    except OSError:
        return [], MISSING_STAMP, ERROR_READ

    if not text:
        return [], stamp, None

    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        return [], MISSING_STAMP, ERROR_READ

    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return [], MISSING_STAMP, ERROR_READ

    return entries, stamp, None


def write_registry(
    entries: list[dict[str, Any]], expected_stamp: tuple[float, int],
) -> tuple[list[Task] | None, list[ValidationError], str | None]:
    """Validate and atomically write the whole registry.

    Two refusals guard a user-owned file:

    * the on-disk stamp must still match *expected_stamp* - the user may have
      edited the file in an editor since the popup read it, and silently
      overwriting those edits (or merging them) is worse than asking for a
      reload;
    * the prospective content must pass :func:`~.tasks.validate_entries` -
      writing a file the next startup would reject turns one bad edit into a
      disabled queue.

    Parameters
    ----------
    entries : list[dict]
        The complete prospective registry, in file order.  File order is
        queue priority, so callers must preserve it.
    expected_stamp : tuple[float, int]
        The stamp returned by the :func:`read_registry` call the edit was
        based on.

    Returns
    -------
    tuple[list[Task] | None, list[ValidationError], str | None]
        ``(tasks, validation_errors, error_key)``.  On success the validated
        tasks are returned so the caller can hand them straight to
        ``TaskRunner.reload()`` without a second read and a second validation
        pass.
    """
    path = registry_path()
    current_stamp = _stamp(path) if path.is_file() else MISSING_STAMP
    if current_stamp != expected_stamp:
        return None, [], ERROR_STALE

    tasks, errors = validate_entries(entries)
    if errors:
        return None, errors, ERROR_INVALID

    temp = path.with_suffix(path.suffix + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temp, path)
    except OSError:
        return None, [], ERROR_WRITE

    return tasks, [], None


def _stamp(path: Path) -> tuple[float, int]:
    """Return the ``(mtime, size)`` pair used to detect out-of-band edits."""
    try:
        info = path.stat()
    except OSError:
        return MISSING_STAMP

    return (info.st_mtime, info.st_size)
