# Task Queue

The task queue runs `claude -p` prompts unattended, but only when your five-hour quota has
enough headroom left. There is no wall-clock schedule: instead of "run at 00:00", the rule is
"run when there is quota".

`README.md` carries the complete usage story. This document is the deeper reference: the exact
validation rules, the state file format, the log format, and troubleshooting.

The design originates in [`execute-claude-tasks.md`](execute-claude-tasks.md), a standalone
launcher driven by Windows Task Scheduler. This app replaces its wall-clock trigger with a
quota trigger; the print-mode caveat and the timeout exit code (`124`) are carried over
unchanged. Time-based scheduling is deliberately **not** ported.

> [!WARNING]
> Queued tasks run with `--dangerously-skip-permissions`. That flag is hardcoded and cannot be
> turned off - it is what makes unattended execution possible. Every queued task can read,
> write, and delete files and run commands in its working directory without asking you first.
> Only queue prompts you would let run unsupervised, and set `cwd` to the project they belong
> to.

---

## Files

All three live in the **effective config directory**: `CLAUDE_CONFIG_DIR` when set (or
`--config-dir="<path>"`), otherwise `~/.claude`. Running one instance per Claude account
therefore gives each account its own queue, state, and logs without any extra configuration.

| Path | Written by | Purpose |
|---|---|---|
| `auto-scheduler-tasks.json` | you | The task registry. The app only reads it. |
| `auto-scheduler-state.json` | the app | Which tasks have run, and how they ended. |
| `auto-scheduler-logs/` | the app | One log file per run. |

If `auto-scheduler-tasks.json` does not exist, the feature is simply off - the tray submenu is
hidden and nothing is written.

---

## `auto-scheduler-tasks.json`

A JSON array of task objects:

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

### Fields

| Field | Required | Type | Rules |
|---|---|---|---|
| `id` | yes | string | Non-empty, unique across the file. Key for the state record and the log name. |
| `prompt` | yes | string | Non-empty. The print-mode caveat is appended automatically. |
| `max_usage_percent` | yes | number | `0`-`100` inclusive. The task runs when five-hour utilization is **at or below** this. |
| `cwd` | no | string | Working directory for the run. Must be an existing directory. Defaults to the app's working directory. |
| `timeout_minutes` | no | number | Default `180`. **`0` means no time limit.** Must be `>= 0`. |
| `description` | no | string | Tray-menu label only. Falls back to `id` when omitted. |

### Validation

Validation collects **every** problem and reports them together in one message box - nothing is
dropped silently. Unlike the settings file, a *single* invalid entry disables the **whole**
queue for the session: a partially loaded queue of unattended agents is worse than no queue.

Rejected, among others:

- a missing or blank `id` or `prompt`
- a duplicate `id`
- `max_usage_percent` outside 0-100, missing, or not a number (`true` is not a number)
- a negative `timeout_minutes`
- a `cwd` that is not an existing directory
- an unknown field name - a `timeout_minutes` typo must not silently fall back to 180 on an
  unattended agent

The file is read **once at startup**. Editing it needs an app restart, the same as the settings
file.

---

## `max_usage_percent`

Five-hour utilization is how much of the rolling five-hour quota is already consumed - the same
percentage the tray icon and popup show. `0` means the window just reset, `100` means it is
exhausted.

```
run the task if  current_five_hour_utilization <= max_usage_percent
```

For a task configured at `40`:

| Tray shows | Comparison | Task starts? |
|---|---|---|
| 0% (window just reset) | 0 ≤ 40 | yes |
| 38% | 38 ≤ 40 | yes |
| 40% | 40 ≤ 40 | yes - this is the "at" part |
| 41% | 41 ≤ 40 | no - skipped, next task considered |
| 92% | 92 ≤ 40 | no |

The number answers: *"how much of my five-hour quota am I willing to have already spent before
letting this unattended task loose?"* Low (10) is conservative - it runs only on a nearly fresh
window. High (90) is permissive - it runs whenever any quota is left.

Three consequences worth remembering:

- **`<=`, not `<`.** Equality counts, which is what makes `max_usage_percent: 0` usable ("only
  on a completely fresh window"). With a strict `<` it would be unsatisfiable.
- **It gates the start, not the run.** A task that begins at 38% can drive the window to 95%
  while it works. Nothing is re-checked mid-run; the next poll simply starts nothing else above
  the line. Heavy tasks want low thresholds (10-20), cheap ones high (80-90).
- **Threshold plus file order gives priority for free.** Selection walks the file top to bottom
  and *skips past* any task whose threshold is not met, so a cheap high-threshold task
  naturally claims a slot an expensive low-threshold task cannot yet use.

The queue is gated on the `five_hour` quota alone. When the API reports no five-hour quota (the
field is absent or `null`), nothing ever starts.

---

## When the queue is evaluated

Right after every successful usage fetch - so on every poll and on every popup-triggered
background refresh. The check itself is cheap: it only starts something when a task's threshold
is met and a concurrency slot is free.

While the queue still has work, the app **keeps polling even when you are idle or your
workstation is locked**. Normally polling pauses entirely while you are away; draining the queue
overnight is the whole point of an auto-scheduler, so the pause is skipped - and only the pause.
Poll cadence, reset alignment, and notification deferral are unchanged.

---

## `auto-scheduler-state.json`

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

- Written atomically: a temp file in the same directory, then an `os.replace`.
- The record is written when the task **starts** (`finished_at: null`) and rewritten on exit.
- **Any id present in `tasks` is complete.** One-shot means once, ever. Re-running a task
  requires a new `id`, or deleting its record by hand.
- A record with `started_at` and no `finished_at` found at startup is an **interrupted** run:
  the app died mid-task. It counts as completed and is never re-run - an unattended agent that
  got halfway must not be silently restarted.
- A state file that exists but cannot be parsed **disables the queue for the session** and
  shows an error. Treating it as empty would re-run every task.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The task finished successfully. |
| any other | Whatever `claude` returned. Recorded as-is; there are no retries. |
| `124` | The task hit `timeout_minutes` and was killed. |
| `130` | You quit the app and chose to stop the running task(s). |

Any finished run - success, failure, or timeout kill - marks the task complete. There are no
retries, so a permanently failing task can never loop and burn quota. Failures raise no
notification: automatic events stay silent by design. The exit code is visible in the tray
submenu and in the state file.

---

## Logs

`auto-scheduler-logs/<id>-<YYYYMMDD-HHMMSS>.log` holds the merged stdout and stderr of one run.
Task ids are sanitized for file-system use.

```
=== task daily ===
started: 2026-08-10T22:04:11.179586+00:00
command: ['C:\\Users\\me\\AppData\\Roaming\\npm\\claude.cmd', '-p', 'Read agent_system/...', '--dangerously-skip-permissions']
cwd:     D:\Projecten\_AI_\trading-agent
timeout: 180 min

...the task's own output...

=== finished: 2026-08-10T23:51:02.351087+00:00 ===
exit code: 0
duration:  6411s
```

One-shot semantics bound growth to one log per task, so there is no retention or rotation
policy. If the log directory cannot be created or the file cannot be opened, the task is **not**
started, an error is shown once, and the queue is disabled for the session - a task whose output
would be lost is worse than a task that never ran.

---

## Settings

Two keys in `auto-scheduler-settings.json`:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_concurrent_tasks` | int | `1` | How many queued tasks may run at the same time. Must be `>= 1`. |
| `task_command` | array of strings | `[]` | Base command tasks are launched with. Empty means auto-detect the native CLI. |

`task_command` exists so a WSL-hosted CLI can run tasks:

```json
{
  "task_command": ["wsl", "claude"]
}
```

It is an argv list, not a shell string - `"wsl claude"` is rejected rather than guessed at. It is
deliberately separate from `cli_command`, which stays display-only and never takes part in
authentication or token refresh.

With `max_concurrent_tasks` above `1`, several tasks can start from the *same* quota snapshot,
before any of their consumption is visible. That is accepted: the setting exists precisely to
allow it, and per-task thresholds are how you express caution.

---

## Tray menu

A **Task queue** submenu appears above "Test event commands" when a task file is loaded, with
one entry per task:

```
<glyph> <description or id> - <status>
```

| Glyph | Status | Meaning |
|---|---|---|
| ○ | waiting | Five-hour utilization is still above the task's `max_usage_percent`. |
| ◔ | pending | The threshold is met, but every concurrency slot is occupied. |
| ▶ | running | A child process is live. |
| ✔ | done | Finished, with its exit code shown. |
| ✖ | done | Finished with a non-zero exit code. |
| ⚠ | interrupted | Started but never finished; the app died mid-run. |

Status is re-rendered every time the menu opens. The task *set* is fixed at startup, because the
registry is read once. Clicking a finished task opens its log; clicking a waiting, pending, or
running one does nothing (the entry is greyed out).

---

## Quitting with tasks running

Quitting while a task is live asks what to do. The Windows button captions are fixed, so the
message spells out each outcome:

- **Yes** - quit now and stop the running task(s). They are recorded with exit code `130`.
- **No** - drain: let the running task(s) finish, start no new ones, and quit when the last one
  ends. The tray tooltip shows a draining marker meanwhile.
- **Cancel** - do not quit.

"Restart" from the tray menu routes through the same dialog, and cancelling it also cancels the
restart.

Killing a task terminates only the direct `claude` child. Any grandchildren it spawned are not
walked and may survive - one more reason for the print-mode caveat, which tells the agent never
to end its turn with background work unfinished.

---

## Environment

Each task inherits the current environment plus:

| Variable | Value |
|---|---|
| `AUTO_SCHEDULER_EVENT` | `task` |
| `AUTO_SCHEDULER_TASK_ID` | The task's `id` |
| `AUTO_SCHEDULER_VERSION` | The app version |
| `AUTO_SCHEDULER_UTILIZATION_<FIELD>` | Utilization per detected quota field, e.g. `..._FIVE_HOUR` |
| `AUTO_SCHEDULER_RESETS_AT_<FIELD>` | Reset timestamp per quota field |
| `AUTO_SCHEDULER_EXTRA_USED` / `_LIMIT` | Extra usage, when enabled (limit only when capped) |

This is the same quota snapshot the startup and double-click [event
commands](event-commands.md) receive, so a task can inspect the quota state it was started with.

---

## Troubleshooting

**Nothing ever starts.** Check the tray submenu. All entries "waiting" means five-hour
utilization has not dropped to any task's `max_usage_percent` yet - the tooltip shows the
current value. No submenu at all means no task file was found in the effective config
directory, or the file was rejected (you would have seen a message box at startup).

**A task ran once and will not run again.** That is one-shot behavior. Delete its record from
`auto-scheduler-state.json`, or give the task a new `id`.

**A task shows "interrupted".** The app was closed or crashed while the task was running. It is
not restarted automatically; remove its record to let it run again.

**Edits to the task file do nothing.** The registry is read once at startup. Restart the app.

**The queue stopped working after an error box.** Any state-file or log-file failure disables
the queue for the rest of the session. Fix the reported path and restart.
