let els;
let statusState = {};
let translations = {};
let textTimerId = null;
let popupPinned = false;
let popupExpanded = false;
let compactHide = [];
let lastData = null;

// Which of the three expanded views is showing.  Only one at a time: the
// detail and editor views replace the overview rather than growing the window
// further, which is already at the monitor's usable width.
let view = 'queue';
let queueFilter = 'all';
let detailTaskId = null;
let elapsedTimerId = null;

// Task editor state.  `entries` and `stamp` come from the registry read the
// edit is based on; the stamp is what lets the save refuse to overwrite a file
// that changed on disk in the meantime.
let formState = { index: -1, stamp: [0, -1], entries: [], completedIds: [], promptMode: 'inline' };

// Log tailing for the detail view.
let logOffset = 0;
let logTimerId = null;
let logPinnedToBottom = true;

/**
 * Set CSS custom properties for theme colors and inject translation strings.
 *
 * Called once by Python after the page loads.  Translations are set as
 * textContent on heading elements so the HTML file stays language-neutral.
 *
 * @param {object} config - { colors, t (translations), app_version, data (initial snapshot) }
 */
function init(config) {
    const s = document.documentElement.style;
    for (const [key, value] of Object.entries(config.colors)) {
        s.setProperty(`--${key.replaceAll('_', '-')}`, value);
    }

    translations = config.t;
    compactHide = config.compact_hide || [];
    popupExpanded = !!config.expanded;
    document.getElementById('title').textContent = translations.title;
    document.getElementById('headingAccount').textContent = translations.account;
    document.getElementById('labelEmail').textContent = translations.email;
    document.getElementById('labelPlan').textContent = translations.plan;
    document.getElementById('headingUsage').textContent = translations.usage;
    document.getElementById('headingExtraUsage').textContent = translations.extra_usage;
    document.getElementById('headingClaudeCode').textContent = translations.claude_code;
    document.getElementById('headingQueue').textContent = translations.task_queue;
    document.getElementById('headingDetailLog').textContent = translations.queue_detail_log;

    const changelogLink = document.getElementById('changelogLink');
    changelogLink.textContent = translations.changelog;
    changelogLink.addEventListener('click', () => pywebview.api.open_url());
    document.getElementById('closeBtn').addEventListener('click', () => pywebview.api.close());
    setupPinButton();
    setupPinnedDrag();
    setupExpandButton();

    document.getElementById('appVersion').textContent = config.app_version;

    els = {
        accountSection: document.getElementById('accountSection'),
        emailRow: document.getElementById('emailRow'),
        emailValue: document.getElementById('emailValue'),
        planRow: document.getElementById('planRow'),
        planValue: document.getElementById('planValue'),
        gateSection: document.getElementById('gateSection'),
        gatePct: document.getElementById('gatePct'),
        gateCaption: document.getElementById('gateCaption'),
        gateBarContainer: document.getElementById('gateBarContainer'),
        gateFill: document.getElementById('gateFill'),
        gateMarkers: document.getElementById('gateMarkers'),
        usageSection: document.getElementById('usageSection'),
        headingUsage: document.getElementById('headingUsage'),
        usageBars: document.getElementById('usageBars'),
        extraSection: document.getElementById('extraSection'),
        extraSpent: document.getElementById('extraSpent'),
        extraPct: document.getElementById('extraPct'),
        extraBarContainer: document.getElementById('extraBarContainer'),
        extraFill: document.getElementById('extraFill'),
        queueSection: document.getElementById('queueSection'),
        queueSummary: document.getElementById('queueSummary'),
        queueNotice: document.getElementById('queueNotice'),
        queueFilters: document.getElementById('queueFilters'),
        queueEmpty: document.getElementById('queueEmpty'),
        queueTable: document.getElementById('queueTable'),
        queueTableHead: document.getElementById('queueTableHead'),
        queueRows: document.getElementById('queueRows'),
        newTaskBtn: document.getElementById('newTaskBtn'),
        detailSection: document.getElementById('detailSection'),
        detailName: document.getElementById('detailName'),
        detailPath: document.getElementById('detailPath'),
        detailStatus: document.getElementById('detailStatus'),
        detailStats: document.getElementById('detailStats'),
        detailLog: document.getElementById('detailLog'),
        detailLogName: document.getElementById('detailLogName'),
        detailBack: document.getElementById('detailBack'),
        detailOpenLog: document.getElementById('detailOpenLog'),
        detailStop: document.getElementById('detailStop'),
        formSection: document.getElementById('formSection'),
        headingForm: document.getElementById('headingForm'),
        formErrors: document.getElementById('formErrors'),
        idWarning: document.getElementById('idWarning'),
        permissionWarning: document.getElementById('permissionWarning'),
        promptModes: document.getElementById('promptModes'),
        fieldId: document.getElementById('fieldId'),
        fieldDescription: document.getElementById('fieldDescription'),
        fieldPrompt: document.getElementById('fieldPrompt'),
        fieldPromptFile: document.getElementById('fieldPromptFile'),
        fieldCwd: document.getElementById('fieldCwd'),
        fieldThreshold: document.getElementById('fieldThreshold'),
        fieldTimeout: document.getElementById('fieldTimeout'),
        thresholdValue: document.getElementById('thresholdValue'),
        thresholdHint: document.getElementById('thresholdHint'),
        formDelete: document.getElementById('formDelete'),
        formCancel: document.getElementById('formCancel'),
        formSave: document.getElementById('formSave'),
        installSection: document.getElementById('installSection'),
        installRows: document.getElementById('installRows'),
        statusSection: document.getElementById('statusSection'),
        statusText: document.getElementById('statusText'),
    };

    setupQueue();
    setupDetail();
    setupForm();

    updateData(config.data);
    requestAnimationFrame(() => document.body.classList.add('open'));
}

function setupExpandButton() {
    const expandBtn = document.getElementById('expandBtn');

    function render() {
        document.body.classList.toggle('expanded', popupExpanded);
        expandBtn.classList.toggle('active', popupExpanded);
        expandBtn.setAttribute('aria-pressed', popupExpanded ? 'true' : 'false');
        const label = popupExpanded ? translations.queue_collapse : translations.queue_expand;
        expandBtn.setAttribute('aria-label', label);
        expandBtn.title = label;
    }

    expandBtn.addEventListener('click', () => {
        popupExpanded = !popupExpanded;
        // Collapsing from a subview would otherwise leave the compact popup
        // pointing at a task detail it has no room to show.
        view = 'queue';
        render();
        reapplyData();
        pywebview.api.set_expanded(popupExpanded).catch(() => {});
    });

    render();
}

function setupPinButton() {
    const pinBtn = document.getElementById('pinBtn');

    function render() {
        document.body.classList.toggle('pinned', popupPinned);
        pinBtn.classList.toggle('pinned', popupPinned);
        pinBtn.setAttribute('aria-pressed', popupPinned ? 'true' : 'false');
        pinBtn.setAttribute('aria-label', popupPinned ? translations.unpin_popup : translations.pin_popup);
        pinBtn.title = popupPinned ? translations.unpin_popup : translations.pin_popup;
    }

    pinBtn.addEventListener('click', () => {
        const nextPinned = !popupPinned;
        popupPinned = nextPinned;
        render();
        reapplyData();
        pywebview.api.set_pinned(nextPinned).then((applied) => {
            popupPinned = !!applied;
            render();
            reapplyData();
        }).catch(() => {
            popupPinned = !nextPinned;
            render();
            reapplyData();
        });
    });

    render();
}

/**
 * Return true if a section or usage bar is hidden by the pinned compact view.
 *
 * Hiding only applies while the popup is pinned; unpinned it always shows
 * everything.  `key` is a section key (account, extra_usage, claude_code,
 * status) or a usage field name (e.g. seven_day_opus).
 */
function compactHidden(key) {
    return popupPinned && compactHide.includes(key);
}

// Re-render the last snapshot so compact hiding takes effect on pin toggle.
function reapplyData() {
    if (lastData) {
        updateData(lastData);
    }
}

function setupPinnedDrag() {
    const header = document.querySelector('header');
    let dragging = false;

    function setDragging(active) {
        dragging = active;
        header.classList.toggle('dragging', active);
    }

    header.addEventListener('mousedown', (event) => {
        if (!popupPinned || event.button !== 0 || event.target.closest('button')) {
            return;
        }
        event.preventDefault();
        setDragging(true);
        pywebview.api.begin_drag().then((started) => {
            setDragging(!!started);
        }).catch(() => {
            setDragging(false);
        });
    });

    document.addEventListener('mousemove', (event) => {
        if (!dragging) {
            return;
        }
        // No button held (e.g. released outside the window): stop dragging.
        if (event.buttons === 0) {
            setDragging(false);
            pywebview.api.end_drag();
            return;
        }
        pywebview.api.drag().catch(() => {});
    });

    document.addEventListener('mouseup', () => {
        if (!dragging) {
            return;
        }
        setDragging(false);
        pywebview.api.end_drag();
    });
}

/**
 * Update all popup sections with fresh data from Python.
 *
 * @param {object} data - Pre-formatted snapshot from _snapshot_to_dict().
 */
function updateData(data) {
    lastData = data;

    const queue = data.queue;
    const overview = !popupExpanded || view === 'queue';
    const queueVisible = popupExpanded && overview && !!queue && !compactHidden('task_queue');
    const gate = queueVisible ? queue.gate : null;

    const hasProfile = !!data.profile;
    const accountVisible = hasProfile && overview && !compactHidden('account');
    els.accountSection.classList.toggle('visible', accountVisible);
    if (hasProfile) {
        els.emailValue.textContent = data.profile.email;
        els.emailRow.style.display = data.profile.email ? '' : 'none';
        els.planValue.textContent = data.profile.plan;
        els.planRow.style.display = data.profile.plan ? '' : 'none';
    }

    els.gateSection.classList.toggle('visible', !!gate);
    if (gate) {
        updateGate(gate, queue.markers || []);
    }

    // The headline bar already shows the field the queue is gated on; keeping
    // it in the list below would show the same quota twice.
    const usage = (data.usage || [])
        .filter((entry) => !compactHidden(entry.key))
        .filter((entry) => !gate || entry.key !== gate.key);
    const hasUsage = !!usage.length && overview;
    els.usageSection.classList.toggle('visible', hasUsage);
    if (hasUsage) {
        updateUsageBars(usage);
    }

    const hasExtra = !!data.extra;
    const extraVisible = hasExtra && overview && !compactHidden('extra_usage');
    els.extraSection.classList.toggle('visible', extraVisible);
    if (hasExtra) {
        els.extraSpent.textContent = data.extra.spent_text;
        els.extraPct.style.display = data.extra.has_limit ? '' : 'none';
        els.extraPct.textContent = data.extra.pct_text;
        els.extraBarContainer.style.display = data.extra.has_limit ? '' : 'none';
        els.extraFill.style.width = `${data.extra.fill_pct * 100}%`;
    }

    els.queueSection.classList.toggle('visible', queueVisible);
    if (queueVisible) {
        updateQueue(queue);
    }

    els.detailSection.classList.toggle('visible', popupExpanded && view === 'detail');
    if (popupExpanded && view === 'detail') {
        updateDetail(queue);
    }

    els.formSection.classList.toggle('visible', popupExpanded && view === 'form');

    const hasInstalls = !!data.installations?.length;
    const installsVisible = hasInstalls && overview && !compactHidden('claude_code');
    els.installSection.classList.toggle('visible', installsVisible);

    // The "Usage" heading only labels the bars against the other sections;
    // when the usage bars stand alone, drop the now-redundant heading.
    const barsStandAlone = hasUsage && !accountVisible && !extraVisible && !installsVisible && !gate && !queueVisible;
    els.headingUsage.style.display = barsStandAlone ? 'none' : '';

    if (hasInstalls) {
        els.installRows.replaceChildren(...data.installations.map((inst) => {
            const row = document.createElement('div');
            const dt = document.createElement('dt');
            dt.textContent = inst.name;
            const dd = document.createElement('dd');
            dd.textContent = inst.version;
            row.append(dt, dd);
            return row;
        }));
    }

    updateStatus(data.status);
    syncElapsedTimer();
}

/**
 * Update the status footer with live timer data or static text.
 *
 * Live mode (has last_success_time): starts a 1-second interval for
 * the text counter.  Static mode (has text): shows plain text.
 */
function updateStatus(status) {
    if (textTimerId) {
        clearInterval(textTimerId);
        textTimerId = null;
    }

    if (!status) {
        els.statusSection.classList.remove('visible');
        return;
    }

    // Keep the live timer running even when the footer is hidden in compact
    // view, so the stale-dimming of the usage bars still updates.
    els.statusSection.classList.toggle('visible', !compactHidden('status'));

    if (status.last_success_time !== undefined) {
        statusState = {
            lastSuccessTime: status.last_success_time,
            nextPollTime: status.next_poll_time,
            refreshing: status.refreshing,
            error: status.error,
        };
        els.statusSection.classList.toggle('error', !!status.error);
        tickStatusText();
        textTimerId = setInterval(tickStatusText, 1000);
    } else {
        statusState = {};
        els.statusText.textContent = status.text || '';
        els.statusText.title = status.is_error ? (status.text || '') : '';
        els.statusSection.classList.toggle('error', !!status.is_error);
    }
}

/**
 * Build and display the status text from current state.
 *
 * < 60s:  "Updated Xs ago"
 * >= 60s: "Updated Xm ago · Next update in Ym"
 * + refreshing or error appended with · separator
 */
function tickStatusText() {
    if (!statusState.lastSuccessTime) return;

    const now = Date.now() / 1000;
    const secondsAgo = Math.max(0, Math.floor(now - statusState.lastSuccessTime));
    const isStale = !!statusState.nextPollTime && (now > statusState.nextPollTime + 30);
    els.usageSection.classList.toggle('stale', isStale);
    els.extraSection.classList.toggle('stale', isStale);

    const parts = [formatDuration(secondsAgo)];

    if (statusState.refreshing) {
        parts.push(translations.status_refreshing);
    } else if (statusState.error) {
        parts.push(statusState.error);
    } else if (secondsAgo >= 60 && statusState.nextPollTime) {
        const secondsUntil = Math.max(0, Math.floor(statusState.nextPollTime - now));
        if (secondsUntil > 0) {
            parts.push(translations.status_next_update.replace('{duration}', formatCountdown(secondsUntil)));
        }
    }

    els.statusText.textContent = parts.join(' \u00b7 ');
    // Errors are raw API messages that can overflow; reveal the full text on hover.
    els.statusText.title = statusState.error ? els.statusText.textContent : '';
}

/**
 * Format seconds into a localized "Updated Xs ago" / "Updated Xm ago" string.
 */
function formatDuration(totalSeconds) {
    if (totalSeconds < 60) {
        return translations.status_updated_s.replace('{s}', totalSeconds);
    }

    const totalMin = Math.floor(totalSeconds / 60);
    const hours = Math.floor(totalMin / 60);
    const mins = totalMin % 60;

    let duration;
    if (hours > 0) {
        duration = translations.duration_hm.replace('{h}', hours).replace('{m}', mins);
    } else {
        duration = translations.duration_m.replace('{m}', totalMin);
    }
    return translations.status_updated.replace('{duration}', duration);
}

/**
 * Format a countdown in seconds into a localized duration string.
 */
function formatCountdown(totalSeconds) {
    if (totalSeconds < 60) {
        return translations.duration_s.replace('{s}', totalSeconds);
    }

    const totalMin = Math.ceil(totalSeconds / 60);
    const hours = Math.floor(totalMin / 60);
    const mins = totalMin % 60;

    if (hours > 0) {
        return translations.duration_hm.replace('{h}', hours).replace('{m}', mins);
    }
    return translations.duration_m.replace('{m}', totalMin);
}

function updateUsageBars(entries) {
    // Rebuild whenever the field set changes, not only the count - after an
    // account switch the same number of bars can carry different quotas, and
    // an in-place update would show the new values under the old labels.
    const bars = els.usageBars.children;
    const sameFields = entries.length === bars.length
        && entries.every((entry, i) => bars[i].dataset.key === entry.key);

    if (!sameFields) {
        els.usageBars.replaceChildren(...entries.map(createBarElement));
        requestAnimationFrame(() => {
            for (let i = 0; i < entries.length; i++) {
                els.usageBars.children[i].querySelector('.bar-fill').style.width =
                    `${entries[i].fill_pct * 100}%`;
            }
        });
    } else {
        for (let i = 0; i < entries.length; i++) {
            updateBarElement(els.usageBars.children[i], entries[i]);
        }
    }
}

function createBarElement(entry) {
    const div = document.createElement('div');
    div.className = 'usage-entry';
    div.dataset.key = entry.key;

    const header = document.createElement('div');
    header.className = 'bar-header';
    const label = document.createElement('span');
    label.textContent = entry.label;
    const pct = document.createElement('span');
    pct.className = 'bar-pct';
    pct.textContent = entry.pct_text;
    header.append(label, pct);

    const container = document.createElement('div');
    container.className = 'bar-container';
    const fill = document.createElement('div');
    fill.className = 'bar-fill';
    fill.classList.toggle('warn', entry.warn);
    fill.style.width = '0%';
    container.appendChild(fill);

    for (const pos of entry.dividers) {
        const d = document.createElement('div');
        d.className = 'bar-divider';
        d.style.left = `calc(${pos * 100}% - 1px)`;
        container.appendChild(d);
    }

    if (entry.marker_rel !== null) {
        const marker = document.createElement('div');
        marker.className = 'bar-marker';
        marker.style.left = `calc(${entry.marker_rel * 100}% - 1px)`;
        container.appendChild(marker);
    }

    div.append(header, container);

    if (entry.reset_text) {
        const reset = document.createElement('div');
        reset.className = 'reset-text';
        reset.textContent = entry.reset_text;
        div.appendChild(reset);
    }

    return div;
}

function updateBarElement(div, entry) {
    div.querySelector('.bar-pct').textContent = entry.pct_text;

    const fill = div.querySelector('.bar-fill');
    fill.style.width = `${entry.fill_pct * 100}%`;
    fill.classList.toggle('warn', entry.warn);

    const container = div.querySelector('.bar-container');
    let marker = container.querySelector('.bar-marker');
    if (entry.marker_rel !== null) {
        if (!marker) {
            marker = document.createElement('div');
            marker.className = 'bar-marker';
            container.appendChild(marker);
        }
        marker.style.left = `calc(${entry.marker_rel * 100}% - 1px)`;
    } else if (marker) {
        marker.remove();
    }

    for (const d of container.querySelectorAll('.bar-divider')) d.remove();
    for (const pos of entry.dividers) {
        const d = document.createElement('div');
        d.className = 'bar-divider';
        d.style.left = `calc(${pos * 100}% - 1px)`;
        container.appendChild(d);
    }

    let resetEl = div.querySelector('.reset-text');
    if (entry.reset_text) {
        if (!resetEl) {
            resetEl = document.createElement('div');
            resetEl.className = 'reset-text';
            div.appendChild(resetEl);
        }
        resetEl.textContent = entry.reset_text;
    } else if (resetEl) {
        resetEl.remove();
    }
}

/* ---------------------------------------------------------------------------
   Task queue
   --------------------------------------------------------------------------- */

/**
 * Suspend the popup's click-outside dismissal while a subview owns the screen.
 *
 * Without it, one click anywhere else destroys the window - and with it a
 * half-typed task, or a log the user was reading.
 */
function setModal(active) {
    if (window.pywebview?.api?.set_modal) {
        pywebview.api.set_modal(active);
    }
}

function setupQueue() {
    const columns = [
        translations.queue_col_status, translations.queue_col_task, translations.queue_col_threshold,
        translations.queue_col_timeout, translations.queue_col_last_run, translations.queue_col_result,
    ];
    els.queueTableHead.replaceChildren(...columns.map((text) => {
        const cell = document.createElement('span');
        cell.textContent = text;
        return cell;
    }));

    els.newTaskBtn.textContent = translations.queue_new_task;
    els.newTaskBtn.addEventListener('click', () => openForm(-1));

    for (const [key, label] of [['all', 'queue_filter_all'], ['active', 'queue_filter_active'], ['done', 'queue_filter_done']]) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.dataset.filter = key;
        chip.dataset.label = translations[label];
        chip.addEventListener('click', () => {
            queueFilter = key;
            reapplyData();
        });
        els.queueFilters.appendChild(chip);
    }
}

function updateGate(gate, markers) {
    els.gatePct.textContent = gate.pct_text;
    els.gateCaption.textContent = [gate.caption, gate.reset_text].filter(Boolean).join(' · ');
    els.gateFill.style.width = `${gate.fill_pct * 100}%`;
    els.gateFill.classList.toggle('warn', gate.warn);

    for (const stale of els.gateBarContainer.querySelectorAll('.bar-divider')) stale.remove();
    for (const stale of els.gateBarContainer.querySelectorAll('.bar-marker')) stale.remove();
    for (const pos of gate.dividers || []) {
        const divider = document.createElement('div');
        divider.className = 'bar-divider';
        divider.style.left = `calc(${pos * 100}% - 1px)`;
        els.gateBarContainer.appendChild(divider);
    }
    if (gate.marker_rel !== null && gate.marker_rel !== undefined) {
        const marker = document.createElement('div');
        marker.className = 'bar-marker';
        marker.style.left = `calc(${gate.marker_rel * 100}% - 1px)`;
        els.gateBarContainer.appendChild(marker);
    }

    // Two rows, alternating: neighbouring thresholds would otherwise print
    // their labels on top of each other.
    els.gateMarkers.replaceChildren(...markers.map((marker, index) => {
        const wrapper = document.createElement('div');
        wrapper.className = index % 2 ? 'gate-marker stagger' : 'gate-marker';
        wrapper.classList.toggle('met', marker.met);
        wrapper.style.left = `${marker.rel * 100}%`;
        const dot = document.createElement('span');
        dot.className = 'dot';
        const label = document.createElement('span');
        label.textContent = `${marker.label} · ${marker.pct_text}`;
        wrapper.append(dot, label);
        return wrapper;
    }));
}

function updateQueue(queue) {
    els.queueSummary.textContent = queue.summary_text;

    let notice = '';
    if (queue.disabled) {
        notice = translations.queue_disabled;
    } else if (!queue.gate) {
        notice = translations.queue_no_gate;
    }
    els.queueNotice.textContent = notice;
    els.queueNotice.classList.toggle('visible', !!notice);
    els.queueNotice.classList.toggle('warn', !!queue.disabled);

    const counts = queue.counts || { all: 0, active: 0, done: 0 };
    for (const chip of els.queueFilters.children) {
        chip.textContent = `${chip.dataset.label} ${counts[chip.dataset.filter] ?? 0}`;
        chip.classList.toggle('active', chip.dataset.filter === queueFilter);
    }

    const all = queue.tasks || [];
    const shown = all.filter(matchesFilter);
    els.queueEmpty.textContent = all.length ? '' : translations.queue_empty;
    els.queueEmpty.style.display = all.length ? 'none' : '';
    els.queueTable.style.display = shown.length ? '' : 'none';
    els.queueRows.replaceChildren(...shown.map(createTaskRow));
}

function matchesFilter(task) {
    if (queueFilter === 'active') return task.active;
    if (queueFilter === 'done') return !task.active;
    return true;
}

function createTaskRow(task) {
    const row = document.createElement('div');
    row.className = 'table-row';
    row.dataset.id = task.id;

    const status = document.createElement('span');
    status.className = `pill ${statusClass(task)}`;
    status.textContent = `${task.glyph} ${task.status_text}`;

    const name = document.createElement('div');
    name.className = 'task-name';
    const title = document.createElement('span');
    title.className = 'ellipsis';
    title.textContent = task.name;
    const identifier = document.createElement('span');
    identifier.className = 'mono muted small ellipsis';
    identifier.textContent = task.cwd ? `${task.id} · ${task.cwd}` : task.id;
    name.append(title, identifier);

    const result = document.createElement('span');
    result.className = 'ellipsis muted result-cell';
    if (task.started_epoch) {
        result.dataset.started = String(task.started_epoch);
        result.textContent = liveElapsed(task.started_epoch);
    } else {
        result.textContent = task.result_text;
    }

    row.append(
        status, name,
        textCell(task.threshold_text), textCell(task.timeout_text),
        textCell(task.last_run_text || '-'), result,
    );
    row.addEventListener('click', () => openDetail(task.id));
    return row;
}

function textCell(text) {
    const cell = document.createElement('span');
    cell.className = 'ellipsis muted';
    cell.textContent = text;
    return cell;
}

function statusClass(task) {
    return task.failed ? 'st-failed' : `st-${task.status}`;
}

function liveElapsed(startedEpoch) {
    return formatElapsed(Date.now() / 1000 - startedEpoch);
}

/**
 * Format seconds as a localized short duration, mirroring format_duration().
 */
function formatElapsed(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    if (seconds < 60) {
        return translations.duration_s.replace('{s}', seconds);
    }

    const totalMin = Math.floor(seconds / 60);
    if (totalMin < 60) {
        return translations.duration_m.replace('{m}', totalMin);
    }
    return translations.duration_hm.replace('{h}', Math.floor(totalMin / 60)).replace('{m}', totalMin % 60);
}

/**
 * Run a 1-second ticker only while something is actually counting up.
 *
 * A running task's elapsed time is the one value that changes without any
 * push from Python, which only sends an update when the data itself changes.
 */
function syncElapsedTimer() {
    const running = (lastData?.queue?.tasks || []).some((task) => task.started_epoch);
    if (running && !elapsedTimerId) {
        elapsedTimerId = setInterval(tickElapsed, 1000);
    } else if (!running && elapsedTimerId) {
        clearInterval(elapsedTimerId);
        elapsedTimerId = null;
    }
}

function tickElapsed() {
    for (const cell of els.queueRows.querySelectorAll('.result-cell')) {
        if (cell.dataset.started) {
            cell.textContent = liveElapsed(Number(cell.dataset.started));
        }
    }
    if (els.detailElapsed?.dataset.started) {
        els.detailElapsed.textContent = liveElapsed(Number(els.detailElapsed.dataset.started));
    }
}

/* ---------------------------------------------------------------------------
   Task detail
   --------------------------------------------------------------------------- */

function setupDetail() {
    els.detailBack.textContent = translations.queue_detail_back;
    els.detailOpenLog.textContent = translations.queue_open_log;
    els.detailStop.textContent = translations.queue_stop;

    els.detailBack.addEventListener('click', closeDetail);
    els.detailOpenLog.addEventListener('click', () => pywebview.api.queue_open_log(detailTaskId));
    els.detailStop.addEventListener('click', stopDetailTask);
    els.detailLog.addEventListener('scroll', () => {
        const distance = els.detailLog.scrollHeight - els.detailLog.scrollTop - els.detailLog.clientHeight;
        logPinnedToBottom = distance < 20;
    });
}

function openDetail(taskId) {
    detailTaskId = taskId;
    view = 'detail';
    logOffset = 0;
    logPinnedToBottom = true;
    els.detailLog.textContent = '';
    setModal(true);
    reapplyData();
    pollLog();
}

function closeDetail() {
    stopLogPolling();
    detailTaskId = null;
    view = 'queue';
    setModal(false);
    reapplyData();
}

function updateDetail(queue) {
    const task = (queue?.tasks || []).find((entry) => entry.id === detailTaskId);
    if (!task) {
        // The task was deleted while its detail was open.
        closeDetail();
        return;
    }

    els.detailName.textContent = task.name;
    els.detailPath.textContent = task.cwd ? `${task.id} · ${task.cwd}` : task.id;
    els.detailStatus.className = `pill ${statusClass(task)}`;
    els.detailStatus.textContent = `${task.glyph} ${task.status_text}`;

    const stats = [
        [translations.queue_detail_started, task.started_text || '-'],
        [translations.queue_detail_elapsed, task.elapsed_text || '-'],
        [translations.queue_col_timeout, task.timeout_text],
        [translations.queue_detail_started_usage, task.started_usage_text || '-'],
    ];
    els.detailStats.replaceChildren(...stats.map(([label, value]) => {
        const wrapper = document.createElement('div');
        const term = document.createElement('dt');
        term.textContent = label;
        const definition = document.createElement('dd');
        definition.textContent = value;
        wrapper.append(term, definition);
        return wrapper;
    }));

    // Elapsed keeps counting while the detail is open.
    els.detailElapsed = els.detailStats.children[1].children[1];
    if (task.started_epoch) {
        els.detailElapsed.dataset.started = String(task.started_epoch);
        els.detailElapsed.textContent = liveElapsed(task.started_epoch);
    }

    els.detailLogName.textContent = task.log_name;
    els.detailOpenLog.disabled = !task.has_log;
    els.detailStop.style.display = task.can_stop ? '' : 'none';
    if (!task.has_log) {
        els.detailLog.textContent = translations.queue_detail_no_log;
    }

    if (task.can_stop && !logTimerId) {
        startLogPolling();
    } else if (!task.can_stop) {
        stopLogPolling();
    }
}

function startLogPolling() {
    if (!logTimerId) {
        logTimerId = setInterval(pollLog, 1000);
    }
}

function stopLogPolling() {
    if (logTimerId) {
        clearInterval(logTimerId);
        logTimerId = null;
    }
}

function pollLog() {
    if (!detailTaskId || !window.pywebview?.api?.queue_log) {
        return;
    }

    const taskId = detailTaskId;
    pywebview.api.queue_log(taskId, logOffset).then((chunk) => {
        // A slow reply can land after the user moved on to another task.
        if (!chunk || taskId !== detailTaskId) {
            return;
        }
        if (chunk.reset) {
            els.detailLog.textContent = chunk.text;
        } else if (chunk.text) {
            els.detailLog.textContent += chunk.text;
        }
        logOffset = chunk.offset;
        if (logPinnedToBottom) {
            els.detailLog.scrollTop = els.detailLog.scrollHeight;
        }
    }).catch(() => {});
}

function stopDetailTask() {
    const task = (lastData?.queue?.tasks || []).find((entry) => entry.id === detailTaskId);
    if (!task) {
        return;
    }

    const message = translations.queue_stop_confirm.replace('{name}', task.name);
    pywebview.api.confirm(message).then((confirmed) => {
        if (confirmed) {
            pywebview.api.queue_stop(task.id);
        }
    }).catch(() => {});
}

/* ---------------------------------------------------------------------------
   Task editor
   --------------------------------------------------------------------------- */

function setupForm() {
    els.formCancel.textContent = translations.queue_cancel;
    els.formDelete.textContent = translations.queue_delete;
    els.permissionWarning.textContent = translations.queue_permission_warning;
    els.permissionWarning.classList.add('visible');
    document.getElementById('labelTaskId').textContent = translations.queue_field_id;
    document.getElementById('labelTaskDescription').textContent = translations.queue_field_description;
    document.getElementById('labelTaskPrompt').textContent = translations.queue_field_prompt;
    document.getElementById('labelTaskCwd').textContent = translations.queue_field_cwd;
    document.getElementById('labelTaskThreshold').textContent = translations.queue_field_threshold;
    document.getElementById('labelTaskTimeout').textContent = translations.queue_field_timeout;
    document.getElementById('promptCaveat').textContent = translations.queue_prompt_caveat;
    document.getElementById('thresholdLow').textContent = translations.queue_threshold_low;
    document.getElementById('thresholdHigh').textContent = translations.queue_threshold_high;

    const browseFile = document.getElementById('browsePromptFile');
    const browseCwd = document.getElementById('browseCwd');
    browseFile.textContent = translations.queue_browse;
    browseCwd.textContent = translations.queue_browse;
    browseFile.addEventListener('click', () => {
        pywebview.api.browse_file(els.fieldCwd.value).then((path) => {
            if (path) els.fieldPromptFile.value = path;
        }).catch(() => {});
    });
    browseCwd.addEventListener('click', () => {
        pywebview.api.browse_folder(els.fieldCwd.value).then((path) => {
            if (path) els.fieldCwd.value = path;
        }).catch(() => {});
    });

    for (const [mode, label] of [['inline', 'queue_prompt_inline'], ['file', 'queue_prompt_file']]) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.dataset.mode = mode;
        chip.textContent = translations[label];
        chip.addEventListener('click', () => {
            formState.promptMode = mode;
            renderPromptMode();
        });
        els.promptModes.appendChild(chip);
    }

    els.fieldThreshold.addEventListener('input', renderThreshold);
    els.fieldId.addEventListener('input', renderIdWarning);
    els.formCancel.addEventListener('click', closeForm);
    els.formSave.addEventListener('click', saveForm);
    els.formDelete.addEventListener('click', deleteEntry);
}

function openForm(index) {
    pywebview.api.queue_registry().then((registry) => {
        if (registry.error) {
            showFormErrors(registry.error, []);
        } else {
            showFormErrors('', []);
        }

        formState.entries = registry.entries || [];
        formState.stamp = registry.stamp || [0, -1];
        formState.completedIds = registry.completed_ids || [];
        formState.index = index;

        const entry = index >= 0 ? (formState.entries[index] || {}) : {};
        // prompt_file wins over prompt in the registry, so the editor must
        // open in the mode that actually decides what runs.
        formState.promptMode = entry.prompt_file ? 'file' : 'inline';

        els.fieldId.value = entry.id || '';
        els.fieldDescription.value = entry.description || '';
        els.fieldPrompt.value = entry.prompt || '';
        els.fieldPromptFile.value = entry.prompt_file || '';
        els.fieldCwd.value = entry.cwd || '';
        els.fieldThreshold.value = entry.max_usage_percent ?? 40;
        els.fieldTimeout.value = entry.timeout_minutes ?? 180;

        els.headingForm.textContent = index >= 0 ? translations.queue_form_edit : translations.queue_form_new;
        els.formSave.textContent = index >= 0 ? translations.queue_save_edit : translations.queue_save;
        els.formDelete.style.display = index >= 0 ? '' : 'none';

        renderPromptMode();
        renderThreshold();
        renderIdWarning();

        view = 'form';
        setModal(true);
        reapplyData();
    }).catch(() => {});
}

function closeForm() {
    view = 'queue';
    setModal(false);
    reapplyData();
}

function renderPromptMode() {
    for (const chip of els.promptModes.children) {
        chip.classList.toggle('active', chip.dataset.mode === formState.promptMode);
    }
    const inline = formState.promptMode === 'inline';
    els.fieldPrompt.style.display = inline ? '' : 'none';
    els.fieldPromptFile.parentNode.style.display = inline ? 'none' : '';
}

function renderThreshold() {
    const threshold = Number(els.fieldThreshold.value);
    els.thresholdValue.textContent = `${threshold}%`;

    const gate = lastData?.queue?.gate;
    if (!gate) {
        els.thresholdHint.textContent = '';
        return;
    }

    const current = Math.round(gate.pct);
    // The same `<=` the queue itself uses, so the hint can never promise a
    // start the selector would not make.
    const template = gate.pct <= threshold ? translations.queue_threshold_hint : translations.queue_threshold_hint_wait;
    els.thresholdHint.textContent = template.replace('{pct}', current);
}

function renderIdWarning() {
    const taskId = els.fieldId.value.trim();
    const reused = !!taskId && formState.completedIds.includes(taskId);
    els.idWarning.textContent = reused ? translations.queue_warn_already_run : '';
    els.idWarning.classList.toggle('visible', reused);
}

/**
 * Build the registry entry from the form.
 *
 * Only fields the user filled in are emitted: an empty string would fail
 * validation, and the unused prompt key must be absent entirely rather than
 * blank - `prompt_file` wins whenever it is present at all.
 */
function buildEntry() {
    const entry = { id: els.fieldId.value.trim() };

    const description = els.fieldDescription.value.trim();
    if (description) entry.description = description;

    if (formState.promptMode === 'file') {
        entry.prompt_file = els.fieldPromptFile.value.trim();
    } else {
        entry.prompt = els.fieldPrompt.value.trim();
    }

    const cwd = els.fieldCwd.value.trim();
    if (cwd) entry.cwd = cwd;

    entry.max_usage_percent = Number(els.fieldThreshold.value);

    const timeout = els.fieldTimeout.value.trim();
    if (timeout !== '') entry.timeout_minutes = Number(timeout);

    return entry;
}

function saveForm() {
    els.formSave.disabled = true;
    pywebview.api.queue_save(buildEntry(), formState.index, formState.stamp).then((result) => {
        els.formSave.disabled = false;
        if (result.ok) {
            closeForm();
            return;
        }
        showFormErrors(result.error, result.errors || []);
    }).catch(() => {
        els.formSave.disabled = false;
    });
}

function deleteEntry() {
    const name = els.fieldDescription.value.trim() || els.fieldId.value.trim();
    pywebview.api.confirm(translations.queue_delete_confirm.replace('{name}', name)).then((confirmed) => {
        if (!confirmed) {
            return;
        }
        return pywebview.api.queue_delete(formState.index, formState.stamp).then((result) => {
            if (result.ok) {
                closeForm();
            } else {
                showFormErrors(result.error, result.errors || []);
            }
        });
    }).catch(() => {});
}

function showFormErrors(headline, errors) {
    const lines = [headline, ...errors.map((error) => error.message)].filter(Boolean);
    els.formErrors.textContent = lines.join('\n');
    els.formErrors.classList.toggle('visible', !!lines.length);
}

// Report content height changes to the host (pywebview or dev.html iframe parent).
new ResizeObserver(() => {
    const height = document.body.scrollHeight;
    if (window.pywebview?.api?.report_height) {
        pywebview.api.report_height(height);
    }
}).observe(document.body);
