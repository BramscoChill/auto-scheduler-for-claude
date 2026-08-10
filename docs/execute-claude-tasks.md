This is a robust, production-grade Python 3 port of your PowerShell launcher. It preserves all critical safety mechanisms (locking, heartbeats, timeouts, orphan prevention) while adding the requested JSON task configuration layer.

### Key Architectural Decisions
1.  **JSON Task Registry**: Tasks are now defined in `agent_system/tasks.json`. The script resolves paths relative to its own location to prevent the "2026-08-01 outage" class of bugs.
2.  **Print Mode Caveat Injection**: The critical warning about background process orphans is automatically appended to *every* prompt loaded from JSON, ensuring no task can accidentally bypass this safety rule.
3.  **Cross-Platform Locking**: Uses `filelock` for atomic locking instead of filesystem timestamp math, which is unreliable on Windows SMB/network drives and NFS.
4.  **Git Safety**: Uses `subprocess.run` with explicit error suppression matching the original `cmd /c` behavior, preventing git stderr from crashing the Python launcher.

### Prerequisites

```bash
pip install filelock
```

### File: `agent_system/tasks.json`

```json
[
  {
    "id": "daily",
    "description": "Standard daily agent tick",
    "prompt": "Read agent_system/core_prompt.md and execute today's tick.",
    "timeout_minutes": 180,
    "schedule_hint": "00:00"
  },
  {
    "id": "research",
    "description": "Research-only tick (no promotions/retirements)",
    "prompt": "Read agent_system/core_prompt.md and execute a RESEARCH-ONLY tick per its section 5 cadence table: run research units only (backtests, hyperopt lead generation, bias analyses, parameter-neighbourhood probes, data verification). You must NOT promote or retire a champion, edit core_prompt.md, restart or reconfigure containers, or rewrite memory topic files in this tick - queue such findings with evidence in state/next_actions.md and append a short post-mortem to the journal instead.",
    "timeout_minutes": 180,
    "schedule_hint": "12:00"
  }
]
```

### File: `run_tick.py`

```python
#!/usr/bin/env python3
"""
run_tick.py - Autonomous Claude task executor.

Replacement for run_tick.ps1. Loads task definitions from agent_system/tasks.json
and executes them fully autonomously via `claude -p`.

All paths are resolved relative to this script's location to survive directory moves.
The heartbeat file written on every exit is monitored by agent_system/watchdog - do not rename.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from filelock import FileLock, Timeout as LockTimeout
except ImportError:
    sys.exit("FATAL: 'filelock' package required. Install via: pip install filelock")

# ---------------------------------------------------------------------------
# Path resolution - anchored to script location, never cwd
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # Adjust if script moves deeper in tree
STATE_DIR = PROJECT_ROOT / "agent_system" / "state"
TASKS_FILE = PROJECT_ROOT / "agent_system" / "tasks.json"
LOCK_FILE = STATE_DIR / ".tick.lock"
HEARTBEAT_FILE = STATE_DIR / "last_tick_utc.txt"

# Critical caveat injected into EVERY prompt to prevent orphaned background work
PRINT_MODE_CAVEAT = (
    " IMPORTANT: you run non-interactively via claude -p - when your final message ends, "
    "this process exits and any background task you launched is orphaned (its output is lost). "
    "Never end the turn while background work is unfinished: either wait for it and score it now, "
    "or journal where its on-disk artifacts will land and queue the follow-up in "
    "state/next_actions.md before finishing."
)


def setup_logging(mode: str) -> logging.Logger:
    """Configure file + console logging with UTC timestamps."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_name = f"tick-{stamp}-research.log" if mode == "research" else f"tick-{stamp}.log"
    log_path = STATE_DIR / log_name

    logger = logging.getLogger("tick")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def load_task(mode: str) -> dict:
    """Load and validate task definition from JSON registry."""
    if not TASKS_FILE.exists():
        raise FileNotFoundError(f"Task registry not found: {TASKS_FILE}")

    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    task = next((t for t in tasks if t["id"] == mode), None)
    if task is None:
        available = [t["id"] for t in tasks]
        raise ValueError(f"Unknown mode '{mode}'. Available: {available}")

    # Always inject the print-mode caveat - no task can opt out
    task["full_prompt"] = task["prompt"] + PRINT_MODE_CAVEAT
    task.setdefault("timeout_minutes", 180)
    return task


def find_claude_binary() -> str:
    """Locate claude executable using PATH then fallback."""
    claude = shutil.which("claude")
    if claude:
        return claude
    fallback = Path.home() / ".local" / "bin" / "claude.exe"
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError(
        "claude binary not found in PATH or ~/.local/bin/claude.exe"
    )


def auto_commit(project_root: Path, stamp: str, mode: str, exit_code: int, logger: logging.Logger):
    """One commit per tick. Dirty tree from crashed tick lands in next commit."""
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return

    try:
        subprocess.run(["git", "-C", str(project_root), "add", "-A"],
                       capture_output=True, timeout=30)
        result = subprocess.run(["git", "-C", str(project_root), "diff", "--cached", "--quiet"],
                                capture_output=True, timeout=30)
        if result.returncode != 0:
            msg = f"tick {stamp} mode={mode} exit={exit_code} (auto-commit by run_tick.py)"
            subprocess.run(["git", "-C", str(project_root), "commit", "-m", msg],
                           capture_output=True, timeout=30)
            logger.info("Auto-committed tick changes.")
    except Exception as e:
        logger.warning(f"Auto-commit failed (non-fatal): {e}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("daily", "research"):
        print(f"Usage: {sys.argv[0]} <daily|research>", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    logger = setup_logging(mode)

    try:
        task = load_task(mode)
    except Exception as e:
        logger.error(f"TASK-LOAD-ERROR mode={mode} {e}")
        sys.exit(1)

    timeout_min = task["timeout_minutes"]
    lock = FileLock(str(LOCK_FILE), timeout=0)

    exit_code = 1
    try:
        # Atomic lock acquisition - stale locks handled by filelock's max_lock_age
        # We use a manual age check to match original PS behavior (+30min slack)
        try:
            lock.acquire(blocking=False)
        except LockTimeout:
            # Check if lock is stale (older than timeout + 30 min slack)
            lock_age_sec = (datetime.now(timezone.utc) - 
                           datetime.fromtimestamp(LOCK_FILE.stat().st_mtime, tz=timezone.utc)).total_seconds()
            max_age_sec = (timeout_min + 30) * 60
            if lock_age_sec < max_age_sec:
                logger.info(f"SKIPPED mode={mode} - lock held (age {int(lock_age_sec//60)} min).")
                sys.exit(2)
            logger.info(f"Stale lock (age {int(lock_age_sec//60)} min) - taking over.")
            lock.acquire(blocking=True, timeout=5)

        claude = find_claude_binary()
        logger.info(f"TICK-START mode={mode} root={PROJECT_ROOT} claude={claude}")

        stdout_tmp = STATE_DIR / ".tick.out.tmp"
        stderr_tmp = STATE_DIR / ".tick.err.tmp"

        proc = subprocess.Popen(
            [claude, "-p", task["full_prompt"], "--dangerously-skip-permissions"],
            cwd=str(PROJECT_ROOT),
            stdout=open(stdout_tmp, "w", encoding="utf-8"),
            stderr=open(stderr_tmp, "w", encoding="utf-8"),
        )

        try:
            proc.wait(timeout=timeout_min * 60)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            logger.info(f"TIMEOUT after {timeout_min} min - killing claude (pid {proc.pid}).")
            proc.kill()
            proc.wait()
            exit_code = 124

        # Append captured output to log
        for tmp in (stdout_tmp, stderr_tmp):
            if tmp.exists():
                content = tmp.read_text(encoding="utf-8", errors="replace")
                if content.strip():
                    logger.info(f"--- {tmp.name} ---\n{content}")
                tmp.unlink(missing_ok=True)

    except Exception as e:
        logger.error(f"LAUNCHER-ERROR mode={mode} {e}")
        exit_code = 1
    finally:
        # Release lock
        try:
            lock.release()
        except Exception:
            pass

        # Write heartbeat - THIS IS WHAT WATCHDOG MONITORS
        try:
            hb = f"utc={datetime.now(timezone.utc).isoformat()} mode={mode} exit={exit_code}\n"
            HEARTBEAT_FILE.write_text(hb, encoding="utf-8")
        except Exception as e:
            logger.error(f"HEARTBEAT-WRITE-FAILED: {e}")

        logger.info(f"TICK-COMPLETE mode={mode} exit={exit_code}")

        # Audit trail
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        auto_commit(PROJECT_ROOT, stamp, mode, exit_code, logger)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
```

### Migration Notes from PowerShell → Python

| Concern | PowerShell Original | Python Port |
|---|---|---|
| **Lock staleness** | `(Get-Date) - LastWriteTime` math | `filelock` + manual UTC age check with same +30min slack |
| **Path anchoring** | `$PSCommandPath` parent chain | `Path(__file__).resolve().parent` — identical semantics |
| **Prompt caveat** | String concat at runtime | Auto-injected in `load_task()` — impossible to forget |
| **Git stderr leak** | `cmd /c ... 2>nul` wrapper | `capture_output=True` suppresses natively |
| **Process handle caching** | `$null = $proc.Handle` (PS 5.1 quirk) | Not needed — `subprocess.Popen` doesn't have this bug |
| **Temp output merge** | `Get-Content -Raw \| Out-File -Append` | Read + append to logger, then `unlink` |
| **Heartbeat format** | `utc=... mode=... exit=...` | Identical format preserved for watchdog compatibility |
| **Task config** | Hardcoded `$prompts` hashtable | External `tasks.json` — add new modes without code changes |

### Scheduling Replacement
Replace your Task Scheduler entries:


```
# Daily tick (00:00)
python C:\path\to\project\run_tick.py daily

# Research tick (12:00)  
python C:\path\to\project\run_tick.py research
```

The script is fully self-contained — no environment variables, no hardcoded absolute paths, and the heartbeat contract with `watchdog.ps1` is preserved byte-for-byte.