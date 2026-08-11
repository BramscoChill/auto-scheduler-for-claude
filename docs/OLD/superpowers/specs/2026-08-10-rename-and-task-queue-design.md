# Rename to Auto Scheduler for Claude + Quota-Gated Task Queue

**Date:** 2026-08-10
**Status:** Approved design, ready for implementation planning

## Summary

Two changes ship together:

1. **Rename** the project from `usage-monitor-for-claude` / `usage_monitor_for_claude` to
   `auto-scheduler-for-claude` / `auto_scheduler_for_claude`, as a clean break with no
   backward-compatibility shims.
2. **Add a task queue** that executes `claude -p` tasks autonomously when the five-hour
   quota window drops at or below a per-task percentage threshold.

The rename is not cosmetic: the app stops being a passive monitor and becomes a scheduler.
The queue is what justifies the new name, and the rename is sequenced first so the queue is
built in the renamed tree.

## Motivation

`docs/execute-claude-tasks.md` describes a standalone Python launcher (`run_tick.py`) that
runs Claude tasks from a JSON registry on a Windows Task Scheduler cadence. That design has
one weakness: it fires on wall-clock time and therefore burns tokens whether or not quota is
available, or sits idle while quota goes unused.

This app already polls the five-hour quota with reset-aligned, adaptive cadence. Moving the
launcher into it replaces "run at 00:00" with "run when there is quota", which is strictly
better for the same workload. Time-based scheduling is deliberately **not** ported.

---

## Part 1 - Rename

### Decision

Full clean break. No compatibility fallbacks, no dual-name reads, no migration code.

### Identifier map

| Kind | From | To |
|---|---|---|
| Package directory | `usage_monitor_for_claude/` | `auto_scheduler_for_claude/` |
| PyInstaller spec | `usage_monitor_for_claude.spec` | `auto_scheduler_for_claude.spec` |
| App icon | `usage_monitor_for_claude.ico` | `auto_scheduler_for_claude.ico` |
| Display name | `Usage Monitor for Claude` | `Auto Scheduler for Claude` |
| Settings file | `usage-monitor-settings.json` | `auto-scheduler-settings.json` |
| Event env prefix | `USAGE_MONITOR_*` | `AUTO_SCHEDULER_*` |
| Autostart reg value | `UsageMonitorForClaude` | `AutoSchedulerForClaude` |
| Mutex base name | `UsageMonitorForClaude_SingleInstance` | `AutoSchedulerForClaude_SingleInstance` |
| PID mapping name | `UsageMonitorForClaude_HolderPID` | `AutoSchedulerForClaude_HolderPID` |
| Notification AUMID | `JensDuttke.UsageMonitorForClaude` | `BramscoChill.AutoSchedulerForClaude` |
| pystray icon name | `usage_monitor` | `auto_scheduler` |
| `PROJECT_URL` | `github.com/jens-duttke/usage-monitor-for-claude` | `github.com/BramscoChill/auto-scheduler-for-claude` |

`notification_logo.ico` keeps its name (already generic).

### Surface

Measured with ripgrep on 2026-08-10:

- 1020 occurrences of `usage_monitor_for_claude`
- 58 of `usage-monitor-for-claude`
- ~45 of the display string `Usage Monitor for Claude`
- 19 of `usage-monitor-settings`
- ~250 of `USAGE_MONITOR_*` env var names

Files touched: the whole package, all 17 test modules, all 13 `locale/*.json`, `build.py`,
`version_info.py` (all four version fields), the spec file, `README.md`, `PRIVACY.md`,
`CHANGELOG.md`, `docs/*.md`, `.github/ISSUE_TEMPLATE/bug_report.md`, `.gitignore`, and
`.claude/CLAUDE.md` + `.claude/commands/*.md`.

Class rename: `UsageMonitorForClaude` -> `AutoSchedulerForClaude` in `app.py` and its
`__all__`, plus every test import.

### Accepted consequences

The clean break orphans two `HKCU` entries created by the old build:

- `Software\Microsoft\Windows\CurrentVersion\Run\UsageMonitorForClaude`, now pointing at a
  renamed (missing) executable.
- `Software\Classes\AppUserModelId\JensDuttke.UsageMonitorForClaude`.

Neither is cleaned up in code - that would mean writing to registry paths the new app does
not otherwise own. Both are documented in `CHANGELOG.md` as manual post-upgrade steps, along
with renaming the settings file and updating `USAGE_MONITOR_*` references in user event-command
scripts. Users re-enable autostart from the tray menu after upgrading.

### Non-goals

`cli_command` keeps its existing name and its display-only role. It is not renamed and gains
no new responsibility (see `.claude/CLAUDE.md`, "Claude CLI").

---

## Part 2 - Task queue

### Data files

All three live in the effective config dir (`CLAUDE_CONFIG_DIR`, else `~/.claude`), resolved
via the existing `instance_id.effective_config_dir()`. This gives per-account isolation for
multi-instance setups for free.

#### `auto-scheduler-tasks.json` (read-only for the app)

```json
[
  {
    "id": "daily",
    "description": "Standard daily agent tick",
    "prompt": "Read agent_system/core_prompt.md and execute today's tick.",
    "cwd": "D:/Projecten/_AI_/trading-agent",
    "max_usage_percent": 40,
    "timeout_minutes": 180
  },
  {
    "id": "research",
    "description": "Research-only tick",
    "prompt": "Read agent_system/core_prompt.md and execute a RESEARCH-ONLY tick.",
    "cwd": "D:/Projecten/_AI_/trading-agent",
    "max_usage_percent": 20,
    "timeout_minutes": 0
  }
]
```

| Field | Required | Type | Rules |
|---|---|---|---|
| `id` | yes | string | Non-empty, unique across the file. Key for state and log names. |
| `prompt` | yes | string | Non-empty. The print-mode caveat is appended automatically. |
| `max_usage_percent` | yes | number | `0`-`100` inclusive. Task runs when five-hour utilization is **at or below** this. |
| `cwd` | no | string | Working directory for the run. Must be an existing directory. Defaults to the app's working directory (same convention as `command.py`). |
| `timeout_minutes` | no | number | Default `180`. **`0` means no time limit.** Must be `>= 0`. |
| `description` | no | string | Tray-menu label only. |

Validation follows `settings.py`: collect every error, drop nothing silently, show one
`MessageBoxW` listing all problems. Unlike settings, a *single* invalid entry disables the
**whole queue** for the session - a partially-loaded queue of autonomous agents is worse than
no queue. A missing tasks file is not an error; it simply means the feature is off.

##### Threshold direction (decided: at or below)

`max_usage_percent` is a **ceiling on consumption**, evaluated as:

```
start the task if  five_hour.utilization <= task.max_usage_percent
```

`utilization` is the same 0-100 number the tray icon and popup show (`formatting.py:428`
renders it with `:.0f}%`). `0` means the window just reset and is untouched; `100` means it is
exhausted.

The inverted reading - run when utilization is at or *above* the threshold - was considered and
rejected. The feature exists to spend spare capacity without hurting interactive work, so the
gate must protect headroom. Firing on high usage would start unattended agents exactly when
quota is scarcest.

Three consequences that must be stated wherever this field is documented:

- **`<=`, not `<`.** Equality counts, which is what makes `max_usage_percent: 0` usable
  ("only on a completely fresh window"). With a strict `<` it would be unsatisfiable and the
  entry would be dead config.
- **It gates the start, not the run.** A task that begins at 38% can drive the window to 95%
  while it works. Nothing is re-checked mid-run; the next poll simply starts nothing else above
  the line. So heavy tasks want low thresholds (10-20) and cheap ones high (80-90).
- **Threshold plus file order gives priority for free.** Selection walks the file top to bottom
  and skips past any task whose threshold is not met, so a cheap high-threshold task naturally
  claims a slot an expensive low-threshold task cannot yet use.

#### `auto-scheduler-state.json` (written by the app)

```json
{
  "version": 1,
  "tasks": {
    "daily": {
      "started_at": "2026-08-10T22:04:11+00:00",
      "finished_at": "2026-08-10T23:51:02+00:00",
      "exit_code": 0,
      "log": "daily-20260810-220411.log"
    }
  }
}
```

- Written atomically: temp file in the same directory, then `os.replace`.
- The record is written when the task **starts** (`finished_at: null`), then rewritten on exit.
- A record with `started_at` and no `finished_at` found at app start is an **interrupted** run
  (the app died mid-task). It counts as completed and is never re-run: an autonomous agent that
  got halfway must not be silently restarted. It renders as "interrupted" in the tray menu.
- Any task id present in `tasks` is complete. One-shot means once, ever. Re-running requires a
  new `id`, or hand-deleting the record.
- A state file that exists but cannot be parsed disables the queue for the session and shows an
  error box. Treating it as empty would re-run every task.

#### `auto-scheduler-logs/<id>-<YYYYMMDD-HHMMSS>.log`

Merged stdout+stderr of one run, opened before launch and passed as the child's handles.
Header records id, resolved command, cwd, and start time; footer records exit code and
duration. Task ids are sanitized for filesystem use. One-shot semantics bound growth to one
log per task, so there is no retention or rotation policy.

If the log directory cannot be created or the file cannot be opened, the task is **not**
started, an error box is shown once, and the queue is disabled for the session.

### Modules

Split along the existing pure/side-effect boundary (`.claude/CLAUDE.md`, "Style").

#### `auto_scheduler_for_claude/tasks.py`

Loading, validation, and the pure selection logic. No subprocesses, no writes.

```python
@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    max_usage_percent: float
    timeout_minutes: int
    cwd: str | None
    description: str


def load_tasks() -> list[Task]: ...


def select_runnable(
    tasks: list[Task], completed_ids: set[str], running_ids: set[str],
    five_hour_pct: float, slots: int,
) -> list[Task]:
    """Return the tasks to start now, in file order, at most *slots* of them."""
```

`PRINT_MODE_CAVEAT` lives here as a module constant, copied verbatim from
`docs/execute-claude-tasks.md`.

#### `auto_scheduler_for_claude/task_runner.py`

A `TaskRunner` class (same shape as `UsageCache`: owns state plus a lock) handling
subprocesses, the state file, logs, concurrency, and shutdown.

```python
class TaskRunner:
    def __init__(self) -> None: ...
    def evaluate(self, data: dict) -> None:        # called after every successful update
    def has_pending_work(self) -> bool:            # any task neither completed nor running
    def running_count(self) -> int:
    def menu_entries(self) -> list[tuple[str, str]]:   # (task id, status label)
    def begin_drain(self) -> None:                 # stop starting new tasks
    def kill_all(self) -> None:                    # terminate children, record exit 130
```

A class is justified here despite the functional preference: it owns mutable cross-thread
state (running processes, completed ids, the drain flag) behind one lock, exactly as
`UsageCache` does for its fetch state.

### Selection algorithm

Runs at the end of `AutoSchedulerForClaude.update()`, after a successful fetch with no
`error` key - so on every poll and on every popup-triggered background refresh.

```
five_hour_pct = (data.get('five_hour') or {}).get('utilization')
if five_hour_pct is None:  return          # no five-hour quota -> queue never fires
if draining or queue disabled: return

slots = MAX_CONCURRENT_TASKS - len(running)
if slots <= 0: return

for task in file order:
    if task.id in completed or task.id in running:  continue
    if five_hour_pct > task.max_usage_percent:      continue   # check the next task
    start(task)
    slots -= 1
    if slots == 0: break
```

The `continue` on a failed threshold - rather than a `break` - is the explicitly requested
behavior: a blocked task must not stop the scan.

Note the `(data.get('five_hour') or {})` idiom: `five_hour` can be present and `null`
(`.claude/CLAUDE.md`, "Quota Fields").

`evaluate()` is reachable from both the poll thread and the popup refresh thread, so it takes
the runner lock for the whole select-and-start sequence.

When `max_concurrent_tasks > 1`, several tasks may start from one snapshot. Their combined
usage is not visible until the next poll. This is accepted: the setting exists precisely to
allow it, and per-task thresholds let the user express caution.

### Executing a task

```
[task_binary..., '-p', task.prompt + PRINT_MODE_CAVEAT, '--dangerously-skip-permissions']
```

- `task_binary` is `claude_cli.CLAUDE_CLI_PATH` (already the exact equivalent of the doc's
  `find_claude_binary()`), overridden by the new `task_command` setting when set.
- `cwd` = `task.cwd`, else the app working directory.
- Environment = current environment plus the quota snapshot, plus `AUTO_SCHEDULER_EVENT=task`,
  `AUTO_SCHEDULER_TASK_ID=<id>`, `AUTO_SCHEDULER_VERSION`. The snapshot builder currently lives
  on the app class as `_quota_snapshot_env()` but is a pure dict-to-dict transform; it moves to
  `formatting.py` as `quota_snapshot_env()` so `app.py` and `task_runner.py` share one
  implementation instead of the runner reaching back into the app.
- `creationflags=subprocess.CREATE_NO_WINDOW`, stdout and stderr both to the log handle.
- One daemon watcher thread per task calls `proc.wait(timeout=...)`, or `proc.wait()` with no
  timeout when `timeout_minutes == 0`. On `TimeoutExpired` it kills the child and records
  exit code `124`, matching the doc.
- On exit the watcher writes the completion record, closes the log, and (when draining and it
  was the last runner) stops the tray icon.

The caveat injection is unconditional - no task can opt out, per the doc's design.

**Security note:** `--dangerously-skip-permissions` is hardcoded, as in the source document.
It is what makes unattended execution possible. Given this project's auditability promise,
it gets a prominent disclosure in `README.md` and `PRIVACY.md` rather than being buried.

Failure handling: any finished run - success, non-zero exit, or timeout kill - marks the task
complete with its exit code recorded. There are no retries, so a permanently failing task can
never loop and burn quota. No notification fires on failure; automatic events stay silent
(`.claude/CLAUDE.md`, "Event Commands"). The exit code is visible in the tray submenu and the
state file.

### Idle behavior

`poll_loop` currently stops polling entirely while the user is idle or the workstation is
locked. The away branch becomes conditional:

```python
if self._is_user_away() and not self.task_runner.has_pending_work():
    ...existing idle pause...
```

With pending work the loop keeps its normal cadence, so the queue drains overnight - the
entire point of an auto-scheduler. This only skips the pause; `_calculate_poll_interval()`,
`_align_to_reset()`, and the danger-window invariant are untouched, and notification
deferral while away is unchanged.

### Shutdown

`on_quit` checks `running_count()`. With nothing running, behavior is unchanged. With tasks
in flight it shows a `MB_YESNOCANCEL | MB_ICONQUESTION` box whose text spells out the three
outcomes:

- **Yes** - quit now, kill the running task(s), record exit code `130`.
- **No** - drain: finish the running task(s), start no new ones, exit when the last one ends.
- **Cancel** - do not quit.

While draining, `_render_tray()` appends a draining marker to the tooltip so the state is
visible. `on_restart` routes through `on_quit` and inherits this.

Windows `MessageBoxW` button labels are fixed, so the message text must map Yes/No/Cancel to
the outcomes explicitly. Return codes: `IDYES=6`, `IDNO=7`, `IDCANCEL=2`.

### Settings

Two new keys in `auto-scheduler-settings.json`:

| Key | Type | Default | Validation |
|---|---|---|---|
| `max_concurrent_tasks` | int | `1` | `>= 1`, via `_NUMERIC_BOUNDS` |
| `task_command` | array of strings | `[]` (auto-detect) | Non-empty strings; empty array means auto-detect |

`task_command` exists so a WSL-hosted CLI can run tasks without giving `cli_command` a second
job, which the project's hard rule forbids.

### Tray menu

A **Task queue** submenu, placed above "Test event commands", hidden when no tasks file
loaded. One item per task:

```
<glyph> <id> - <description>
```

Statuses, one locale key each:

| Status | Meaning |
|---|---|
| waiting | Five-hour utilization is still above the task's `max_usage_percent`. |
| pending | Threshold is met, but every concurrency slot is occupied. |
| running | A child process is live. |
| done | Finished, with its exit code shown. |
| interrupted | Started but never finished; the app died mid-run. |

Each item's `text` is a lambda, so status re-renders every time the menu opens. The task
*set* is fixed at startup (the file is read once), so a static item list with dynamic text is
sufficient - no pystray dynamic-menu API is needed. Clicking a finished task opens its log
with `os.startfile`; clicking a pending or running one does nothing.

New locale keys (13 files): `task_queue`, `task_state_pending`, `task_state_waiting`,
`task_state_running`, `task_state_done`, `task_state_interrupted`, `quit_tasks_running_title`,
`quit_tasks_running`, `tooltip_draining`. All are template keys, not per-task keys.

---

## Testing

New `tests/test_tasks.py`:

- valid file parses; every field default applied
- missing `id` / `prompt` / `max_usage_percent` rejected
- duplicate ids rejected
- `max_usage_percent` outside 0-100 rejected; booleans rejected as numbers
- `timeout_minutes: 0` accepted and meaning no limit; negative rejected
- non-existent `cwd` rejected
- one invalid entry disables the whole queue
- missing file yields an empty queue with no error box
- `select_runnable`: utilization exactly equal to the threshold runs; a blocked task does not
  stop the scan; completed and running ids are skipped; `slots` caps the result; file order
  is preserved

New `tests/test_task_runner.py` (subprocess mocked, config dir redirected to a temp dir):

- start writes a start record and a log file
- normal exit records `exit_code` and `finished_at`
- timeout kills the child and records `124`
- `timeout_minutes: 0` passes no timeout to `wait`
- `kill_all` records `130`
- drain starts no new tasks and stops the icon after the last one
- interrupted record at startup is not re-run
- corrupt state file disables the queue
- unwritable log directory prevents the start
- `max_concurrent_tasks` enforced across repeated `evaluate` calls
- `evaluate` is safe under concurrent calls from two threads

`tests/test_app.py` additions:

- `evaluate` is called after a successful update and not after an error response
- `five_hour` missing or `null` starts nothing
- the idle pause is bypassed while work is pending and still applies when it is not
- quit with tasks running: each of the three dialog outcomes

Every existing test module needs the rename pass: import paths, `AUTO_SCHEDULER_*` env var
assertions, the settings filename, and display strings. `tests/test_settings.py` gains cases
for `max_concurrent_tasks` and `task_command`. `tests/test_i18n.py` enforces locale key parity
and will catch a locale file missing the new keys.

Full suite: `python -m unittest discover -s tests`, run inside the virtual environment.

## Documentation

| File | Change |
|---|---|
| `docs/task-queue.md` | New. Schema, worked examples, state file, logs, the `--dangerously-skip-permissions` note. |
| `README.md` | Rename, repositioning from monitor to scheduler, new feature bullet in the "Proactive protection" tier, **plus a full task-queue usage section** - see "README task-queue section" below. |
| `PRIVACY.md` | The app now writes files. Document the three paths and the skip-permissions behavior. |
| `.claude/CLAUDE.md` | Replace the "no file write operations" rule with an explicit three-item write list; add a Task Queue section. |
| `docs/configuration.md` | `max_concurrent_tasks`, `task_command`, new settings filename. |
| `docs/event-commands.md` | `USAGE_MONITOR_*` -> `AUTO_SCHEDULER_*`. |
| `CHANGELOG.md` | Breaking rename with the manual migration steps; the new queue feature. |
| `docs/AUTO_SCHEDULER_FOR_CLAUDE_TECHNICAL_REFERENCE.md` | New/updated per the global rule. |
| `docs/execute-claude-tasks.md` | Kept as the origin document, cross-referenced from `docs/task-queue.md`. |

### README task-queue section

`README.md` must carry the complete usage story, not a pointer to `docs/task-queue.md`. The
queue is the reason for the rename, and a user deciding whether to adopt the tool has to be
able to understand it without opening a second file. `docs/task-queue.md` stays as the deeper
reference (state-file internals, log format, troubleshooting).

Required subsections, in order:

1. **What the task queue does** - two or three sentences: queued `claude -p` prompts run
   unattended when there is spare five-hour quota, one shot each, no wall-clock schedule.
2. **Setting it up** - where `auto-scheduler-tasks.json` lives (the effective config dir,
   `CLAUDE_CONFIG_DIR` else `~/.claude`), a complete two-task example file, and the note that
   the file is read once at startup so edits need a restart.
3. **Task fields** - the full field table reproduced from this spec (`id`, `prompt`,
   `max_usage_percent`, `cwd`, `timeout_minutes`, `description`), including
   `timeout_minutes: 0` = no limit.
4. **Understanding `max_usage_percent`** - the reminder chapter. Requirements below.
5. **Running more than one task at a time** - `max_concurrent_tasks` (default `1`), and the
   caveat that several tasks starting from one quota snapshot cannot see each other's
   consumption until the next poll.
6. **Where results go** - the state file, the per-run log, the tray "Task queue" submenu with
   its five statuses, and the fact that clicking a finished task opens its log.
7. **Quitting with tasks running** - the three-way dialog and what each button does.
8. **Safety** - tasks run with `--dangerously-skip-permissions`, what that means, and that the
   app now writes three paths under the config dir.

#### Requirements for the "Understanding `max_usage_percent`" chapter

This is the chapter the author (and future readers) will come back to, because the direction of
the comparison is not self-evident from the field name alone. It must contain, explicitly:

- The plain-language definition: five-hour utilization is how much of the rolling five-hour
  quota is already consumed - the same percentage the tray icon shows. `0` = just reset,
  `100` = exhausted.
- The rule as a one-line formula:
  `run the task if current_five_hour_utilization <= max_usage_percent`.
- A worked table for a task configured at `40`:

  | Tray shows | Comparison | Task starts? |
  |---|---|---|
  | 0% (window just reset) | 0 ≤ 40 | yes |
  | 38% | 38 ≤ 40 | yes |
  | 40% | 40 ≤ 40 | yes - this is the "at" part |
  | 41% | 41 ≤ 40 | no - skipped, next task considered |
  | 92% | 92 ≤ 40 | no |

- The question the number actually answers: *"how much of my five-hour quota am I willing to
  have already spent before letting this unattended task loose?"* Low (10) is conservative -
  it runs only on a nearly fresh window. High (90) is permissive - it runs whenever any quota
  is left.
- The three consequences from "Threshold direction" above: equality counts (so `0` is usable);
  the gate applies at start only and a running task can push the window far past its own
  threshold; file order plus thresholds act as priority.

## Implementation order

1. **Rename.** Purely mechanical. Verified by the existing suite passing unchanged plus a
   successful `build.py` run. No behavior change.
2. **Queue core.** `tasks.py` and `task_runner.py` with their tests. No app wiring, so the
   suite stays green throughout.
3. **Integration.** `update()` hook, `poll_loop` idle bypass, tray submenu, quit dialog, and
   the `app.py` tests.
4. **Surface.** Settings validation, 13 locale files, docs, spec `datas`, `build.py`.

Each phase ends with the full suite green.

## Known limitations

- Only the `five_hour` quota gates execution. A task can start at 5% five-hour usage while the
  seven-day quota sits at 99%. A `max_weekly_usage_percent` field would be a natural follow-up
  but is out of scope by explicit decision.
- Started tasks are not throttled against each other. With `max_concurrent_tasks > 1`, several
  tasks can launch from one quota snapshot before any of their consumption is visible.
- No retry. A task that fails for a transient reason is done and needs a new id to run again.
- The tray submenu reflects the tasks file as read at startup. Editing the file requires a
  restart, matching how settings already behave.
- Killing a task on quit terminates only the direct `claude` child. Grandchildren it spawned
  are not walked and may survive.
