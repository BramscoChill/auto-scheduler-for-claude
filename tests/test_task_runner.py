"""
Task Runner Tests
==================

Unit tests for task execution, the state file, logs, concurrency, and
shutdown.  Every subprocess is faked and the config dir is redirected to a
temporary directory, so no task ever really runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import auto_scheduler_for_claude.task_runner as runner_mod
import auto_scheduler_for_claude.tasks as tasks_mod
from auto_scheduler_for_claude.task_runner import (
    EXIT_KILLED, EXIT_STOPPED, EXIT_TIMEOUT, LOG_TAIL_BYTES, LOGS_DIRNAME, STATE_FILENAME,
    STATUS_DONE, STATUS_INTERRUPTED, STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING, TaskRunner,
)

WAIT_TIMEOUT = 5.0


class FakeProcess:
    """Stand-in for ``subprocess.Popen`` with a controllable exit."""

    def __init__(self, exit_code: int = 0, block: bool = False, times_out: bool = False) -> None:
        self.exit_code = exit_code
        self.times_out = times_out
        self.killed = False
        self.wait_timeouts: list[float | None] = []
        self._release = threading.Event()
        if not block:
            self._release.set()

    def wait(self, timeout: float | None = None):
        self.wait_timeouts.append(timeout)
        if self.times_out and not self.killed:
            raise subprocess.TimeoutExpired(cmd='claude', timeout=timeout or 0)
        if not self._release.wait(WAIT_TIMEOUT):
            raise AssertionError('FakeProcess.wait() was never released')
        return self.exit_code

    def kill(self) -> None:
        self.killed = True
        self._release.set()

    def finish(self) -> None:
        """Let a blocking wait() return."""
        self._release.set()


class RecordingPopen:
    """Callable replacement for ``subprocess.Popen`` that records every launch."""

    def __init__(self, *processes: FakeProcess) -> None:
        self._queue = list(processes)
        self.calls: list[tuple[tuple, dict]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, *args, **kwargs) -> FakeProcess:
        self.calls.append((args, kwargs))
        process = self._queue.pop(0) if self._queue else FakeProcess()
        self.processes.append(process)
        return process

    @property
    def commands(self) -> list[list[str]]:
        return [call[0][0] for call in self.calls]


def _usage(five_hour_pct: float | None = 10) -> dict:
    """Return a minimal successful API response."""
    if five_hour_pct is None:
        return {'five_hour': None, 'seven_day': {'utilization': 20, 'resets_at': ''}}
    return {'five_hour': {'utilization': five_hour_pct, 'resets_at': '2026-08-10T22:00:00+00:00'}}


def _entry(task_id: str = 'daily', **overrides) -> dict:
    entry = {'id': task_id, 'prompt': 'Do the thing.', 'max_usage_percent': 40}
    entry.update(overrides)
    return entry


@contextmanager
def _runner(entries, *, state=None, max_concurrent=1, popen=None, task_command=None, on_drained=None, files=None):
    """Build a TaskRunner against a temporary config dir with faked subprocesses.

    Yields ``(runner, config_dir, popen, mock_ctypes)``.  Everything runs
    inside the patch context so ``evaluate`` uses the fakes too.  ``files``
    maps a name to its content and is written into the config dir before the
    registry is loaded, for tasks that read their prompt from a file.
    """
    with TemporaryDirectory() as tmp:
        config_dir = Path(tmp)
        for name, content in (files or {}).items():
            (config_dir / name).write_text(content, encoding='utf-8')
        if entries is not None:
            content = entries if isinstance(entries, str) else json.dumps(entries)
            (config_dir / tasks_mod.TASKS_FILENAME).write_text(content, encoding='utf-8')
        if state is not None:
            content = state if isinstance(state, str) else json.dumps(state)
            (config_dir / STATE_FILENAME).write_text(content, encoding='utf-8')

        popen = popen if popen is not None else RecordingPopen()
        mock_ctypes = MagicMock()
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tmp}), \
             patch.object(tasks_mod, 'ctypes', MagicMock()), \
             patch.object(runner_mod, 'ctypes', mock_ctypes), \
             patch.object(runner_mod, 'MAX_CONCURRENT_TASKS', max_concurrent), \
             patch.object(runner_mod, 'TASK_COMMAND', task_command if task_command is not None else []), \
             patch.object(runner_mod.subprocess, 'Popen', popen):
            task_runner = TaskRunner(on_drained=on_drained)
            try:
                yield task_runner, config_dir, popen, mock_ctypes
            finally:
                # A watcher thread holds the run's log file open, and Windows
                # refuses to delete the temp dir while it does - so a test that
                # leaves a task running would fail during cleanup, not on its
                # own assertion.
                task_runner.kill_all()
                _wait_until(lambda: task_runner.running_count() == 0, 'runs to finish')


def _wait_until(condition, message: str) -> None:
    """Block until *condition* holds, failing the test after WAIT_TIMEOUT."""
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f'timed out waiting for: {message}')


def _read_state(config_dir: Path) -> dict:
    return json.loads((config_dir / STATE_FILENAME).read_text(encoding='utf-8'))


class TestStart(unittest.TestCase):
    """Tests for launching a task."""

    def test_start_writes_start_record_and_log(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, config_dir, popen, _):
            runner.evaluate(_usage(10))

            self.assertEqual(runner.running_count(), 1)
            record = _read_state(config_dir)['tasks']['daily']
            self.assertIsNotNone(record['started_at'])
            self.assertIsNone(record['finished_at'])
            self.assertIsNone(record['exit_code'])

            log_file = config_dir / LOGS_DIRNAME / record['log']
            self.assertTrue(log_file.is_file())
            header = log_file.read_text(encoding='utf-8')
            self.assertIn('=== task daily ===', header)

            blocking.finish()
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

    def test_prompt_file_content_is_sent_and_logged(self):
        entry = _entry(prompt='Inline, must be ignored.', prompt_file='daily-prompt.md')
        with _runner([entry], files={'daily-prompt.md': 'Read the playbook.'}) as (runner, config_dir, popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            command = popen.commands[0]
            self.assertTrue(command[-2].startswith('Read the playbook.'))
            self.assertNotIn('Inline, must be ignored.', command[-2])

            log_text = runner.log_path('daily').read_text(encoding='utf-8')
            self.assertIn(f'prompt:  {config_dir / "daily-prompt.md"}', log_text)

    def test_inline_prompt_logs_no_prompt_source(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            self.assertNotIn('\nprompt:', runner.log_path('daily').read_text(encoding='utf-8'))

    def test_state_file_carries_a_version(self):
        with _runner([_entry()]) as (runner, config_dir, _, _mock):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            self.assertEqual(_read_state(config_dir)['version'], runner_mod.STATE_VERSION)

    def test_command_appends_caveat_and_skips_permissions(self):
        with _runner([_entry()]) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

        command = popen.commands[0]
        self.assertEqual(command[-3], '-p')
        self.assertTrue(command[-2].startswith('Do the thing.'))
        self.assertTrue(command[-2].endswith(tasks_mod.PRINT_MODE_CAVEAT))
        self.assertEqual(command[-1], '--dangerously-skip-permissions')

    def test_task_command_setting_overrides_the_binary(self):
        with _runner([_entry()], task_command=['wsl', 'claude']) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

        self.assertEqual(popen.commands[0][:2], ['wsl', 'claude'])

    def test_environment_carries_the_quota_snapshot_and_task_id(self):
        with _runner([_entry()]) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

        env = popen.calls[0][1]['env']
        self.assertEqual(env['AUTO_SCHEDULER_EVENT'], 'task')
        self.assertEqual(env['AUTO_SCHEDULER_TASK_ID'], 'daily')
        self.assertEqual(env['AUTO_SCHEDULER_UTILIZATION_FIVE_HOUR'], '10')
        self.assertIn('AUTO_SCHEDULER_VERSION', env)

    def test_cwd_is_passed_when_configured(self):
        with TemporaryDirectory() as work_dir:
            with _runner([_entry(cwd=work_dir)]) as (runner, _config_dir, popen, _):
                runner.evaluate(_usage(10))
                _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            self.assertEqual(popen.calls[0][1]['cwd'], work_dir)

    def test_cwd_defaults_to_none(self):
        with _runner([_entry()]) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
        self.assertIsNone(popen.calls[0][1]['cwd'])

    def test_unwritable_log_directory_prevents_the_start(self):
        """A file where the log directory belongs makes mkdir fail."""
        with _runner([_entry()]) as (runner, config_dir, popen, mock_ctypes):
            (config_dir / LOGS_DIRNAME).write_text('not a directory', encoding='utf-8')
            runner.evaluate(_usage(10))

            self.assertEqual(popen.calls, [])
            self.assertEqual(runner.running_count(), 0)
            mock_ctypes.windll.user32.MessageBoxW.assert_called_once()

            # Queue disabled for the session: a later evaluate starts nothing.
            runner.evaluate(_usage(10))
            self.assertEqual(popen.calls, [])
            self.assertFalse(runner.has_pending_work())


class TestEvaluateGating(unittest.TestCase):
    """Tests for the conditions under which nothing is started."""

    def test_missing_five_hour_starts_nothing(self):
        with _runner([_entry()]) as (runner, _config_dir, popen, _):
            runner.evaluate({'seven_day': {'utilization': 5}})
        self.assertEqual(popen.calls, [])

    def test_null_five_hour_starts_nothing(self):
        """five_hour can be present and null when the quota is not enabled."""
        with _runner([_entry()]) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(None))
        self.assertEqual(popen.calls, [])

    def test_utilization_above_threshold_starts_nothing(self):
        with _runner([_entry()]) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(41))
        self.assertEqual(popen.calls, [])

    def test_no_task_file_starts_nothing(self):
        with _runner(None) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(0))
        self.assertEqual(popen.calls, [])
        self.assertFalse(runner.has_tasks())

    def test_completed_task_is_never_re_run(self):
        """One-shot means once, ever."""
        state = {'version': 1, 'tasks': {'daily': {'started_at': 'x', 'finished_at': 'y', 'exit_code': 0, 'log': 'l'}}}
        with _runner([_entry()], state=state) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(0))
        self.assertEqual(popen.calls, [])

    def test_interrupted_record_is_not_re_run(self):
        """A run the app died in the middle of must not silently restart."""
        state = {'version': 1, 'tasks': {'daily': {'started_at': 'x', 'finished_at': None, 'exit_code': None, 'log': 'l'}}}
        with _runner([_entry()], state=state) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(0))
            self.assertEqual(popen.calls, [])
            self.assertEqual(dict(runner.menu_entries())['daily'], STATUS_INTERRUPTED)

    def test_corrupt_state_file_disables_the_queue(self):
        """Treating an unreadable state file as empty would re-run everything."""
        with _runner([_entry()], state='{ not json') as (runner, _config_dir, popen, mock_ctypes):
            mock_ctypes.windll.user32.MessageBoxW.assert_called_once()
            runner.evaluate(_usage(0))
            self.assertEqual(popen.calls, [])
            self.assertFalse(runner.has_pending_work())

    def test_state_file_without_tasks_object_disables_the_queue(self):
        with _runner([_entry()], state={'version': 1}) as (runner, _config_dir, popen, mock_ctypes):
            mock_ctypes.windll.user32.MessageBoxW.assert_called_once()
            runner.evaluate(_usage(0))
            self.assertEqual(popen.calls, [])


class TestCompletion(unittest.TestCase):
    """Tests for what a finished run records."""

    def test_normal_exit_records_exit_code_and_finished_at(self):
        with _runner([_entry()], popen=RecordingPopen(FakeProcess(exit_code=0))) as (runner, config_dir, _, _mock):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            record = _read_state(config_dir)['tasks']['daily']
            self.assertEqual(record['exit_code'], 0)
            self.assertIsNotNone(record['finished_at'])
            self.assertEqual(runner.exit_code('daily'), 0)
            self.assertEqual(dict(runner.menu_entries())['daily'], STATUS_DONE)

    def test_non_zero_exit_still_completes_the_task(self):
        """There are no retries: a failing task is done, not requeued."""
        with _runner([_entry()], popen=RecordingPopen(FakeProcess(exit_code=3))) as (runner, config_dir, popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            self.assertEqual(_read_state(config_dir)['tasks']['daily']['exit_code'], 3)
            runner.evaluate(_usage(10))
            self.assertEqual(len(popen.calls), 1)

    def test_log_footer_records_the_exit_code(self):
        with _runner([_entry()], popen=RecordingPopen(FakeProcess(exit_code=7))) as (runner, config_dir, _, _mock):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            log_path = runner.log_path('daily')
            self.assertIsNotNone(log_path)
            self.assertIn('exit code: 7', log_path.read_text(encoding='utf-8'))
            self.assertEqual(log_path.parent, config_dir / LOGS_DIRNAME)

    def test_timeout_kills_the_child_and_records_124(self):
        timing_out = FakeProcess(times_out=True)
        with _runner([_entry(timeout_minutes=1)], popen=RecordingPopen(timing_out)) as (runner, config_dir, _, _mock):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            self.assertTrue(timing_out.killed)
            self.assertEqual(_read_state(config_dir)['tasks']['daily']['exit_code'], EXIT_TIMEOUT)
            self.assertEqual(timing_out.wait_timeouts[0], 60)

    def test_zero_timeout_passes_no_timeout_to_wait(self):
        process = FakeProcess()
        with _runner([_entry(timeout_minutes=0)], popen=RecordingPopen(process)) as (runner, _config_dir, _, _mock):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

        self.assertEqual(process.wait_timeouts, [None])


class TestConcurrency(unittest.TestCase):
    """Tests for max_concurrent_tasks and thread safety."""

    def test_single_slot_enforced_across_repeated_evaluates(self):
        first, second = FakeProcess(block=True), FakeProcess(block=True)
        entries = [_entry('a'), _entry('b')]
        with _runner(entries, popen=RecordingPopen(first, second)) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            runner.evaluate(_usage(10))
            runner.evaluate(_usage(10))
            self.assertEqual(len(popen.calls), 1)
            self.assertEqual(runner.running_count(), 1)

            first.finish()
            _wait_until(lambda: runner.running_count() == 0, 'first run to finish')

            runner.evaluate(_usage(10))
            self.assertEqual(len(popen.calls), 2)
            second.finish()
            _wait_until(lambda: runner.running_count() == 0, 'second run to finish')

    def test_two_slots_start_two_tasks_from_one_snapshot(self):
        first, second = FakeProcess(block=True), FakeProcess(block=True)
        entries = [_entry('a'), _entry('b'), _entry('c')]
        with _runner(entries, max_concurrent=2, popen=RecordingPopen(first, second)) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            self.assertEqual(len(popen.calls), 2)
            self.assertEqual(runner.running_count(), 2)

            first.finish()
            second.finish()
            _wait_until(lambda: runner.running_count() == 0, 'runs to finish')

    def test_blocked_task_does_not_block_the_next_one(self):
        entries = [_entry('expensive', max_usage_percent=10), _entry('cheap', max_usage_percent=90)]
        with _runner(entries) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(50))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

        env = popen.calls[0][1]['env']
        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(env['AUTO_SCHEDULER_TASK_ID'], 'cheap')

    def test_concurrent_evaluate_calls_start_each_task_once(self):
        entries = [_entry(f't{index}') for index in range(6)]
        with _runner(entries, max_concurrent=6) as (runner, _config_dir, popen, _):
            barrier = threading.Barrier(4)

            def hammer() -> None:
                barrier.wait()
                for _ in range(5):
                    runner.evaluate(_usage(10))

            threads = [threading.Thread(target=hammer) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(WAIT_TIMEOUT)

            _wait_until(lambda: runner.running_count() == 0, 'runs to finish')
            started = [call[1]['env']['AUTO_SCHEDULER_TASK_ID'] for call in popen.calls]

        self.assertEqual(sorted(started), sorted(entry['id'] for entry in entries))


class TestStatuses(unittest.TestCase):
    """Tests for the tray-menu status keys."""

    def test_waiting_until_the_threshold_is_met(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            self.assertEqual(dict(runner.menu_entries())['daily'], STATUS_WAITING)
            runner.evaluate(_usage(80))
            self.assertEqual(dict(runner.menu_entries())['daily'], STATUS_WAITING)

    def test_pending_when_the_threshold_is_met_but_no_slot_is_free(self):
        blocking = FakeProcess(block=True)
        entries = [_entry('a'), _entry('b')]
        with _runner(entries, popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))
            statuses = dict(runner.menu_entries())
            self.assertEqual(statuses['a'], STATUS_RUNNING)
            self.assertEqual(statuses['b'], STATUS_PENDING)

            blocking.finish()
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

    def test_menu_entries_follow_file_order(self):
        entries = [_entry('c'), _entry('a'), _entry('b')]
        with _runner(entries) as (runner, _config_dir, _popen, _):
            self.assertEqual([task_id for task_id, _status in runner.menu_entries()], ['c', 'a', 'b'])


class TestShutdown(unittest.TestCase):
    """Tests for draining and killing on quit."""

    def test_kill_all_records_130(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(10))
            self.assertEqual(runner.running_count(), 1)

            runner.kill_all()

            self.assertTrue(blocking.killed)
            self.assertEqual(runner.running_count(), 0)
            self.assertEqual(_read_state(config_dir)['tasks']['daily']['exit_code'], EXIT_KILLED)

    def test_drain_starts_no_new_tasks_and_calls_back_after_the_last_one(self):
        blocking = FakeProcess(block=True)
        drained = threading.Event()
        entries = [_entry('a'), _entry('b')]
        with _runner(entries, popen=RecordingPopen(blocking), on_drained=drained.set) as (runner, _config_dir, popen, _):
            runner.evaluate(_usage(10))
            runner.begin_drain()

            runner.evaluate(_usage(10))
            self.assertEqual(len(popen.calls), 1)
            self.assertTrue(runner.is_draining())
            self.assertFalse(drained.is_set())

            blocking.finish()
            self.assertTrue(drained.wait(WAIT_TIMEOUT))
            self.assertEqual(runner.running_count(), 0)

    def test_kill_all_with_nothing_running_is_a_no_op(self):
        with _runner([_entry()]) as (runner, config_dir, popen, _):
            runner.kill_all()
        self.assertEqual(popen.calls, [])
        self.assertFalse((config_dir / STATE_FILENAME).exists())


class TestPendingWork(unittest.TestCase):
    """has_pending_work() drives the poll loop's idle bypass."""

    def test_pending_while_a_task_has_not_run(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            self.assertTrue(runner.has_pending_work())

    def test_not_pending_once_every_task_is_recorded(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            self.assertFalse(runner.has_pending_work())

    def test_not_pending_without_a_task_file(self):
        with _runner(None) as (runner, _config_dir, _popen, _):
            self.assertFalse(runner.has_pending_work())

    def test_pending_while_a_task_runs_even_when_draining(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))
            runner.begin_drain()
            self.assertTrue(runner.has_pending_work())

            blocking.finish()
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            self.assertFalse(runner.has_pending_work())


class TestVersion(unittest.TestCase):
    """Tests for the change counter the popup dedups its refresh on."""

    def test_bumps_when_a_task_starts_and_when_it_ends(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            before = runner.version
            runner.evaluate(_usage(10))
            started = runner.version
            self.assertGreater(started, before)

            blocking.finish()
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            self.assertGreater(runner.version, started)

    def test_bumps_when_the_quota_reading_changes(self):
        """Waiting and pending are derived from it, so rows move without any run."""
        with _runner([_entry(max_usage_percent=5)]) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(80))
            first = runner.version
            runner.evaluate(_usage(60))

            self.assertGreater(runner.version, first)

    def test_unchanged_quota_reading_does_not_bump(self):
        """Otherwise every poll would repaint the popup for nothing."""
        with _runner([_entry(max_usage_percent=5)]) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(80))
            first = runner.version
            runner.evaluate(_usage(80))

            self.assertEqual(runner.version, first)


class TestSnapshot(unittest.TestCase):
    """Tests for the single-lock queue snapshot the popup renders."""

    def test_reports_status_keys_not_text(self):
        with _runner([_entry('a'), _entry('b', max_usage_percent=5)]) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(20))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            snapshot = runner.snapshot()

            self.assertEqual([row['status'] for row in snapshot['tasks']], [STATUS_DONE, STATUS_WAITING])

    def test_carries_registry_fields_and_queue_state(self):
        with _runner([_entry(description='Nightly', timeout_minutes=30)], max_concurrent=3) as (runner, _c, _p, _):
            runner.evaluate(_usage(10))
            snapshot = runner.snapshot()

        self.assertEqual(snapshot['max_concurrent'], 3)
        self.assertEqual(snapshot['five_hour_pct'], 10)
        self.assertFalse(snapshot['disabled'])
        row = snapshot['tasks'][0]
        self.assertEqual(row['description'], 'Nightly')
        self.assertEqual(row['timeout_minutes'], 30)
        self.assertEqual(row['max_usage_percent'], 40)

    def test_running_row_reports_elapsed_and_finished_row_does_not(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))
            self.assertIsNotNone(runner.snapshot()['tasks'][0]['elapsed_seconds'])

            blocking.finish()
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')
            self.assertIsNone(runner.snapshot()['tasks'][0]['elapsed_seconds'])

    def test_records_utilization_at_start(self):
        with _runner([_entry()]) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(31))

            self.assertEqual(runner.snapshot()['tasks'][0]['started_at_utilization'], 31)
            self.assertEqual(_read_state(config_dir)['tasks']['daily']['started_at_utilization'], 31)

    def test_tolerates_a_state_record_written_before_utilization_was_recorded(self):
        old_record = {'version': 1, 'tasks': {'daily': {
            'started_at': '2026-08-10T22:04:11+00:00', 'finished_at': '2026-08-10T22:10:11+00:00',
            'exit_code': 0, 'log': 'daily-20260810-220411.log',
        }}}
        with _runner([_entry()], state=old_record) as (runner, _config_dir, _popen, _):
            row = runner.snapshot()['tasks'][0]

        self.assertIsNone(row['started_at_utilization'])
        self.assertEqual(row['status'], STATUS_DONE)

    def test_empty_registry_snapshots_cleanly(self):
        with _runner(None) as (runner, _config_dir, _popen, _):
            snapshot = runner.snapshot()

        self.assertEqual(snapshot['tasks'], [])
        self.assertEqual(snapshot['running_count'], 0)
        self.assertIsNone(snapshot['five_hour_pct'])


class TestStop(unittest.TestCase):
    """Tests for stopping one running task from the popup."""

    def test_records_the_stopped_exit_code(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(10))

            self.assertTrue(runner.stop('daily'))
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            self.assertTrue(blocking.killed)
            record = _read_state(config_dir)['tasks']['daily']
            self.assertEqual(record['exit_code'], EXIT_STOPPED)
            self.assertIsNotNone(record['finished_at'])

    def test_does_not_start_draining(self):
        """Stopping one task is not a decision to stop starting others."""
        blocking = FakeProcess(block=True)
        with _runner([_entry('a'), _entry('b')], popen=RecordingPopen(blocking)) as (runner, _c, popen, _):
            runner.evaluate(_usage(10))
            runner.stop('a')
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            self.assertFalse(runner.is_draining())
            runner.evaluate(_usage(10))
            self.assertEqual(runner.status('b'), STATUS_RUNNING)
            self.assertEqual(len(popen.calls), 2)

    def test_unknown_or_finished_task_is_a_no_op(self):
        with _runner([_entry()]) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.status('daily') == STATUS_DONE, 'run to finish')

            self.assertFalse(runner.stop('daily'))
            self.assertFalse(runner.stop('nope'))
            self.assertEqual(_read_state(config_dir)['tasks']['daily']['exit_code'], 0)

    def test_keeps_the_recorded_start_utilization(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(31))
            runner.stop('daily')
            _wait_until(lambda: runner.running_count() == 0, 'run to finish')

            self.assertEqual(_read_state(config_dir)['tasks']['daily']['started_at_utilization'], 31)


class TestReload(unittest.TestCase):
    """Tests for swapping in a registry the caller has already validated."""

    def test_replaces_the_task_set_and_bumps_the_version(self):
        with _runner([_entry('a')]) as (runner, _config_dir, _popen, _):
            before = runner.version
            replacement = tasks_mod.Task(
                id='b', prompt='x', max_usage_percent=10, timeout_minutes=1, cwd=None, description='',
            )
            runner.reload([replacement])

            self.assertEqual([task.id for task in runner.tasks], ['b'])
            self.assertGreater(runner.version, before)

    def test_keeps_completed_records(self):
        """A reload must never resurrect a task that already ran."""
        with _runner([_entry()]) as (runner, _config_dir, _popen, popen_mock):
            runner.evaluate(_usage(10))
            _wait_until(lambda: runner.status('daily') == STATUS_DONE, 'run to finish')

            runner.reload(list(runner.tasks))

            self.assertEqual(runner.status('daily'), STATUS_DONE)
            self.assertEqual(runner.completed_ids(), ['daily'])

    def test_keeps_a_running_task_running(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))
            runner.reload(list(runner.tasks))

            self.assertEqual(runner.running_count(), 1)
            self.assertEqual(runner.status('daily'), STATUS_RUNNING)

    def test_reloading_to_empty_turns_the_queue_off(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            runner.reload([])

            self.assertFalse(runner.has_tasks())
            self.assertEqual(runner.snapshot()['tasks'], [])


class TestLogTail(unittest.TestCase):
    """Tests for the incremental log reader behind the popup's log view."""

    def test_first_read_returns_the_header_and_an_offset(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))

            chunk = runner.log_tail('daily')

            self.assertTrue(chunk['reset'])
            self.assertIn('=== task daily ===', chunk['text'])
            self.assertGreater(chunk['offset'], 0)
            self.assertTrue(chunk['name'].startswith('daily-'))

    def test_second_read_returns_only_what_was_appended(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(10))
            first = runner.log_tail('daily')

            log_path = config_dir / LOGS_DIRNAME / first['name']
            with open(log_path, 'a', encoding='utf-8', newline='') as handle:
                handle.write('progress line\n')

            second = runner.log_tail('daily', first['offset'])

            self.assertFalse(second['reset'])
            self.assertEqual(second['text'], 'progress line\n')

    def test_offset_past_the_end_restarts_the_tail(self):
        """A replaced or truncated file must not silently return nothing."""
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, _config_dir, _popen, _):
            runner.evaluate(_usage(10))

            chunk = runner.log_tail('daily', 10_000_000)

            self.assertTrue(chunk['reset'])
            self.assertIn('=== task daily ===', chunk['text'])

    def test_reads_at_most_one_tail_chunk(self):
        blocking = FakeProcess(block=True)
        with _runner([_entry()], popen=RecordingPopen(blocking)) as (runner, config_dir, _popen, _):
            runner.evaluate(_usage(10))
            name = runner.log_tail('daily')['name']

            with open(config_dir / LOGS_DIRNAME / name, 'a', encoding='utf-8') as handle:
                handle.write('x' * (LOG_TAIL_BYTES * 2))

            chunk = runner.log_tail('daily')

            self.assertLessEqual(len(chunk['text'].encode('utf-8')), LOG_TAIL_BYTES)

    def test_task_without_a_run_has_no_log(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            self.assertIsNone(runner.log_tail('daily'))

    def test_unknown_task_has_no_log(self):
        with _runner([_entry()]) as (runner, _config_dir, _popen, _):
            self.assertIsNone(runner.log_tail('nope'))

    def test_a_record_pointing_outside_the_log_dir_is_refused(self):
        """The log view must not become a way to read arbitrary files."""
        state = {'version': 1, 'tasks': {'daily': {
            'started_at': '2026-08-10T22:04:11+00:00', 'finished_at': '2026-08-10T22:10:11+00:00',
            'exit_code': 0, 'log': '../auto-scheduler-state.json',
        }}}
        with _runner([_entry()], state=state) as (runner, _config_dir, _popen, _):
            self.assertIsNone(runner.log_tail('daily'))


if __name__ == '__main__':
    unittest.main()
