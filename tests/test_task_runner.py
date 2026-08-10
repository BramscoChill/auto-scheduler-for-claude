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
    EXIT_KILLED, EXIT_TIMEOUT, LOGS_DIRNAME, STATE_FILENAME,
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
def _runner(entries, *, state=None, max_concurrent=1, popen=None, task_command=None, on_drained=None):
    """Build a TaskRunner against a temporary config dir with faked subprocesses.

    Yields ``(runner, config_dir, popen, mock_ctypes)``.  Everything runs
    inside the patch context so ``evaluate`` uses the fakes too.
    """
    with TemporaryDirectory() as tmp:
        config_dir = Path(tmp)
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
            yield TaskRunner(on_drained=on_drained), config_dir, popen, mock_ctypes


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


if __name__ == '__main__':
    unittest.main()
