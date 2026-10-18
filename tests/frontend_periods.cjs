const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.children = [];
    this.textContent = '';
    this.value = '';
    this.attributes = {};
    this.tBodies = [this];
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  querySelector() { return this; }
}
const nodes = new Map();
let liveCharacters = [{ value: '42' }, { value: '43' }];
const element = id => {
  if (!nodes.has(id)) nodes.set(id, new Element());
  return nodes.get(id);
};
global.document = {
  getElementById: element,
  querySelector: () => ({ content: 'test-csrf' }),
  querySelectorAll: () => liveCharacters,
  createElement: () => new Element(),
  addEventListener() {},
};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
assert.equal(typeof periodRange, 'function', 'period range control is missing');
assert.deepEqual(periodRange('day', '2026-10-17'), { from: '2026-10-17', to: '2026-10-17' });
assert.deepEqual(periodRange('week', '2026-01-03'), { from: '2025-12-28', to: '2026-01-03' });
assert.deepEqual(periodRange('month', '2024-02-29'), { from: '2024-02-01', to: '2024-02-29' });
assert.deepEqual(periodRange('month', '2026-02-28'), { from: '2026-02-01', to: '2026-02-28' });
assert.equal(shiftDate('2026-01-01', -1), '2025-12-31');
assert.equal(shiftMonth('2026-01', -1), '2025-12');
assert.equal(shiftMonth('2024-01', 1), '2024-02');
assert.throws(() => monthRange('2026-13'));
assert.throws(() => monthRange(''));

const days = Array.from({ length: 31 }, (_, index) => ({
  date: `2026-10-${String(index + 1).padStart(2, '0')}`,
  income: '9007199254740993.129', tax: '0.009', net: '9007199254740993.120',
}));
assert.deepEqual(visibleLedgerDays(days, '2026-10', '2026-10-17', false).map(x => x.date),
  ['2026-10-15', '2026-10-16', '2026-10-17']);
assert.deepEqual(visibleLedgerDays(days, '2026-10', '2026-10-01', false).map(x => x.date), ['2026-10-01']);
assert.deepEqual(visibleLedgerDays(days, '2026-10', '2026-11-03', false).map(x => x.date),
  ['2026-10-29', '2026-10-30', '2026-10-31']);
assert.equal(visibleLedgerDays(days, '2026-10', '2026-10-17', true).length, 31);
assert.deepEqual(visibleLedgerDays(days, '2026-09', '2026-10-17', false), []);

// Monetary display must keep decimal strings intact, including values beyond Number precision.
assert.equal(isk('9007199254740993.129'), '9,007,199,254,740,993.12 ISK');
assert.equal(isk('-0.019'), '-0.01 ISK');

(async () => {
  vm.runInThisContext("selectedGroupId = '7'; ledgerMonth = '2026-10'; periodMode = 'day'; periodAnchor = '2026-10-17';");
  const requests = [];
  let pendingOldSummary;
  global.fetch = async path => {
    requests.push(path);
    if (path.startsWith('/api/entries')) return { ok: true, json: async () => ({ entries: [] }) };
    const url = new URL(path, 'http://local.test');
    const from = url.searchParams.get('from');
    const data = { income: '10', tax: '2', net: from === '2026-10-16' ? '16' : '17',
      categories: [], characters: [], daily: days };
    if (from === '2026-10-16') {
      await new Promise(resolve => { pendingOldSummary = resolve; });
    }
    return { ok: true, json: async () => data };
  };
  await load();
  assert(element('period-range').textContent.includes('2026-10-17'), 'displayed report has no date context');
  assert(requests.some(path => path.includes('from=2026-10-17&to=2026-10-17')));
  assert(requests.some(path => path.includes('from=2026-10-01&to=2026-10-31')));
  assert(requests.every(path => path.includes('group_id=7') && path.includes('character_id=42')));
  const summary = element('summary-net').textContent;
  requests.length = 0;
  liveCharacters = [{ value: '42' }];
  await loadLedger();
  assert.equal(requests.length, 1);
  assert(requests[0].includes('character_id=43'), 'month switch applied pending character selection to only the ledger');
  assert.equal(element('summary-net').textContent, summary, 'ledger refresh changed summary');
  const before = element('daily-table').children;
  requests.length = 0;
  await loadSummary();
  assert.equal(requests.length, 2);
  assert(requests.every(path => path.includes('character_id=43')), 'period switch applied pending selection to only the summary');
  assert.equal(element('daily-table').children, before, 'summary refresh changed ledger');
  const normalFetch = global.fetch;
  const knownLp = {net:'95',has_coverage:true,complete:false,
    warnings:[{message:'旧项目尾账无法读取，统计存在缺口。'}]};
  global.fetch = async () => ({ok:true,json:async () => ({
    lp:knownLp,daily:[{date:'2026-10-17',income:'0',tax:'0',net:'0',lp:knownLp}],
  })});
  await loadLedger();
  assert.equal(element('summary-net').textContent, summary, 'LP ledger refresh changed summary');
  assert.match(element('daily-table').children[0].children[4].textContent, /95 LP.*统计不完整/);
  assert.match(element('ledger-lp-note').textContent, /旧项目尾账无法读取/);
  assert.match(element('ledger-lp-note').textContent, /统计不完整/);
  global.fetch = normalFetch;
  await loadLedger();
  global.fetch = async path => {
    if (path === '/api/groups') throw new Error('roles offline');
    return normalFetch(path);
  };
  await assert.rejects(roles(), /roles offline/);
  global.fetch = normalFetch;
  requests.length = 0;
  await loadLedger();
  assert(requests[0].includes('character_id=43'), 'failed role refresh changed the applied character filter');
  requests.length = 0;
  await load();
  assert(requests.every(path => !path.includes('character_id=43')), 'update report did not apply character choice to both regions');

  vm.runInThisContext("periodAnchor = '2026-10-16';");
  const displayedContext = element('period-range').textContent;
  updatePeriodControls();
  assert.equal(element('period-range').textContent, displayedContext, 'navigation relabeled existing totals before loading the new period');
  const oldRequest = loadSummary();
  assert.equal(element('summary-content').hidden, false, 'period navigation hid the current report while loading');
  assert.equal(element('entry-details').hidden, false, 'period navigation hid the entry panel while loading');
  assert.equal(element('summary-content').attributes['aria-busy'], 'true');
  vm.runInThisContext("periodAnchor = '2026-10-17';");
  await loadSummary();
  pendingOldSummary();
  await oldRequest;
  assert.equal(element('summary-net').textContent, '17 ISK', 'stale response overwrote current period');

  const loadedLedger = element('daily-table').children;
  let finishLedger;
  global.fetch = async () => {
    await new Promise(resolve => { finishLedger = resolve; });
    return { ok: true, json: async () => ({ daily: days }) };
  };
  const ledgerRefresh = loadLedger();
  assert.equal(element('daily-table').hidden, false, 'month navigation hid the current ledger while loading');
  assert.equal(element('daily-table').children, loadedLedger, 'month navigation cleared existing ledger rows before the response');
  finishLedger();
  await ledgerRefresh;

  const previousLedger = element('daily-table').children;
  global.fetch = async () => ({ ok: false, json: async () => ({ error: { message: 'offline' } }) });
  await loadSummary();
  assert(element('status-message').textContent.includes('offline'));
  assert.equal(element('summary-content').hidden, false, 'failed refresh hid the last successful report');
  assert.equal(element('summary-net').textContent, '17 ISK');
  assert.equal(element('daily-table').children, previousLedger, 'summary failure destroyed monthly ledger');
  await loadLedger();
  assert.equal(element('daily-table').hidden, false, 'failed refresh hid the last successful ledger');
  assert.equal(element('daily-table').children, previousLedger, 'failed ledger refresh destroyed existing rows');
  assert.equal(element('ledger-toggle').disabled, false, 'failed ledger refresh left existing rows unusable');

  const reportContext = element('period-range').textContent;
  const ledgerContext = element('ledger-caption').textContent;
  vm.runInThisContext("selectedGroupId = '8';");
  await load();
  assert.equal(element('period-range').textContent, reportContext, 'failed group switch relabeled the old report');
  assert.equal(element('ledger-caption').textContent, ledgerContext, 'failed group switch relabeled the old ledger');
  assert(element('period-range').textContent.includes('7'), 'retained report does not identify its source group');
  assert(element('ledger-caption').textContent.includes('7'), 'retained ledger does not identify its source group');
})().catch(error => { console.error(error); process.exitCode = 1; });
