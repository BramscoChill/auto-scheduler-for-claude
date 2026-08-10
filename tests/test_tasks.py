"""
Task Queue Tests
=================

Unit tests for task registry loading, validation, and runnable selection.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import auto_scheduler_for_claude.tasks as tasks_mod
from auto_scheduler_for_claude.tasks import Task, sanitize_task_id, select_runnable


def _write_and_load(content, config_dir: str) -> tuple[list[Task], MagicMock]:
    """Write *content* as the task file in *config_dir* and load it.

    ``content`` is written verbatim when it is a string, otherwise dumped as
    JSON.  Returns the loaded tasks and the mocked ``ctypes`` module so tests
    can assert whether an error box was shown.
    """
    path = Path(config_dir) / tasks_mod.TASKS_FILENAME
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')

    mock_ctypes = MagicMock()
    with patch.dict('os.environ', {'CLAUDE_CONFIG_DIR': config_dir}), \
         patch.object(tasks_mod, 'ctypes', mock_ctypes):
        return tasks_mod.load_tasks(), mock_ctypes


def _valid_entry(**overrides) -> dict:
    """Return a minimal valid task entry with optional field overrides."""
    entry = {'id': 'daily', 'prompt': 'Do the thing.', 'max_usage_percent': 40}
    entry.update(overrides)
    return entry


class TestLoadTasks(unittest.TestCase):
    """Tests for load_tasks() file discovery and parsing."""

    def test_missing_file_yields_empty_queue_without_error(self):
        """No task file means the feature is off, not an error."""
        with TemporaryDirectory() as tmp:
            mock_ctypes = MagicMock()
            with patch.dict('os.environ', {'CLAUDE_CONFIG_DIR': tmp}), \
                 patch.object(tasks_mod, 'ctypes', mock_ctypes):
                result = tasks_mod.load_tasks()
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_empty_file_yields_empty_queue_without_error(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load('   \n', tmp)
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_valid_file_parses_with_defaults_applied(self):
        """Optional fields fall back to their documented defaults."""
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([_valid_entry()], tmp)

        self.assertEqual(len(result), 1)
        task = result[0]
        self.assertEqual(task.id, 'daily')
        self.assertEqual(task.prompt, 'Do the thing.')
        self.assertEqual(task.max_usage_percent, 40.0)
        self.assertEqual(task.timeout_minutes, 180.0)
        self.assertIsNone(task.cwd)
        self.assertEqual(task.description, '')
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_all_fields_read(self):
        with TemporaryDirectory() as tmp:
            entry = _valid_entry(cwd=tmp, timeout_minutes=30, description='Nightly run')
            result, _ = _write_and_load([entry], tmp)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].cwd, tmp)
        self.assertEqual(result[0].timeout_minutes, 30.0)
        self.assertEqual(result[0].description, 'Nightly run')

    def test_file_order_preserved(self):
        with TemporaryDirectory() as tmp:
            entries = [_valid_entry(id='a'), _valid_entry(id='b'), _valid_entry(id='c')]
            result, _ = _write_and_load(entries, tmp)
        self.assertEqual([task.id for task in result], ['a', 'b', 'c'])

    def test_invalid_json_disables_queue_with_error(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load('[{', tmp)
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_called_once()

    def test_object_instead_of_array_rejected(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load({'id': 'daily'}, tmp)
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_called_once()


class TestValidation(unittest.TestCase):
    """Tests for per-entry validation rules."""

    def _reject(self, entry) -> str:
        """Assert that a single-entry file is rejected, returning the error text."""
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([entry], tmp)
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_called_once()
        return mock_ctypes.windll.user32.MessageBoxW.call_args[0][1]

    def _accept(self, entry) -> Task:
        """Assert that a single-entry file is accepted, returning the task."""
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([entry], tmp)
        self.assertEqual(len(result), 1)
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()
        return result[0]

    def test_missing_id_rejected(self):
        message = self._reject({'prompt': 'x', 'max_usage_percent': 10})
        self.assertIn('"id"', message)

    def test_blank_id_rejected(self):
        self._reject({'id': '   ', 'prompt': 'x', 'max_usage_percent': 10})

    def test_missing_prompt_rejected(self):
        message = self._reject({'id': 'a', 'max_usage_percent': 10})
        self.assertIn('"prompt"', message)

    def test_blank_prompt_rejected(self):
        self._reject({'id': 'a', 'prompt': '  ', 'max_usage_percent': 10})

    def test_missing_max_usage_percent_rejected(self):
        message = self._reject({'id': 'a', 'prompt': 'x'})
        self.assertIn('"max_usage_percent"', message)

    def test_max_usage_percent_above_100_rejected(self):
        self._reject(_valid_entry(max_usage_percent=101))

    def test_max_usage_percent_below_zero_rejected(self):
        self._reject(_valid_entry(max_usage_percent=-1))

    def test_max_usage_percent_boolean_rejected(self):
        """A JSON true is an int in Python but is not a valid percentage."""
        self._reject(_valid_entry(max_usage_percent=True))

    def test_max_usage_percent_string_rejected(self):
        self._reject(_valid_entry(max_usage_percent='40'))

    def test_max_usage_percent_bounds_accepted(self):
        self.assertEqual(self._accept(_valid_entry(max_usage_percent=0)).max_usage_percent, 0.0)
        self.assertEqual(self._accept(_valid_entry(max_usage_percent=100)).max_usage_percent, 100.0)

    def test_timeout_zero_accepted_as_no_limit(self):
        self.assertEqual(self._accept(_valid_entry(timeout_minutes=0)).timeout_minutes, 0.0)

    def test_negative_timeout_rejected(self):
        self._reject(_valid_entry(timeout_minutes=-1))

    def test_timeout_boolean_rejected(self):
        self._reject(_valid_entry(timeout_minutes=True))

    def test_fractional_timeout_accepted(self):
        self.assertEqual(self._accept(_valid_entry(timeout_minutes=1.5)).timeout_minutes, 1.5)

    def test_existing_cwd_accepted(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([_valid_entry(cwd=tmp)], tmp)
            self.assertEqual(len(result), 1)
            self.assertTrue(Path(result[0].cwd).is_dir())
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_nonexistent_cwd_rejected(self):
        message = self._reject(_valid_entry(cwd='D:/definitely/not/here/at/all'))
        self.assertIn('"cwd"', message)

    def test_non_string_description_rejected(self):
        self._reject(_valid_entry(description=5))

    def test_unknown_field_rejected(self):
        """A typo like max_usage_pct must not silently fall back to a default."""
        message = self._reject(_valid_entry(timeout_minutes_typo=5))
        self.assertIn('timeout_minutes_typo', message)

    def test_duplicate_ids_rejected(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([_valid_entry(), _valid_entry()], tmp)
        self.assertEqual(result, [])
        self.assertIn('duplicate', mock_ctypes.windll.user32.MessageBoxW.call_args[0][1])

    def test_non_object_entry_rejected(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load(['daily'], tmp)
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_called_once()

    def test_one_invalid_entry_disables_the_whole_queue(self):
        """A partially loaded queue of unattended agents is worse than no queue."""
        with TemporaryDirectory() as tmp:
            entries = [_valid_entry(id='good'), _valid_entry(id='bad', max_usage_percent=500)]
            result, mock_ctypes = _write_and_load(entries, tmp)
        self.assertEqual(result, [])
        mock_ctypes.windll.user32.MessageBoxW.assert_called_once()

    def test_all_errors_reported_in_one_box(self):
        with TemporaryDirectory() as tmp:
            entries = [_valid_entry(id='one', max_usage_percent=500), _valid_entry(id='two', timeout_minutes=-5)]
            result, mock_ctypes = _write_and_load(entries, tmp)
        self.assertEqual(result, [])
        message = mock_ctypes.windll.user32.MessageBoxW.call_args[0][1]
        self.assertIn('"one"', message)
        self.assertIn('"two"', message)


class TestPromptFile(unittest.TestCase):
    """Tests for reading the prompt from a file instead of the registry."""

    def test_prompt_file_used_instead_of_prompt(self):
        """When both are given, the file wins and the inline prompt is ignored."""
        with TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'prompt.md'
            prompt_path.write_text('From the file.', encoding='utf-8')
            entry = _valid_entry(prompt='Inline, must be ignored.', prompt_file=str(prompt_path))
            result, mock_ctypes = _write_and_load([entry], tmp)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].prompt, 'From the file.')
            self.assertEqual(Path(result[0].prompt_file), prompt_path)
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_prompt_file_without_prompt_accepted(self):
        with TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'prompt.md'
            prompt_path.write_text('Only the file.', encoding='utf-8')
            entry = {'id': 'daily', 'prompt_file': str(prompt_path), 'max_usage_percent': 40}
            result, mock_ctypes = _write_and_load([entry], tmp)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].prompt, 'Only the file.')
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_invalid_inline_prompt_ignored_when_prompt_file_given(self):
        """"Ignored" is literal - a leftover blank prompt is not validated."""
        with TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'prompt.md'
            prompt_path.write_text('From the file.', encoding='utf-8')
            entry = _valid_entry(prompt='   ', prompt_file=str(prompt_path))
            result, mock_ctypes = _write_and_load([entry], tmp)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].prompt, 'From the file.')
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_file_contents_are_stripped(self):
        with TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'prompt.md'
            prompt_path.write_text('\n\n  Do the thing.\n\n', encoding='utf-8')
            entry = _valid_entry(prompt_file=str(prompt_path))
            result, _ = _write_and_load([entry], tmp)

            self.assertEqual(result[0].prompt, 'Do the thing.')

    def test_empty_prompt_file_rejected(self):
        """An unattended agent must never be launched with a blank prompt."""
        with TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'prompt.md'
            prompt_path.write_text('   \n\n', encoding='utf-8')
            entry = _valid_entry(prompt_file=str(prompt_path))
            result, mock_ctypes = _write_and_load([entry], tmp)

            self.assertEqual(result, [])
            self.assertIn('empty', mock_ctypes.windll.user32.MessageBoxW.call_args[0][1])

    def test_missing_prompt_file_rejected(self):
        with TemporaryDirectory() as tmp:
            entry = _valid_entry(prompt_file=str(Path(tmp) / 'nope.md'))
            result, mock_ctypes = _write_and_load([entry], tmp)

            self.assertEqual(result, [])
            self.assertIn('"prompt_file"', mock_ctypes.windll.user32.MessageBoxW.call_args[0][1])

    def test_blank_prompt_file_rejected(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([_valid_entry(prompt_file='  ')], tmp)

            self.assertEqual(result, [])
            self.assertIn('"prompt_file"', mock_ctypes.windll.user32.MessageBoxW.call_args[0][1])

    def test_null_prompt_file_falls_back_to_prompt(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([_valid_entry(prompt_file=None)], tmp)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].prompt, 'Do the thing.')
            self.assertIsNone(result[0].prompt_file)
        mock_ctypes.windll.user32.MessageBoxW.assert_not_called()

    def test_neither_prompt_nor_prompt_file_rejected(self):
        with TemporaryDirectory() as tmp:
            result, mock_ctypes = _write_and_load([{'id': 'a', 'max_usage_percent': 10}], tmp)

            self.assertEqual(result, [])
            self.assertIn('"prompt_file"', mock_ctypes.windll.user32.MessageBoxW.call_args[0][1])

    def test_relative_prompt_file_resolves_against_cwd(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            (project / 'prompts').mkdir(parents=True)
            (project / 'prompts' / 'daily.md').write_text('Project prompt.', encoding='utf-8')
            entry = _valid_entry(cwd=str(project), prompt_file='prompts/daily.md')
            result, _ = _write_and_load([entry], tmp)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].prompt, 'Project prompt.')

    def test_relative_prompt_file_resolves_against_config_dir_without_cwd(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / 'prompts').mkdir()
            (Path(tmp) / 'prompts' / 'daily.md').write_text('Config dir prompt.', encoding='utf-8')
            entry = _valid_entry(prompt_file='prompts/daily.md')
            result, _ = _write_and_load([entry], tmp)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].prompt, 'Config dir prompt.')

    def test_prompt_file_is_read_once_at_load(self):
        """Editing the file afterwards does not change an already loaded task."""
        with TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'prompt.md'
            prompt_path.write_text('Original.', encoding='utf-8')
            result, _ = _write_and_load([_valid_entry(prompt_file=str(prompt_path))], tmp)
            prompt_path.write_text('Edited.', encoding='utf-8')

            self.assertEqual(result[0].prompt, 'Original.')


class TestSelectRunnable(unittest.TestCase):
    """Tests for the pure selection logic."""

    @staticmethod
    def _task(task_id: str, max_usage_percent: float) -> Task:
        return Task(
            id=task_id, prompt='p', max_usage_percent=max_usage_percent,
            timeout_minutes=180.0, cwd=None, description='',
        )

    def test_utilization_below_threshold_runs(self):
        tasks = [self._task('a', 40)]
        self.assertEqual(select_runnable(tasks, set(), set(), 38, 1), tasks)

    def test_utilization_equal_to_threshold_runs(self):
        """The comparison is <=, which is what makes max_usage_percent 0 usable."""
        tasks = [self._task('a', 40)]
        self.assertEqual(select_runnable(tasks, set(), set(), 40, 1), tasks)

    def test_utilization_above_threshold_does_not_run(self):
        tasks = [self._task('a', 40)]
        self.assertEqual(select_runnable(tasks, set(), set(), 41, 1), [])

    def test_zero_threshold_runs_only_on_a_fresh_window(self):
        tasks = [self._task('a', 0)]
        self.assertEqual(select_runnable(tasks, set(), set(), 0, 1), tasks)
        self.assertEqual(select_runnable(tasks, set(), set(), 1, 1), [])

    def test_blocked_task_does_not_stop_the_scan(self):
        """A task above its threshold is skipped, not treated as a barrier."""
        blocked = self._task('expensive', 10)
        allowed = self._task('cheap', 90)
        result = select_runnable([blocked, allowed], set(), set(), 50, 2)
        self.assertEqual([task.id for task in result], ['cheap'])

    def test_completed_ids_are_skipped(self):
        tasks = [self._task('a', 90), self._task('b', 90)]
        result = select_runnable(tasks, {'a'}, set(), 10, 5)
        self.assertEqual([task.id for task in result], ['b'])

    def test_running_ids_are_skipped(self):
        tasks = [self._task('a', 90), self._task('b', 90)]
        result = select_runnable(tasks, set(), {'a'}, 10, 5)
        self.assertEqual([task.id for task in result], ['b'])

    def test_slots_cap_the_result(self):
        tasks = [self._task('a', 90), self._task('b', 90), self._task('c', 90)]
        result = select_runnable(tasks, set(), set(), 10, 2)
        self.assertEqual([task.id for task in result], ['a', 'b'])

    def test_zero_slots_selects_nothing(self):
        tasks = [self._task('a', 90)]
        self.assertEqual(select_runnable(tasks, set(), set(), 10, 0), [])
        self.assertEqual(select_runnable(tasks, set(), set(), 10, -1), [])

    def test_file_order_is_preserved(self):
        tasks = [self._task('c', 90), self._task('a', 90), self._task('b', 90)]
        result = select_runnable(tasks, set(), set(), 10, 3)
        self.assertEqual([task.id for task in result], ['c', 'a', 'b'])

    def test_empty_queue_selects_nothing(self):
        self.assertEqual(select_runnable([], set(), set(), 0, 5), [])


class TestSanitizeTaskId(unittest.TestCase):
    """Tests for turning a task id into a safe file name component."""

    def test_plain_id_unchanged(self):
        self.assertEqual(sanitize_task_id('daily-tick_1.0'), 'daily-tick_1.0')

    def test_path_separators_replaced(self):
        self.assertEqual(sanitize_task_id('a/b\\c'), 'a_b_c')

    def test_leading_and_trailing_punctuation_stripped(self):
        self.assertEqual(sanitize_task_id('..daily..'), 'daily')

    def test_fully_unsafe_id_falls_back(self):
        self.assertEqual(sanitize_task_id('***'), 'task')


class TestPrintModeCaveat(unittest.TestCase):
    """The caveat is appended to every prompt and cannot be opted out of."""

    def test_caveat_mentions_orphaned_background_work(self):
        self.assertIn('orphaned', tasks_mod.PRINT_MODE_CAVEAT)
        self.assertTrue(tasks_mod.PRINT_MODE_CAVEAT.startswith(' '))


if __name__ == '__main__':
    unittest.main()
