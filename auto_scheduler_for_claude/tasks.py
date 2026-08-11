"""
Task Queue
===========

Loads the optional ``auto-scheduler-tasks.json`` task registry and decides
which queued tasks may start at the current five-hour utilization.

Pure module: it reads the registry and computes a selection, but never
spawns a process and never writes a file.  Writing the registry lives in
``task_registry``; every other side effect lives in ``task_runner``.
"""
from __future__ import annotations

import ctypes
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .instance_id import effective_config_dir

__all__ = [
    'PRINT_MODE_CAVEAT', 'TASKS_FILENAME', 'Task', 'ValidationError',
    'load_tasks', 'sanitize_task_id', 'select_runnable', 'validate_entries',
]

TASKS_FILENAME = 'auto-scheduler-tasks.json'

# Default when a task omits ``timeout_minutes``.  ``0`` means "no time limit"
# and is deliberately a valid value, so the default cannot be expressed as 0.
DEFAULT_TIMEOUT_MINUTES = 180.0

# Appended verbatim to every task prompt, copied from docs/execute-claude-tasks.md.
# No task can opt out: a print-mode run ends the moment the final message is
# written, orphaning any background work the agent left running.
PRINT_MODE_CAVEAT = (
    ' IMPORTANT: you run non-interactively via claude -p - when your final message ends, '
    'this process exits and any background task you launched is orphaned (its output is lost). '
    'Never end the turn while background work is unfinished: either wait for it and score it now, '
    'or journal where its on-disk artifacts will land and queue the follow-up in '
    'state/next_actions.md before finishing.'
)

_KNOWN_FIELDS = frozenset({'id', 'prompt', 'prompt_file', 'max_usage_percent', 'cwd', 'timeout_minutes', 'description'})

_UNSAFE_FILENAME_CHARS = re.compile(r'[^A-Za-z0-9._-]+')


@dataclass(frozen=True)
class ValidationError:
    """One problem found in a registry entry.

    Attributes
    ----------
    index : int
        Position of the offending entry in the registry array.
    task_id : str | None
        The entry's id, when it was readable.
    field : str | None
        Name of the offending field, so an editor can point at it.  None
        when the problem is with the entry as a whole.
    message : str
        The full human-readable line, as shown in the startup message box.
    """

    index: int
    task_id: str | None
    field: str | None
    message: str


@dataclass(frozen=True)
class Task:
    """One queued ``claude -p`` run.

    Attributes
    ----------
    id : str
        Unique key, used for the state record and the log file name.
    prompt : str
        The resolved prompt text, without ``PRINT_MODE_CAVEAT``: either the
        inline ``prompt`` as written by the user, or the contents of
        ``prompt_file`` when one was given.
    prompt_file : str | None
        Resolved path the prompt was read from, or None for an inline prompt.
        Recorded for the log header only - the text is read once at startup.
    max_usage_percent : float
        Ceiling on five-hour consumption.  The task starts when current
        utilization is at or below this value.
    timeout_minutes : float
        Wall-clock limit for the run.  ``0`` means no limit.
    cwd : str | None
        Working directory, or None to use the app's working directory.
    description : str
        Tray-menu label only.
    """

    id: str
    prompt: str
    max_usage_percent: float
    timeout_minutes: float
    cwd: str | None
    description: str
    prompt_file: str | None = None


def load_tasks() -> list[Task]:
    """Read and validate the task registry from the effective config dir.

    A missing file is not an error - it simply means the queue is off.  A
    file that exists but contains any invalid entry disables the **whole**
    queue for the session (a partially loaded queue of unattended agents is
    worse than no queue) and reports every problem in one message box.

    Returns
    -------
    list[Task]
        The validated tasks in file order, or an empty list when the file is
        absent, unreadable, or invalid.
    """
    path = effective_config_dir() / TASKS_FILENAME
    if not path.is_file():
        return []

    try:
        # utf-8-sig also reads BOM-less UTF-8, matching settings.py.
        text = path.read_text(encoding='utf-8-sig').strip()
    except OSError as exc:
        _show_error(f'Cannot read the task file:\n{path}\n\n{exc}')
        return []

    if not text:
        return []

    try:
        entries = json.loads(text)
    except json.JSONDecodeError as exc:
        _show_error(f'Invalid JSON in the task file:\n{path}\n\n{exc}')
        return []

    if not isinstance(entries, list):
        _show_error(f'Invalid task file:\n{path}\n\nExpected a JSON array, got {type(entries).__name__}.')
        return []

    tasks, errors = validate_entries(entries)
    if errors:
        _show_error(
            f'Invalid entries in the task file:\n{path}\n\n' + '\n'.join(error.message for error in errors)
            + '\n\nThe task queue is disabled until these are fixed.'
        )
        return []

    return tasks


def select_runnable(
    tasks: list[Task], completed_ids: set[str], running_ids: set[str],
    five_hour_pct: float, slots: int,
) -> list[Task]:
    """Return the tasks to start now, in file order, at most *slots* of them.

    A task qualifies when it is neither completed nor already running and the
    current five-hour utilization is **at or below** its
    ``max_usage_percent``.  A task whose threshold is not met is skipped, not
    treated as a barrier: the scan continues, so a cheap high-threshold task
    can claim a slot an expensive low-threshold task cannot yet use.

    Parameters
    ----------
    tasks : list[Task]
        The loaded registry, in file order.
    completed_ids : set[str]
        Ids already recorded in the state file (including interrupted runs).
    running_ids : set[str]
        Ids with a live child process.
    five_hour_pct : float
        Current five-hour utilization, 0-100.
    slots : int
        Free concurrency slots.  Zero or fewer selects nothing.
    """
    if slots <= 0:
        return []

    selected: list[Task] = []
    for task in tasks:
        if task.id in completed_ids or task.id in running_ids:
            continue
        if five_hour_pct > task.max_usage_percent:
            continue
        selected.append(task)
        if len(selected) == slots:
            break

    return selected


def sanitize_task_id(task_id: str) -> str:
    """Return *task_id* reduced to characters that are safe in a file name."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub('_', task_id).strip('._-')
    return cleaned or 'task'


def validate_entries(entries: list) -> tuple[list[Task], list[ValidationError]]:
    """Convert raw registry entries to tasks, collecting every problem found.

    The single validator for the registry, shared by :func:`load_tasks` and by
    the popup's task editor: an entry the editor accepts is an entry the next
    startup accepts, and the editor can never write a file that would disable
    the queue.

    Pure - it reads ``prompt_file`` from disk (the prompt text is part of a
    valid entry) but writes nothing and shows no message box.

    Parameters
    ----------
    entries : list
        The raw JSON array from the registry file.

    Returns
    -------
    tuple[list[Task], list[ValidationError]]
        The tasks that validated, and every problem found.  A non-empty
        error list means the whole registry is rejected - callers must not
        use the partial task list.
    """
    tasks: list[Task] = []
    errors: list[ValidationError] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(entries):
        label = f'  entry {index + 1}'
        if not isinstance(entry, dict):
            errors.append(ValidationError(index, None, None, f'{label}: expected an object, got {type(entry).__name__}'))
            continue

        task_id = entry.get('id')
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append(ValidationError(index, None, 'id', f'{label}: "id" is required and must be a non-empty string'))
            continue

        task_id = task_id.strip()
        label = f'  task "{task_id}"'
        if task_id in seen_ids:
            errors.append(ValidationError(index, task_id, 'id', f'{label}: duplicate id'))
            continue
        seen_ids.add(task_id)

        entry_errors: list[ValidationError] = []

        def fail(field: str | None, message: str) -> None:
            entry_errors.append(ValidationError(index, task_id, field, f'{label}: {message}'))

        # Validated before the prompt because a relative "prompt_file" resolves against it.
        cwd = entry.get('cwd')
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd.strip():
                fail('cwd', '"cwd" must be a non-empty string')
            elif not Path(cwd).is_dir():
                fail('cwd', f'"cwd" is not an existing directory: {cwd}')

        working_dir = cwd.strip() if isinstance(cwd, str) and cwd.strip() else None

        prompt, prompt_file, prompt_field, prompt_error = _resolve_prompt(entry, working_dir)
        if prompt_error is not None:
            fail(prompt_field, prompt_error)

        max_usage_percent = entry.get('max_usage_percent')
        if isinstance(max_usage_percent, bool) or not isinstance(max_usage_percent, (int, float)):
            fail('max_usage_percent', '"max_usage_percent" is required and must be a number between 0 and 100')
        elif not (0 <= max_usage_percent <= 100):
            fail('max_usage_percent', f'"max_usage_percent" must be between 0 and 100, got {max_usage_percent}')

        timeout_minutes = entry.get('timeout_minutes', DEFAULT_TIMEOUT_MINUTES)
        if isinstance(timeout_minutes, bool) or not isinstance(timeout_minutes, (int, float)):
            fail('timeout_minutes', '"timeout_minutes" must be a number (0 means no time limit)')
        elif timeout_minutes < 0:
            fail('timeout_minutes', f'"timeout_minutes" must be >= 0, got {timeout_minutes}')

        description = entry.get('description', '')
        if not isinstance(description, str):
            fail('description', '"description" must be a string')

        unknown = sorted(set(entry) - _KNOWN_FIELDS)
        if unknown:
            fail(None, f'unknown field(s): {", ".join(unknown)}')

        if entry_errors:
            errors.extend(entry_errors)
            continue

        tasks.append(Task(
            id=task_id,
            prompt=prompt,                                       # type: ignore[arg-type]  # validated above
            max_usage_percent=float(max_usage_percent),          # type: ignore[arg-type]  # validated above
            timeout_minutes=float(timeout_minutes),
            cwd=working_dir,
            description=description,
            prompt_file=prompt_file,
        ))

    return tasks, errors


def _resolve_prompt(entry: dict, working_dir: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Return the prompt text, its source file, and the first problem found.

    ``prompt_file`` wins whenever it is present: the inline ``prompt`` is then
    ignored completely, so a leftover draft can never run instead of the file
    the user pointed at.  The file is read here, once at startup, and must
    contain more than whitespace - an unattended agent must not be launched
    with an empty prompt.

    Returns
    -------
    tuple[str | None, str | None, str | None, str | None]
        ``(prompt, prompt_file, error_field, error_message)``.  The last two
        are None when the prompt resolved cleanly.
    """
    raw_path = entry.get('prompt_file')
    if raw_path is None:
        prompt = entry.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            return None, None, 'prompt', '"prompt" is required and must be a non-empty string (or use "prompt_file")'
        return prompt, None, None, None

    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, None, 'prompt_file', '"prompt_file" must be a non-empty string'

    path = _resolve_prompt_path(raw_path.strip(), working_dir)
    if not path.is_file():
        return None, None, 'prompt_file', f'"prompt_file" is not an existing file: {path}'

    try:
        text = path.read_text(encoding='utf-8-sig').strip()
    except OSError as exc:
        return None, None, 'prompt_file', f'"prompt_file" cannot be read: {path} ({exc})'

    if not text:
        return None, None, 'prompt_file', f'"prompt_file" is empty: {path}'

    return text, str(path), None, None


def _resolve_prompt_path(raw_path: str, working_dir: str | None) -> Path:
    """Resolve a prompt file path against the task's cwd, else the config dir."""
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path

    base = Path(working_dir) if working_dir else effective_config_dir()
    return base / path


def _show_error(message: str) -> None:
    """Show a task-queue error message box."""
    ctypes.windll.user32.MessageBoxW(0, message[:2000], 'Auto Scheduler for Claude - Task Queue Error', 0x30)
