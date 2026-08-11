"""
Application
=============

System tray application class with adaptive polling and event handling.
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pystray  # type: ignore[import-untyped]  # no type stubs available

from .api import api_headers, read_access_token
from .autostart import is_autostart_enabled, set_autostart, sync_autostart_path
from .cache import UsageCache
from .claude_cli import PROJECT_URL
from .command import run_event_command
from .idle import get_idle_seconds, is_workstation_locked
from .instance_id import effective_config_dir, is_default_config_dir
from .settings import (
    ALERT_EXTRA_USAGE_SPENT, ALERT_TIME_AWARE, ALERT_TIME_AWARE_BELOW, ICON_FIELDS, IDLE_PAUSE, NOTIFY_CLAUDE_UPDATE,
    ON_DOUBLE_CLICK_COMMAND, ON_RESET_COMMAND, ON_STARTUP_COMMAND, ON_THRESHOLD_COMMAND,
    POLL_ERROR, POLL_FAST, POLL_FAST_EXTRA, POLL_INTERVAL, get_alert_thresholds,
)
from .formatting import (
    divider_positions, elapsed_pct, field_period, format_credits, format_duration, format_timestamp,
    format_tooltip, parse_field_name, popup_label, quota_snapshot_env, time_until,
)
from .i18n import T
from .popup import UsagePopup
from .task_registry import ERROR_STALE, read_registry, write_registry
from .task_runner import STATUS_DONE, STATUS_INTERRUPTED, STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING, TaskRunner
from .tasks import Task
from .tray_icon import create_icon_image, create_status_image, taskbar_uses_light_theme, watch_theme_change

__all__ = ['AutoSchedulerForClaude', 'crash_log']

# Seconds after a reset at which to place the confirming poll.  A small buffer
# absorbs minor timing differences (clocks, caches, server-side propagation).
RESET_BUFFER = 5

# Win32 tray mouse messages, delivered by the shell as the WM_NOTIFY lParam.
# pystray natively acts only on WM_LBUTTONUP; WM_LBUTTONDBLCLK drives the
# optional double-click command.
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203

# MessageBoxW flags and return codes for the quit-with-tasks-running dialog.
# The Windows button captions are fixed, so the message text has to spell out
# what Yes, No, and Cancel each do.
MB_YESNOCANCEL = 0x03
MB_ICONQUESTION = 0x20
IDCANCEL = 2
IDYES = 6
IDNO = 7

# Tray-menu glyph per task status, so the state is readable at a glance.
TASK_STATUS_GLYPHS = {
    STATUS_WAITING: '○',        # hollow circle - five-hour usage still above the threshold
    STATUS_PENDING: '◔',        # part-filled circle - threshold met, no free slot
    STATUS_RUNNING: '▶',        # play - a child process is live
    STATUS_DONE: '✔',           # check - finished
    STATUS_INTERRUPTED: '⚠',    # warning - the app died mid-run
}
TASK_FAILED_GLYPH = '✖'         # cross - finished with a non-zero exit code

# Statuses that can still turn into a run.  Drives the popup's Active/Done
# filter and which thresholds get a marker on the headline quota bar.
TASK_ACTIVE_STATUSES = frozenset({STATUS_WAITING, STATUS_PENDING, STATUS_RUNNING})

# The quota field the task queue is gated on.  This is queue logic, not display
# logic: tasks start on this field alone (``TaskRunner.evaluate``,
# docs/task-queue.md), so the popup's headline bar names the same field instead
# of auto-detecting one.  The auto-detected bars are unaffected.
QUEUE_GATE_FIELD = 'five_hour'


def _future_iso(**kwargs: float) -> str:
    """Return an ISO 8601 timestamp offset from now by the given timedelta kwargs."""
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat()


def _iso_to_epoch(iso_str: str | None) -> float | None:
    """Return a Unix timestamp for an ISO 8601 string, or None if unparseable."""
    if not iso_str:
        return None

    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except (TypeError, ValueError):
        return None


def _run_duration(row: dict[str, Any]) -> float | None:
    """Return a finished run's wall-clock duration in seconds, or None."""
    started = _iso_to_epoch(row.get('started_at'))
    finished = _iso_to_epoch(row.get('finished_at'))
    if started is None or finished is None:
        return None

    return max(0.0, finished - started)


def _task_result_text(row: dict[str, Any], gate_pct: float | None) -> str:
    """Return the "what happened / what is it waiting for" cell for one task.

    A running task returns an empty string: its elapsed time ticks in the
    popup, which would otherwise only advance when something else changes.
    """
    status = row['status']
    if status == STATUS_RUNNING:
        return ''

    if status == STATUS_DONE:
        duration = _run_duration(row)
        if duration is None:
            return T['task_state_done'].format(code=row['exit_code'])
        return T['queue_result_duration'].format(code=row['exit_code'], duration=format_duration(duration))

    if status == STATUS_WAITING and gate_pct is not None:
        headroom = gate_pct - row['max_usage_percent']
        return T['queue_needs_headroom'].format(pct=f'{headroom:.0f}')

    return T[f'task_state_{status}']


def _threshold_markers(rows: list[dict[str, Any]], gate_pct: float | None) -> list[dict[str, Any]]:
    """Place one marker per still-runnable task on the headline quota bar.

    Completed and interrupted tasks are left off: one-shot means they will
    never run again, so their threshold says nothing about what happens next.
    A marker is *met* under the same ``<=`` comparison ``select_runnable``
    uses, so the bar and the queue can never disagree about what would start.
    """
    markers = []
    for row in rows:
        if not row['active']:
            continue
        threshold = row['threshold_pct']
        markers.append({
            'rel': max(0.0, min(1.0, threshold / 100)),
            'label': row['id'],
            'pct_text': f'{threshold:g}%',
            'met': gate_pct is not None and gate_pct <= threshold,
        })

    markers.sort(key=lambda marker: marker['rel'])
    return markers


def _align_to_reset(interval: int, next_reset: float | None) -> tuple[int, bool]:
    """Shift the next poll so the confirming poll lands just after the reset.

    Every returned interval stays at or above ``POLL_FAST`` (the cache
    cooldown), so the reset is caught without polling faster.  The poll before
    the reset is pulled forward to ``POLL_FAST - RESET_BUFFER`` seconds before
    it (the danger-window start); from there the confirming poll lands
    ``RESET_BUFFER`` seconds after the reset.  When the current poll is
    already too close to pull the previous one forward without breaking the
    cooldown, the confirming poll is committed directly.

    Parameters
    ----------
    interval : int
        The normal cadence interval before reset alignment.
    next_reset : float or None
        Seconds until the nearest upcoming reset, or None.

    Returns
    -------
    tuple[int, bool]
        The (possibly adjusted) interval and whether alignment engaged.
    """
    if next_reset is None or next_reset <= 0:
        return interval, False

    danger = POLL_FAST - RESET_BUFFER          # last window where a poll can no longer be exact
    post = int(next_reset) + RESET_BUFFER      # offset that lands the poll just after the reset

    if next_reset <= danger:
        # Already inside that last window: the confirming poll can only land
        # POLL_FAST after this one (small, unavoidable overshoot).
        return POLL_FAST, True

    if post <= interval * 1.5:
        # Reset near enough: commit the confirming poll to just after it.
        return post, True

    if next_reset < interval + danger:
        # A normal interval would drop the next poll into that last window,
        # from where the confirming poll would overshoot.  Pull it forward to
        # the window start (POLL_FAST - RESET_BUFFER before the reset); if
        # that is too close to keep POLL_FAST spacing, commit to the
        # confirming poll directly.
        pre = int(next_reset) - danger
        return (pre if pre >= POLL_FAST else post), True

    return interval, False                     # reset still far - keep the normal cadence


class AutoSchedulerForClaude:
    """System tray application displaying Claude usage."""

    def __init__(self) -> None:
        """Set up the tray icon with context menu and polling state."""
        self.running = True
        self.cache = UsageCache()
        self.task_runner = TaskRunner(on_drained=self._on_tasks_drained)

        # Last raw API response (may contain 'error') - for icon and polling decisions
        self._last_response: dict[str, Any] = {}

        # Notification state
        self._prev_utilization: dict[str, float] = {}
        self._prev_account_uuid: str | None = None
        self._first_update_done = False
        self._notified_thresholds: dict[str, float] = {}

        # Adaptive polling state
        self._fast_polls_remaining = 0
        self._idle_reset_pending = False
        # Guarded by _notify_lock: deferrals arrive from the popup and poll
        # threads while the poll loop flushes.
        self._notify_lock = threading.Lock()
        self._deferred_notifications: dict[str, tuple[str, str]] = {}

        # Popup state
        self._popup_lock = threading.Lock()
        self._popup_open = False
        self._popup_closed_at = 0.0
        self._next_poll_time: float | None = None
        # Compact or task-queue width, remembered across popup opens.  Kept in
        # memory rather than in the settings file, which the app never writes.
        self._popup_expanded = False

        # Theme state
        self._light_taskbar = taskbar_uses_light_theme()

        self.restart_requested = False

        # Non-default config dirs get a tooltip prefix so multiple
        # instances (one per Claude account) can be told apart.
        self._tooltip_prefix = '' if is_default_config_dir() else f'[{effective_config_dir().name}] '

        self.icon = pystray.Icon(
            'auto_scheduler',
            icon=create_icon_image(0, 0, self._light_taskbar),
            title=self._tooltip_prefix + T['loading'],
            menu=self._build_menu(),
        )

        # Double-click support.  pystray fires the default action (the popup) on
        # every left-button release, so a double-click command requires deferring
        # that single click by the system double-click interval and cancelling it
        # when a second click arrives.  Only wired up when a command is
        # configured, so the default single-click behavior is otherwise untouched.
        self._click_lock = threading.Lock()
        self._single_click_timer: threading.Timer | None = None
        self._swallow_next_up = False
        if ON_DOUBLE_CLICK_COMMAND:
            self._double_click_seconds = ctypes.windll.user32.GetDoubleClickTime() / 1000.0
            self._install_double_click_handler()

    # Menu actions

    def on_show_popup(self, icon: Any = None, item: Any = None) -> None:
        with self._popup_lock:
            if self._popup_open:
                return
            if time.time() - self._popup_closed_at < 0.15:
                return
            self._popup_open = True
        threading.Thread(target=self._open_popup, daemon=True).start()

    def on_toggle_autostart(self, icon: Any = None, item: Any = None) -> None:
        set_autostart(not is_autostart_enabled())

    def on_restart(self, icon: Any = None, item: Any = None) -> None:
        self.restart_requested = True
        self.on_quit(icon, item)

    def on_open_project(self, icon: Any = None, item: Any = None) -> None:
        webbrowser.open(PROJECT_URL)

    def on_test_reset_5h(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_RESET_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'reset',
            'AUTO_SCHEDULER_VARIANT': 'five_hour',
            'AUTO_SCHEDULER_UTILIZATION': '0',
            'AUTO_SCHEDULER_PREV_UTILIZATION': '95',
            'AUTO_SCHEDULER_UTILIZATION_FIVE_HOUR': '0',
            'AUTO_SCHEDULER_UTILIZATION_SEVEN_DAY': '45',
            'AUTO_SCHEDULER_RESETS_AT': _future_iso(hours=5),
            'AUTO_SCHEDULER_TITLE': T['notify_reset_title'],
            'AUTO_SCHEDULER_MESSAGE': T['notify_reset'],
        }, capture_output=True)

    def on_test_reset_7d(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_RESET_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'reset',
            'AUTO_SCHEDULER_VARIANT': 'seven_day',
            'AUTO_SCHEDULER_UTILIZATION': '0',
            'AUTO_SCHEDULER_PREV_UTILIZATION': '99',
            'AUTO_SCHEDULER_UTILIZATION_FIVE_HOUR': '12',
            'AUTO_SCHEDULER_UTILIZATION_SEVEN_DAY': '0',
            'AUTO_SCHEDULER_RESETS_AT': _future_iso(days=7),
            'AUTO_SCHEDULER_TITLE': T['notify_reset_title'],
            'AUTO_SCHEDULER_MESSAGE': T['notify_reset'],
        }, capture_output=True)

    def on_test_threshold_5h(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_THRESHOLD_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'threshold',
            'AUTO_SCHEDULER_VARIANT': 'five_hour',
            'AUTO_SCHEDULER_UTILIZATION': '82',
            'AUTO_SCHEDULER_THRESHOLD': '80',
            'AUTO_SCHEDULER_RESETS_AT': _future_iso(hours=3),
            'AUTO_SCHEDULER_TITLE': T['notify_threshold_title'],
            'AUTO_SCHEDULER_MESSAGE': T['notify_threshold_generic'].format(label=popup_label('five_hour'), pct='82'),
        }, capture_output=True)

    def on_test_threshold_7d(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_THRESHOLD_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'threshold',
            'AUTO_SCHEDULER_VARIANT': 'seven_day',
            'AUTO_SCHEDULER_UTILIZATION': '81',
            'AUTO_SCHEDULER_THRESHOLD': '80',
            'AUTO_SCHEDULER_RESETS_AT': _future_iso(days=4),
            'AUTO_SCHEDULER_TITLE': T['notify_threshold_title'],
            'AUTO_SCHEDULER_MESSAGE': T['notify_threshold_generic'].format(label=popup_label('seven_day'), pct='81'),
        }, capture_output=True)

    def on_test_startup(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_STARTUP_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'startup',
            'AUTO_SCHEDULER_UTILIZATION_FIVE_HOUR': '0',
            'AUTO_SCHEDULER_RESETS_AT_FIVE_HOUR': '',
            'AUTO_SCHEDULER_UTILIZATION_SEVEN_DAY': '45',
            'AUTO_SCHEDULER_RESETS_AT_SEVEN_DAY': _future_iso(days=3),
        }, capture_output=True)

    def on_test_double_click(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_DOUBLE_CLICK_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'double_click',
            'AUTO_SCHEDULER_UTILIZATION_FIVE_HOUR': '30',
            'AUTO_SCHEDULER_RESETS_AT_FIVE_HOUR': _future_iso(hours=3),
            'AUTO_SCHEDULER_UTILIZATION_SEVEN_DAY': '55',
            'AUTO_SCHEDULER_RESETS_AT_SEVEN_DAY': _future_iso(days=4),
        }, capture_output=True)

    def on_quit(self, icon: Any = None, item: Any = None) -> None:
        """Quit, asking what to do with any task that is still running.

        Windows fixes the Yes/No/Cancel captions, so the message text maps
        each button to its outcome: kill now, drain, or stay running.
        """
        running = self.task_runner.running_count()
        if running > 0:
            choice = ctypes.windll.user32.MessageBoxW(
                0, T['quit_tasks_running'].format(count=running),
                T['quit_tasks_running_title'], MB_YESNOCANCEL | MB_ICONQUESTION,
            )
            if choice == IDCANCEL:
                self.restart_requested = False
                return

            if choice == IDNO:
                # Drain: the last watcher thread calls _on_tasks_drained.
                self.task_runner.begin_drain()
                self._render_tray()
                return

            self.task_runner.kill_all()

        self.running = False
        self.icon.stop()

    def _on_tasks_drained(self) -> None:
        """Finish quitting once the last draining task has ended."""
        self.running = False
        self.icon.stop()

    # Tray menu

    def _build_menu(self) -> Any:
        """Build the whole tray menu.

        Rebuilt rather than mutated after a registry edit: the task submenu's
        entries and its visibility are both fixed at construction time, so a
        newly added task only appears once the menu is built again.
        """
        return pystray.Menu(
            pystray.MenuItem(T['menu_show'], self.on_show_popup, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                T['autostart'], self.on_toggle_autostart,
                checked=lambda item: is_autostart_enabled(),
                visible=getattr(sys, 'frozen', False),
            ),
            pystray.MenuItem(T['task_queue'], self._build_task_menu(), visible=self.task_runner.has_tasks()),
            pystray.MenuItem(T['test_commands'], pystray.Menu(
                pystray.MenuItem(T['test_reset_5h'], self.on_test_reset_5h, enabled=bool(ON_RESET_COMMAND)),
                pystray.MenuItem(T['test_reset_7d'], self.on_test_reset_7d, enabled=bool(ON_RESET_COMMAND)),
                pystray.MenuItem(T['test_threshold_5h'], self.on_test_threshold_5h, enabled=bool(ON_THRESHOLD_COMMAND)),
                pystray.MenuItem(T['test_threshold_7d'], self.on_test_threshold_7d, enabled=bool(ON_THRESHOLD_COMMAND)),
                pystray.MenuItem(T['test_startup'], self.on_test_startup, enabled=bool(ON_STARTUP_COMMAND)),
                pystray.MenuItem(T['test_double_click'], self.on_test_double_click, enabled=bool(ON_DOUBLE_CLICK_COMMAND)),
            ), enabled=bool(ON_RESET_COMMAND or ON_STARTUP_COMMAND or ON_THRESHOLD_COMMAND or ON_DOUBLE_CLICK_COMMAND)),
            pystray.MenuItem(T['restart'], self.on_restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(T['menu_project'], self.on_open_project),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(T['quit'], self.on_quit),
        )

    def _rebuild_menu(self) -> None:
        """Re-create the tray menu after the task registry changed.

        Best-effort: the popup edit has already succeeded by the time this
        runs, so a tray that refuses to update must not surface as a failed
        save.  It corrects itself on the next restart.
        """
        try:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
        except Exception:
            traceback.print_exc()

    # Task queue menu

    def _build_task_menu(self) -> Any:
        """Build one static menu item per queued task, with dynamic status text.

        The task *set* is fixed at startup (the registry is read once), so a
        static item list carries the queue; only each item's text and enabled
        state are callables, re-evaluated every time the menu opens.
        """
        return pystray.Menu(*[
            pystray.MenuItem(
                self._task_item_text(task),
                self._task_item_action(task.id),
                enabled=self._task_item_enabled(task.id),
            )
            for task in self.task_runner.tasks
        ])

    def _task_item_text(self, task: Task) -> Callable[[Any], str]:
        """Return a callable rendering one task's menu label and current status."""
        def render(item: Any = None) -> str:
            status = self.task_runner.status(task.id)
            glyph = TASK_STATUS_GLYPHS.get(status, '')
            if status == STATUS_DONE and self.task_runner.exit_code(task.id):
                glyph = TASK_FAILED_GLYPH
            return f'{glyph} {task.description or task.id} - {self._task_status_label(task.id, status)}'

        return render

    def _task_status_label(self, task_id: str, status: str) -> str:
        """Return the localized status text, with the exit code for finished runs."""
        if status == STATUS_DONE:
            return T['task_state_done'].format(code=self.task_runner.exit_code(task_id))
        return T[f'task_state_{status}']

    def _task_item_enabled(self, task_id: str) -> Callable[[Any], bool]:
        """Only a run with a log on disk is clickable; the click opens that log."""
        return lambda item=None: self.task_runner.log_path(task_id) is not None

    def _task_item_action(self, task_id: str) -> Callable[..., None]:
        def open_log(icon: Any = None, item: Any = None) -> None:
            log_path = self.task_runner.log_path(task_id)
            if log_path is None:
                return
            try:
                os.startfile(log_path)
            except OSError:
                traceback.print_exc()

        return open_log

    # Task queue payload for the popup

    def queue_payload(self) -> dict[str, Any]:
        """Build the popup's task-queue block from the runner's snapshot.

        Lives here rather than in ``popup`` because this is where ``STATUS_*``
        keys become locale strings - ``task_runner`` must stay free of UI text,
        and ``popup`` receives text it only has to place.
        """
        queue = self.task_runner.snapshot()
        gate_pct = queue['five_hour_pct']
        rows = [self._task_row(row, gate_pct) for row in queue['tasks']]

        running = queue['running_count']
        active = sum(1 for row in rows if row['active'])
        free_slots = max(0, queue['max_concurrent'] - running)

        return {
            'disabled': queue['disabled'],
            'draining': queue['draining'],
            'summary_text': T['queue_summary'].format(running=running, waiting=active - running, free=free_slots),
            'counts': {'all': len(rows), 'active': active, 'done': len(rows) - active},
            'gate': self._queue_gate_bar(),
            'markers': _threshold_markers(rows, gate_pct),
            'tasks': rows,
        }

    def _task_row(self, row: dict[str, Any], gate_pct: float | None) -> dict[str, Any]:
        """Turn one raw runner row into the pre-formatted row the popup renders."""
        status = row['status']
        exit_code = row['exit_code']
        failed = status == STATUS_DONE and bool(exit_code)
        threshold = row['max_usage_percent']
        timeout = row['timeout_minutes']
        started_at = row['started_at']
        utilization = row['started_at_utilization']

        return {
            'id': row['id'],
            'name': row['description'] or row['id'],
            'cwd': row['cwd'] or '',
            'status': status,
            'status_text': self._task_status_label(row['id'], status),
            'glyph': TASK_FAILED_GLYPH if failed else TASK_STATUS_GLYPHS.get(status, ''),
            'failed': failed,
            'active': status in TASK_ACTIVE_STATUSES,
            'threshold_pct': threshold,
            'threshold_text': T['queue_threshold'].format(pct=f'{threshold:g}'),
            'timeout_text': T['queue_no_timeout'] if timeout == 0 else T['queue_timeout'].format(min=f'{timeout:g}'),
            'last_run_text': format_timestamp(started_at) if started_at else '',
            'result_text': _task_result_text(row, gate_pct),
            'started_text': format_timestamp(started_at) if started_at else '',
            'started_epoch': _iso_to_epoch(started_at) if status == STATUS_RUNNING else None,
            'elapsed_text': format_duration(row['elapsed_seconds']) if row['elapsed_seconds'] is not None else '',
            'started_usage_text': f'{utilization:.0f}%' if utilization is not None else '',
            'log_name': row['log'] or '',
            'has_log': row['has_log'],
            'can_stop': status == STATUS_RUNNING,
            'can_delete': status != STATUS_RUNNING,
        }

    def _queue_gate_bar(self) -> dict[str, Any] | None:
        """Build the headline bar for the quota the queue is gated on.

        Returns None when the account reports no such quota - the same
        condition under which ``TaskRunner.evaluate`` starts nothing, so the
        popup says so instead of showing a bar for a different field.
        """
        usage = self.cache.snapshot.usage or {}
        entry = usage.get(QUEUE_GATE_FIELD) or {}
        pct = entry.get('utilization')
        if pct is None:
            return None

        resets_at = entry.get('resets_at', '')
        period = field_period(QUEUE_GATE_FIELD)
        time_pct = elapsed_pct(resets_at, period) if period else None

        return {
            'key': QUEUE_GATE_FIELD,
            'pct': pct,
            'pct_text': f'{pct:.0f}%',
            'fill_pct': max(0.0, min(1.0, pct / 100)),
            'warn': pct >= 100 or (time_pct is not None and pct > time_pct),
            'caption': T['queue_gate_caption'].format(label=popup_label(QUEUE_GATE_FIELD)),
            'reset_text': time_until(resets_at) if resets_at else '',
            'dividers': divider_positions(resets_at, period) if period else [],
            'marker_rel': max(0.0, min(1.0, time_pct / 100)) if time_pct is not None else None,
        }

    # Task queue editing

    # Every method below runs only from an explicit user action in the popup's
    # task editor.  Nothing here is reachable from the poll loop, a timer, or
    # any other background thread - the registry is never written on its own.

    def registry_read(self) -> dict[str, Any]:
        """Return the raw registry entries, a change stamp, and the used ids."""
        entries, stamp, error = read_registry()
        return {
            'entries': entries,
            'stamp': list(stamp),
            'completed_ids': self.task_runner.completed_ids(),
            'error': T[error] if error else None,
        }

    def registry_save(self, entry: dict[str, Any], index: int, stamp: list[float]) -> dict[str, Any]:
        """Add or replace one registry entry.

        *index* below zero (or past the end) appends.  Replacing keeps the
        entry's position, because file order is queue priority.
        """
        entries, current_stamp, error = read_registry()
        if error:
            return self._registry_failure(error)
        if list(current_stamp) != list(stamp):
            return self._registry_failure(ERROR_STALE)

        prospective = list(entries)
        if 0 <= index < len(prospective):
            prospective[index] = entry
        else:
            prospective.append(entry)

        return self._apply_registry(prospective, current_stamp)

    def registry_delete(self, index: int, stamp: list[float]) -> dict[str, Any]:
        """Remove one registry entry, refusing while its task is running.

        Deleting the entry of a live process would leave the tray menu, the
        popup, and the running child disagreeing about what exists.  The state
        record and the log file of a finished task are deliberately left
        alone: the record is what enforces one-shot.
        """
        entries, current_stamp, error = read_registry()
        if error:
            return self._registry_failure(error)
        if list(current_stamp) != list(stamp):
            return self._registry_failure(ERROR_STALE)
        if not 0 <= index < len(entries):
            return self._registry_failure(ERROR_STALE)

        task_id = entries[index].get('id')
        if isinstance(task_id, str) and self.task_runner.status(task_id) == STATUS_RUNNING:
            return {'ok': False, 'error': T['queue_error_delete_running'], 'errors': []}

        return self._apply_registry([e for i, e in enumerate(entries) if i != index], current_stamp)

    def stop_task(self, task_id: str) -> bool:
        """Terminate one running task from the popup."""
        return self.task_runner.stop(task_id)

    def task_log(self, task_id: str, offset: int) -> dict[str, Any] | None:
        """Return the next chunk of one run's log for the popup's log view."""
        return self.task_runner.log_tail(task_id, offset)

    def open_task_log(self, task_id: str) -> bool:
        """Open one run's log in the system's default handler."""
        log_path = self.task_runner.log_path(task_id)
        if log_path is None:
            return False

        try:
            os.startfile(log_path)
        except OSError:
            traceback.print_exc()
            return False

        return True

    def _apply_registry(self, entries: list[dict[str, Any]], stamp: tuple[float, int]) -> dict[str, Any]:
        """Write a validated registry, then swap it in and refresh the tray."""
        tasks, validation_errors, error = write_registry(entries, stamp)
        if error is not None:
            return {
                'ok': False,
                'error': T[error],
                'errors': [{'field': e.field, 'message': e.message.strip()} for e in validation_errors],
            }

        self.task_runner.reload(tasks or [])
        self._rebuild_menu()

        return {'ok': True, 'error': None, 'errors': [], **self.registry_read()}

    def _registry_failure(self, error: str) -> dict[str, Any]:
        return {'ok': False, 'error': T[error], 'errors': []}

    # Popup

    def _should_refresh_usage(self) -> bool:
        """Return whether opening the popup should trigger a background usage fetch.

        Refreshes stale data, with one exception: when a quota reset is closer
        than the cache cooldown, a fetch now would advance
        ``last_success_time`` into the last ``POLL_FAST`` window before the
        reset and force the reset-aligned poll to overshoot.  Such a fetch is
        deferred to the scheduled reset poll, whose fresh data the open popup
        picks up live.  The very first fetch (no data yet) always refreshes.
        """
        last = self.cache.last_success_time
        if last is None:
            return True
        if time.time() - last < POLL_FAST:
            return False

        next_reset = self._seconds_until_next_reset()
        return not (next_reset is not None and next_reset < POLL_FAST)

    def _open_popup(self) -> None:
        # _popup_open is set True under _popup_lock (in on_show_popup) and
        # reset here without the lock.  This is safe because False is the
        # permissive default - a momentary stale True only delays the next open.
        try:
            needs_profile = not self.cache.profile
            needs_refresh = self._should_refresh_usage()
            if needs_profile or needs_refresh:
                # Single thread: ensure_profile() and update() both acquire
                # cache._lock, so they must run sequentially.  Two threads
                # would cause update()'s non-blocking acquire to fail while
                # ensure_profile() holds the lock.
                def _bg_refresh() -> None:
                    if needs_profile:
                        self.cache.ensure_profile()
                    if needs_refresh:
                        self.update()
                threading.Thread(target=_bg_refresh, daemon=True).start()
            UsagePopup(self)
        finally:
            self._popup_closed_at = time.time()
            self._popup_open = False

    # Double-click handling

    def _install_double_click_handler(self) -> None:
        """Replace pystray's tray-message handler with a double-click-aware one.

        Locates the ``WM_NOTIFY`` entry in pystray's handler table by identity
        and swaps in :meth:`_on_tray_message`, keeping the original handler for
        right-click and every other message.
        """
        self._pystray_on_notify = self.icon._on_notify
        for code, handler in self.icon._message_handlers.items():
            if handler == self._pystray_on_notify:
                self.icon._message_handlers[code] = self._on_tray_message
                break

    def _on_tray_message(self, wparam: int, lparam: int) -> int:
        """Dispatch a tray mouse message, adding double-click handling.

        A left-button release schedules the popup after the double-click
        interval; a double-click cancels that pending popup and runs the
        configured command instead.  The trailing release that follows every
        double-click is swallowed so it does not schedule a second popup.  All
        other messages (right-click menu, etc.) fall through to pystray's own
        handler.
        """
        if lparam == WM_LBUTTONUP:
            with self._click_lock:
                if self._swallow_next_up:
                    self._swallow_next_up = False
                    return 0
                if self._single_click_timer is not None:
                    self._single_click_timer.cancel()
                self._single_click_timer = threading.Timer(self._double_click_seconds, self._fire_single_click)
                self._single_click_timer.daemon = True
                self._single_click_timer.start()
            return 0

        if lparam == WM_LBUTTONDBLCLK:
            with self._click_lock:
                self._swallow_next_up = True
                if self._single_click_timer is not None:
                    self._single_click_timer.cancel()
                    self._single_click_timer = None
            self._run_double_click_command()
            return 0

        return self._pystray_on_notify(wparam, lparam)

    def _fire_single_click(self) -> None:
        """Open the popup once the double-click interval passes without a second click.

        Bails out if the timer was cleared meanwhile - a double-click that
        arrived right as the timer fired cancels it here, so the popup never
        opens for a completed double-click.
        """
        with self._click_lock:
            if self._single_click_timer is None:
                return
            self._single_click_timer = None
        self.on_show_popup()

    # Tray rendering

    def _render_tray(self) -> None:
        """Re-render tray icon and tooltip from current state."""
        data = self._last_response
        if 'error' in data:
            self.icon.icon = create_status_image('C!' if data.get('auth_error') else '!', self._light_taskbar)
        else:
            top_field, top_mode = ICON_FIELDS[0].split(':', 1) if ':' in ICON_FIELDS[0] else (ICON_FIELDS[0], 'utilization')
            bottom_field, bottom_mode = ICON_FIELDS[1].split(':', 1) if ':' in ICON_FIELDS[1] else (ICON_FIELDS[1], 'utilization')
            # isinstance instead of truthiness: a configured field may point at
            # a non-dict response value (e.g. the raw limits array).
            top_entry = data.get(top_field)
            bottom_entry = data.get(bottom_field)
            if not isinstance(top_entry, dict):
                top_entry = {}
            if not isinstance(bottom_entry, dict):
                bottom_entry = {}
            pct_top = top_entry.get('utilization', 0) or 0
            pct_bottom = bottom_entry.get('utilization', 0) or 0
            top_period = field_period(top_field)
            bottom_period = field_period(bottom_field)
            time_pct_top = elapsed_pct(top_entry.get('resets_at', ''), top_period) if top_period else None
            time_pct_bottom = elapsed_pct(bottom_entry.get('resets_at', ''), bottom_period) if bottom_period else None
            extra = data.get('extra_usage') or {}
            extra_limit = extra.get('monthly_limit') or 0
            extra_used = extra.get('used_credits') or 0
            # A missing/null monthly_limit means uncapped pay-as-you-go extra
            # usage, which cannot be exhausted.
            extra_usage_available = bool(extra.get('is_enabled')) and (extra_limit <= 0 or extra_used < extra_limit)
            self.icon.icon = create_icon_image(
                pct_top, pct_bottom, self._light_taskbar,
                mode_top=top_mode, mode_bottom=bottom_mode,
                time_pct_top=time_pct_top, time_pct_bottom=time_pct_bottom,
                extra_usage_available=extra_usage_available,
            )
        title = self._tooltip_prefix + format_tooltip(data)
        if self.task_runner.is_draining():
            title += f"\n{T['tooltip_draining']}"
        self.icon.title = title

    def _on_theme_changed(self) -> None:
        """Re-render the tray icon when the Windows theme changes."""
        light = taskbar_uses_light_theme()
        if light == self._light_taskbar:
            return

        self._light_taskbar = light
        if self._last_response:
            self._render_tray()

    # Update orchestration

    def update(self, force: bool = False) -> None:
        """Request a data refresh from the cache and process the result.

        Parameters
        ----------
        force : bool
            When True, bypass the cache cooldown and the 429 rate-limit
            backoff so the refresh happens immediately.  Used after a
            confirmed account switch, where the freshly selected account
            has no polling history that those throttles need to protect.
        """
        result = self.cache.update(force=force)
        if result.data is None:
            return

        self._last_response = result.data
        self._render_tray()

        # Handle CLI update notification from token refresh
        if NOTIFY_CLAUDE_UPDATE and result.token_refresh and result.token_refresh.updated:
            self.icon.notify(
                T['notify_update'].format(old=result.token_refresh.old_version, new=result.token_refresh.new_version),
                T['notify_update_title'],
            )

        if 'error' in result.data:
            return

        # The credentials token changed after this usage data was fetched: the user switched
        # accounts while the request was in flight, so the data still belongs to the previous
        # account.  Comparing it against the new account's profile would announce the switch on
        # top of the old account's numbers and consume the UUID baseline, leaving the stale data
        # in place until the next regular poll.  Keep every baseline untouched instead - for a
        # real switch the poll loop's token watcher forces an immediate refetch that reports it
        # with the new data; a same-account token rotation just resumes on the next poll.
        if result.token is not None and result.token != read_access_token():
            return

        # Detect account switch: re-fetch profile if the access token changed, then compare UUIDs.
        # When the user runs 'claude auth login', the token changes and the next profile fetch
        # returns a different account UUID, preventing a false quota-reset notification.
        self.cache.ensure_profile()
        current_profile = self.cache.profile
        current_account_uuid = (current_profile.get('account') or {}).get('uuid') if isinstance(current_profile, dict) else None

        # Unknown identity with a known baseline: the profile fetch failed
        # after a token change, so this usage data may already belong to a
        # different account.  Skip all cross-poll comparisons and keep the
        # baselines untouched; the poll where the profile is readable again
        # detects the switch (or resumes normally for the same account).
        if self._prev_account_uuid is not None and current_account_uuid is None:
            return

        if self._prev_account_uuid is not None and current_account_uuid is not None and current_account_uuid != self._prev_account_uuid:
            email = (current_profile.get('account') or {}).get('email', '')
            message = T['notify_account_switched'].format(email=email) if email else T['notify_account_switched_title']
            self._notify_or_defer('account_switched', message, T['notify_account_switched_title'])
            self._prev_utilization = {}
            self._notified_thresholds = {}
            self._prev_account_uuid = current_account_uuid
            return
        self._prev_account_uuid = current_account_uuid

        # Collect all quota fields with utilization (extra_usage has a different structure)
        quota_fields: dict[str, float] = {}
        for key, value in result.data.items():
            if key == 'extra_usage':
                continue
            if isinstance(value, dict) and 'utilization' in value:
                quota_fields[key] = value.get('utilization', 0) or 0

        # Notify when quota resets after being nearly exhausted, but only if no other quota is blocking usage.
        # While idle/locked, defer notifications until the user returns (avoids lock screen privacy concerns).
        # The message carries no field information, so several quotas resetting
        # within one polling gap still produce a single notification.
        reset_detected = False
        for key, pct in quota_fields.items():
            prev = self._prev_utilization.get(key)
            if prev is None:
                continue

            parsed = parse_field_name(key)
            if parsed is None:
                continue

            _, unit, _ = parsed
            reset_threshold = 95 if unit == 'hour' else 98
            any_blocking = any(other_pct >= 99 for other_key, other_pct in quota_fields.items() if other_key != key)

            if prev > reset_threshold and pct < prev and not any_blocking:
                reset_detected = True

        if reset_detected:
            self._notify_or_defer('reset', T['notify_reset'], T['notify_reset_title'])

        # Run reset command on any detected usage drop (independent of notification threshold)
        for key, pct in quota_fields.items():
            prev = self._prev_utilization.get(key)
            if prev is not None and pct < prev:
                self._run_reset_command(key, pct, prev, data=result.data, entry=result.data.get(key, {}))
                self._idle_reset_pending = False

        self._check_threshold_alerts(result.data)

        # Adaptive polling: speed up when icon top field usage is increasing
        icon_top_key = ICON_FIELDS[0].split(':', 1)[0]
        icon_top_pct = quota_fields.get(icon_top_key, 0)
        icon_top_prev = self._prev_utilization.get(icon_top_key)
        if icon_top_prev is not None and icon_top_pct > icon_top_prev:
            self._fast_polls_remaining = POLL_FAST_EXTRA + 1
        elif self._fast_polls_remaining > 0:
            self._fast_polls_remaining -= 1

        self._prev_utilization = quota_fields

        if not self._first_update_done:
            self._run_startup_command(result.data)

        self._first_update_done = True

        # Start whatever the fresh quota snapshot allows.  Reached from the
        # poll thread and the popup's background refresh, so the runner takes
        # its own lock around the whole select-and-start sequence.
        self.task_runner.evaluate(result.data)

    # Notifications

    def _notify_or_defer(self, category: str, message: str, title: str) -> None:
        """Show a notification immediately, or defer it if the user is away.

        Parameters
        ----------
        category : str
            Deduplication key (e.g. ``'reset'``, ``'threshold_five_hour'``).
            While deferred, only the latest notification per category is
            kept so the user does not get a flood on return.
        message : str
            Notification body text.
        title : str
            Notification title.
        """
        if self._is_user_away():
            with self._notify_lock:
                self._deferred_notifications[category] = (message, title)
        else:
            self.icon.notify(message, title)

    def _flush_deferred_notifications(self) -> None:
        """Show all deferred notifications and clear the queue.

        The queue is swapped out under the lock so a deferral landing
        mid-flush (from the popup thread) is kept for the next flush
        instead of mutating the dict being iterated.
        """
        with self._notify_lock:
            pending, self._deferred_notifications = self._deferred_notifications, {}
        for message, title in pending.values():
            self.icon.notify(message, title)

    def _check_threshold_alerts(self, data: dict[str, Any]) -> None:
        """Show a notification when usage crosses a configured threshold.

        Dynamically detects all quota fields in the API response.  For
        each field, finds the highest threshold exceeded by current
        utilization.  If it exceeds a threshold not yet notified, shows a
        single notification with the current usage percentage.  When usage
        drops (e.g. after reset), tracking resets so thresholds can
        re-trigger in the next cycle.
        """
        for variant_key, entry in data.items():
            if variant_key == 'extra_usage':
                continue
            if not isinstance(entry, dict) or entry.get('utilization') is None:
                continue

            pct = entry['utilization']
            thresholds = get_alert_thresholds(variant_key)
            if not thresholds:
                continue

            exceeded = [t for t in thresholds if pct >= t]
            highest_exceeded = max(exceeded) if exceeded else 0
            last_notified = self._notified_thresholds.get(variant_key, 0)

            if ALERT_TIME_AWARE and highest_exceeded > last_notified and highest_exceeded < ALERT_TIME_AWARE_BELOW:
                period = field_period(variant_key)
                if period:
                    time_pct = elapsed_pct(entry.get('resets_at'), period)
                    if time_pct is not None and pct <= time_pct:
                        self._notified_thresholds[variant_key] = highest_exceeded
                        continue

            if highest_exceeded > last_notified:
                title = T['notify_threshold_title']
                label = popup_label(variant_key)
                message = T['notify_threshold_generic'].format(label=label, pct=f'{pct:.0f}')
                self._notify_or_defer(f'threshold_{variant_key}', message, title)
                self._run_threshold_command(variant_key, pct, highest_exceeded, entry, title, message)
                self._notified_thresholds[variant_key] = highest_exceeded
            elif highest_exceeded < last_notified:
                self._notified_thresholds[variant_key] = highest_exceeded

        self._check_extra_usage_alerts(data)

    def _check_extra_usage_alerts(self, data: dict[str, Any]) -> None:
        """Show a notification when extra usage crosses a configured threshold.

        Extra usage has a different data format (``used_credits`` /
        ``monthly_limit``) and no time-based reset, so it is handled
        separately from the sliding-window quotas.
        """
        extra = data.get('extra_usage')
        if not extra or not extra.get('is_enabled'):
            return

        used = extra.get('used_credits', 0) or 0
        currency = extra.get('currency')
        decimal_places = extra.get('decimal_places')
        used_text = format_credits(used, currency, decimal_places)

        limit = extra.get('monthly_limit', 0) or 0
        if limit > 0:
            pct = used / limit * 100
            thresholds = get_alert_thresholds('extra_usage')
            exceeded = [t for t in thresholds if pct >= t]
            highest_exceeded = max(exceeded) if exceeded else 0
            last_notified = self._notified_thresholds.get('extra_usage', 0)

            if highest_exceeded > last_notified:
                title = T['notify_threshold_title']
                limit_text = format_credits(limit, currency, decimal_places)
                message = T['notify_threshold_extra_usage'].format(
                    pct=f'{pct:.0f}', used=used_text, limit=limit_text,
                )
                self._notify_or_defer('threshold_extra_usage', message, title)
                self._run_threshold_command(
                    'extra_usage', pct, highest_exceeded, extra, title, message,
                    extra_used=used_text, extra_limit=limit_text,
                )
                self._notified_thresholds['extra_usage'] = highest_exceeded
            elif highest_exceeded < last_notified:
                self._notified_thresholds['extra_usage'] = highest_exceeded

        self._check_extra_usage_spent_alerts(extra, used, used_text)

    def _check_extra_usage_spent_alerts(self, extra: dict[str, Any], used: float, used_text: str) -> None:
        """Show a notification when extra-usage spending crosses a configured amount.

        Amounts in ``ALERT_EXTRA_USAGE_SPENT`` are absolute major-unit values
        (e.g. dollars), so they also work for accounts whose extra usage has
        no monthly limit and can never produce a percentage.
        """
        if not ALERT_EXTRA_USAGE_SPENT:
            return

        decimal_places = extra.get('decimal_places')
        places = decimal_places if decimal_places is not None else 2
        spent = used / (10 ** places)

        exceeded = [amount for amount in ALERT_EXTRA_USAGE_SPENT if spent >= amount]
        highest_exceeded = max(exceeded) if exceeded else 0
        last_notified = self._notified_thresholds.get('extra_usage_spent', 0)

        if highest_exceeded > last_notified:
            title = T['notify_threshold_title']
            message = T['notify_threshold_extra_usage_spent'].format(used=used_text)
            self._notify_or_defer('threshold_extra_usage_spent', message, title)
            self._run_threshold_command(
                'extra_usage_spent', None, highest_exceeded, extra, title, message,
                extra_used=used_text,
            )
            self._notified_thresholds['extra_usage_spent'] = highest_exceeded
        elif highest_exceeded < last_notified:
            self._notified_thresholds['extra_usage_spent'] = highest_exceeded

    # Event commands

    def _run_startup_command(self, data: dict[str, Any]) -> None:
        """Run the user-configured startup command if set.

        Fires once after the first successful API update.  Receives the
        full quota state so the command can decide what to do (e.g. only
        ping Claude when no five-hour session is active).
        """
        if not ON_STARTUP_COMMAND:
            return

        env_vars = {'AUTO_SCHEDULER_EVENT': 'startup', **quota_snapshot_env(data)}
        run_event_command(ON_STARTUP_COMMAND, env_vars)

    def _run_double_click_command(self) -> None:
        """Run the user-configured double-click command if set.

        Receives the latest quota state (from the most recent successful
        update) so the command can act on current usage, mirroring the
        startup command's environment.  A double-click is a user-driven
        action, so a command that exits with a non-zero code surfaces its
        stderr in an error dialog (``capture_output``) instead of failing
        silently - unlike the automatic reset/threshold/startup commands.
        """
        if not ON_DOUBLE_CLICK_COMMAND:
            return

        env_vars = {'AUTO_SCHEDULER_EVENT': 'double_click', **quota_snapshot_env(self._last_response)}
        run_event_command(ON_DOUBLE_CLICK_COMMAND, env_vars, capture_output=True)

    def _run_reset_command(
        self, variant: str, pct: float, prev_pct: float, *, data: dict[str, Any], entry: dict[str, Any],
    ) -> None:
        """Run the user-configured reset command if set."""
        if not ON_RESET_COMMAND:
            return

        pct_5h = (data.get('five_hour') or {}).get('utilization', 0) or 0
        pct_7d = (data.get('seven_day') or {}).get('utilization', 0) or 0
        run_event_command(ON_RESET_COMMAND, {
            'AUTO_SCHEDULER_EVENT': 'reset',
            'AUTO_SCHEDULER_VARIANT': variant,
            'AUTO_SCHEDULER_UTILIZATION': str(round(pct)),
            'AUTO_SCHEDULER_PREV_UTILIZATION': str(round(prev_pct)),
            'AUTO_SCHEDULER_UTILIZATION_FIVE_HOUR': str(round(pct_5h)),
            'AUTO_SCHEDULER_UTILIZATION_SEVEN_DAY': str(round(pct_7d)),
            'AUTO_SCHEDULER_RESETS_AT': entry.get('resets_at', ''),
            'AUTO_SCHEDULER_TITLE': T['notify_reset_title'],
            'AUTO_SCHEDULER_MESSAGE': T['notify_reset'],
        })

    def _run_threshold_command(
        self, variant: str, pct: float | None, threshold: float,
        entry: dict[str, Any], title: str, message: str,
        *, extra_used: str = '', extra_limit: str = '',
    ) -> None:
        """Run the user-configured threshold command if set.

        Skipped on the first update (before ``_first_update_done`` is set)
        so that already-exceeded thresholds at app startup do not trigger
        commands.  Notifications still fire - commands react to *events*,
        not *state*.

        ``pct`` is None for spend-amount alerts, which have no utilization
        percentage; ``AUTO_SCHEDULER_UTILIZATION`` is omitted from the
        environment in that case.
        """
        if not ON_THRESHOLD_COMMAND or not self._first_update_done:
            return

        env_vars = {
            'AUTO_SCHEDULER_EVENT': 'threshold',
            'AUTO_SCHEDULER_VARIANT': variant,
        }
        if pct is not None:
            env_vars['AUTO_SCHEDULER_UTILIZATION'] = str(round(pct))
        env_vars.update({
            'AUTO_SCHEDULER_THRESHOLD': str(round(threshold)),
            'AUTO_SCHEDULER_RESETS_AT': entry.get('resets_at', ''),
            'AUTO_SCHEDULER_TITLE': title,
            'AUTO_SCHEDULER_MESSAGE': message,
        })
        if extra_used:
            env_vars['AUTO_SCHEDULER_EXTRA_USED'] = extra_used
        if extra_limit:
            env_vars['AUTO_SCHEDULER_EXTRA_LIMIT'] = extra_limit

        run_event_command(ON_THRESHOLD_COMMAND, env_vars)

    # Polling

    def _seconds_until_next_reset(self) -> float | None:
        """Return seconds until the earliest upcoming quota reset, or None."""
        now = datetime.now(timezone.utc)
        earliest = None
        for key, entry in self._last_response.items():
            if not isinstance(entry, dict) or not entry.get('resets_at'):
                continue
            try:
                reset_time = datetime.fromisoformat(entry['resets_at'])
                seconds = (reset_time - now).total_seconds()
                if seconds > 0 and (earliest is None or seconds < earliest):
                    earliest = seconds
            except Exception:
                continue

        return earliest

    def _account_switched(self) -> bool:
        """Return whether the current credentials belong to a different account.

        Probes the account profile with the token now in the credentials
        file (bypassing the 429 backoff, since a freshly selected account
        cannot be the source of that rate limit) and compares its UUID
        against the last seen one.  Returns False until a baseline UUID is
        known, so the first successful update is never taken for a switch.
        """
        if self._prev_account_uuid is None:
            return False

        self.cache.ensure_profile(bypass_rate_limit=True)
        profile = self.cache.profile
        current_uuid = (profile.get('account') or {}).get('uuid') if isinstance(profile, dict) else None

        return current_uuid is not None and current_uuid != self._prev_account_uuid

    def _reset_aligned_poll_target(self, next_reset: float) -> float:
        """Return the absolute time for a poll landing just after a reset.

        Clamped to the cache cooldown (``last_success_time + POLL_FAST``) so
        the confirming poll never fires before a fresh fetch is permitted.

        Parameters
        ----------
        next_reset : float
            Seconds until the upcoming reset.
        """
        target = time.time() + next_reset + RESET_BUFFER
        last = self.cache.last_success_time
        if last is not None:
            target = max(target, last + POLL_FAST)

        return target

    def _calculate_poll_interval(self) -> int:
        """Determine the next poll interval based on current state.

        Returns
        -------
        int
            Seconds to wait before the next poll.
        """
        data = self._last_response

        if data.get('rate_limited'):
            remaining = self.cache.rate_limit_remaining
            interval = max(math.ceil(remaining), POLL_INTERVAL) if remaining > 0 else POLL_INTERVAL
        elif 'error' in data:
            interval = POLL_ERROR
        elif self._fast_polls_remaining > 0:
            interval = POLL_FAST
        else:
            interval = POLL_INTERVAL

        # Align the next poll around an imminent reset for faster feedback.
        # The confirming poll is placed just after the reset; a follow-up uses
        # POLL_FAST regardless of user activity (quota was likely exhausted).
        next_reset = self._seconds_until_next_reset()
        interval, aligned = _align_to_reset(interval, next_reset)
        if aligned:
            self._fast_polls_remaining = max(self._fast_polls_remaining, 2)

        return interval

    def _is_user_away(self) -> bool:
        """Return True if the user is idle or the workstation is locked."""
        if is_workstation_locked():
            return True
        return IDLE_PAUSE > 0 and get_idle_seconds() >= IDLE_PAUSE

    def _wait_for_activity(self, until: float | None = None) -> None:
        """Block until user activity resumes or the app is stopping.

        Parameters
        ----------
        until : float | None
            Optional deadline (``time.time()`` epoch).  When set, the
            wait ends even if the user is still away, allowing a
            time-critical poll (e.g. quota reset command) to proceed.
        """
        while self.running and self._is_user_away():
            if until is not None and time.time() >= until:
                break
            time.sleep(2)

    def poll_loop(self) -> None:
        """Poll the API in a loop with adaptive intervals.

        Pauses polling when the user is idle or the workstation is
        locked.  On resume, polls immediately if the regular interval
        has elapsed since the last successful fetch.
        """
        self.cache.ensure_profile()
        force_next = False
        while self.running:
            # Read before the update, not after: an account switch that lands while the
            # request is in flight would otherwise already be part of the token read
            # afterwards and never register as a change - leaving the previous account's
            # usage on screen until the next regular poll.
            token_seen = read_access_token()
            self.update(force=force_next)
            force_next = False
            interval = self._calculate_poll_interval()

            target = time.time() + interval
            self._next_poll_time = target
            last_success_seen = self.cache.last_success_time
            while self.running and time.time() < target:
                time.sleep(1)

                # React to a credentials token change between polls. A switch to
                # a different account forces an immediate refresh (bypassing the
                # cooldown) so the new account's usage shows right away. A token
                # change while the last fetch failed auth is retried at once so a
                # freshly refreshed token recovers usage and profile without
                # waiting out the error cadence or needing a restart.
                current_token = read_access_token()
                if current_token and current_token != token_seen:
                    token_seen = current_token
                    if self._account_switched():
                        force_next = True
                        break
                    if self._last_response.get('auth_error'):
                        break

                # Re-anchor the wait target after a backward clock jump -
                # otherwise the poll would stall until the wall clock catches
                # up with the pre-jump target, potentially for hours.  The
                # bound leaves room for reset-aligned targets, which may lie
                # up to roughly POLL_FAST past a normal interval.
                if target - time.time() > interval + POLL_FAST:
                    target = time.time() + interval
                    self._next_poll_time = target

                # If another thread (popup) fetched successfully, push the next
                # poll a full interval past that fetch to avoid a redundant one.
                # Only react to an actual new fetch (last_success advanced), not
                # to a target the idle-return path lowered on its own.
                lst = self.cache.last_success_time
                if lst is not None and (last_success_seen is None or lst > last_success_seen):
                    last_success_seen = lst
                    new_target = max(target, lst + interval)
                    # Never let that push move the poll past a reset-aligned
                    # slot, nor drop it into the danger window (the last
                    # POLL_FAST - RESET_BUFFER seconds before the reset): a
                    # poll there consumes the cooldown, so the confirming poll
                    # would overshoot the reset by up to a full cooldown.
                    next_reset = self._seconds_until_next_reset()
                    if next_reset is not None:
                        reset_epoch = time.time() + next_reset
                        aligned = self._reset_aligned_poll_target(next_reset)
                        if new_target > aligned or reset_epoch - (POLL_FAST - RESET_BUFFER) < new_target < reset_epoch:
                            new_target = aligned
                    target = new_target
                    self._next_poll_time = target

                # Show notifications deferred while the user was away as soon
                # as they are present, even when the away branch below is
                # never entered (the user returned in the short gap between a
                # deferral and this loop's next away check).
                if self._deferred_notifications and not self._is_user_away():
                    self._flush_deferred_notifications()

                # Pause polling while the user is away - unless the task queue
                # still has work.  Draining the queue overnight is the point of
                # an auto-scheduler, so pending work keeps the normal cadence;
                # only the pause is skipped.  Interval calculation, reset
                # alignment, and notification deferral are untouched.
                # Regular polling otherwise stops entirely during idle/lock.
                # The only exception: when on_reset_command is configured
                # and a quota reset is due, the idle pause is interrupted
                # so the command fires on time.  The flag
                # _idle_reset_pending keeps polling at POLL_INTERVAL
                # until the reset is actually confirmed (usage drop) -
                # this covers server-side delays and transient network
                # errors.  The flag is cleared when update() detects the
                # drop, or when the user returns (they'll see it anyway).
                if self._is_user_away() and not self.task_runner.has_pending_work():
                    reset_deadline = None
                    if ON_RESET_COMMAND:
                        next_reset = self._seconds_until_next_reset()
                        if next_reset is not None:
                            reset_deadline = time.time() + next_reset + RESET_BUFFER
                            self._idle_reset_pending = True
                        elif self._idle_reset_pending:
                            reset_deadline = time.time() + POLL_INTERVAL

                    self._wait_for_activity(until=reset_deadline)

                    if reset_deadline is not None and self._is_user_away():
                        # Woke up for a reset while still idle - poll once
                        break

                    # User returned - show any notifications deferred
                    # during idle and poll immediately if interval elapsed.
                    # _idle_reset_pending is intentionally kept: if the
                    # user locks again before a successful poll confirms
                    # the reset (e.g. network was down), idle polling
                    # must resume.  The flag is only cleared by update()
                    # when a usage drop is actually detected.
                    self._flush_deferred_notifications()
                    lst = self.cache.last_success_time
                    if lst is None:
                        continue

                    next_reset = self._seconds_until_next_reset()
                    if next_reset is not None and next_reset < POLL_FAST:
                        # Returned within the cooldown window before a reset:
                        # polling now would advance last_success into that window
                        # and force the confirming poll to overshoot.  Realign the
                        # wait to just after the reset and keep waiting for it.
                        target = self._reset_aligned_poll_target(next_reset)
                        self._next_poll_time = target
                        continue

                    if time.time() - lst >= interval:
                        break

    # Lifecycle

    def _on_icon_ready(self, icon: Any) -> None:
        """Called by pystray in a separate thread once the tray icon is set up."""
        try:
            icon.visible = True
            if getattr(sys, 'frozen', False):
                sync_autostart_path()
            if not api_headers():
                icon.notify(f"{T['warn_no_token']}\n{T['warn_login']}", T['popup_title'])
            threading.Thread(target=watch_theme_change, args=(self._on_theme_changed,), daemon=True).start()
            self.poll_loop()
        except Exception:
            crash_log(traceback.format_exc())

    def run(self) -> None:
        self.icon.run(setup=self._on_icon_ready)


def crash_log(msg: str) -> None:
    """Show a crash message box (for windowless EXE builds)."""
    ctypes.windll.user32.MessageBoxW(0, msg[:2000], 'Auto Scheduler for Claude - Error', 0x10)
