# Implementation prompt - task queue in the popup (wireframes 1b, 1d, 1e)

Hand this document to a fresh session as the task description. It is self-contained: it names
every file to read, every constraint the change collides with, and the order to build in.

**Design source:** Claude Design project `97796d97-e6a5-453f-a62d-c5d476b7608c`, file
`Task Queue Wireframes.dc.html`, options `1b`, `1d`, `1e`. The wireframes are rendered by
`support.js`, a generic `dc-runtime` harness (`x-dc`, `sc-for`, `{{ }}`, `DCLogic.renderVals()`).
**None of that runtime is ported.** The wireframes are layout and copy references only; the
`renderVals()` data in the `.dc.html` is placeholder sample data, not a schema.

**Palette warning:** the wireframes are drawn in a warm light palette (`#f5ead8`, `#c67139`,
`Caprasimo`/`Figtree`). The live popup is themed entirely through CSS custom properties fed from
`settings.py` (`BG`, `FG`, `FG_DIM`, `FG_HEADING`, `FG_LINK`, `BAR_BG`, `BAR_FG`, `BAR_FG_WARN`,
`BAR_DIVIDER`, `BAR_MARKER`) and defaults to a dark theme. **Port the layout, not the colours or
the fonts.** Every colour in the new markup must resolve to an existing `--*` token. Do not add
colour settings keys; derive the status tones from what exists (see "Status tones" below).

---

## Read before writing any code

| File | Why |
|---|---|
| `auto_scheduler_for_claude/popup.py` | Window host, geometry, DPI, dismiss-watch, JS bridge, `_snapshot_to_dict()`, `_update_loop()` |
| `auto_scheduler_for_claude/popup/popup.html` / `popup.css` / `popup.js` | The current 340px popup: sections, `init()`, `updateData()`, bar rendering, `ResizeObserver` |
| `auto_scheduler_for_claude/tasks.py` | Pure registry load + validation + selection. `Task`, `_validate()`, `_resolve_prompt()`, `select_runnable()` |
| `auto_scheduler_for_claude/task_runner.py` | Side effects: processes, state file, logs, concurrency, `STATUS_*`, `EXIT_*` |
| `auto_scheduler_for_claude/app.py` (lines ~135, 181, 298-375, 661, 1087-1101) | `TaskRunner` wiring, the tray submenu, the quit dialog, `evaluate()` call site, idle bypass |
| `auto_scheduler_for_claude/settings.py` | Colour tokens, `POPUP_FIELDS`, `COMPACT_HIDE`, `MAX_CONCURRENT_TASKS`, `TASK_COMMAND` |
| `auto_scheduler_for_claude/formatting.py` | `expand_popup_fields`, `popup_label`, `field_period`, `elapsed_pct`, `time_until`, `divider_positions` |
| `docs/task-queue.md` | The behavioural contract this change extends |
| `.claude/CLAUDE.md` sections "Popup Window & DPI", "Quota Fields", "Task Queue", "Security & Transparency" | The rules below are quoted from here |
| `docs/AUTO_SCHEDULER_FOR_CLAUDE_TECHNICAL_REFERENCE.md` | Must be updated in the same task |

---

## Decisions already made - do not re-litigate

1. **1b is an expand toggle, not a replacement.** The popup keeps its 340px tray form by default;
   the header gains an expand control that switches the same window to the wide stacked layout.
2. **1d writes the registry - full CRUD.** The app may add, edit and delete entries in
   `auto-scheduler-tasks.json`. This is a deliberate, documented change to the audit story.
3. **1e includes "Stop task", with a new exit code** distinct from `130`, so the state file can
   still tell "the user quit and killed it" apart from "the user stopped this one task".

---

## Constraints this change collides with - handle each explicitly

### C1. The write-path enumeration (highest risk)

`CLAUDE.md` > Security & Transparency currently states:

> The app writes exactly three paths, all inside the effective config dir and all owned by the
> task queue (`task_runner.py`) [...] Everything else - settings, tasks, credentials - is
> read-only. [...] This exact list is repeated in `README.md` and `PRIVACY.md`; a fourth write
> path needs all three updated in the same change, because the enumeration is part of the audit
> story and must never become inaccurate.

Decision 2 creates that fourth write path. **In the same change, update all of:**

- `.claude/CLAUDE.md` - the enumeration, and the "Task Queue" section's pure/side-effect split
- `README.md` - the read-only claim and the task-queue feature description
- `PRIVACY.md` - the same enumeration
- `docs/task-queue.md` - the "Files" table (`auto-scheduler-tasks.json` is no longer "you" only)

The new enumeration is four paths: `auto-scheduler-state.json`, `auto-scheduler-logs/`,
`<id>-<timestamp>.log`, and `auto-scheduler-tasks.json`. Say plainly that the registry is written
**only** in response to an explicit user action in the popup, never by a background thread.

### C2. `tasks.py` must stay pure

`CLAUDE.md`: *"`tasks.py` loads and validates the registry and computes the selection (no
subprocesses, no writes) [...] Never move a subprocess or a write into `tasks.py`."*

`task_runner.py`'s charter is *processes, the state file, logs, concurrency, shutdown* - the
registry file is not in it. **Add a third module, `task_registry.py`**, that owns registry writes
and nothing else. The three-way split becomes:

- `tasks.py` - read, validate, select. Pure. Unchanged charter.
- `task_registry.py` - write the registry file atomically. Side effects, no processes.
- `task_runner.py` - processes, state, logs, concurrency, shutdown. Unchanged charter.

Update the "Task Queue" section of `CLAUDE.md` to describe the split as three modules.

### C3. Validation must not be duplicated in JavaScript

The form must round-trip through the exact validator the loader uses, or the popup will accept
entries that disable the whole queue on the next start.

- Promote `tasks._validate()` to public `tasks.validate_entries(entries)` and add it to `__all__`.
  Keep `load_tasks()` calling it. It must stay silent (it returns `(tasks, errors)`; the message
  box lives in `load_tasks`, not in the validator).
- The popup sends a raw entry dict; Python validates the **whole prospective file** (existing
  entries plus the new/edited one) so duplicate-`id` and cross-entry rules are enforced.
- JS may do cheap affordance-level hinting (empty field, non-numeric) but the authoritative
  errors always come back from Python and are rendered inline in the form.

### C4. Quota-field rules

`CLAUDE.md`: *"Never hardcode API quota field names in display logic"* and *"always use
`(data.get('key') or {})`"*.

- The **sub-bar grid** in 1b is display logic: build it from `expand_popup_fields(POPUP_FIELDS,
  usage)` exactly like `_usage_entries()` does today. Never enumerate fields.
- The **hero quota line** in 1b is queue logic, not display logic: the queue is gated on
  `five_hour` alone (`task_runner.evaluate()`, `docs/task-queue.md` > `max_usage_percent`), so the
  hero line legitimately reads `(usage.get('five_hour') or {})`. Mirror `evaluate()`'s guard: when
  `five_hour` is absent or null, hide the hero line and the markers and show the queue table with
  a "no five-hour quota reported - nothing will start" note. Do not fall back to another field.

### C5. pywebview geometry

`CLAUDE.md` > Popup Window & DPI: `resize()` and `move()` take **logical** pixels;
`_tray_position()` takes **physical** and returns **logical**; never replace them with raw
`SetWindowPos` outside the pinned-drag path. All of that stands. See "Expanding" below for the
one new wrinkle (`WIDTH` becoming per-instance).

### C6. Dismiss-watch versus a form

`popup.py._dismiss_watch()` closes the popup on click-outside, Escape, and foreground change. With
an open "New task" form that destroys unsaved input, and a native file dialog **is** a foreground
change. Handle before building the form (see "Modal suppression").

### C7. Locale parity

`tests/test_i18n.py::test_all_files_have_same_keys_as_english` fails unless every new key exists
in all 13 `locale/*.json` files. No per-field translation keys (`CLAUDE.md` > Quota Fields) - use
template keys with placeholders.

### C8. PyInstaller

`auto_scheduler_for_claude.spec` lists `popup.html`, `popup.css`, `popup.js` **individually**
(lines 16-18). Any new file under `popup/` needs its own `datas` entry.

---

## Shared plumbing (build this first)

### `TaskRunner` additions

All under the existing `self._lock`; the runner must keep returning `STATUS_*` keys and raw values
and must never import `i18n` or the app (`CLAUDE.md` > Task Queue).

```python
version -> int                       # bumped on every mutation; drives popup refresh
snapshot() -> list[dict[str, Any]]   # one row per task, taken under one lock hold
stop(task_id: str) -> bool           # user-initiated stop of one running task
reload() -> bool                     # swap _tasks after a registry write
log_tail(task_id, offset) -> dict    # incremental log read
```

**`version`** - a plain integer bumped in `_start`, `_finalize`, `stop`, `reload`, `_disable`, and
wherever `_last_five_hour_pct` changes a derived status. Needed because
`UsagePopup._update_loop()` (popup.py:550-577) dedups on `cache.snapshot.version` only, and a task
finishing changes nothing in the cache. Add the runner version to that dedup check.

**`snapshot()`** - must be one lock hold so the rows are mutually consistent. Per task:

| Key | Source |
|---|---|
| `id`, `description`, `cwd`, `max_usage_percent`, `timeout_minutes`, `prompt`, `prompt_file` | `Task` |
| `status` | `self._status(task)` - a `STATUS_*` key, not text |
| `exit_code` | `_records[id]['exit_code']` |
| `started_at`, `finished_at` | `_records[id]`, ISO strings |
| `started_at_utilization` | new optional state-record key, see below |
| `has_log` | `_records[id].get('log')` and the file exists |
| `elapsed_seconds` | for a running task, from `_Run.started_monotonic` |

Also expose `five_hour_pct` (`self._last_five_hour_pct`), `max_concurrent`, `running_count`, and
`disabled` at the top level of the popup payload so the header line ("1 running - 2 waiting -
1 slot free") and the "needs X% more headroom" cell can be computed without a second call.

**`started_at_utilization`** - 1e shows "Started at usage". It is not recorded today. Add it to the
state record in `_start()` (the value is already at hand: `(data.get('five_hour') or {})
.get('utilization')`) and to the log header. It is **additive and optional**: keep
`STATE_VERSION = 1`, and every reader must tolerate its absence (old records render "-").

**`stop()`** - mirror `kill_all()` (task_runner.py:214-232): set `run.forced_exit_code`, kill and
wait **outside** the lock, then `_finalize`. It must **not** set `_draining`. New constant:

```python
EXIT_STOPPED = 143    # user stopped this task from the popup (SIGTERM convention)
```

Add it to `__all__` and to the exit-code table in `docs/task-queue.md`. `130` keeps its current
meaning (the user quit the app and chose to stop everything).

**`reload()`** - re-run `load_tasks()` and swap `_tasks` under the lock. It must not touch
`_records`, `_running`, `_disabled`, or `_last_five_hour_pct`. If the reloaded registry is empty
because validation failed, keep the previous `_tasks` and report the failure to the caller rather
than silently emptying the queue. After a successful reload the tray submenu is stale
(`app.py:331-345` builds it once, and `docs/task-queue.md` documents the set as fixed at startup):
rebuild the menu and call `icon.update_menu()`, and update that sentence in `docs/task-queue.md`.

**`log_tail(task_id, offset)`** - security-sensitive. Resolve the path **only** through
`self._records[task_id]['log']` joined to `self._logs_dir()`; never accept a caller-supplied path,
and reject a resolved path that escapes the logs directory. Read with
`encoding='utf-8', errors='replace'` to match how the log is written (task_runner.py:311). First
call returns the last 64 KB and the new offset; later calls return only bytes past `offset`. On a
truncated/replaced file (offset beyond EOF) reset to a full tail.

### `task_registry.py` (new)

```python
__all__ = ['REGISTRY_MTIME_MISMATCH', 'read_registry', 'write_registry']
```

- `read_registry()` -> `(entries: list[dict], stamp: tuple[float, int])` - the raw JSON array plus
  an `(mtime, size)` stamp. Raw dicts, not `Task` objects: the form edits the file's own shape.
- `write_registry(entries, expected_stamp)` -> ok / error key. Rules:
  - **Refuse the write when the on-disk stamp no longer matches `expected_stamp`** - the user may
    have edited the file in an editor since the popup read it. Return a distinct error the popup
    renders as "the task file changed on disk - reopen the queue to reload it". Never merge.
  - Validate the prospective list with `tasks.validate_entries()` first. Never write a file that
    would disable the queue on the next start.
  - Atomic: temp file in the same directory then `os.replace`, exactly like
    `TaskRunner._write_state()` (task_runner.py:285-297).
  - `json.dumps(entries, indent=2, ensure_ascii=False)` + trailing newline. Hand formatting is
    normalized; document that in `docs/task-queue.md`.
  - Never write when the queue is `_disabled`.

### Popup data contract

Extend `_snapshot_to_dict()` with a `queue` key, and `_init_config()` with the new translations.
Keep the existing rule that Python pre-formats everything user-visible: JS receives text, not
`datetime` objects or `STATUS_*` keys needing lookup tables. Localization stays in Python
(`app.py` owns `STATUS_*` -> text today; the popup should use the same `T['task_state_*']` keys).

```
queue: {
  enabled, disabled, running_count, max_concurrent, free_slots,
  five_hour: { pct, pct_text, reset_text, present } | null,
  markers: [ { rel, label, met } ],
  counts: { all, active, done },
  tasks: [ {
      id, name, cwd, status, status_text, glyph,
      threshold_text, timeout_text, last_run_text, result_text, meta_text,
      has_log, can_stop, can_delete
  } ]
}
```

New pure formatting helpers go in `formatting.py` with numpydoc docstrings and unit tests:
elapsed duration, "needs {n}% more headroom", "exit {code} - {duration}".

### Status tones

No new settings keys. Map `STATUS_*` to existing tokens via CSS classes:

| Status | Token |
|---|---|
| running | `--bar-fg` |
| pending | `--fg` |
| waiting | `--fg-dim` |
| done, exit 0 | `--fg` on a `--bar-bg` pill |
| done, exit != 0 | `--bar-fg-warn` |
| interrupted | `--fg-dim` |

Glyphs stay the ones already documented in `docs/task-queue.md` > Tray menu
(`○ ◔ ▶ ✔ ✖ ⚠`), so the tray and the popup agree.

---

## Screen 1b - expanded popup with the task queue

Layout, top to bottom (see wireframe `#1b`):

1. **Header** - title, and `email · plan · vX.Y.Z` on the right. The existing pin and close
   buttons stay; the expand control is added to the same `.window-controls` group.
2. **Hero quota** - the five-hour percentage at display size, "of the five-hour window used ·
   resets in Xh Ym", then a full-width bar with:
   - the fill at the current percentage,
   - the existing time marker (`--bar-marker`, reuse `elapsed_pct`/`divider_positions`),
   - **one dot per task** at its `max_usage_percent`, labelled `<id> · <n>%`. A dot is "met" when
     `five_hour_pct <= max_usage_percent` (the same `<=` as `select_runnable`, tasks.py:162) and
     renders in the active tone; unmet dots use `--fg-dim`.
   - a `0% / 50% / 100%` scale line underneath.
3. **Sub-bars** - a two-column grid of the *remaining* quota fields, from `expand_popup_fields`
   minus the field used by the hero. Same fill/warn/divider/marker logic as `createBarElement()`.
4. **Task queue** - heading, `All N / Active N / Done N` filter chips, a `+ New task` button, and
   the table: `Status | Task | Runs at | Timeout | Last run | Result`. Rows are clickable and open
   1e. "Active" = waiting + pending + running; "Done" = done + interrupted.

### Expanding

- `UsagePopup.WIDTH` is a class constant read by `_resize_and_position()` (popup.py:635-637) and
  `_end_drag()` (popup.py:548). **Make it an instance attribute** (`self._width`) so both paths use
  the current width. Keep the class constants as the two presets
  (`WIDTH_COMPACT = 340`, `WIDTH_EXPANDED = 940`).
- New bridge method `set_expanded(expanded: bool)`. It takes `_geometry_lock`, sets `self._width`,
  and re-runs the resize/position path with the last reported height. JS toggles a body class and
  the `ResizeObserver` reports the new height as usual.
- **Clamp the expanded width** to the work area: `min(WIDTH_EXPANDED, work_width_logical - 2 *
  margin)`. On a 1366px or heavily-scaled display 940 logical pixels does not fit, and an
  off-screen popup with no taskbar button is unrecoverable. Compute from the same
  `GetMonitorInfoW` call `_tray_position()` already makes.
- **Expanded while pinned and moved**: `_resize_and_position()` returns early when
  `self._pinned and self._moved_while_pinned` (popup.py:638-639), so a pinned popup that grows by
  600px can run off the right edge. Add a clamp-only path for that case: keep the user's position
  but shift `x`/`y` back inside the work area if the new size pushes it out. Do this through
  `move()` in logical coordinates, not `SetWindowPos` - the raw-Win32 exception is the drag path
  only.
- **Persist for the process, not to disk**: settings are read-only. Store the flag on
  `AutoSchedulerForClaude` (e.g. `self._popup_expanded`) so reopening the popup from the tray
  keeps the last choice, and pass it into `_init_config()`. Do not write a settings key.
- **Compact/pinned view**: `compact_hide` only applies while pinned (popup.js:103-105). Expanded
  and compact are independent; leave `compactHidden()` alone. The queue section is only rendered
  when `queue.enabled`, and is hidden by the same rule as the other sections when the user lists it
  in `compact_hide` (add `task_queue` as a valid `compact_hide` key and document it in
  `docs/configuration.md`).

### Refresh

`_update_loop()` (popup.py:550-577) must also push when `task_runner.version` changes, not only on
`cache.snapshot.version` / `next_poll_time`. Keep the existing "commit the markers only after a
successful push" behaviour and the broad `except Exception: continue` - a pinned popup lives for
days.

A running task's elapsed time ticks in JS off `started_at`, like `tickStatusText()` already does
for the footer. Do not add a second Python poll for it.

---

## Screen 1d - add / edit task

A form rendered inside the popup (not a second window), over the expanded layout. Fields map 1:1
to the registry (see `docs/task-queue.md` > Fields, and `tasks._validate()`):

| Control | Field | Rules from `tasks.py` |
|---|---|---|
| Id | `id` | non-empty, unique across the file |
| Description | `description` | string, optional |
| Prompt (Inline / From file toggle) | `prompt` / `prompt_file` | exactly one is used; `prompt_file` wins unconditionally when present and the inline `prompt` is then not even type-checked - so the toggle must **remove** the unused key, never write both |
| Working directory | `cwd` | must be an existing directory; validated **before** the prompt, because a relative `prompt_file` resolves against it |
| Threshold slider | `max_usage_percent` | integer 0-100, `<=` at start only |
| Timeout | `timeout_minutes` | number `>= 0`, `0` = no limit, default 180 |

Copy that must appear, per `docs/task-queue.md` and `CLAUDE.md`:

- Under the prompt box: "The print-mode caveat is appended automatically."
- The warning banner: queued tasks run with `--dangerously-skip-permissions`, which is hardcoded
  and cannot be turned off. Locale key, not literal text.
- Live threshold hint: "Usage is X% right now, so this task would start on the next poll" -
  computed with the same `five_hour_pct <= max_usage_percent` comparison, and suppressed when there
  is no five-hour quota.

### One-shot warning (do not skip)

Any id present in `auto-scheduler-state.json` is complete forever
(`docs/task-queue.md` > State semantics). A user who adds a task with an id that already has a
state record will watch it never run. The form must warn at validation time: *"a run is already
recorded for this id - it will not start again until that record is removed"*. This is a warning,
not a hard error: the state file is the user's to edit.

### Edit and delete

- **Edit** reuses the same form, prefilled, and rewrites the entry in place (same index, so file
  order - which is queue priority - is preserved).
- **Delete a running task's entry: reject.** Tell the user to stop it first. The `_Run` holds a
  `Task` reference and the log header already names it; removing the registry entry under a live
  process makes the tray menu and the popup disagree with reality.
- **Delete a completed task's entry** leaves its state record and its log file untouched. Say so in
  the confirm dialog - the record is what enforces one-shot, and silently deleting it would make
  a re-added id run again unexpectedly.
- Deleting is destructive to a user-owned file: confirm before writing.

### Modal suppression (C6)

While the form is open, the popup must not be dismissed by a click outside, by Escape, or by a
foreground change:

- Add a `_modal` flag on `UsagePopup`, set from a bridge call when the form opens/closes.
- `_dismiss_watch._post_quit()` (popup.py:361-363) already gates on `self._pinned`; gate it on
  `self._pinned or self._modal` too, in all three hooks.
- Escape inside the form should close the *form*, handled in JS, not the window.
- The `cwd` and `prompt_file` browse buttons use `webview.create_file_dialog` (`FOLDER_DIALOG` and
  `OPEN_DIALOG`). A native dialog raises `EVENT_SYSTEM_FOREGROUND`; the `_modal` flag is what keeps
  `_delayed_fg_check()` from tearing the window down mid-dialog. Verify this by hand, not only by
  unit test - it is the most likely thing to break.

### Write flow

1. JS posts the entry -> Python builds the prospective list from `read_registry()` plus the edit.
2. `tasks.validate_entries()` -> errors are returned per field and rendered inline; nothing is
   written.
3. `task_registry.write_registry(entries, stamp)` - atomic, stamp-guarded.
4. `TaskRunner.reload()` -> rebuild the tray submenu -> bump `version` so the popup repaints.
5. The next `evaluate()` (after the next successful fetch) may start the new task. Do **not** call
   `evaluate()` directly from the write path: that would start an unattended agent from a UI click
   without a fresh quota snapshot, and it bypasses the `POLL_FAST` cooldown reasoning in
   `CLAUDE.md` > Polling & Reset Alignment.

---

## Screen 1e - task detail

Opened by clicking a row in 1b; replaces the queue panel with a back affordance.

- **Header** - `description or id`, then `id · cwd` in monospace, and the status pill.
- **Stats row** - Started, Elapsed, Timeout, Started at usage. Any of them may be unknown for an
  older record; render "-" rather than a zero.
- **Log** - the log file name, then a scrolling monospace view fed by `log_tail()`. Poll only while
  the detail view is open and the task is running (~1s), stop when it closes or the task finishes.
  Auto-scroll to the bottom unless the user has scrolled up. Never render log text as HTML.
- **Open log file** - reuse the tray path (`os.startfile`, app.py:369-375), guarded by
  `log_path() is not None`.
- **Stop task** - only for `STATUS_RUNNING`. Confirm first, and the confirm text must say what
  `docs/task-queue.md` already documents: killing a task terminates only the direct `claude` child;
  grandchildren it spawned are not walked and may survive. On confirm, `TaskRunner.stop(id)` records
  `EXIT_STOPPED` (143) and the row moves to Done.

---

## Documentation to update in the same change

| File | What |
|---|---|
| `.claude/CLAUDE.md` | Write-path enumeration (four paths); Task Queue section - three-module split, `EXIT_STOPPED`, registry is no longer read-once, `validate_entries` is the single validator |
| `README.md` | Read-only claim, the write enumeration, and the task-queue feature description (the feature list is tiered - the popup queue belongs under "Proactive protection" with the existing Task queue bullet, not as a new bullet) |
| `PRIVACY.md` | The same enumeration |
| `docs/task-queue.md` | Files table, exit-code table (`143`), state record's optional `started_at_utilization`, "read once at startup" is now "reloaded after an in-app edit", registry formatting is normalized on write, the tray "set is fixed at startup" sentence |
| `docs/configuration.md` | The new `task_queue` value for `compact_hide` |
| `docs/AUTO_SCHEDULER_FOR_CLAUDE_TECHNICAL_REFERENCE.md` | Re-read it in full first, then update the module map, the `tasks.py`/`task_runner.py` sections, add `task_registry.py`, the popup data contract, and Known limitations. Do not append a changelog entry and leave the rest stale |
| `CHANGELOG.md` | User-facing: expanded popup view, in-app task management, task detail with live log and stop. Use the `/changelog` command |
| `auto_scheduler_for_claude.spec` | A `datas` entry for every new file under `popup/` |
| `locale/*.json` (all 13) | Every new key, template-style |

---

## Tests

Run `python -m unittest discover -s tests` at the end; all must pass. Add:

- `tests/test_tasks.py` - `validate_entries` is public and behaves identically to the old private
  path; the prospective-file duplicate-id case.
- `tests/test_task_registry.py` (new) - atomic write; stamp mismatch refuses; invalid prospective
  list refuses; UTF-8 round-trip; `prompt`/`prompt_file` mutual exclusion is written correctly.
- `tests/test_task_runner.py` - `stop()` records 143 and does not set draining; `stop()` on a
  non-running id is a no-op; `reload()` swaps tasks without touching records/running; a failed
  reload keeps the previous registry; `version` bumps on every mutation; `snapshot()` shape;
  `log_tail()` offsets, truncation reset, and path-escape rejection; `started_at_utilization` is
  recorded and absent-tolerant.
- `tests/test_popup.py` - `queue` payload construction from a fake snapshot: no five-hour field,
  `five_hour: null`, zero tasks, disabled queue; marker `met` boundary at exactly the threshold;
  expanded-width clamping against a narrow work area.
- `tests/test_popup_js.py` - table rendering, filter chips, row -> detail, marker placement, and
  that a status/field-set change rebuilds rather than updates in place (the same class of bug
  `updateUsageBars()` guards against at popup.js:333-335).
- `tests/test_app.py` - the tray submenu is rebuilt after a reload.
- `tests/test_formatting.py` - the new duration/headroom/result helpers.

Cover the edge cases, not just the happy path (`CLAUDE.md` > Testing).

---

## Suggested order

1. **Plumbing** - `TaskRunner.version`/`snapshot()`/`reload()`/`stop()`/`log_tail()`,
   `EXIT_STOPPED`, `started_at_utilization`, `tasks.validate_entries`. Tests. No UI yet.
2. **1b read-only** - expand toggle, geometry and clamping, the `queue` payload, hero line,
   markers, sub-bars, table, filter chips. This is shippable on its own.
3. **1e** - detail view, log tail, Open log file, Stop task. Also shippable on its own.
4. **1d** - `task_registry.py`, the form, modal suppression, file dialogs, reload + tray rebuild.
   Ship this last: it is the one that changes the audit story, so it wants the most review.
5. **Docs and changelog** - C1's four-file update plus the technical reference.

Steps 2, 3 and 4 are each a coherent commit. Never create the commits yourself
(`CLAUDE.md` > Git) - suggest the messages and let the user commit.

---

## Out of scope

- Wireframes `1a` (two-column) and `1c`. `1b` was chosen instead.
- Any wall-clock scheduling. `docs/task-queue.md` states time-based scheduling is deliberately not
  ported; the trigger stays quota-only.
- Making `--dangerously-skip-permissions` or `PRINT_MODE_CAVEAT` opt-out. Both are hardcoded by
  design and disclosed in three documents.
- Re-running a completed task by clearing its state record from the UI. One-shot is absolute today
  (`CLAUDE.md` > Task Queue); if you want it, raise it as a separate decision rather than slipping
  it in with the CRUD form.
- Retries, notifications on task failure (automatic events stay silent), and any change to poll
  cadence, reset alignment, or the danger-window invariant.
