# Implementation progress - rename + task queue

Spec: `docs/superpowers/specs/2026-08-10-rename-and-task-queue-design.md`
Started: 2026-08-10

Update this file after every completed step. If the session is cut short, resume from the
first unchecked box.

## Phase 1 - Rename (mechanical)

- [x] 1.1 `git mv` package dir, spec file, icon
- [x] 1.2 Global string replacement pass (10 ordered rules, see below)
- [x] 1.3 Verify no stale identifiers remain (`git grep`)
- [x] 1.4 Full test suite green (1070 tests, unchanged count)
- [x] 1.5 `build.py` sanity - `dist/AutoSchedulerForClaude.exe` built (15.0 MB)

Replacement rules, in order:
1. `usage_monitor_for_claude` -> `auto_scheduler_for_claude`
2. `usage-monitor-for-claude` -> `auto-scheduler-for-claude`
3. `Usage Monitor for Claude` -> `Auto Scheduler for Claude`
4. `usage-monitor-settings` -> `auto-scheduler-settings`
5. `USAGE_MONITOR_` -> `AUTO_SCHEDULER_`
6. `JensDuttke.UsageMonitorForClaude` -> `BramscoChill.AutoSchedulerForClaude`
7. `UsageMonitorForClaude` -> `AutoSchedulerForClaude`
8. `jens-duttke/auto-scheduler-for-claude` -> `BramscoChill/auto-scheduler-for-claude`
9. `Usage Monitor` -> `Auto Scheduler` (locale tooltip prefix + its tests)
10. `usage_monitor` -> `auto_scheduler` (pystray icon name, single occurrence)

Deliberately NOT renamed: `jens-duttke/agent-monitor-for-claude` (companion tool),
`.github/FUNDING.yml`, `notification_logo.ico`, `cli_command`.

## Phase 2 - Queue core (no app wiring)

- [x] 2.1 `auto_scheduler_for_claude/tasks.py` (Task, load_tasks, select_runnable, PRINT_MODE_CAVEAT)
- [x] 2.2 `tests/test_tasks.py` (45 tests)
- [x] 2.3 `quota_snapshot_env()` moved from `app.py` to `formatting.py`
- [x] 2.4 `auto_scheduler_for_claude/task_runner.py` (+ `max_concurrent_tasks` / `task_command`
      settings, pulled forward from phase 4 because the runner imports them)
- [x] 2.5 `tests/test_task_runner.py` (35 tests)
- [x] 2.6 Full suite green (1150 tests)

## Phase 3 - Integration

- [x] 3.1 `update()` hook calls `task_runner.evaluate()`
- [x] 3.2 `poll_loop` idle bypass while work pending
- [x] 3.3 Tray "Task queue" submenu
- [x] 3.4 Quit dialog (Yes/No/Cancel), drain, tooltip marker
- [x] 3.5 `tests/test_app.py` additions (23 new tests; `_make_app` now injects a mocked
      `TaskRunner` so the suite never touches the real config dir)
- [x] 3.6 Full suite green (1173 tests)
- [x] 3.7 13 locale files got the 9 new keys (pulled forward from 4.2)

## Phase 4 - Surface

- [x] 4.1 Settings: `max_concurrent_tasks`, `task_command` (+ tests, + `quota_snapshot_env` tests)
- [x] 4.2 13 locale files: 9 new keys (`test_i18n` parity passes)
- [x] 4.3 `docs/task-queue.md`
- [x] 4.4 `README.md` task-queue section (all 8 subsections) + repositioning + feature bullet
      + Security & Transparency rewrite
- [x] 4.5 `PRIVACY.md`, `.claude/CLAUDE.md`, `docs/configuration.md`, `docs/event-commands.md`
- [x] 4.6 `CHANGELOG.md` (Added: queue + quit dialog; Changed: breaking rename with the four
      manual migration steps, idle-pause behaviour)
- [x] 4.7 `docs/AUTO_SCHEDULER_FOR_CLAUDE_TECHNICAL_REFERENCE.md`
- [x] 4.8 Spec `datas` / `build.py` - no new data files; `tasks.py` and `task_runner.py` are
      plain modules and appear in the frozen dependency graph (checked `build/*/xref-*.html`)
- [x] 4.9 Full suite green (1193 tests) + `build.py` succeeds + end-to-end smoke run

## DONE - all four phases complete

Verification performed:
- `.\.venv\Scripts\python.exe -m unittest discover -s tests` -> 1193 tests, OK
- `.\.venv\Scripts\python.exe build.py` -> `dist/AutoSchedulerForClaude.exe`, 15.0 MB
- End-to-end smoke run with a real `TaskRunner` and a stub binary: task launched, log header
  and footer written, state file written, tray label moved waiting -> done (exit 0), and a
  second task correctly stayed "waiting" because its threshold was not met
- `git grep -i "usage.?monitor"` -> only the intentional migration steps in `CHANGELOG.md`
- All relative markdown links across `*.md` and `docs/*.md` resolve

## Notes / decisions taken during implementation

- No virtual environment existed; created `.venv` (already covered by `.gitignore`) and
  installed `requirements.txt`. All commands below run as `.\.venv\Scripts\python.exe ...`.
- Rule 9 (`Usage Monitor` -> `Auto Scheduler`) also rewrote the short tooltip prefix in all 13
  locale files (`"loading"`, `"error_label"`) and the matching assertions in
  `tests/test_formatting.py`. Intentional: it is the display name in shortened form.
- `version_info.py` `CompanyName` stays `Jens Duttke` and `.github/FUNDING.yml` stays
  `jens-duttke` - the spec's identifier map does not list them.
- Pre-existing house style: module docstring `====` underlines are one or two characters longer
  than the title. Left untouched.
- `Task.timeout_minutes` is a `float`, not the spec's `int`: JSON numbers may be fractional and
  the value is only ever multiplied into `proc.wait(timeout=...)`.
- `tasks.py` also rejects **unknown fields** in an entry (not in the spec). A `timeout_minutes`
  typo would otherwise silently fall back to 180 on an unattended agent.
- `TaskRunner.menu_entries()` returns `(task id, STATUS_* key)`, not a localized label - the
  runner stays free of UI text and `app.py` maps the key to a locale string.
- `TaskRunner.__init__` takes an `on_drained` callback (the app passes `icon.stop`) so the
  runner never imports the app. Extra public helpers beyond the spec's list: `tasks`,
  `has_tasks()`, `is_draining()`, `exit_code()`, `log_path()`.
- `waiting` vs `pending` needs the current utilization, so the runner remembers the last
  `five_hour` percentage it evaluated (`_last_five_hour_pct`).
- Tray item text is `<glyph> <description or id> - <localized status>`. The spec's literal
  format had no room for the five status locale keys it also mandates, so `description` takes
  the label slot (it is documented as "tray-menu label only") and the status keys carry the
  state. `done` shows its exit code via a `{code}` placeholder.
- The rename pass also rewrote historical `CHANGELOG.md` entries (old release notes now say
  "Auto Scheduler" and `auto-scheduler-settings.json`). The spec listed `CHANGELOG.md` among
  the renamed files, so this is intentional, but it is worth knowing that released-version
  entries no longer quote the names those versions actually shipped with.
- Pre-existing and untouched: `README.md` still advertises "single EXE (~12.5 MB)" while the
  build produces 15.0 MB. Unrelated to this change - the size was already off before it.
