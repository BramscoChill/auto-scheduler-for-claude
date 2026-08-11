"""
Task Registry Tests
====================

Unit tests for the registry writer: the stale-file guard, the validation
gate, atomic replacement, and the round-trip of the ``prompt`` /
``prompt_file`` distinction.  Every test runs against a temporary config dir.
"""
from __future__ import annotations

import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import auto_scheduler_for_claude.tasks as tasks_mod
from auto_scheduler_for_claude.task_registry import (
    ERROR_INVALID, ERROR_READ, ERROR_STALE, ERROR_WRITE, MISSING_STAMP,
    read_registry, registry_path, write_registry,
)


def _entry(task_id: str = 'daily', **overrides) -> dict:
    entry = {'id': task_id, 'prompt': 'Do the thing.', 'max_usage_percent': 40}
    entry.update(overrides)
    return entry


@contextmanager
def _config_dir(content=None):
    """Yield a temporary effective config dir, optionally holding a registry."""
    with TemporaryDirectory() as tmp:
        path = Path(tmp)
        if content is not None:
            text = content if isinstance(content, str) else json.dumps(content)
            (path / tasks_mod.TASKS_FILENAME).write_text(text, encoding='utf-8')
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tmp}), \
             patch.object(tasks_mod, 'ctypes', MagicMock()):
            yield path


def _read_file(config_dir: Path) -> list:
    return json.loads((config_dir / tasks_mod.TASKS_FILENAME).read_text(encoding='utf-8'))


class TestReadRegistry(unittest.TestCase):
    """Tests for read_registry."""

    def test_missing_file_is_empty_not_an_error(self):
        """A missing registry is how the first task ever gets created."""
        with _config_dir():
            entries, stamp, error = read_registry()

        self.assertEqual(entries, [])
        self.assertEqual(stamp, MISSING_STAMP)
        self.assertIsNone(error)

    def test_reads_entries_and_a_stamp(self):
        with _config_dir([_entry()]) as config_dir:
            entries, stamp, error = read_registry()

            self.assertEqual(entries, [_entry()])
            self.assertIsNone(error)
            self.assertNotEqual(stamp, MISSING_STAMP)
            self.assertEqual(stamp[1], (config_dir / tasks_mod.TASKS_FILENAME).stat().st_size)

    def test_empty_file_is_empty_registry(self):
        with _config_dir('   '):
            entries, _stamp, error = read_registry()

        self.assertEqual(entries, [])
        self.assertIsNone(error)

    def test_broken_json_reports_read_error(self):
        with _config_dir('[{'):
            entries, stamp, error = read_registry()

        self.assertEqual(error, ERROR_READ)
        self.assertEqual(entries, [])
        self.assertEqual(stamp, MISSING_STAMP)

    def test_object_instead_of_array_reports_read_error(self):
        with _config_dir({'id': 'daily'}):
            _entries, _stamp, error = read_registry()

        self.assertEqual(error, ERROR_READ)

    def test_array_of_non_objects_reports_read_error(self):
        """An editor must not offer to rewrite a file it cannot represent."""
        with _config_dir(['daily', 3]):
            _entries, _stamp, error = read_registry()

        self.assertEqual(error, ERROR_READ)


class TestWriteRegistry(unittest.TestCase):
    """Tests for write_registry."""

    def test_writes_and_returns_validated_tasks(self):
        with _config_dir([]) as config_dir:
            _entries, stamp, _error = read_registry()
            tasks, errors, error = write_registry([_entry()], stamp)

            self.assertIsNone(error)
            self.assertEqual(errors, [])
            self.assertEqual([task.id for task in tasks], ['daily'])
            self.assertEqual(_read_file(config_dir), [_entry()])

    def test_creates_the_file_when_none_exists(self):
        """Adding the very first task has no file to be stale against."""
        with _config_dir() as config_dir:
            tasks, _errors, error = write_registry([_entry()], MISSING_STAMP)

            self.assertIsNone(error)
            self.assertEqual(len(tasks), 1)
            self.assertTrue((config_dir / tasks_mod.TASKS_FILENAME).is_file())

    def test_stale_stamp_refuses_to_write(self):
        """An out-of-band edit must never be silently overwritten."""
        with _config_dir([_entry()]) as config_dir:
            _entries, stamp, _error = read_registry()
            hand_edited = [_entry(), _entry('other')]
            (config_dir / tasks_mod.TASKS_FILENAME).write_text(json.dumps(hand_edited), encoding='utf-8')

            tasks, errors, error = write_registry([_entry('replacement')], stamp)

            self.assertEqual(error, ERROR_STALE)
            self.assertIsNone(tasks)
            self.assertEqual(errors, [])
            self.assertEqual(_read_file(config_dir), hand_edited)

    def test_stamp_of_a_missing_file_is_stale_once_one_exists(self):
        with _config_dir([_entry()]):
            tasks, _errors, error = write_registry([_entry('other')], MISSING_STAMP)

        self.assertEqual(error, ERROR_STALE)
        self.assertIsNone(tasks)

    def test_invalid_content_is_never_written(self):
        """Writing a file the loader rejects would disable the queue on restart."""
        with _config_dir([_entry()]) as config_dir:
            _entries, stamp, _error = read_registry()

            tasks, errors, error = write_registry([_entry(max_usage_percent=140)], stamp)

            self.assertEqual(error, ERROR_INVALID)
            self.assertIsNone(tasks)
            self.assertEqual([e.field for e in errors], ['max_usage_percent'])
            self.assertEqual(_read_file(config_dir), [_entry()])

    def test_duplicate_ids_are_rejected(self):
        with _config_dir([]) as config_dir:
            _entries, stamp, _error = read_registry()

            _tasks, errors, error = write_registry([_entry(), _entry()], stamp)

            self.assertEqual(error, ERROR_INVALID)
            self.assertTrue(any('duplicate' in e.message for e in errors))
            self.assertEqual(_read_file(config_dir), [])

    def test_file_order_is_preserved(self):
        """File order is queue priority, so it must survive a rewrite."""
        with _config_dir([]) as config_dir:
            _entries, stamp, _error = read_registry()
            write_registry([_entry('a'), _entry('b'), _entry('c')], stamp)

            self.assertEqual([e['id'] for e in _read_file(config_dir)], ['a', 'b', 'c'])

    def test_prompt_file_entry_round_trips(self):
        with _config_dir([]) as config_dir:
            (config_dir / 'prompt.md').write_text('Read the plan.', encoding='utf-8')
            _entries, stamp, _error = read_registry()

            entry = {'id': 'daily', 'prompt_file': 'prompt.md', 'max_usage_percent': 40}
            tasks, _errors, error = write_registry([entry], stamp)

            self.assertIsNone(error)
            self.assertEqual(tasks[0].prompt, 'Read the plan.')
            self.assertEqual(_read_file(config_dir), [entry])

    def test_non_ascii_is_written_unescaped(self):
        with _config_dir([]) as config_dir:
            _entries, stamp, _error = read_registry()
            write_registry([_entry(description='Tägliche Aufgabe')], stamp)

            text = (config_dir / tasks_mod.TASKS_FILENAME).read_text(encoding='utf-8')
            self.assertIn('Tägliche Aufgabe', text)

    def test_no_temp_file_is_left_behind(self):
        with _config_dir([]) as config_dir:
            _entries, stamp, _error = read_registry()
            write_registry([_entry()], stamp)

            self.assertEqual([p.name for p in config_dir.glob('*.tmp')], [])

    def test_unwritable_target_reports_write_error(self):
        with _config_dir([]):
            _entries, stamp, _error = read_registry()
            with patch('auto_scheduler_for_claude.task_registry.os.replace', side_effect=OSError('locked')):
                tasks, errors, error = write_registry([_entry()], stamp)

        self.assertEqual(error, ERROR_WRITE)
        self.assertIsNone(tasks)
        self.assertEqual(errors, [])


class TestRegistryPath(unittest.TestCase):
    """Tests for registry_path."""

    def test_lives_in_the_effective_config_dir(self):
        with _config_dir() as config_dir:
            self.assertEqual(registry_path(), config_dir / tasks_mod.TASKS_FILENAME)


if __name__ == '__main__':
    unittest.main()
