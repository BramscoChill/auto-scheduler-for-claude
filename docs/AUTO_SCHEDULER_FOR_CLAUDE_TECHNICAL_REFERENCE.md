# Auto Scheduler for Claude - Technical Reference

Internal architecture reference. User-facing documentation lives in `README.md`,
`docs/configuration.md`, `docs/event-commands.md`, and `docs/task-queue.md`; project rules for
contributors live in `.claude/CLAUDE.md`.

Last updated: 2026-08-10 (rename from Usage Monitor for Claude + task queue).

---

## Overview

A Windows-only system tray application. It polls the Anthropic usage API with reset-aligned
adaptive cadence, renders the result as a tray icon, tooltip, and detail popup, fires
user-configured event commands, and - new in this change - runs queued `claude -p` tasks
unattended when the five-hour quota leaves enough headroom.

Entry point: `auto_scheduler_for_claude/__main__.py`. Distribution: a single PyInstaller EXE,
`AutoSchedulerForClaude.exe`, built from `auto_scheduler_for_claude.spec` via `build.py`.

---

## Module map

| Module | Responsibility | Side effects |
|---|---|---|
| `__main__.py` | Startup: single-instance guard, notification identity, config dir, app run | process-level |
| `app.py` | `AutoSchedulerForClaude` - tray icon, menu, poll loop, update orchestration, notifications | tray, subprocess (via `command`), dialogs |
| `api.py` | The only module that touches credentials; HTTP to `api.anthropic.com` | network |
| `cache.py` | `UsageCache` - fetch cooldown, 429 backoff, token refresh, profile | network (via `api`) |
| `claude_cli.py` | CLI discovery, version probing, `claude update` token refresh | subprocess |
| `command.py` | User-configured event commands | subprocess |
| `tasks.py` | **New.** Task registry loading, validation, pure selection logic | dialog on invalid input only |
| `task_runner.py` | **New.** `TaskRunner` - task processes, state file, logs, concurrency, drain/kill | subprocess, file writes, dialogs |
| `formatting.py` | Pure formatting helpers, quota snapshot env | none |
| `settings.py` | `auto-scheduler-settings.json` loading and validation | dialog on invalid input only |
| `i18n.py` | Locale loading (`locale/*.json`) | none |
| `instance_id.py` | Effective config dir, per-instance name suffix | none |
| `popup.py` | pywebview detail popup | window |
| `tray_icon.py` | Icon image rendering, taskbar theme detection | none |
| `autostart.py` | `HKCU\...\Run` entry | registry |
| `notification_identity.py` | `HKCU\Software\Classes\AppUserModelId\...` + AUMID | registry |
| `idle.py`, `verbose.py`, `single_instance.py` | Idle/lock detection, diagnostics, mutex guard | Win32 |

---

## Rename (this change)

The project was renamed from `usage-monitor-for-claude` / `usage_monitor_for_claude` to
`auto-scheduler-for-claude` / `auto_scheduler_for_claude` as a clean break, with no
compatibility shims or dual-name reads.

| Kind | From | To |
|---|---|---|
| Package directory | `usage_monitor_for_claude/` | `auto_scheduler_for_claude/` |
| PyInstaller spec | `usage_monitor_for_claude.spec` | `auto_scheduler_for_claude.spec` |
| App icon | `usage_monitor_for_claude.ico` | `auto_scheduler_for_claude.ico` |
| App class | `UsageMonitorForClaude` | `AutoSchedulerForClaude` |
| Display name | `Usage Monitor for Claude` | `Auto Scheduler for Claude` |
| Short tooltip prefix | `Usage Monitor:` | `Auto Scheduler:` |
| Settings file | `usage-monitor-settings.json` | `auto-scheduler-settings.json` |
| Event env prefix | `USAGE_MONITOR_*` | `AUTO_SCHEDULER_*` |
| Autostart reg value | `UsageMonitorForClaude` | `AutoSchedulerForClaude` |
| Mutex base name | `UsageMonitorForClaude_SingleInstance` | `AutoSchedulerForClaude_SingleInstance` |
| PID mapping name | `UsageMonitorForClaude_HolderPID` | `AutoSchedulerForClaude_HolderPID` |
| Notification AUMID | `JensDuttke.UsageMonitorForClaude` | `BramscoChill.AutoSchedulerForClaude` |
| pystray icon name | `usage_monitor` | `auto_scheduler` |
| `PROJECT_URL` | `github.com/jens-duttke/usage-monitor-for-claude` | `github.com/BramscoChill/auto-scheduler-for-claude` |

Deliberately unchanged: `notification_logo.ico` (already generic), the `cli_command` setting
name and its display-only role, `.github/FUNDING.yml`, `version_info.py`'s `CompanyName`, and
every reference to the separate companion tool `jens-duttke/agent-monitor-for-claude`.

Two `HKCU` entries created by the old build are orphaned by the rename and are **not** cleaned
up in code (that would mean writing to registry paths the new app does not own): the old
autostart value and the old AppUserModelId. Both are documented in `CHANGELOG.md` as manual
post-upgrade steps.

---

## Task queue (this change)

### Data files

All three live in the effective config dir (`instance_id.effective_config_dir()`:
`CLAUDE_CONFIG_DIR` when set, else `~/.claude`), which gives per-account isolation for
multi-instance setups for free.

| Path | Constant | Direction |
|---|---|---|
| `auto-scheduler-tasks.json` | `tasks.TASKS_FILENAME` | read only |
| `auto-scheduler-state.json` | `task_runner.STATE_FILENAME` | written atomically |
| `auto-scheduler-logs/` | `task_runner.LOGS_DIRNAME` | written |

These are the **only** files the application writes. Without a task file it writes none.

### `tasks.py` (pure)

```python
@dataclass(frozen=True)
class Task:
    id: str
    prompt: str                  # resolved text: inline, or the contents of prompt_file
    max_usage_percent: float
    timeout_minutes: float       # 0 means no limit
    cwd: str | None
    description: str
    prompt_file: str | None = None   # resolved source path, for the log header


def load_tasks() -> list[Task]
def select_runnable(tasks, completed_ids, running_ids, five_hour_pct, slots) -> list[Task]
def sanitize_task_id(task_id: str) -> str

# private
def _resolve_prompt(entry, label, working_dir) -> tuple[str | None, str | None, list[str]]
def _resolve_prompt_path(raw_path: str, working_dir: str | None) -> Path
```

Constants: `TASKS_FILENAME`, `DEFAULT_TIMEOUT_MINUTES` (`180.0`), `PRINT_MODE_CAVEAT` (copied
verbatim from `docs/execute-claude-tasks.md` and appended to every prompt, no opt-out).

Validation collects every error and reports them in one `MessageBoxW`. Unlike `settings.py`,
which drops individual bad keys, a single invalid entry disables the **whole** queue for the
session. Unknown field names are rejected so a `timeout_minutes` typo cannot silently fall back
to the default on an unattended agent. A missing file is not an error - the feature is simply
off.

#### Prompt resolution

An entry supplies either `prompt` or `prompt_file`. `_resolve_prompt()` runs during validation,
so `Task.prompt` always holds the final text and `task_runner.py` never touches the file system
for a prompt:

- `prompt_file` present -> it wins unconditionally and the inline `prompt` is **ignored, not
  validated** (a leftover draft must never run instead of the file the user pointed at). An
  explicit `null` counts as absent and falls back to `prompt`.
- The file is read once at load with `utf-8-sig` and `.strip()`ed. Missing, unreadable, or
  whitespace-only is a validation error - an unattended agent must never start on an empty
  prompt.
- `_resolve_prompt_path()` expands `~`, uses an absolute path as-is, and resolves a relative one
  against the task's `cwd`, falling back to `effective_config_dir()`. This is why `cwd` is
  validated **before** the prompt inside `_validate()`.
- The resolved path is kept in `Task.prompt_file` purely so `task_runner._start()` can write a
  `prompt:  <path>` line into the log header (omitted for inline prompts).

`select_runnable` walks the list in file order and `continue`s past a task whose threshold is
not met (never `break`s), so file order plus per-task thresholds act as priority.

### `task_runner.py` (side effects)

```python
class TaskRunner:
    def __init__(self, on_drained: Callable[[], None] | None = None) -> None

    # queue state
    tasks -> list[Task]                 # property
    def has_tasks(self) -> bool
    def has_pending_work(self) -> bool
    def running_count(self) -> int
    def menu_entries(self) -> list[tuple[str, str]]     # (task id, STATUS_* key)
    def status(self, task_id: str) -> str
    def exit_code(self, task_id: str) -> int | None
    def log_path(self, task_id: str) -> Path | None

    # evaluation
    def evaluate(self, data: dict[str, Any]) -> None

    # shutdown
    def begin_drain(self) -> None
    def is_draining(self) -> bool
    def kill_all(self) -> None
```

Constants: `STATE_FILENAME`, `LOGS_DIRNAME`, `STATE_VERSION` (`1`), `EXIT_TIMEOUT` (`124`),
`EXIT_KILLED` (`130`), and the five status keys `STATUS_WAITING`, `STATUS_PENDING`,
`STATUS_RUNNING`, `STATUS_DONE`, `STATUS_INTERRUPTED`.

A class rather than free functions, for the same reason `UsageCache` is one: running processes,
completed ids, the drain flag, and the last-seen utilization are mutated from the poll thread,
the popup refresh thread, the tray thread, and one watcher thread per task. All of it sits
behind a single `threading.Lock`, and `evaluate()` holds that lock across the entire
select-and-start sequence.

The runner never imports `i18n` or `app`: it returns `STATUS_*` keys and calls back through
`on_drained`.

### Selection

Called at the end of `AutoSchedulerForClaude.update()`, after a successful fetch with no
`error` key - so on every poll and on every popup-triggered background refresh.

```
five_hour_pct = (data.get('five_hour') or {}).get('utilization')
if five_hour_pct is None:                 return    # queue never fires without a 5h quota
if disabled or draining or no tasks:      return
slots = MAX_CONCURRENT_TASKS - len(running)
if slots <= 0:                            return
for task in select_runnable(...):         start(task)
```

The `or {}` idiom matters: `five_hour` can be present and `null`.

### Executing a task

```
[*task_binary, '-p', task.prompt + PRINT_MODE_CAVEAT, '--dangerously-skip-permissions']
```

- `task_binary` is `settings.TASK_COMMAND` when set, else `[str(claude_cli.CLAUDE_CLI_PATH)]`.
- `cwd` is `task.cwd`, else `None` (the app's working directory).
- Environment: current environment + `formatting.quota_snapshot_env(data)` +
  `AUTO_SCHEDULER_EVENT=task`, `AUTO_SCHEDULER_TASK_ID`, `AUTO_SCHEDULER_VERSION`.
- `creationflags=subprocess.CREATE_NO_WINDOW`; stdout and stderr both go to the log handle.
- One daemon watcher thread per task calls `proc.wait(timeout=minutes*60)`, or `proc.wait()`
  with no timeout when `timeout_minutes == 0`. `TimeoutExpired` kills the child and records
  `124`.
- Any failure that would lose a run's output (log directory, log file) prevents the start,
  shows one error, and disables the queue for the session.

`_finalize()` is idempotent (guarded by `_Run.finalized`), so a watcher and `kill_all()`
racing on the same run write exactly one record.

### State semantics

Any id present in `tasks` is complete - one-shot means once, ever. A record with `started_at`
and no `finished_at` found at startup is an interrupted run (the app died mid-task); it counts
as complete, renders as `interrupted`, and is never restarted. A state file that exists but
cannot be parsed disables the queue rather than being treated as empty, which would re-run
everything.

---

## App integration (this change)

| Location | Change |
|---|---|
| `AutoSchedulerForClaude.__init__` | Creates `self.task_runner = TaskRunner(on_drained=self._on_tasks_drained)` |
| Tray menu | New `T['task_queue']` submenu above "Test event commands", `visible=has_tasks()` |
| `update()` | Calls `self.task_runner.evaluate(result.data)` as its last step |
| `_render_tray()` | Appends `T['tooltip_draining']` to the tooltip while draining |
| `poll_loop()` | Away branch is now `if self._is_user_away() and not self.task_runner.has_pending_work():` - only the pause is skipped |
| `on_quit()` | Three-way `MB_YESNOCANCEL` dialog when tasks are running |

New module-level names in `app.py`: `MB_YESNOCANCEL`, `MB_ICONQUESTION`, `IDCANCEL`, `IDYES`,
`IDNO`, `TASK_STATUS_GLYPHS`, `TASK_FAILED_GLYPH`.

New methods: `_on_tasks_drained()`, `_build_task_menu()`, `_task_item_text()`,
`_task_status_label()`, `_task_item_enabled()`, `_task_item_action()`.

Menu items are static (the registry is read once at startup) with callable `text` and `enabled`,
so status re-renders every time the menu opens. Item text is
`<glyph> <description or id> - <localized status>`; a finished run's label carries its exit
code, and only a run with a log on disk is clickable (`os.startfile`).

`on_quit()` outcomes: **Yes** -> `kill_all()` (exit `130`) then stop; **No** -> `begin_drain()`
and return, with the last watcher thread calling `_on_tasks_drained()`; **Cancel** -> return,
clearing `restart_requested` so a cancelled restart does not relaunch.

### Moved function

`AutoSchedulerForClaude._quota_snapshot_env()` moved to `formatting.quota_snapshot_env()` as a
public pure function, so `app.py` and `task_runner.py` share one implementation instead of the
runner reaching back into the app. Behavior is unchanged; the app's startup and double-click
command paths now call the module-level function.

---

## Settings (this change)

| Key | Type | Default | Validation |
|---|---|---|---|
| `max_concurrent_tasks` | int | `1` | `>= 1`, via `_NUMERIC_BOUNDS` |
| `task_command` | array of strings | `[]` | Array of non-empty strings; empty means auto-detect. A shell string is rejected, not split - it is an argv list |

Exported as `settings.MAX_CONCURRENT_TASKS` and `settings.TASK_COMMAND`.

`task_command` exists so a WSL-hosted CLI can run tasks without giving `cli_command` a second
job. `cli_command` remains display-only and must never take part in authentication or token
refresh.

---

## Locale (this change)

Nine template keys added to all 13 `locale/*.json` files:

`task_queue`, `task_state_waiting`, `task_state_pending`, `task_state_running`,
`task_state_done` (`{code}` placeholder), `task_state_interrupted`,
`quit_tasks_running_title`, `quit_tasks_running` (`{count}` placeholder), `tooltip_draining`.

All are templates - there are no per-task keys. `tests/test_i18n.py` enforces key parity across
the 13 files.

The rename also rewrote the short tooltip prefix in every locale file: `"Usage Monitor: ..."`
became `"Auto Scheduler: ..."` for the `loading` and `error_label` keys.

---

## Tests

| File | Coverage |
|---|---|
| `tests/test_tasks.py` | Registry loading, every validation rule, prompt/`prompt_file` precedence and path resolution, `select_runnable` (equality, non-blocking skip, slots, order), id sanitizing |
| `tests/test_task_runner.py` | Start/state/log writing, exit codes, timeout kill (`124`), no-timeout path, `kill_all` (`130`), drain callback, interrupted and corrupt state, unwritable log dir, concurrency limits, concurrent `evaluate` from four threads |
| `tests/test_app.py` | `evaluate` delegation (and non-delegation on errors), idle bypass, the three quit outcomes, draining tooltip, task menu rendering |
| `tests/test_settings.py` | `max_concurrent_tasks` and `task_command` validation and defaults |
| `tests/test_formatting.py` | `quota_snapshot_env` |

Every subprocess in the runner tests is faked and the config dir is redirected to a temporary
directory, so no task ever really runs. `_make_app()` in `tests/test_app.py` injects a mocked
`TaskRunner` with explicit falsy defaults - a bare `MagicMock` returns truthy values, which
would silently enable the draining tooltip, the idle bypass, and the quit dialog across the
whole suite.

Run: `python -m unittest discover -s tests` (1193 tests).

---

## Known limitations

- Only the `five_hour` quota gates execution. A task can start at 5% five-hour usage while the
  seven-day quota sits at 99%. A `max_weekly_usage_percent` field is a natural follow-up, out of
  scope by explicit decision.
- Started tasks are not throttled against each other. With `max_concurrent_tasks > 1`, several
  tasks can launch from one quota snapshot before any of their consumption is visible.
- No retry. A task that fails for a transient reason is done and needs a new `id` to run again.
- The tray submenu reflects the task file as read at startup; editing it requires a restart,
  matching how settings already behave.
- Killing a task on quit terminates only the direct `claude` child. Grandchildren it spawned are
  not walked and may survive.
- `MessageBoxW` error reporting inside `TaskRunner._disable()` can be reached while the runner
  lock is held (state-write failure during finalization), which blocks other queue operations
  until the dialog is dismissed. It is an error path that also ends the queue for the session.
