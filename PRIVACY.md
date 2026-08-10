# Privacy Policy

**Auto Scheduler for Claude** is a local desktop application that monitors your Claude API usage and
can run Claude Code tasks you have queued yourself when spare quota is available.

## Data Collection

This application does **not** collect, store, or transmit any personal data.

## Network Communication

The application communicates exclusively with `api.anthropic.com` to retrieve your current API usage
data. No other network connections are made.

## Credentials

The application reads your existing Claude OAuth token from the local Claude CLI configuration file
(`~/.claude/.credentials.json`). This token is:

- Used solely in HTTP Authorization headers to authenticate with the Anthropic API
- Never logged, stored elsewhere, copied, or transmitted to any third party

## Local Storage

Usage data is kept in memory only and discarded when the application closes. The optional settings
file (`auto-scheduler-settings.json`) and the optional task file (`auto-scheduler-tasks.json`) are
read-only to the application.

Without a task file, the application writes no files at all. When you create a task file, it writes
exactly three paths, all inside your effective Claude config directory (`CLAUDE_CONFIG_DIR` when
set, otherwise `~/.claude`):

- `auto-scheduler-state.json` - which queued tasks have run, when they started and finished, their
  exit codes, and their log file names.
- `auto-scheduler-logs/` - the directory holding those logs.
- `auto-scheduler-logs/<task-id>-<timestamp>.log` - one file per run, containing the combined
  standard output and standard error of that run.

No other location on disk is written to.

Two values are written to the Windows registry, both under `HKEY_CURRENT_USER`:

- `Software\Classes\AppUserModelId\BramscoChill.AutoSchedulerForClaude` - the display name and icon
  shown in the header of the application's notifications. Re-registered on every start.
- `Software\Microsoft\Windows\CurrentVersion\Run` - the autostart entry. Written only when you
  enable autostart from the tray menu, removed when you disable it again.

## Task Execution

The task queue is off until you create `auto-scheduler-tasks.json` yourself. When it is on, the
application launches the Claude Code CLI as `claude -p "<your prompt>"
--dangerously-skip-permissions` for each task you queued, in the working directory you configured.

The `--dangerously-skip-permissions` flag is hardcoded and cannot be disabled - it is what allows a
task to run without anyone present to approve each step. A queued task can therefore read, write,
and delete files and run commands in its working directory without asking. Only queue prompts you
are willing to have run unsupervised.

Prompts and working directories are read from your local task file and passed to the local Claude
Code CLI. Whatever a task itself sends to Anthropic is governed by Claude Code, not by this
application.

## Claude Code Installation

When the OAuth token has expired, the application runs `claude update` so that the Claude Code CLI
renews the token in its own credentials file. As a side effect of that command, a newer Claude Code
version may be installed. No other software on your system is modified.

## Third-Party Services

The application does not integrate with any analytics, tracking, advertising, or telemetry services.

## Contact

For questions about this privacy policy, please open an issue at
https://github.com/BramscoChill/auto-scheduler-for-claude/issues
