# Auto Scheduler for Claude - Technical Reference

Internal architecture reference. User-facing documentation lives in `README.md`,
`docs/configuration.md`, `docs/event-commands.md`, and `docs/task-queue.md`; project rules for
contributors live in `.claude/CLAUDE.md`.

Last updated: 2026-08-11 (task queue in the popup: expanded view, task detail, task editor).

---

## Overview

A Windows-only system tray application. It polls the Anthropic usage API with reset-aligned
adaptive cadence, renders the result as a tray icon, tooltip, and detail popup, fires
user-configured event commands, and runs queued `claude -p` tasks unattended when the five-hour
quota leaves enough headroom. The queue is managed from the popup's expanded view.

Entry point: `auto_scheduler_for_claude/__main__.py`. Distribution: a single PyInstaller EXE,
`AutoSchedulerForClaude.exe`, built from `auto_scheduler_for_claude.spec` via `build.py`.

---

## Module map

| Module | Responsibility | Side effects |
|---|---|---|
| `__main__.py` | Startup: single-instance guard, notification identity, config dir, app run | process-level |
| `app.py` | `AutoSchedulerForClaude` - tray icon, menu, poll loop, update orchestration, notifications, the popup's queue payload and registry-editing API | tray, subprocess (via `command`), registry writes (via `task_registry`), dialogs |
| `api.py` | The only module that touches credentials; HTTP to `api.anthropic.com` | network |
| `cache.py` | `UsageCache` - fetch cooldown, 429 backoff, token refresh, profile | network (via `api`) |
| `claude_cli.py` | CLI discovery, version probing, `claude update` token refresh | subprocess |
| `command.py` | User-configured event commands | subprocess |
| `tasks.py` | Task registry loading, validation, pure selection logic | dialog on invalid input only |
| `task_registry.py` | **New.** The only writer of `auto-scheduler-tasks.json` | one file write, user-initiated only |
| `task_runner.py` | `TaskRunner` - task processes, state file, logs, concurrency, drain/kill/stop, log tailing | subprocess, file writes, dialogs |
| `formatting.py` | Pure formatting helpers, quota snapshot env | none |
| `settings.py` | `auto-scheduler-settings.json` loading and validation | dialog on invalid input only |
| `i18n.py` | Locale loading (`locale/*.json`) | none |
| `instance_id.py` | Effective config dir, per-instance name suffix | none |
| `popup.py` | pywebview detail popup: window host, geometry, DPI, JS bridge | window, native dialogs |
| `popup/popup.html`, `popup.css`, `popup.js` | The popup's markup, theme-token stylesheet, and view logic | none |
| `popup/dev.html` | Browser-only preview harness for the popup (not shipped in the EXE) | none |
| `tray_icon.py` | Icon image rendering, taskbar theme detection | none |
| `autostart.py` | `HKCU\...\Run` entry | registry |
| `notification_identity.py` | `HKCU\Software\Classes\AppUserModelId\...` + AUMID | registry |
| `idle.py`, `verbose.py`, `single_instance.py` | Idle/lock detection, diagnostics, mutex guard | Win32 |

---

## Task queue

### Data files

All live in the effective config dir (`instance_id.effective_config_dir()`: `CLAUDE_CONFIG_DIR`
when set, else `~/.claude`), which gives per-account isolation for multi-instance setups for free.

| Path | Constant | Direction |
|---|---|---|
| `auto-scheduler-tasks.json` | `tasks.TASKS_FILENAME` | read at startup; written **only** by the popup's task editor |
| `auto-scheduler-state.json` | `task_runner.STATE_FILENAME` | written atomically |
| `auto-scheduler-logs/` | `task_runner.LOGS_DIRNAME` | written |
| `auto-scheduler-logs/<id>-<timestamp>.log` | - | written, one per run |

These are the **only** files the application writes. Until a task exists it writes none. The
enumeration is repeated in `README.md`, `PRIVACY.md`, and `.claude/CLAUDE.md`; a fifth write path
must update all three in the same change.

### The three-way split

| Module | Owns | Never does |
|---|---|---|
| `tasks.py` | Reading and validating the registry, computing the selection | Subprocesses, writes |
| `task_registry.py` | Writing the registry file | Subprocesses, background writes |
| `task_runner.py` | Processes, state file, logs, concurrency, shutdown | Registry writes, UI text |

`task_registry.py` is separate from `task_runner.py` rather than folded into it because the two
have opposite trust properties: the runner writes from background threads as tasks run, while the
registry is a user-owned file that must only ever change in response to a click.

### `tasks.py` (pure)

```python
@dataclass(frozen=True)
class ValidationError:
    index: int                   # position in the registry array
    task_id: str | None
    field: str | None            # 'id', 'cwd', 'prompt', ... - None for a whole-entry problem
    message: str                 # the line shown in the startup message box


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
def validate_entries(entries: list) -> tuple[list[Task], list[ValidationError]]
def select_runnable(tasks, completed_ids, running_ids, five_hour_pct, slots) -> list[Task]
def sanitize_task_id(task_id: str) -> str

# private
def _resolve_prompt(entry, working_dir) -> tuple[str | None, str | None, str | None, str | None]
def _resolve_prompt_path(raw_path: str, working_dir: str | None) -> Path
```

Constants: `TASKS_FILENAME`, `DEFAULT_TIMEOUT_MINUTES` (`180.0`), `PRINT_MODE_CAVEAT` (copied
verbatim from `docs/execute-claude-tasks.md` and appended to every prompt, no opt-out).

`validate_entries()` is public because it is the **single** validator, shared by `load_tasks()`
and by the popup's task editor through `task_registry.write_registry()`. The editor must never
grow a parallel check in JavaScript: an entry the editor accepts has to be one the next startup
accepts. It is pure and silent - it reads `prompt_file` from disk but writes nothing and shows no
dialog; the `MessageBoxW` lives in `load_tasks()`.

Errors are structured rather than plain strings so the editor can point at the offending field.
`load_tasks()` joins `error.message` for its message box, which is the same text as before.

Validation collects every error and reports them together. Unlike `settings.py`, which drops
individual bad keys, a single invalid entry disables the **whole** queue for the session. Unknown
field names are rejected so a `timeout_minutes` typo cannot silently fall back to the default on
an unattended agent. A missing file is not an error - the feature is simply off.

#### Prompt resolution

An entry supplies either `prompt` or `prompt_file`. `_resolve_prompt()` runs during validation, so
`Task.prompt` always holds the final text and `task_runner.py` never touches the file system for a
prompt:

- `prompt_file` present -> it wins unconditionally and the inline `prompt` is **ignored, not
  validated** (a leftover draft must never run instead of the file the user pointed at). An
  explicit `null` counts as absent and falls back to `prompt`.
- The file is read once at load with `utf-8-sig` and `.strip()`ed. Missing, unreadable, or
  whitespace-only is a validation error - an unattended agent must never start on an empty prompt.
- `_resolve_prompt_path()` expands `~`, uses an absolute path as-is, and resolves a relative one
  against the task's `cwd`, falling back to `effective_config_dir()`. This is why `cwd` is
  validated **before** the prompt inside `validate_entries()`.
- The resolved path is kept in `Task.prompt_file` purely so `task_runner._start()` can write a
  `prompt:  <path>` line into the log header (omitted for inline prompts).

`select_runnable` walks the list in file order and `continue`s past a task whose threshold is not
met (never `break`s), so file order plus per-task thresholds act as priority.

### `task_registry.py` (the registry writer)

```python
def registry_path() -> Path
def read_registry() -> tuple[list[dict], tuple[float, int], str | None]
def write_registry(entries, expected_stamp) -> tuple[list[Task] | None, list[ValidationError], str | None]
```

Constants: `ERROR_READ`, `ERROR_STALE`, `ERROR_INVALID`, `ERROR_WRITE` (locale **keys**, mapped to
text by `app.py` - this module holds no UI strings), and `MISSING_STAMP` (`(0.0, -1)`).

- `read_registry()` returns the **raw** entries, not `Task` objects: the editor edits the file's
  own shape, including the `prompt` / `prompt_file` distinction that `Task` has already collapsed.
  A missing file reads as an empty registry with `MISSING_STAMP`, which is what lets the editor
  create the first task.
- The stamp is `(st_mtime, st_size)`. `write_registry()` refuses outright when it no longer
  matches, and never merges: the file is the user's, and a hand edit made in the meantime must not
  be silently overwritten.
- The prospective content is validated with `tasks.validate_entries()` before anything is written,
  so the app can never produce a file its own loader would reject.
- The write is atomic (temp file in the same directory, then `os.replace`), with
  `json.dumps(indent=2, ensure_ascii=False)` and a trailing newline. Hand formatting is normalized.
- On success it returns the validated tasks, so the caller can hand them straight to
  `TaskRunner.reload()` without a second read and a second validation pass.

### `task_runner.py` (side effects)

```python
class TaskRunner:
    def __init__(self, on_drained: Callable[[], None] | None = None) -> None

    # queue state
    tasks -> list[Task]                 # property
    version -> int                      # property, bumped on every queue change
    def has_tasks(self) -> bool
    def has_pending_work(self) -> bool
    def running_count(self) -> int
    def menu_entries(self) -> list[tuple[str, str]]     # (task id, STATUS_* key)
    def status(self, task_id: str) -> str
    def completed_ids(self) -> list[str]
    def exit_code(self, task_id: str) -> int | None
    def log_path(self, task_id: str) -> Path | None
    def snapshot(self) -> dict[str, Any]
    def log_tail(self, task_id: str, offset: int = 0) -> dict[str, Any] | None

    # evaluation
    def evaluate(self, data: dict[str, Any]) -> None

    # mutation
    def reload(self, tasks: list[Task]) -> None
    def stop(self, task_id: str) -> bool

    # shutdown
    def begin_drain(self) -> None
    def is_draining(self) -> bool
    def kill_all(self) -> None
```

Constants: `STATE_FILENAME`, `LOGS_DIRNAME`, `STATE_VERSION` (`1`), `LOG_TAIL_BYTES` (`65536`),
`EXIT_TIMEOUT` (`124`), `EXIT_KILLED` (`130`), `EXIT_STOPPED` (`143`), and the five status keys
`STATUS_WAITING`, `STATUS_PENDING`, `STATUS_RUNNING`, `STATUS_DONE`, `STATUS_INTERRUPTED`.

A class rather than free functions, for the same reason `UsageCache` is one: running processes,
completed ids, the drain flag, and the last-seen utilization are mutated from the poll thread, the
popup refresh thread, the tray thread, and one watcher thread per task. All of it sits behind a
single `threading.Lock`, and `evaluate()` holds that lock across the entire select-and-start
sequence.

The runner never imports `i18n` or `app`: it returns `STATUS_*` keys and calls back through
`on_drained`.

#### `version`

A plain counter bumped inside the lock by `_start`, `_finalize`, `stop`, `reload`, `_disable`, and
by `evaluate()` whenever the five-hour reading changes (waiting/pending is derived from it, so
rows move between those states with nothing else happening). It exists because
`UsagePopup._update_loop()` dedups on `cache.snapshot.version`, which does not move when a task
starts or ends - without it the queue view would freeze between polls. `evaluate()` deliberately
does **not** bump on an unchanged reading, or every poll would repaint the popup for nothing.

#### `snapshot()`

One lock hold for the whole queue, because the rows must be mutually consistent: a task that
finished between two smaller calls would show as running in one column and done in the next.
Returns `version`, `disabled`, `draining`, `running_count`, `max_concurrent`, `five_hour_pct`, and
a `tasks` list of raw rows (registry fields, `STATUS_*` key, `exit_code`, `started_at`,
`finished_at`, `started_at_utilization`, `log`, `has_log`, `elapsed_seconds`). No localized text.

#### `stop()`

Mirrors `kill_all()` - sets `_Run.forced_exit_code`, kills and waits outside the lock so a watcher
finalizing at the same moment is never blocked, then `_finalize`s - but deliberately does **not**
set `_draining`: stopping one task is not a decision to stop starting others. Records
`EXIT_STOPPED` (`143`), kept distinct from `EXIT_KILLED` (`130`) so the state file still says
whether a run ended because the app was shutting down or because that one task was stopped.

#### `reload()`

Takes already-validated tasks rather than re-reading the file: the editor has just written content
it validated, and a second read could pick up a third party's edit or pop a second error box for
the same problem. Only `_tasks` changes - `_records`, `_running`, `_disabled`, and
`_last_five_hour_pct` are untouched, so a reload can never resurrect a completed task or orphan a
live one.

#### `log_tail()`

Incremental reader behind the popup's log view. The path is resolved **only** through the task's
own state record joined to the logs directory, and a resolved path that escapes that directory is
refused - the log view must not become a way to read arbitrary files. Reads at most
`LOG_TAIL_BYTES` per call, in binary, decoded `utf-8`/`replace` to match how the log is written.
`offset <= 0` or an offset past EOF starts a fresh tail and sets `reset: True`, which covers a
truncated or replaced file.

### Selection

Called at the end of `AutoSchedulerForClaude.update()`, after a successful fetch with no `error`
key - so on every poll and on every popup-triggered background refresh. The task editor never
calls it: adding a task must not start an unattended agent from a UI click on a stale quota
snapshot.

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
- One daemon watcher thread per task calls `proc.wait(timeout=minutes*60)`, or `proc.wait()` with
  no timeout when `timeout_minutes == 0`. `TimeoutExpired` kills the child and records `124`.
- Any failure that would lose a run's output (log directory, log file) prevents the start, shows
  one error, and disables the queue for the session.

`_finalize()` is idempotent (guarded by `_Run.finalized`), so a watcher and `kill_all()`/`stop()`
racing on the same run write exactly one record.

### State semantics

Any id present in `tasks` is complete - one-shot means once, ever. A record with `started_at` and
no `finished_at` found at startup is an interrupted run (the app died mid-task); it counts as
complete, renders as `interrupted`, and is never restarted. A state file that exists but cannot be
parsed disables the queue rather than being treated as empty, which would re-run everything.

Records carry an optional `started_at_utilization`: the five-hour utilization the run started at,
shown in the popup's task detail and written into the log header as a `usage:` line. It is
additive, so `STATE_VERSION` stays `1` and every reader tolerates its absence (records written by
older versions render `-`). `_finalize()` carries the value forward from the start record.

---

## The popup

### Window host (`popup.py`)

Two widths rather than one:

```python
class UsagePopup:
    WIDTH_COMPACT = 340
    WIDTH_EXPANDED = 940
    _MARGIN = 12
```

Neither is read directly. `_effective_width()` returns the current one, clamping the expanded
value to the monitor's work area (`min(WIDTH_EXPANDED, work_width_logical - 2 * _MARGIN)`): a
frameless popup has no taskbar button, so one that hangs off the screen edge cannot be recovered -
the width gives way, not the position.

| Member | Purpose |
|---|---|
| `_expanded` | Current layout. Seeded from and written back to `app._popup_expanded`, so reopening the popup keeps the choice without writing to the read-only settings file |
| `_modal` | Suspends the whole dismiss watch while a subview or native dialog is up |
| `_last_width`, `_last_height` | Dedup for `report_height`, which now compares both |
| `_last_queue_version` | Dedup for the queue half of `_update_loop` |
| `_set_expanded()` | Records the flag and re-runs the resize itself - only the width changes when the taller layout happens to report the same height, which `report_height` would see as no change at all |
| `_monitor_rects()`, `_scale()` | Shared Win32 lookups behind `_tray_position` and the clamps |
| `_clamp_into_work_area()` | Pulls a moved pinned popup back on-screen after it grew, correcting **inward only** so a popup that still fits stays where the user put it |
| `_browse()` | Native folder/file picker, holding `_modal` for the duration |

The DPI rules are unchanged: `resize()` and `move()` take logical pixels, `_tray_position()` takes
physical and returns logical, and the pinned-drag path remains the only place that uses raw
`SetWindowPos`.

`_dismiss_watch._post_quit()` now gates on `self._pinned or self._modal`. A native file dialog
raises `EVENT_SYSTEM_FOREGROUND`, so without `_modal` the delayed foreground check would destroy
the window mid-pick, taking a half-typed task with it.

### JS bridge (`_PopupApi`)

Existing: `close`, `open_url`, `set_pinned`, `begin_drag`, `drag`, `end_drag`, `report_height`.

New: `set_expanded`, `set_modal`, `confirm` (a native `MB_OKCANCEL`, holding `_modal`),
`browse_folder`, `browse_file`, and the queue calls `queue_registry`, `queue_save`, `queue_delete`,
`queue_stop`, `queue_log`, `queue_open_log`. Every queue call is a direct result of a click and
delegates straight to `app.py`.

### Data contract

`_snapshot_to_dict()` gained a `queue` key and `_init_config()` an `expanded` key. The queue block
is **not** built in `popup.py`: it comes from `AutoSchedulerForClaude.queue_payload()`, because
that is where `STATUS_*` keys become locale strings. The popup receives text it only has to place.

Translations are sent as an explicit `_POPUP_TEXT_KEYS` tuple rather than all of `T`, so the page
cannot start depending on a string the popup does not own.

### Markup and stylesheet

`popup.html` gained five sections: `gateSection` (the headline quota bar and its markers),
`queueSection` (summary, notice, filter chips, table), `detailSection` (1e), `formSection` (1d),
and the `expandBtn` control in the header.

Only one of the queue, detail, and editor views is visible at a time - the subviews replace the
overview rather than growing a window that is already at the monitor's usable width.

Every colour in `popup.css` resolves to a theme token fed from `settings.py`; no new colour
settings keys were added and status tones map onto the existing ones:

| Status | Token |
|---|---|
| running | `--bar-fg` |
| pending | `--fg` |
| waiting, interrupted | `--fg-dim` |
| done (exit 0) | `--fg` |
| done (non-zero) | `--bar-fg-warn` |

`body.expanded` caps label/value rows and the editor to a readable measure, so the extra width
goes to the table and the quota line rather than stretching every field across it.

### View logic (`popup.js`)

Module state: `popupExpanded`, `view` (`queue` | `detail` | `form`), `queueFilter`,
`detailTaskId`, `formState`, plus the elapsed and log-tail timers.

- `updateGate()` redraws the headline bar: fill, the existing time marker and dividers, and one
  staggered threshold marker per still-runnable task. It clears markers and dividers first, or
  they would pile up on every poll.
- `updateQueue()` renders the summary, the notice (queue disabled, or no five-hour quota), the
  filter chips with whole-queue counts, and the rows for the active filter.
- A running row's elapsed time ticks in JS off `started_epoch`; `syncElapsedTimer()` runs the
  1-second interval only while something is actually counting up. Python sends an empty
  `result_text` for a running task precisely so the page owns that cell.
- `openDetail()` starts a 1-second log poll that stops as soon as the task is no longer running,
  and a reply for a task the user has already navigated away from is dropped.
- `openForm()` reads the registry through the bridge for a fresh stamp, `buildEntry()` emits only
  the fields the user filled in (an empty string would fail validation, and the unused prompt key
  must be absent entirely because `prompt_file` wins whenever it is present at all), and
  `renderThreshold()` uses the same `<=` the queue does so the hint can never promise a start the
  selector would not make.

### `dev.html`

The browser preview harness now passes the whole locale as the translation object, builds a
representative queue payload, stubs the new bridge calls, and has a "Toggle expanded" control. It
is a developer tool and is not part of the EXE.

---

## App integration

| Location | Change |
|---|---|
| `AutoSchedulerForClaude.__init__` | Creates `self.task_runner = TaskRunner(on_drained=self._on_tasks_drained)`; `self._popup_expanded = False` |
| Tray menu | `_build_menu()` builds the whole menu, including the `T['task_queue']` submenu (`visible=has_tasks()`); `_rebuild_menu()` re-creates it after a registry edit |
| `update()` | Calls `self.task_runner.evaluate(result.data)` as its last step |
| `_render_tray()` | Appends `T['tooltip_draining']` to the tooltip while draining |
| `poll_loop()` | Away branch is `if self._is_user_away() and not self.task_runner.has_pending_work():` - only the pause is skipped |
| `on_quit()` | Three-way `MB_YESNOCANCEL` dialog when tasks are running |

Module-level names in `app.py`: `MB_YESNOCANCEL`, `MB_ICONQUESTION`, `IDCANCEL`, `IDYES`, `IDNO`,
`TASK_STATUS_GLYPHS`, `TASK_FAILED_GLYPH`, `TASK_ACTIVE_STATUSES`, `QUEUE_GATE_FIELD`, and the
helpers `_iso_to_epoch()`, `_run_duration()`, `_task_result_text()`, `_threshold_markers()`.

Methods: `_on_tasks_drained()`, `_build_menu()`, `_rebuild_menu()`, `_build_task_menu()`,
`_task_item_text()`, `_task_status_label()`, `_task_item_enabled()`, `_task_item_action()`,
`queue_payload()`, `_task_row()`, `_queue_gate_bar()`, `registry_read()`, `registry_save()`,
`registry_delete()`, `stop_task()`, `task_log()`, `open_task_log()`, `_apply_registry()`,
`_registry_failure()`.

Menu items are static with callable `text` and `enabled`, so status re-renders every time the menu
opens; the *set* is rebuilt only when the registry changes. Item text is
`<glyph> <description or id> - <localized status>`; a finished run's label carries its exit code,
and only a run with a log on disk is clickable (`os.startfile`).

`on_quit()` outcomes: **Yes** -> `kill_all()` (exit `130`) then stop; **No** -> `begin_drain()` and
return, with the last watcher thread calling `_on_tasks_drained()`; **Cancel** -> return, clearing
`restart_requested` so a cancelled restart does not relaunch.

### `QUEUE_GATE_FIELD`

The popup's headline bar names `five_hour` explicitly. That is queue logic, not display logic -
tasks start on that field alone - so the "never hardcode a quota field" rule does not apply to it.
It still applies to the bars below it, which stay auto-detected via `expand_popup_fields`, and the
gate field is filtered out of that list so the same quota is not shown twice. When the field is
absent or `null`, `_queue_gate_bar()` returns `None` and the popup says nothing will start, which
is exactly what `evaluate()` does.

### Registry-editing API

`registry_read()`, `registry_save(entry, index, stamp)`, and `registry_delete(index, stamp)` are
reachable only from the popup's editor. `registry_save` appends on a negative or out-of-range
index and otherwise replaces in place, because file order is queue priority.
`_apply_registry()` writes, then `task_runner.reload(tasks)`, then `_rebuild_menu()`.

`registry_delete` refuses while the task is running - removing a live task's entry would leave the
tray, the popup, and the child process disagreeing - and otherwise leaves the state record and log
file alone, because the record is what enforces one-shot. `registry_read()` also returns
`completed_ids` so the editor can warn that a reused id will never start.

---

## Formatting

Added to `formatting.py`, both pure and localized through `T`:

```python
def format_duration(total_seconds: float) -> str      # '1h 12m' / '6m' / '42s'
def format_timestamp(iso_str: str, clock_24h: bool | None = None) -> str
```

`format_duration` truncates minutes rather than rounding, so a displayed duration never runs ahead
of the real one, and treats negative input as zero. `format_timestamp` renders a past UTC
timestamp in local time as a clock (today), `Yesterday HH:MM`, a weekday within a week, or a full
date, and returns `''` for empty or unparseable input so an older state record cannot break the
popup. `popup.js` mirrors `format_duration` in `formatElapsed()` for the live elapsed counter.

---

## Settings

| Key | Type | Default | Validation |
|---|---|---|---|
| `max_concurrent_tasks` | int | `1` | `>= 1`, via `_NUMERIC_BOUNDS` |
| `task_command` | array of strings | `[]` | Array of non-empty strings; empty means auto-detect. A shell string is rejected, not split - it is an argv list |

Exported as `settings.MAX_CONCURRENT_TASKS` and `settings.TASK_COMMAND`.

`task_command` exists so a WSL-hosted CLI can run tasks without giving `cli_command` a second job.
`cli_command` remains display-only and must never take part in authentication or token refresh.

`compact_hide` accepts a new `task_queue` value, hiding the headline bar and the task table in the
pinned compact view. No settings change was needed - the list is free-form strings.

The expanded/compact choice is **not** a setting: it lives on `app._popup_expanded` for the process
lifetime, because the app never writes the settings file.

---

## Locale

62 template keys were added to all 13 `locale/*.json` files (133 keys each), covering the queue
view, the task detail, the task editor, the registry error messages, and three timestamp
templates (`time_yesterday`, `time_weekday`, `time_date`).

All are templates - there are no per-task or per-field keys. `tests/test_i18n.py` enforces key
parity across the 13 files, and `tests/test_popup.py` enforces that the set the popup sends, the
set the page reads, and the keys present in English all agree.

---

## Tests

| File | Coverage |
|---|---|
| `tests/test_tasks.py` | Registry loading, every validation rule, prompt/`prompt_file` precedence and path resolution, `select_runnable` (equality, non-blocking skip, slots, order), id sanitizing |
| `tests/test_task_registry.py` | **New.** Read of missing/empty/broken files, stamp mismatch refusal, validation gate, atomic write, file order, `prompt_file` round-trip, UTF-8, write failure |
| `tests/test_task_runner.py` | Start/state/log writing, exit codes, timeout kill (`124`), no-timeout path, `kill_all` (`130`), `stop` (`143`), drain callback, interrupted and corrupt state, unwritable log dir, concurrency limits, concurrent `evaluate` from four threads, `version` bumps, `snapshot` shape, `reload` isolation, `log_tail` offsets and path-escape refusal |
| `tests/test_app.py` | `evaluate` delegation (and non-delegation on errors), idle bypass, the three quit outcomes, draining tooltip, task menu rendering, `queue_payload` (counts, glyphs, markers, gate presence, headroom), registry add/edit/delete including the stale-stamp and running-task refusals |
| `tests/test_popup.py` | Payload shape, DPI resize and positioning, expanded-width clamping, the pinned-and-moved clamp, and the popup asset cross-checks (element ids, `els` entries, translation keys, no hardcoded colours) |
| `tests/test_popup_js.py` | Usage bar create/update, queue table rows and filters, empty and disabled states, live elapsed, gate markers and redraw idempotency (Node.js; skipped when absent) |
| `tests/test_settings.py` | `max_concurrent_tasks` and `task_command` validation and defaults |
| `tests/test_formatting.py` | `quota_snapshot_env`, `format_duration`, `format_timestamp` |

Every subprocess in the runner tests is faked and the config dir is redirected to a temporary
directory, so no task ever really runs. The `_runner` fixture kills and drains on exit: a watcher
thread holds the run's log file open, and Windows refuses to delete the temp dir while it does, so
a test that leaves a task running would otherwise fail during cleanup rather than on its own
assertion. `_make_app()` in `tests/test_app.py` injects a mocked `TaskRunner` with explicit falsy
defaults - a bare `MagicMock` returns truthy values, which would silently enable the draining
tooltip, the idle bypass, and the quit dialog across the whole suite.

`_run_scenario()` in `tests/test_popup_js.py` passes `encoding='utf-8'` to `subprocess.run`;
`text=True` alone decodes Node's output with the locale codepage and mangles the status glyphs.

Run: `python -m unittest discover -s tests` (1312 tests).

---

## History

### Rename (2026-08-10)

The project was renamed from `usage-monitor-for-claude` / `usage_monitor_for_claude` to
`auto-scheduler-for-claude` / `auto_scheduler_for_claude` as a clean break, with no compatibility
shims or dual-name reads.

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

Deliberately unchanged: `notification_logo.ico` (already generic), the `cli_command` setting name
and its display-only role, `.github/FUNDING.yml`, `version_info.py`'s `CompanyName`, and every
reference to the separate companion tool `jens-duttke/agent-monitor-for-claude`.

Two `HKCU` entries created by the old build are orphaned by the rename and are **not** cleaned up
in code (that would mean writing to registry paths the new app does not own): the old autostart
value and the old AppUserModelId. Both are documented in `CHANGELOG.md` as manual post-upgrade
steps.

### Moved function (2026-08-10)

`AutoSchedulerForClaude._quota_snapshot_env()` moved to `formatting.quota_snapshot_env()` as a
public pure function, so `app.py` and `task_runner.py` share one implementation instead of the
runner reaching back into the app.

---

## Known limitations

- Only the `five_hour` quota gates execution. A task can start at 5% five-hour usage while the
  seven-day quota sits at 99%. A `max_weekly_usage_percent` field is a natural follow-up, out of
  scope by explicit decision.
- Started tasks are not throttled against each other. With `max_concurrent_tasks > 1`, several
  tasks can launch from one quota snapshot before any of their consumption is visible.
- No retry. A task that fails for a transient reason is done and needs a new `id` to run again.
- One-shot cannot be cleared from the UI. Re-running a task still means editing
  `auto-scheduler-state.json` by hand or giving the task a new id; the editor only warns that the
  id is used up. A "reset this run" action was deliberately left out of this change.
- A registry change made in a text editor still needs a restart. Only an edit made through the
  popup reloads the queue and rebuilds the tray submenu.
- Writing the registry from the editor normalizes its formatting (two-space indent). Nothing is
  lost - JSON has no comments - but hand-tuned whitespace does not survive.
- Validation errors surface in the editor as the same sentences the startup message box uses,
  listed together. They carry a `field`, but the editor does not yet highlight the offending
  control.
- Killing or stopping a task terminates only the direct `claude` child. Grandchildren it spawned
  are not walked and may survive.
- `MessageBoxW` error reporting inside `TaskRunner._disable()` can be reached while the runner lock
  is held (state-write failure during finalization), which blocks other queue operations until the
  dialog is dismissed. It is an error path that also ends the queue for the session.
