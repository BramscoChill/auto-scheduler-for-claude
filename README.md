# Auto Scheduler for Claude

[![Feature Ideas](https://img.shields.io/badge/Feature_Ideas-Vote_%26_Discuss-blue?style=for-the-badge&logo=github)](https://github.com/BramscoChill/auto-scheduler-for-claude/discussions/categories/ideas)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ff69b4?style=for-the-badge&logo=github)](https://github.com/sponsors/jens-duttke)

**Watch your Claude rate limits in real time - and spend the leftovers automatically.**

A native Windows tray app that shows your Claude usage at a glance and runs queued Claude Code tasks whenever there is spare quota - lightweight, portable, and fully auditable. Rate limits are shared across claude.ai, Claude Code, Claude Code Cowork, and IDE extensions for VS Code and JetBrains - always know how much of your session and weekly limits (Sonnet, Opus, Fable, Cowork, and any future quota types) you have left, and let the [task queue](#task-queue) put the rest to work.

![Detail popup showing account info and usage bars](screenshot.png)

> [!TIP]
> **Companion tool: [Agent Monitor for Claude](https://github.com/jens-duttke/agent-monitor-for-claude)**
>
> Auto Scheduler for Claude tells you *how much* of your rate limits you have left. Its companion tool, [**Agent Monitor for Claude**](https://github.com/jens-duttke/agent-monitor-for-claude), tells you *what your agents are actually doing* - a live overview of every running Claude Code agent across all your projects: which ones are working, waiting for your input, blocked, finished, or errored, refreshed every few seconds. Agents are grouped by project with the ones that need attention floated to the top, each with its estimated cost, token breakdown, model, and host - and one click brings any agent's window to the foreground. If you run more than one agent at a time, it turns "which window was that again?" into a glance at the tray.
>
> You can even [launch it with a double-click on the tray icon](docs/event-commands.md#launch-agent-monitor-for-claude-on-double-click).

## Features

- **Portable** - single EXE (~12.5 MB), no installation, no Electron, no runtime required. Download, place anywhere, run. To uninstall, delete the file
- **Zero configuration** - authenticates through your existing Claude Code login, no API key or manual token entry needed
- **Live tray icon** with two [configurable](docs/configuration.md#tray-icon-bars) progress bars (session + weekly by default) or, via the `icon_style` setting, both values as stacked percentages, plus [configurable tooltip](docs/configuration.md#tooltip-fields), percentage display, and theme-aware colors for light and dark taskbars
- **Detail popup** (left-click) showing account info, dynamically detected usage bars for all active quota types (Session, Weekly, Sonnet, Opus, Fable, Cowork, and any new quotas Anthropic adds) with [configurable field selection](docs/configuration.md#popup-fields), extra usage, reset countdowns, and a stale-data indicator when values may be outdated; pin it open and move it while pinned to keep usage details visible during long sessions, and [configure a compact pinned view](docs/configuration.md#compact-pinned-view) that hides the sections and bars you do not need. Reset times follow your Windows 24-hour or 12-hour clock format automatically
- **Claude Code versions** - the popup shows which version is installed in each environment (native CLI, VS Code, Cursor, Windsurf), making it easy to spot when your IDE extension is ahead of or behind the CLI. Run Claude Code somewhere the app cannot see it, such as WSL? Add it with the [`cli_command`](docs/configuration.md#claude-cli-command) setting and its version is listed alongside the rest
- **Smart alerts** - configurable threshold notifications per quota type, with time-aware mode that only alerts when usage outpaces elapsed time. Reset notifications when a nearly exhausted quota refills. Extra usage can also alert on absolute spending amounts (e.g. $50 / $100 / $150 spent), the only alert available when it has no monthly limit
- **[Task queue](#task-queue)** - queue Claude Code prompts and let them run unattended whenever your five-hour quota drops to the level you set for each one. No wall-clock schedule that burns tokens at a fixed hour or sits idle while quota goes unused: a task starts because there is room for it. Each task runs once, logs everything it printed, and reports its exit code. Manage the whole queue from the popup: expand it to see every task against your live quota line - with a marker showing what each one is waiting for - add, edit and delete tasks without hand-editing JSON, watch a running task's log stream in, and stop it if it goes wrong
- **[Event commands](docs/event-commands.md)** - run a custom shell command when a quota resets, a usage threshold is crossed, the app starts up, or you double-click the tray icon. Send push notifications to your phone, resume an AI agent, start a fresh 5-hour session automatically, play an alert sound, launch a companion tool like [Agent Monitor for Claude](https://github.com/jens-duttke/agent-monitor-for-claude) on double-click, or trigger any custom workflow
- **Time marker** on each bar showing elapsed time in the current period - in the detail popup and on the tray icon bars - so you can instantly see whether your usage is ahead of or behind the clock; bars that outpace the clock turn red, in the popup and tray alike
- **Automatic token refresh** - when the OAuth session expires, runs `claude update` in the background to renew the token without user intervention. If a CLI update is installed, shows a notification (which you can turn off via the `notify_claude_update` setting)
- **Adaptive polling** - speeds up during active usage, pauses when the computer is idle or locked, aligns to imminent quota resets, and backs off on rate-limit errors. Switching your Claude account refreshes the tray immediately, so it never lingers on the previous account's usage
- **Multi-account** - monitor several Claude accounts side by side: launch one instance per account with `--config-dir="<path>"` pointing at each account's Claude config directory. Each tray icon shows its account's usage, with a `[dir-name]` tooltip prefix, per-instance settings, and its own autostart entry
- **13 languages** (English, German, French, Spanish, Portuguese, Italian, Japanese, Korean, Hindi, Indonesian, Chinese Simplified, Chinese Traditional, Ukrainian) - auto-detected from your Windows display language, with optional manual override via the `language` setting
- **[Customizable](docs/configuration.md)** - optionally override polling intervals, colors, alert thresholds, and more via a JSON settings file

---

## Security & Transparency

This tool handles your Claude Code OAuth token, so you should be able to verify it is safe. The codebase is deliberately structured for easy auditing:

- **Single network destination** - communicates exclusively with `api.anthropic.com`, no other hosts
- **Credentials stay local** - the OAuth token is used only in HTTP Authorization headers, never logged, stored elsewhere, or transmitted to third parties
- **Writes almost nothing** - until you create a task the app writes no files at all. With a task queue it writes exactly four paths inside your Claude config directory: `auto-scheduler-state.json`, the `auto-scheduler-logs/` directory, one log file per run, and `auto-scheduler-tasks.json` itself - the last only when you add, edit, or delete a task in the popup, never on its own. Nothing else on disk is touched. The only lasting registry changes are two `HKEY_CURRENT_USER` values: the toast notification identity (display name and icon, re-registered on every start) and the autostart entry, written only when you enable autostart. An expired OAuth token additionally triggers `claude update`, which may install a newer Claude Code version
- **Unattended tasks are opt-in and loud about it** - the [task queue](#task-queue) runs only what you put in `auto-scheduler-tasks.json`, and it runs those tasks with `--dangerously-skip-permissions`. That flag is hardcoded and cannot be turned off; see [Safety](#safety) before you queue anything
- **No dynamic code execution** - no `eval()`, `exec()`, `compile()`, or dynamic imports
- **No obfuscation** - no encoded strings, no hidden URLs, no minified logic
- **Modular architecture** - small, focused modules with security-critical code (credentials, API calls) isolated in a single file ([`api.py`](auto_scheduler_for_claude/api.py))
- **Minimal runtime dependencies** - only four well-known packages: [requests](https://pypi.org/project/requests/), [Pillow](https://pypi.org/project/pillow/), [pystray](https://pypi.org/project/pystray/), [pywebview](https://pypi.org/project/pywebview/)

---

## Requirements

- **Windows 10 or Windows 11** (64-bit)
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** installed and logged in (CLI, VS Code extension, or JetBrains plugin - any variant works). The app reads the OAuth token that Claude Code stores locally (`~/.claude/.credentials.json`). If you have `CLAUDE_CONFIG_DIR` set, the app uses that directory instead, and the `--config-dir="<path>"` command-line parameter overrides both - useful to run one instance per Claude account (log each extra account in via Claude Code with `CLAUDE_CONFIG_DIR` pointing to its own directory first).

> [!TIP]
> If the token expires, the app automatically runs `claude update` to refresh it. If the token is missing entirely, the app shows a notification and a "!" icon - log in to Claude Code and the monitor picks it up automatically.

---

## Quick Start

**No Python required.** Download the latest [**AutoSchedulerForClaude.exe**](https://github.com/BramscoChill/auto-scheduler-for-claude/releases/latest), place it wherever you like, and run it. To remove, disable "Start with Windows" in the context menu first (if enabled), then delete the file.

---

## How to Use

| Action | What happens |
|---|---|
| **Hover** over the tray icon | Tooltip shows 5h and 7d usage percentages with reset times |
| **Left-click** the tray icon | Opens the detail popup with account info and all usage bars |
| **Double-click** the tray icon | Runs the [`on_double_click_command`](docs/event-commands.md) if configured (e.g. launch [Agent Monitor for Claude](https://github.com/jens-duttke/agent-monitor-for-claude)); otherwise does nothing |
| **Right-click** the tray icon | Context menu: open popup, autostart toggle, [task queue](#task-queue) status (when configured), test event commands, restart, GitHub link, or quit |
| **Escape** or click outside | Closes the detail popup |

### Tray icon not visible?

Windows may hide new tray icons by default. To keep the icon always visible:

1. Right-click the **taskbar** → **Taskbar settings**
2. Expand **Other system tray icons** (Win 11) or **Select which icons appear on the taskbar** (Win 10)
3. Toggle **AutoSchedulerForClaude** to **On**

### Reading the progress bars

Each bar in the detail popup has up to four visual elements:

1. **Blue fill** - how much of the limit you have used
2. **Time dividers** - subtle gaps splitting the session bar into equal hour sections and marking local midnights on the weekly bars, visually grouping usage into hour and day segments
3. **White vertical line** - how much *time* has passed in the current period. The fill turns **red** when it passes this marker, warning that you may hit the limit before the period resets.
4. **Reset text** - when the limit resets, shown as a countdown with clock time

---

## Configuration

All settings work out of the box - no configuration file is needed. To customize behavior, create a file called `auto-scheduler-settings.json` with only the keys you want to change:

```json
{
  "poll_interval": 180,
  "bar_fg": "#00cc66",
  "bar_fg_warn": "#ff6600"
}
```

The app searches for this file in these locations (first match wins):

1. **`$CLAUDE_CONFIG_DIR/auto-scheduler-settings.json`** (when a custom config directory is set via `--config-dir` or `CLAUDE_CONFIG_DIR`) - so each instance can have its own settings
2. **Next to the EXE** (or project root when running from source)
3. **`~/.claude/auto-scheduler-settings.json`**

The app never creates or modifies this file. See [Configuration](docs/configuration.md) for all available settings (alert thresholds, polling intervals, colors, language, and more).

---

## Task Queue

### What the task queue does

Queue Claude Code prompts and the app runs them unattended, as `claude -p`, whenever your five-hour quota has enough headroom. There is no wall-clock schedule: instead of "run at 00:00", the rule is "run when there is quota", so nothing fires while quota is scarce and nothing sits idle while quota goes unused.

Each task runs **once, ever**. It gets its own log file, its exit code is recorded, and the tray menu shows where every task stands. The feature is off until you create the task file.

### Setting it up

Create `auto-scheduler-tasks.json` in your **effective config directory** - `CLAUDE_CONFIG_DIR` when set (or `--config-dir="<path>"`), otherwise `~/.claude`. Running one instance per Claude account therefore gives each account its own queue automatically.

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
    "prompt_file": "prompts/research-tick.md",
    "cwd": "D:/Projecten/_AI_/trading-agent",
    "max_usage_percent": 20,
    "timeout_minutes": 0
  }
]
```

The file is read **once at startup**, so edits need an app restart - the same as the settings file. If a single entry is invalid, the app lists every problem in one dialog and disables the whole queue for the session: a partially loaded queue of unattended agents is worse than no queue.

### Task fields

| Field | Required | Type | Rules |
|---|---|---|---|
| `id` | yes | string | Non-empty, unique across the file. Key for the state record and the log name. |
| `prompt` | yes* | string | Non-empty. A print-mode caveat is appended automatically, telling the agent never to end its turn with background work unfinished. |
| `prompt_file` | yes* | string | Path to a file holding the prompt, for prompts too long to escape into JSON. Must exist and contain more than whitespace. |
| `max_usage_percent` | yes | number | `0`-`100` inclusive. The task runs when five-hour utilization is **at or below** this. |
| `cwd` | no | string | Working directory for the run. Must be an existing directory. Defaults to the app's working directory. |
| `timeout_minutes` | no | number | Default `180`. **`0` means no time limit.** Must be `>= 0`. |
| `description` | no | string | Tray-menu label only. Falls back to `id` when omitted. |

\* Give a task a `prompt` or a `prompt_file`. If you give it **both, `prompt_file` wins and `prompt` is ignored entirely** - a leftover draft can never run instead of the file you pointed at. A relative `prompt_file` resolves against the task's `cwd`, or against the config directory when the task has no `cwd`; the file is read once at startup, so editing it later needs an app restart.

### Understanding `max_usage_percent`

Five-hour utilization is how much of the rolling five-hour quota is **already consumed** - the same percentage the tray icon shows. `0` means the window just reset and is untouched; `100` means it is exhausted.

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

The number answers one question: *"how much of my five-hour quota am I willing to have already spent before letting this unattended task loose?"* Low (10) is conservative - the task runs only on a nearly fresh window. High (90) is permissive - it runs whenever any quota is left. It is a ceiling on consumption, so the gate protects your headroom for interactive work.

Three consequences worth keeping in mind:

- **`<=`, not `<`.** Equality counts, which is what makes `max_usage_percent: 0` usable ("only on a completely fresh window"). With a strict `<` it would be unsatisfiable and the entry would be dead config.
- **It gates the start, not the run.** A task that begins at 38% can drive the window to 95% while it works. Nothing is re-checked mid-run; the next poll simply starts nothing else above the line. So heavy tasks want low thresholds (10-20) and cheap ones high (80-90).
- **Threshold plus file order gives priority for free.** Selection walks the file top to bottom and *skips past* any task whose threshold is not met, so a cheap high-threshold task naturally claims a slot an expensive low-threshold task cannot yet use.

Only the five-hour quota gates execution. A task can start at 5% five-hour usage while your weekly quota sits at 99%.

### Running more than one task at a time

By default one task runs at a time. Raise it with the `max_concurrent_tasks` setting in `auto-scheduler-settings.json`:

```json
{
  "max_concurrent_tasks": 2
}
```

Be aware that several tasks starting from the *same* quota snapshot cannot see each other's consumption until the next poll - two tasks launched at 10% may together push the window far past either one's threshold. The setting exists precisely to allow that; per-task thresholds are how you express caution.

If your Claude CLI lives somewhere the app cannot launch directly (for example inside WSL), point the queue at it with `task_command`, an argv list:

```json
{
  "task_command": ["wsl", "claude"]
}
```

### Where results go

- **`auto-scheduler-state.json`** in the same config directory records each task's `started_at`, `finished_at`, `exit_code`, and log file name. Any id present in it is complete - re-running a task means giving it a new `id` or deleting its record by hand.
- **`auto-scheduler-logs/<id>-<timestamp>.log`** holds the merged stdout and stderr of one run, with a header (command, working directory, start time) and a footer (exit code, duration).
- **The tray "Task queue" submenu** shows one entry per task, refreshed every time you open the menu:

  | Glyph | Status | Meaning |
  |---|---|---|
  | ○ | waiting | Five-hour utilization is still above this task's `max_usage_percent`. |
  | ◔ | pending | The threshold is met, but every concurrency slot is occupied. |
  | ▶ | running | A child process is live. |
  | ✔ / ✖ | done | Finished, with its exit code shown. |
  | ⚠ | interrupted | Started but never finished - the app died mid-run. It is never restarted automatically. |

  **Clicking a finished task opens its log.** Waiting, pending, and running entries are greyed out.

- **The popup's task queue** - expand the popup (the ⇱ button in its header) for the full picture: every task against your live five-hour quota, with a marker on the bar showing the level each one is waiting for, filters for active and finished tasks, and a per-task detail view with the run's start time, elapsed time, the quota level it started at, and its log streaming in live. A running task can be stopped from there.

Exit code `124` means the task hit its `timeout_minutes` and was killed; `130` means you quit the app and chose to stop everything; `143` means you stopped that one task from the popup. There are no retries, so a permanently failing task can never loop and burn quota, and failures raise no notification - automatic events stay silent by design.

While the queue still has work, the app keeps polling even when your computer is idle or locked. Normally polling pauses entirely while you are away; draining the queue overnight is the whole point.

### Quitting with tasks running

If you quit while a task is live, the app asks what to do. The Windows button captions are fixed, so the message spells out each outcome:

- **Yes** - quit now and stop the running task(s), recorded with exit code `130`.
- **No** - let them finish, start no new ones, and quit when the last one ends. The tooltip shows a draining marker meanwhile.
- **Cancel** - do not quit.

"Restart" routes through the same dialog, and cancelling it cancels the restart too.

### Safety

> [!WARNING]
> Queued tasks run with `--dangerously-skip-permissions`. The flag is hardcoded and cannot be turned off - it is what makes unattended execution possible at all. Every queued task can read, write, and delete files and run commands in its working directory **without asking you first**. Only queue prompts you would be comfortable letting run unsupervised, and set `cwd` to the project they belong to.

Using the task queue is also the only situation in which this app writes files. It writes exactly four paths, all inside your Claude config directory:

1. `auto-scheduler-state.json`
2. the `auto-scheduler-logs/` directory
3. one log file per run inside it
4. `auto-scheduler-tasks.json` - **only** when you add, edit, or delete a task in the popup

The first three are written in the background as tasks run. The task file is different: it is yours, and the app touches it only in direct response to a click in the editor. It refuses to write if the file changed on disk since the editor opened it, so an edit you made in a text editor meanwhile is never silently overwritten, and it validates the whole file first, so a bad edit can never disable your queue on the next start.

Nothing else on disk is touched. Killing a task terminates only the direct `claude` child - grandchildren it spawned are not walked and may survive.

See [Task Queue](docs/task-queue.md) for the full reference: validation rules, the state file format, the log format, the environment variables each task receives, and troubleshooting.

---

## Building from Source

<details>
<summary>For developers who want to build the EXE themselves</summary>

### Prerequisites

- Python 3.10+
- pip

### Setup

```bash
git clone https://github.com/BramscoChill/auto-scheduler-for-claude.git
cd auto-scheduler-for-claude
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Run

```bash
python -m auto_scheduler_for_claude
```

### Build EXE

```bash
python build.py
```

Produces `dist/AutoSchedulerForClaude.exe` (~12.5 MB), a single-file executable that bundles Python and all dependencies.

### Popup UI Development

The popup UI lives in [`auto_scheduler_for_claude/popup/`](auto_scheduler_for_claude/popup/) as separate HTML, CSS, and JS files. To preview and iterate on the UI without running the full app:

```bash
start http://localhost:8080/auto_scheduler_for_claude/popup/dev.html && python -m http.server 8080
```

This starts a local server and opens the dev preview in your default browser. Use the buttons to switch between data presets (full, minimal, error, loading) and the language dropdown to preview every locale (so you can spot strings that overflow the popup width). Test CSS/JS changes with instant feedback.

### Create a Release

1. Update dependencies: `pip install --upgrade -r requirements.txt`
2. Update `__version__` in [`auto_scheduler_for_claude/__init__.py`](auto_scheduler_for_claude/__init__.py) and the version in [`version_info.py`](version_info.py) (`filevers`, `prodvers`, `FileVersion`, `ProductVersion`)
3. Update `_FALLBACK_USER_AGENT` in [`auto_scheduler_for_claude/api.py`](auto_scheduler_for_claude/api.py) to the current Claude Code version
4. In [`CHANGELOG.md`](CHANGELOG.md), rename `## [Unreleased]` to `## [1.x.x] - YYYY-MM-DD` and add a fresh empty `## [Unreleased]` section above it
5. Run the test suite: `python -m unittest discover -s tests`
6. Smoke test: `python -m auto_scheduler_for_claude` - verify tray icon, popup, and settings
7. Build the EXE with `python build.py`
8. Smoke test: `dist\AutoSchedulerForClaude.exe` - verify tray icon, popup, and settings
9. Stage the changes from steps 2 to 4
10. Commit, tag, push, and publish:

   ```bash
   git commit -m "Release v1.x.x"
   git tag v1.x.x
   git push origin main v1.x.x
   gh release create v1.x.x dist/AutoSchedulerForClaude.exe --title "v1.x.x" --notes "<release notes from CHANGELOG.md, followed by a [README for this version](https://github.com/BramscoChill/auto-scheduler-for-claude/blob/v1.x.x/README.md) link>"
   ```

</details>

---

## Contributing

Contributions are welcome - whether it's bug reports, feature ideas, or pull requests. [Open an issue](https://github.com/BramscoChill/auto-scheduler-for-claude/issues) to report bugs or ask questions. For feature ideas, browse and vote on existing proposals or submit your own in [Ideas](https://github.com/BramscoChill/auto-scheduler-for-claude/discussions/categories/ideas).

<details>
<summary>For developers who want to contribute to the project</summary>

This project is developed with [Claude Code](https://docs.anthropic.com/en/docs/claude-code). The [`.claude/CLAUDE.md`](.claude/CLAUDE.md) file contains all project conventions, coding standards, and architectural guidelines - Claude Code applies these automatically during development.

### Workflow

1. Read `.claude/CLAUDE.md` to understand the project conventions
2. Implement your changes with Claude Code - it will follow the guidelines automatically
3. Before committing, run the `/review` slash command to perform a systematic quality review of all staged changes (code, tests, documentation)
4. Stage remaining fixes if any, then run `/commit-message` to generate a properly formatted commit message

### Adding features

New features should follow the existing architecture. Key points from the guidelines:

- Security-critical code (credentials, API calls) stays isolated in [`api.py`](auto_scheduler_for_claude/api.py)
- All user-facing changes need updates in [`CHANGELOG.md`](CHANGELOG.md), [`README.md`](README.md), and [`docs/configuration.md`](docs/configuration.md) where applicable
- Tests are required - run `python -m unittest discover -s tests` before committing
- The app must never write files to disk - the only system state it changes is the two documented `HKEY_CURRENT_USER` registry values (notification identity, autostart entry)

</details>

---

## License

MIT

---

## Disclaimer

This is an independent, community-built project. It is **not** created, endorsed, or officially supported by [Anthropic](https://www.anthropic.com/). "Claude" and "Anthropic" are trademarks of Anthropic, PBC. Use of these names is solely for descriptive purposes to indicate compatibility.
