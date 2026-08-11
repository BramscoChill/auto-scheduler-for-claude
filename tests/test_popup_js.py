"""
Popup JS Tests
===============

Behavior tests for popup.js DOM update logic, executed with Node.js
against a minimal DOM stub.  Skipped when Node.js is not installed -
the app itself never needs Node; it is only used as a test runner
for the popup's JavaScript.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_POPUP_JS = Path(__file__).parent.parent / 'auto_scheduler_for_claude' / 'popup' / 'popup.js'

_NODE = shutil.which('node')

# Minimal DOM stub covering exactly the APIs the bar create/update path uses.
_DOM_STUB = r'''
class StubElement {
    constructor(tag) {
        this.tagName = tag;
        this.className = '';
        this.textContent = '';
        this.title = '';
        this.style = {};
        this.dataset = {};
        this.children = [];
        this.parentNode = null;
        const element = this;
        this.classList = {
            toggle(name, force) {
                const classes = element._classSet();
                const on = force === undefined ? !classes.has(name) : !!force;
                if (on) classes.add(name); else classes.delete(name);
                element.className = [...classes].join(' ');
                return on;
            },
            add(name) { const classes = element._classSet(); classes.add(name); element.className = [...classes].join(' '); },
            remove(name) { const classes = element._classSet(); classes.delete(name); element.className = [...classes].join(' '); },
            contains(name) { return element._classSet().has(name); },
        };
    }
    _classSet() { return new Set(this.className.split(/\s+/).filter(Boolean)); }
    appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
    append(...nodes) { for (const node of nodes) this.appendChild(node); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    remove() {
        if (this.parentNode) {
            const index = this.parentNode.children.indexOf(this);
            if (index >= 0) this.parentNode.children.splice(index, 1);
            this.parentNode = null;
        }
    }
    addEventListener(type, handler) { (this.listeners ??= {})[type] = handler; }
    matches(selector) { return selector.startsWith('.') && this._classSet().has(selector.slice(1)); }
    querySelector(selector) {
        for (const child of this.children) {
            if (child.matches(selector)) return child;
            const nested = child.querySelector(selector);
            if (nested) return nested;
        }
        return null;
    }
    querySelectorAll(selector) {
        const found = [];
        for (const child of this.children) {
            if (child.matches(selector)) found.push(child);
            found.push(...child.querySelectorAll(selector));
        }
        return found;
    }
}

globalThis.document = {
    createElement: (tag) => new StubElement(tag),
    body: new StubElement('body'),
};
globalThis.ResizeObserver = class { constructor() {} observe() {} };
globalThis.requestAnimationFrame = (callback) => callback();
'''

_SCENARIO_PRELUDE = r'''
els = {
    usageBars: document.createElement('div'),
    gateBarContainer: document.createElement('div'),
    gateFill: document.createElement('div'),
    gateMarkers: document.createElement('div'),
    gatePct: document.createElement('span'),
    gateCaption: document.createElement('span'),
    queueSummary: document.createElement('span'),
    queueNotice: document.createElement('div'),
    queueFilters: document.createElement('div'),
    queueEmpty: document.createElement('div'),
    queueTable: document.createElement('div'),
    queueRows: document.createElement('div'),
};

translations = {
    duration_s: '{s}s', duration_m: '{m}m', duration_hm: '{h}h {m}m',
    queue_empty: 'No tasks yet.', queue_disabled: 'Queue disabled.', queue_no_gate: 'No five-hour quota.',
    queue_filter_all: 'All', queue_filter_active: 'Active', queue_filter_done: 'Done',
};

function makeEntry(overrides) {
    return Object.assign({
        key: 'five_hour', label: '5h', pct_text: '0%', fill_pct: 0.0,
        warn: false, dividers: [], marker_rel: null, reset_text: '',
    }, overrides);
}

function makeTask(overrides) {
    return Object.assign({
        id: 'daily', name: 'Daily tick', cwd: '', status: 'waiting', status_text: 'waiting for quota',
        glyph: '○', failed: false, active: true, threshold_pct: 40, threshold_text: '≤ 40%',
        timeout_text: '180 min', last_run_text: '', result_text: 'needs 18% more headroom',
        started_text: '', started_epoch: null, elapsed_text: '', started_usage_text: '',
        log_name: '', has_log: false, can_stop: false, can_delete: true,
    }, overrides);
}

function makeQueue(tasks, overrides) {
    const list = tasks || [];
    const active = list.filter((task) => task.active).length;
    return Object.assign({
        disabled: false, draining: false, summary_text: 'summary',
        counts: { all: list.length, active: active, done: list.length - active },
        gate: { key: 'five_hour', pct: 38, pct_text: '38%', fill_pct: 0.38, warn: false,
                caption: 'Session (5hr) used', reset_text: 'Resets in 2h', dividers: [], marker_rel: 0.5 },
        markers: [], tasks: list,
    }, overrides);
}

// setupQueue() builds the filter chips; the stub has no addEventListener,
// so the scenarios build them the same way the real one does.
function buildFilterChips() {
    for (const [key, label] of [['all', 'queue_filter_all'], ['active', 'queue_filter_active'], ['done', 'queue_filter_done']]) {
        const chip = document.createElement('button');
        chip.dataset.filter = key;
        chip.dataset.label = translations[label];
        els.queueFilters.appendChild(chip);
    }
}
'''


def _run_scenario(scenario: str) -> dict:
    """Execute the DOM stub + popup.js + scenario with Node and parse its JSON output."""
    script = _DOM_STUB + _POPUP_JS.read_text(encoding='utf-8') + _SCENARIO_PRELUDE + scenario
    with TemporaryDirectory() as tmp:
        script_path = Path(tmp) / 'scenario.js'
        script_path.write_text(script, encoding='utf-8')
        # encoding is explicit: text=True would decode Node's UTF-8 output with
        # the locale codepage, which mangles the status glyphs into mojibake.
        proc = subprocess.run(
            [_NODE, str(script_path)], capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
    if proc.returncode != 0:
        raise AssertionError(f'Node scenario failed:\n{proc.stderr}')
    return json.loads(proc.stdout)


@unittest.skipUnless(_NODE, 'Node.js not available')
class TestUsageBarUpdates(unittest.TestCase):
    """Tests for updateUsageBars/updateBarElement in popup.js."""

    def test_changed_field_set_with_equal_count_updates_labels(self):
        """When the set of quota fields changes but the count stays the same
        (e.g. an account switch between plans), the bars must not show the new
        percentages under the old labels."""
        result = _run_scenario('''
updateUsageBars([
    makeEntry({ key: 'five_hour', label: '5h', pct_text: '10%' }),
    makeEntry({ key: 'seven_day', label: '7d', pct_text: '20%' }),
]);
updateUsageBars([
    makeEntry({ key: 'five_hour', label: '5h', pct_text: '30%' }),
    makeEntry({ key: 'seven_day_opus', label: '7d Opus', pct_text: '99%' }),
]);
console.log(JSON.stringify(els.usageBars.children.map((bar) => ({
    label: bar.children[0].children[0].textContent,
    pct: bar.querySelector('.bar-pct').textContent,
}))));
''')
        self.assertEqual(result, [
            {'label': '5h', 'pct': '30%'},
            {'label': '7d Opus', 'pct': '99%'},
        ])

    def test_marker_and_divider_positions_stable_across_update(self):
        """The 2 px marker/divider elements are centered with a -1px correction
        on create; an in-place update must use the identical expression, or the
        elements shift by 1 px after the first data update."""
        result = _run_scenario('''
const fields = { key: 'five_hour', label: '5h', marker_rel: 0.5, dividers: [0.25] };
updateUsageBars([makeEntry(Object.assign({ pct_text: '10%' }, fields))]);
const container = els.usageBars.children[0].querySelector('.bar-container');
const before = {
    marker: container.querySelector('.bar-marker').style.left,
    divider: container.querySelector('.bar-divider').style.left,
};
updateUsageBars([makeEntry(Object.assign({ pct_text: '11%' }, fields))]);
const after = {
    marker: container.querySelector('.bar-marker').style.left,
    divider: container.querySelector('.bar-divider').style.left,
};
console.log(JSON.stringify({ before, after }));
''')
        self.assertEqual(result['after'], result['before'])

    def test_unchanged_field_set_updates_in_place(self):
        """With an unchanged field set, bars are updated in place (no rebuild)."""
        result = _run_scenario('''
updateUsageBars([makeEntry({ key: 'five_hour', label: '5h', pct_text: '10%' })]);
const barBefore = els.usageBars.children[0];
updateUsageBars([makeEntry({ key: 'five_hour', label: '5h', pct_text: '50%', fill_pct: 0.5 })]);
console.log(JSON.stringify({
    sameElement: els.usageBars.children[0] === barBefore,
    pct: els.usageBars.children[0].querySelector('.bar-pct').textContent,
    fillWidth: els.usageBars.children[0].querySelector('.bar-fill').style.width,
}));
''')
        self.assertEqual(result, {'sameElement': True, 'pct': '50%', 'fillWidth': '50%'})


@unittest.skipUnless(_NODE, 'Node.js not available')
class TestQueueTable(unittest.TestCase):
    """Tests for the task-queue table rendering in popup.js."""

    def test_rows_carry_status_class_and_glyph(self):
        result = _run_scenario('''
buildFilterChips();
updateQueue(makeQueue([
    makeTask({ id: 'a', status: 'running', glyph: '▶', status_text: 'running', active: true }),
    makeTask({ id: 'b', status: 'done', glyph: '✖', status_text: 'done (exit 1)', failed: true, active: false }),
]));
console.log(JSON.stringify(els.queueRows.children.map((row) => ({
    id: row.dataset.id,
    status: row.children[0].className,
    text: row.children[0].textContent,
}))));
''')
        self.assertEqual(result, [
            {'id': 'a', 'status': 'pill st-running', 'text': '▶ running'},
            {'id': 'b', 'status': 'pill st-failed', 'text': '✖ done (exit 1)'},
        ])

    def test_filter_limits_the_rows_without_changing_the_counts(self):
        """The chips must keep reporting the whole queue while filtering it."""
        result = _run_scenario('''
buildFilterChips();
const queue = makeQueue([
    makeTask({ id: 'a', active: true }),
    makeTask({ id: 'b', status: 'done', active: false }),
    makeTask({ id: 'c', status: 'done', active: false }),
]);
queueFilter = 'done';
updateQueue(queue);
console.log(JSON.stringify({
    rows: els.queueRows.children.map((row) => row.dataset.id),
    chips: els.queueFilters.children.map((chip) => chip.textContent),
    active: els.queueFilters.children.filter((chip) => chip.className.includes('active')).map((c) => c.dataset.filter),
}));
''')
        self.assertEqual(result['rows'], ['b', 'c'])
        self.assertEqual(result['chips'], ['All 3', 'Active 1', 'Done 2'])
        self.assertEqual(result['active'], ['done'])

    def test_empty_queue_shows_the_empty_text_and_hides_the_table(self):
        result = _run_scenario('''
buildFilterChips();
updateQueue(makeQueue([]));
console.log(JSON.stringify({
    empty: els.queueEmpty.textContent,
    emptyDisplay: els.queueEmpty.style.display,
    tableDisplay: els.queueTable.style.display,
}));
''')
        self.assertEqual(result['empty'], 'No tasks yet.')
        self.assertEqual(result['emptyDisplay'], '')
        self.assertEqual(result['tableDisplay'], 'none')

    def test_missing_gate_is_called_out(self):
        """The queue is gated on the five-hour field alone; without it, say so."""
        result = _run_scenario('''
buildFilterChips();
updateQueue(makeQueue([makeTask({})], { gate: null }));
console.log(JSON.stringify({ notice: els.queueNotice.textContent, visible: els.queueNotice.className }));
''')
        self.assertEqual(result['notice'], 'No five-hour quota.')
        self.assertIn('visible', result['visible'])

    def test_disabled_queue_notice_wins_over_the_missing_gate(self):
        result = _run_scenario('''
buildFilterChips();
updateQueue(makeQueue([], { disabled: true, gate: null }));
console.log(JSON.stringify({ notice: els.queueNotice.textContent, warn: els.queueNotice.className }));
''')
        self.assertEqual(result['notice'], 'Queue disabled.')
        self.assertIn('warn', result['warn'])

    def test_running_row_shows_a_live_elapsed_time(self):
        """Python sends no result text for a running task - the page ticks it."""
        result = _run_scenario('''
buildFilterChips();
const started = Date.now() / 1000 - (3600 + 12 * 60);
updateQueue(makeQueue([makeTask({ status: 'running', started_epoch: started, result_text: '' })]));
const cell = els.queueRows.children[0].children[5];
console.log(JSON.stringify({ text: cell.textContent, started: !!cell.dataset.started }));
''')
        self.assertEqual(result, {'text': '1h 12m', 'started': True})

    def test_finished_row_shows_the_result_text_verbatim(self):
        result = _run_scenario('''
buildFilterChips();
updateQueue(makeQueue([makeTask({ status: 'done', active: false, result_text: 'exit 0 · 6m' })]));
const cell = els.queueRows.children[0].children[5];
console.log(JSON.stringify({ text: cell.textContent, started: !!cell.dataset.started }));
''')
        self.assertEqual(result, {'text': 'exit 0 · 6m', 'started': False})

    def test_row_without_a_previous_run_shows_a_dash(self):
        result = _run_scenario('''
buildFilterChips();
updateQueue(makeQueue([makeTask({ last_run_text: '' })]));
console.log(JSON.stringify(els.queueRows.children[0].children[4].textContent));
''')
        self.assertEqual(result, '-')


@unittest.skipUnless(_NODE, 'Node.js not available')
class TestGateBar(unittest.TestCase):
    """Tests for the headline quota bar and its task threshold markers."""

    def test_markers_are_placed_and_staggered(self):
        """Neighbouring thresholds must not print their labels on top of each other."""
        result = _run_scenario('''
updateGate(makeQueue([]).gate, [
    { rel: 0.2, label: 'research', pct_text: '20%', met: false },
    { rel: 0.4, label: 'daily', pct_text: '40%', met: true },
    { rel: 0.8, label: 'nightly', pct_text: '80%', met: true },
]);
console.log(JSON.stringify(els.gateMarkers.children.map((marker) => ({
    left: marker.style.left,
    className: marker.className,
    label: marker.children[1].textContent,
}))));
''')
        self.assertEqual([m['left'] for m in result], ['20%', '40%', '80%'])
        self.assertEqual([m['label'] for m in result], ['research · 20%', 'daily · 40%', 'nightly · 80%'])
        self.assertNotIn('stagger', result[0]['className'])
        self.assertIn('stagger', result[1]['className'])
        self.assertIn('met', result[1]['className'])
        self.assertNotIn('met', result[0]['className'])

    def test_redraw_does_not_accumulate_markers_or_dividers(self):
        """The bar is redrawn on every poll; leftovers would pile up all day."""
        result = _run_scenario('''
const gate = Object.assign(makeQueue([]).gate, { dividers: [0.2, 0.4], marker_rel: 0.5 });
updateGate(gate, [{ rel: 0.2, label: 'a', pct_text: '20%', met: false }]);
updateGate(gate, [{ rel: 0.2, label: 'a', pct_text: '20%', met: false }]);
console.log(JSON.stringify({
    dividers: els.gateBarContainer.querySelectorAll('.bar-divider').length,
    markers: els.gateBarContainer.querySelectorAll('.bar-marker').length,
    labels: els.gateMarkers.children.length,
}));
''')
        self.assertEqual(result, {'dividers': 2, 'markers': 1, 'labels': 1})

    def test_absent_time_marker_draws_nothing(self):
        result = _run_scenario('''
updateGate(Object.assign(makeQueue([]).gate, { marker_rel: null }), []);
console.log(JSON.stringify(els.gateBarContainer.querySelectorAll('.bar-marker').length));
''')
        self.assertEqual(result, 0)


if __name__ == '__main__':
    unittest.main()
