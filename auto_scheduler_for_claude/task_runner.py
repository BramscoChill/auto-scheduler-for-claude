"""
Task Runner
============

Executes queued tasks as unattended ``claude -p`` child processes when the
five-hour quota leaves enough headroom, and owns everything that has a side
effect: the child processes, the state file, the per-run logs, concurrency,
and shutdown.

The selection rules themselves stay pure in ``tasks``.

Security note: tasks are launched with ``--dangerously-skip-permissions``.
That is what makes unattended execution possible and it is not optional -
see ``README.md`` and ``PRIVACY.md``.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable

from . import __version__
from .claude_cli import CLAUDE_CLI_PATH
from .formatting import quota_snapshot_env
from .instance_id import effective_config_dir
from .settings import MAX_CONCURRENT_TASKS, TASK_COMMAND
from .tasks import PRINT_MODE_CAVEAT, Task, load_tasks, sanitize_task_id, select_runnable

__all__ = [
    'EXIT_KILLED', 'EXIT_TIMEOUT', 'LOGS_DIRNAME', 'STATE_FILENAME',
    'STATUS_DONE', 'STATUS_INTERRUPTED', 'STATUS_PENDING', 'STATUS_RUNNING', 'STATUS_WAITING',
    'TaskRunner',
]

STATE_FILENAME = 'auto-scheduler-state.json'
LOGS_DIRNAME = 'auto-scheduler-logs'
STATE_VERSION = 1

# Exit codes recorded for runs the app itself ended.
EXIT_TIMEOUT = 124                             # timeout_minutes elapsed, child killed
EXIT_KILLED = 130                              # user quit and chose to kill running tasks

STATUS_WAITING = 'waiting'                     # five-hour utilization still above the threshold
STATUS_PENDING = 'pending'                     # threshold met, but every slot is occupied
STATUS_RUNNING = 'running'                     # a child process is live
STATUS_DONE = 'done'                           # finished, exit code recorded
STATUS_INTERRUPTED = 'interrupted'             # started but never finished - the app died mid-run


@dataclass
class _Run:
    """A live task run and the handles that must be closed when it ends."""

    task: Task
    process: subprocess.Popen
    log_handle: IO[str]
    log_name: str
    started_at: str
    started_monotonic: float
    forced_exit_code: int | None = None
    finalized: bool = False


class TaskRunner:
    """Owns the task queue's mutable cross-thread state behind one lock.

    A class rather than free functions (against the project's functional
    preference) for the same reason ``UsageCache`` is one: running processes,
    completed ids, and the drain flag are mutated from the poll thread, the
    popup refresh thread, the tray thread, and one watcher thread per task.
    """

    def __init__(self, on_drained: Callable[[], None] | None = None) -> None:
        """Load the registry and the previous state.

        Parameters
        ----------
        on_drained : Callable[[], None] | None
            Called from the last watcher thread once every running task has
            finished while draining.  The app passes the tray icon's stop.
        """
        self._lock = threading.Lock()
        self._on_drained = on_drained

        self._draining = False
        self._disabled = False
        self._running: dict[str, _Run] = {}
        self._records: dict[str, dict[str, Any]] = {}

        # Last five-hour utilization seen by evaluate(), so the tray menu can
        # tell "waiting for quota" apart from "waiting for a free slot".
        self._last_five_hour_pct: float | None = None

        self._tasks = load_tasks()
        if self._tasks:
            self._load_state()

    # Queue state

    @property
    def tasks(self) -> list[Task]:
        """The loaded registry, in file order.  Empty when the queue is off."""
        return self._tasks

    def has_tasks(self) -> bool:
        """Return whether a task file was loaded (drives tray-menu visibility)."""
        return bool(self._tasks)

    def has_pending_work(self) -> bool:
        """Return whether any task is still running or still waiting for its slot.

        Drives the poll loop's idle bypass: with pending work the app keeps
        polling while the user is away, so the queue drains overnight.
        """
        with self._lock:
            if self._disabled:
                return False
            if self._running:
                return True
            if self._draining:
                return False
            return any(task.id not in self._records for task in self._tasks)

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def menu_entries(self) -> list[tuple[str, str]]:
        """Return one ``(task id, status key)`` pair per task, in file order.

        The status key is one of the ``STATUS_*`` constants; localizing it is
        the caller's job, so this module stays free of UI text.
        """
        with self._lock:
            return [(task.id, self._status(task)) for task in self._tasks]

    def status(self, task_id: str) -> str:
        """Return the current ``STATUS_*`` key for one task id."""
        with self._lock:
            for task in self._tasks:
                if task.id == task_id:
                    return self._status(task)

        return STATUS_WAITING

    def exit_code(self, task_id: str) -> int | None:
        """Return the recorded exit code of a finished task, or None."""
        with self._lock:
            return self._records.get(task_id, {}).get('exit_code')

    def log_path(self, task_id: str) -> Path | None:
        """Return the log file of a recorded run, or None when there is none."""
        with self._lock:
            log_name = self._records.get(task_id, {}).get('log')

        if not log_name:
            return None

        path = self._logs_dir() / log_name
        return path if path.is_file() else None

    # Evaluation

    def evaluate(self, data: dict[str, Any]) -> None:
        """Start whatever the current quota snapshot allows.

        Called after every successful update, from both the poll thread and
        the popup's background refresh thread, so the whole select-and-start
        sequence runs under the lock.

        Parameters
        ----------
        data : dict[str, Any]
            The successful API response.  A missing or null ``five_hour``
            quota starts nothing - the queue is gated on that field alone.
        """
        five_hour_pct = (data.get('five_hour') or {}).get('utilization')
        if five_hour_pct is None:
            return

        with self._lock:
            self._last_five_hour_pct = five_hour_pct
            if self._disabled or self._draining or not self._tasks:
                return

            slots = MAX_CONCURRENT_TASKS - len(self._running)
            if slots <= 0:
                return

            runnable = select_runnable(self._tasks, set(self._records), set(self._running), five_hour_pct, slots)
            for task in runnable:
                if not self._start(task, data):
                    break

    # Shutdown

    def begin_drain(self) -> None:
        """Stop starting new tasks, letting the running ones finish."""
        with self._lock:
            self._draining = True

    def is_draining(self) -> bool:
        with self._lock:
            return self._draining

    def kill_all(self) -> None:
        """Terminate every running child and record exit code 130.

        The kill and the wait happen outside the lock so a watcher thread
        finalizing at the same moment is never blocked; whichever path gets
        there first writes the record, the other one finds it finalized.
        """
        with self._lock:
            self._draining = True
            runs = list(self._running.values())

        for run in runs:
            run.forced_exit_code = EXIT_KILLED
            try:
                run.process.kill()
                run.process.wait(timeout=5)
            except Exception:
                traceback.print_exc()
            self._finalize(run, EXIT_KILLED)

    # Internals

    def _status(self, task: Task) -> str:
        """Return the status key for *task*.  Caller holds the lock.

        An unstarted task is *pending* when its threshold is already met (so
        only a free slot is missing) and *waiting* while five-hour utilization
        is still above it - including before the first quota snapshot arrives.
        """
        if task.id in self._running:
            return STATUS_RUNNING

        record = self._records.get(task.id)
        if record is not None:
            return STATUS_DONE if record.get('finished_at') is not None else STATUS_INTERRUPTED

        threshold_met = self._last_five_hour_pct is not None and self._last_five_hour_pct <= task.max_usage_percent
        return STATUS_PENDING if threshold_met else STATUS_WAITING

    def _state_path(self) -> Path:
        return effective_config_dir() / STATE_FILENAME

    def _logs_dir(self) -> Path:
        return effective_config_dir() / LOGS_DIRNAME

    def _load_state(self) -> None:
        """Read the state file, or disable the queue when it cannot be parsed.

        A state file that exists but is unreadable must never be treated as
        empty: that would re-run every task the app already ran.
        """
        path = self._state_path()
        if not path.is_file():
            return

        try:
            state = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, json.JSONDecodeError) as exc:
            self._disable(f'Cannot read the task state file:\n{path}\n\n{exc}')
            return

        records = state.get('tasks') if isinstance(state, dict) else None
        if not isinstance(records, dict):
            self._disable(f'Invalid task state file:\n{path}\n\nExpected an object with a "tasks" object.')
            return

        # Every id present counts as complete, including a record with no
        # finished_at: the app died mid-run and an unattended agent that got
        # halfway must not be silently restarted.
        self._records = {task_id: value for task_id, value in records.items() if isinstance(value, dict)}

    def _write_state(self) -> bool:
        """Write the state file atomically.  Caller holds the lock."""
        path = self._state_path()
        temp = path.with_suffix(path.suffix + '.tmp')
        payload = {'version': STATE_VERSION, 'tasks': self._records}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
            os.replace(temp, path)
            return True
        except OSError as exc:
            self._disable(f'Cannot write the task state file:\n{path}\n\n{exc}')
            return False

    def _start(self, task: Task, data: dict[str, Any]) -> bool:
        """Launch *task*.  Caller holds the lock.  Returns False on failure.

        A failure to prepare the log or record the start disables the queue
        for the session rather than running a task whose output would be lost.
        """
        now = datetime.now(timezone.utc)
        log_name = f'{sanitize_task_id(task.id)}-{now.strftime("%Y%m%d-%H%M%S")}.log'
        logs_dir = self._logs_dir()

        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_handle = open(logs_dir / log_name, 'w', encoding='utf-8', errors='replace', buffering=1)
        except OSError as exc:
            self._disable(f'Cannot open the task log file:\n{logs_dir / log_name}\n\n{exc}')
            return False

        command = self._build_command(task)
        working_dir = task.cwd or None

        try:
            log_handle.write(f'=== task {task.id} ===\n')
            log_handle.write(f'started: {now.isoformat()}\n')
            if task.prompt_file:
                log_handle.write(f'prompt:  {task.prompt_file}\n')
            log_handle.write(f'command: {command}\n')
            log_handle.write(f'cwd:     {working_dir or os.getcwd()}\n')
            log_handle.write(f'timeout: {"none" if task.timeout_minutes == 0 else f"{task.timeout_minutes:g} min"}\n\n')
            log_handle.flush()

            process = subprocess.Popen(
                command, cwd=working_dir, env=self._build_env(task, data),
                stdout=log_handle, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            log_handle.close()
            self._disable(f'Cannot start the queued task "{task.id}":\n\n{exc}')
            return False

        run = _Run(
            task=task, process=process, log_handle=log_handle, log_name=log_name,
            started_at=now.isoformat(), started_monotonic=time.monotonic(),
        )
        self._running[task.id] = run
        self._records[task.id] = {
            'started_at': run.started_at, 'finished_at': None, 'exit_code': None, 'log': log_name,
        }
        self._write_state()

        threading.Thread(target=self._watch, args=(run,), daemon=True).start()
        return True

    def _build_command(self, task: Task) -> list[str]:
        """Return the argv for *task*, with the print-mode caveat appended."""
        base = list(TASK_COMMAND) if TASK_COMMAND else [str(CLAUDE_CLI_PATH)]
        return [*base, '-p', task.prompt + PRINT_MODE_CAVEAT, '--dangerously-skip-permissions']

    def _build_env(self, task: Task, data: dict[str, Any]) -> dict[str, str]:
        """Return the child environment: the current one plus the quota snapshot."""
        return {
            **os.environ,
            **quota_snapshot_env(data),
            'AUTO_SCHEDULER_EVENT': 'task',
            'AUTO_SCHEDULER_TASK_ID': task.id,
            'AUTO_SCHEDULER_VERSION': __version__,
        }

    def _watch(self, run: _Run) -> None:
        """Wait for one child, then record its outcome.  Runs on a daemon thread."""
        timeout = run.task.timeout_minutes * 60 if run.task.timeout_minutes > 0 else None
        try:
            exit_code = run.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                run.process.kill()
                run.process.wait()
            except Exception:
                traceback.print_exc()
            exit_code = EXIT_TIMEOUT
        except Exception:
            traceback.print_exc()
            exit_code = 1

        self._finalize(run, exit_code)

    def _finalize(self, run: _Run, exit_code: int) -> None:
        """Record the outcome once, close the log, and drain if this was the last run."""
        with self._lock:
            if run.finalized:
                return
            run.finalized = True

            if run.forced_exit_code is not None:
                exit_code = run.forced_exit_code

            duration = time.monotonic() - run.started_monotonic
            finished_at = datetime.now(timezone.utc).isoformat()

            self._running.pop(run.task.id, None)
            self._records[run.task.id] = {
                'started_at': run.started_at, 'finished_at': finished_at,
                'exit_code': exit_code, 'log': run.log_name,
            }
            self._write_state()

            drained = self._draining and not self._running

        try:
            run.log_handle.write(f'\n=== finished: {finished_at} ===\n')
            run.log_handle.write(f'exit code: {exit_code}\n')
            run.log_handle.write(f'duration:  {duration:.0f}s\n')
            run.log_handle.close()
        except Exception:
            traceback.print_exc()

        if drained and self._on_drained is not None:
            self._on_drained()

    def _disable(self, message: str) -> None:
        """Disable the queue for the session and report why.

        Reported once: a repeated failure (e.g. an unwritable state file) must
        not stack message boxes on top of the first one.
        """
        if self._disabled:
            return

        self._disabled = True
        ctypes.windll.user32.MessageBoxW(0, message[:2000], 'Auto Scheduler for Claude - Task Queue Error', 0x30)
