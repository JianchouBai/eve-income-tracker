const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name=csrf-token]').content;
let reportRequestGeneration = 0;
let ledgerRequestGeneration = 0;
let periodMode = 'day';
let periodAnchor = beijingDate();
let ledgerMonth = beijingDate().slice(0, 7);
let ledgerExpanded = false;
let ledgerDays = [];
let groups = [];
let selectedGroupId = null;
let selectedCharacterIds = new Set();
let appliedCharacterIds = new Set();
let charactersById = new Map();
let displayedReport = null;
let displayedLedger = null;
let reportDirection = 0;
let ledgerDirection = 0;
const activeTransitions = new WeakMap();

function reducedMotion() {
  return globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;
}

// Update in one frame; keep existing content in place until its replacement is ready.
function transitionContent(target, update, direction = 0) {
  const previousHeight = target.getBoundingClientRect?.().height;
  activeTransitions.get(target)?.cancel();
  update();
  if (!target.animate || reducedMotion() || !previousHeight) return;
  const height = target.getBoundingClientRect().height;
  if (!height) return;
  const from = { opacity: 0.82, transform: `translateX(${direction * 10}px)` };
  const to = { opacity: 1, transform: 'translateX(0)' };
  if (Math.abs(height - previousHeight) > 1) {
    Object.assign(from, { height: `${previousHeight}px`, overflow: 'clip' });
    Object.assign(to, { height: `${height}px`, overflow: 'clip' });
  }
  const animation = target.animate([from, to], { duration: 220, easing: 'cubic-bezier(0.2, 0.7, 0.2, 1)' });
  activeTransitions.set(target, animation);
  animation.finished.catch(() => {}).then(() => {
    if (activeTransitions.get(target) === animation) activeTransitions.delete(target);
  });
}

function animateEntryDisclosure(details, expanded, height = details.getBoundingClientRect().height) {
  const summary = details.querySelector('summary');
  activeTransitions.get(details)?.cancel();
  activeTransitions.delete(details);
  summary.setAttribute('aria-expanded', String(expanded));
  if (!details.animate || reducedMotion()) {
    details.open = expanded;
    return;
  }
  details.open = true;
  const targetHeight = expanded ? details.getBoundingClientRect().height
    : summary.getBoundingClientRect().height + details.offsetHeight - details.clientHeight;
  const animation = details.animate([
    { height: `${height}px`, overflow: 'clip' },
    { height: `${targetHeight}px`, overflow: 'clip' },
  ], { duration: 220, easing: 'cubic-bezier(0.2, 0.7, 0.2, 1)' });
  activeTransitions.set(details, animation);
  animation.finished.then(() => {
    if (activeTransitions.get(details) !== animation) return;
    details.open = expanded;
    activeTransitions.delete(details);
  }).catch(() => {});
}

function bindEntryDisclosure() {
  const details = $('entry-details');
  const summary = details.querySelector('summary');
  let expanded = details.open;
  summary.addEventListener('click', (event) => {
    if (!details.animate || reducedMotion()) return;
    event.preventDefault();
    expanded = !expanded;
    const height = details.getBoundingClientRect().height;
    activeTransitions.get(details.querySelector('.scroll'))?.cancel();
    animateEntryDisclosure(details, expanded, height);
  });
  details.addEventListener('toggle', () => {
    if (!activeTransitions.has(details)) {
      expanded = details.open;
      summary.setAttribute('aria-expanded', String(expanded));
    }
  });
}

function bindMonitorDisclosure() {
  const monitor = $('monitor');
  const content = $('monitor-content');
  const toggle = $('monitor-toggle');
  toggle.addEventListener('click', () => {
    const collapsed = toggle.getAttribute('aria-expanded') === 'true';
    if (collapsed && content.contains(document.activeElement)) toggle.focus();
    transitionContent($('monitor-heading'), () => {
      monitor.setAttribute('data-collapsed', String(collapsed));
      toggle.setAttribute('aria-expanded', String(!collapsed));
      $('monitor-toggle-label').textContent = collapsed ? '展开监控台' : '收起监控台';
      $('monitor-heading-copy').hidden = collapsed;
      $('monitor-return').hidden = collapsed;
      $('monitor-summary').hidden = !collapsed;
      content.inert = collapsed;
      content.setAttribute('aria-hidden', String(collapsed));
    });
  });
}

function beijingDate(offset = 0) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + offset);
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(date);
}

function shiftDate(value, offset) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

function monthRange(month) {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month) || month.startsWith('0000')) {
    throw new Error('请选择有效月份。');
  }
  const from = `${month}-01`;
  const end = new Date(`${from}T00:00:00Z`);
  end.setUTCMonth(end.getUTCMonth() + 1);
  end.setUTCDate(0);
  return { from, to: end.toISOString().slice(0, 10) };
}

function shiftMonth(month, offset) {
  const date = new Date(`${monthRange(month).from}T00:00:00Z`);
  date.setUTCMonth(date.getUTCMonth() + offset);
  return date.toISOString().slice(0, 7);
}

function periodRange(mode = periodMode, anchor = periodAnchor) {
  if (mode === 'month') return monthRange(anchor.slice(0, 7));
  return { from: mode === 'week' ? shiftDate(anchor, -6) : anchor, to: anchor };
}

function adjacentPeriod(direction) {
  return periodMode === 'month'
    ? `${shiftMonth(periodAnchor.slice(0, 7), direction)}-01`
    : shiftDate(periodAnchor, direction * (periodMode === 'week' ? 7 : 1));
}

function updatePeriodControls() {
  const names = { day: '日', week: '周', month: '月' };
  const next = { day: '周', week: '月', month: '日' };
  const { from, to } = periodRange();
  const today = beijingDate();
  $('period-kind').textContent = `按${names[periodMode]}显示`;
  $('period-label').textContent = periodMode === 'month'
    ? `${from.slice(0, 4)} 年 ${Number(from.slice(5, 7))} 月`
    : (periodMode === 'week' ? `${from.slice(5)} — ${to.slice(5)}` : `${to === today ? '今天 · ' : ''}${to}`);
  $('period-mode').setAttribute('aria-label', `当前按${names[periodMode]}显示，${from}${from === to ? '' : ` 至 ${to}`}，点击切换为按${next[periodMode]}显示`);
  $('period-prev').setAttribute('aria-label', `上一${names[periodMode]}`);
  $('period-next').setAttribute('aria-label', `下一${names[periodMode]}`);
  $('period-next').disabled = periodMode === 'month'
    ? adjacentPeriod(1).slice(0, 7) > today.slice(0, 7)
    : adjacentPeriod(1) > today;
}

async function movePeriod(direction) {
  if (direction > 0 && $('period-next').disabled) return;
  periodAnchor = adjacentPeriod(direction);
  reportDirection = direction;
  transitionContent($('period-mode').querySelector('span'), updatePeriodControls, direction);
  await loadSummary();
}

async function cyclePeriod() {
  periodMode = { day: 'week', week: 'month', month: 'day' }[periodMode];
  periodAnchor = beijingDate();
  reportDirection = 0;
  transitionContent($('period-mode').querySelector('span'), updatePeriodControls);
  await loadSummary();
}

function visibleLedgerDays(days, month, today, expanded) {
  const monthly = days.filter(day => day.date.startsWith(`${month}-`));
  if (expanded) return monthly;
  return monthly.filter(day => day.date <= today).slice(-3);
}

function renderLedger(direction = 0) {
  transitionContent($('monthly-ledger'), () => {
    const month = displayedLedger ? displayedLedger.from.slice(0, 7) : ledgerMonth;
    const visible = visibleLedgerDays(ledgerDays, month, beijingDate(), ledgerExpanded);
    rows($('daily-table').tBodies[0], visible, [
      { key: 'date' }, { key: 'income', money: true }, { key: 'tax', money: true },
      { key: 'net', money: true }, { format: (item) => lpReading(item.lp) },
    ]);
    $('ledger-month-label').textContent = `${month.slice(0, 4)} 年 ${Number(month.slice(5))} 月`;
    const caption = ledgerExpanded ? '整月明细 · 北京时间' : `最近 ${visible.length} 天明细 · 展开可查看整月`;
    $('ledger-caption').textContent = caption + (displayedLedger ? ` · ${reportScope(displayedLedger)}` : '');
    $('ledger-caption').setAttribute('title', displayedLedger?.characterNames.join('、') || '默认启用角色');
    $('ledger-toggle').textContent = ledgerExpanded ? '收起明细' : '展开整月';
    $('ledger-toggle').setAttribute('aria-expanded', String(ledgerExpanded));
    $('ledger-next').disabled = ledgerMonth >= beijingDate().slice(0, 7);
    $('ledger-prev').disabled = ledgerMonth <= '2003-05';
  }, direction);
}

async function selectLedgerMonth(month) {
  try {
    monthRange(month);
    if (month < '2003-05' || month > beijingDate().slice(0, 7)) throw new Error('请选择 2003 年 5 月至当前月份之间的月份。');
    ledgerDirection = Math.sign(month.localeCompare(ledgerMonth));
    ledgerMonth = month;
    $('ledger-month').value = month;
    $('ledger-next').disabled = month >= beijingDate().slice(0, 7);
    $('ledger-prev').disabled = month <= '2003-05';
    await loadLedger();
  } catch (error) {
    $('ledger-month').value = ledgerMonth;
    $('ledger-status').textContent = error.message;
  }
}

function isk(value, fractionDigits = 2) {
  const [whole, rawFraction = ''] = String(value).split('.');
  const sign = whole.startsWith('-') ? '-' : '';
  const digits = whole.replace('-', '');
  const fraction = rawFraction.padEnd(fractionDigits, '0').slice(0, fractionDigits);
  return `${sign}${digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fractionDigits ? `.${fraction}` : ''} ISK`;
}

function lp(value, places = 2) {
  const [whole, rawFraction = ''] = String(value).split('.');
  const fraction = rawFraction.slice(0, places).replace(/0+$/, '');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction ? `.${fraction}` : '');
}

function lpReading(data) {
  return data?.has_coverage ? `${lp(data.net)} LP` : '— LP';
}

function lpNote(data) {
  const notes = ['按同步日统计，扣除 5%'];
  if (data?.started_at) {
    notes.push('起算：' + new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
    }).format(new Date(data.started_at)));
  }
  if (!data?.complete) notes.push('统计不完整');
  (data?.warnings || []).forEach(warning => notes.push(warning.message));
  return notes.join('；');
}

function cell(value) {
  const td = document.createElement('td');
  td.textContent = isk(value);
  return td;
}

function checkedCharacterIds() {
  return new Set([...document.querySelectorAll('[data-report-select]:checked')].map((input) => input.value));
}

function query(range = periodRange()) {
  const params = new URLSearchParams(range);
  params.set('group_id', String(selectedGroupId));
  appliedCharacterIds.forEach((characterId) => params.append('character_id', characterId));
  return params;
}

function captureReportContext(range) {
  const characterNames = [...appliedCharacterIds].map(id => charactersById.get(id)?.name || `角色 ${id}`);
  return { ...range, groupName: activeGroup()?.name || `角色组 ${selectedGroupId}`, characterNames };
}

function reportScope(context) {
  return `${context.groupName} · ${context.characterNames.length ? `已选 ${context.characterNames.length} 名角色` : '默认启用角色'}`;
}

function showReportContext(direction) {
  transitionContent($('report-deck').querySelector('.report-toolbar'), () => {
    const { from, to } = displayedReport;
    $('period-range').textContent = `${reportScope(displayedReport)} · 北京时间 ${from}${from === to ? '' : ` 至 ${to}`}`;
    $('period-range').setAttribute('title', displayedReport.characterNames.join('、') || '默认启用角色');
  }, direction);
}

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new Error(payload?.error?.message || '请求失败，请稍后再试。');
  }
  return response.json();
}

async function get(path) {
  return request(path);
}

function message(text, error = false) {
  const element = $('status-message');
  element.textContent = text;
  element.className = `notice status-bar${error ? ' error' : ''}`;
}

function waitForAuthorizationPoll() {
  return new Promise((resolve) => setTimeout(resolve, 1000));
}

async function startDesktopAuthorization(event) {
  event.preventDefault();
  const control = event.currentTarget;
  if (control.getAttribute('aria-disabled') === 'true') return;
  control.setAttribute('aria-disabled', 'true');
  try {
    const started = await request('/api/authorization-attempts', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrf },
    });
    await window.pywebview.api.open_authorization_url(started.authorization_url);
    const expiresInSeconds = started.expires_in_seconds;
    if (!Number.isInteger(expiresInSeconds) || expiresInSeconds <= 0 || expiresInSeconds > 3600) {
      throw new Error('授权有效期无效。');
    }
    const deadline = Date.now() + expiresInSeconds * 1000;
    while (Date.now() < deadline) {
      await waitForAuthorizationPoll();
      const status = await request('/api/authorization-attempts/' + started.attempt_id);
      if (status.status === 'succeeded') {
        location.reload();
        return;
      }
      if (status.status === 'failed') {
        throw new Error(status.message || '授权未完成，请重试。');
      }
    }
    throw new Error('授权等待超时，请重试。');
  } catch (error) {
    message('授权未完成，请重试。', true);
  } finally {
    control.removeAttribute('aria-disabled');
  }
}

function bindDesktopAuthorization() {
  document.querySelectorAll("[data-desktop-authorization='true']").forEach((control) => {
    control.addEventListener('click', startDesktopAuthorization);
  });
}

function rows(target, items, columns) {
  target.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement('tr');
    columns.forEach((column) => {
      const td = document.createElement('td');
      td.textContent = column.format ? column.format(item) : (column.money ? isk(item[column.key]) : item[column.key]);
      row.append(td);
    });
    target.append(row);
  });
}

function breakdown(target, title, items, label, summaryLp = null) {
  target.replaceChildren();
  const marker = document.createElement('span');
  marker.className = 'section-index';
  marker.textContent = label === 'category' ? '来源' : '舰长';
  const heading = document.createElement('h2');
  heading.textContent = title;
  target.append(marker, heading);
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-guidance';
    empty.textContent = '暂无 ISK 收入';
    target.append(empty);
  }
  items.forEach((item) => {
    const line = document.createElement('div');
    line.className = label === 'category' ? 'breakdown-row' : 'character-income';
    const name = document.createElement('span');
    name.className = 'breakdown-name';
    name.textContent = item[label];
    const value = document.createElement('strong');
    value.textContent = isk(item.net);
    line.append(name, value);
    if (label === 'name') {
      const lp = document.createElement('span');
      lp.className = 'lp-placeholder';
      lp.textContent = `C 海军 ${lpReading(item.lp)}`;
      line.append(lp);
    }
    target.append(line);
  });
  if (label === 'category') {
    const lp = document.createElement('div');
    lp.className = 'breakdown-row lp-row';
    const name = document.createElement('span');
    name.textContent = 'C 海军 LP';
    const value = document.createElement('strong');
    value.textContent = lpReading(summaryLp);
    lp.append(name, value);
    target.append(lp);
  }
}

function render(data, entries, direction = 0) {
  transitionContent($('summary-content'), () => {
    $('summary-income').textContent = isk(data.income, 0);
    $('summary-tax').textContent = isk(data.tax, 0);
    $('summary-net').textContent = isk(data.net, 0);
    $('summary-lp').textContent = lpReading(data.lp);
    $('lp-note').textContent = lpNote(data.lp);
    breakdown($('category-breakdown'), '类型分项', data.categories, 'category', data.lp);
    breakdown($('character-breakdown'), '角色分项', data.characters, 'name');
  }, direction);
  const details = $('entry-details');
  const scroll = details.querySelector('.scroll');
  const resizing = activeTransitions.has(details);
  const height = resizing ? details.getBoundingClientRect().height : null;
  const updateEntries = () => {
    rows($('entry-details').querySelector('tbody'), entries.entries, [
      { key: 'date' }, { key: 'character_name' }, { key: 'category' }, { key: 'amount', format: (x) => isk(x.amount) }, { key: 'description' },
    ]);
  };
  if (resizing) {
    activeTransitions.get(scroll)?.cancel();
    updateEntries();
    animateEntryDisclosure(details, details.querySelector('summary').getAttribute('aria-expanded') === 'true', height);
  } else {
    transitionContent(scroll, updateEntries, direction);
  }
}

async function loadSummary() {
  const generation = ++reportRequestGeneration;
  const requestedGroupId = selectedGroupId;
  const direction = reportDirection;
  if (!requestedGroupId) {
    $('report-deck').hidden = true;
    $('summary-content').removeAttribute('aria-busy');
    return;
  }
  $('report-deck').hidden = false;
  $('summary-content').setAttribute('aria-busy', 'true');
  message('正在加载当前角色组报表…');
  try {
    const range = periodRange();
    const context = captureReportContext(range);
    const parameters = query(range);
    const [data, entries] = await Promise.all([
      get(`/api/dashboard?${parameters}`), get(`/api/entries?${parameters}`),
    ]);
    if (generation !== reportRequestGeneration || requestedGroupId !== selectedGroupId) return;
    render(data, entries, direction);
    displayedReport = context;
    showReportContext(direction);
    $('summary-content').hidden = false;
    $('entry-details').hidden = false;
    message('报表已更新。');
  } catch (error) {
    if (generation !== reportRequestGeneration || requestedGroupId !== selectedGroupId) return;
    message(`${error.message || '无法加载报表，请稍后再试。'} 当前保留上次显示的报表，请重试。`, true);
  } finally {
    if (generation === reportRequestGeneration) $('summary-content').removeAttribute('aria-busy');
  }
}

async function loadLedger() {
  const generation = ++ledgerRequestGeneration;
  const requestedGroupId = selectedGroupId;
  const requestedMonth = ledgerMonth;
  const direction = ledgerDirection;
  if (!requestedGroupId) {
    $('monthly-ledger').removeAttribute('aria-busy');
    return;
  }
  $('ledger-toggle').disabled = true;
  $('monthly-ledger').setAttribute('aria-busy', 'true');
  $('ledger-status').textContent = '正在加载月度账本…';
  try {
    const range = monthRange(requestedMonth);
    const context = captureReportContext(range);
    const data = await get('/api/dashboard?' + query(range));
    if (generation !== ledgerRequestGeneration || requestedGroupId !== selectedGroupId || requestedMonth !== ledgerMonth) return;
    ledgerDays = data.daily;
    if (displayedLedger?.from.slice(0, 7) !== requestedMonth) ledgerExpanded = false;
    displayedLedger = context;
    renderLedger(direction);
    $('daily-table').hidden = false;
    $('ledger-toggle').disabled = false;
    $('ledger-status').textContent = '';
  } catch (error) {
    if (generation !== ledgerRequestGeneration || requestedGroupId !== selectedGroupId || requestedMonth !== ledgerMonth) return;
    $('ledger-status').textContent = `${error.message || '无法加载月度账本，请重试。'} 当前保留上次显示的账本。`;
  } finally {
    if (generation === ledgerRequestGeneration) {
      $('monthly-ledger').removeAttribute('aria-busy');
      $('ledger-toggle').disabled = displayedLedger === null;
    }
  }
}

async function load() {
  selectedCharacterIds = checkedCharacterIds();
  appliedCharacterIds = new Set(selectedCharacterIds);
  await Promise.all([loadSummary(), loadLedger()]);
}

function roleStatus(character) {
  const wallet = character.authorization_required ? '钱包需重新授权'
    : (character.last_synced_at ? `钱包上次同步：${character.last_synced_at}` : '钱包未同步');
  const status = character.lp;
  const loyalty = status?.error?.message || (status?.status === 'ok' ? 'LP 已同步'
    : status?.status === 'authorization_required' ? 'LP 待授权' : 'LP 尚未建立基线');
  return `${wallet}；${loyalty}`;
}

async function setEnabled(characterId, enabled, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/characters/${characterId}/enabled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok) throw new Error('request failed');
    await roles();
    await load();
    message(enabled ? '该角色已启用统计。' : '该角色已停止统计；仍可勾选查看历史。');
  } catch (error) {
    button.disabled = false;
    message(error.message || '无法更新角色统计状态，请稍后再试。', true);
  }
}

function emptyState(target, text) {
  target.replaceChildren();
  const guidance = document.createElement('p');
  guidance.className = 'empty-guidance';
  guidance.textContent = text;
  target.append(guidance);
}

function activeGroup() {
  return groups.find((group) => String(group.group_id) === selectedGroupId) || null;
}

function setWorkspaceAvailability() {
  const hasGroup = Boolean(activeGroup());
  $('rename-group-button').disabled = !hasGroup;
  $('delete-group-button').disabled = !hasGroup;
  $('report-button').disabled = !hasGroup;
  $('sync-button').disabled = !hasGroup;
  $('report-deck').hidden = !hasGroup;
}

function renderGroupOptions() {
  const select = $('group-select');
  select.replaceChildren();
  if (!groups.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '暂无角色组';
    select.append(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  groups.forEach((group) => {
    const option = document.createElement('option');
    option.value = group.group_id;
    option.textContent = `${group.name}（${group.member_count} 名）`;
    option.selected = String(group.group_id) === selectedGroupId;
    select.append(option);
  });
}

function renderMember(character, target) {
  const row = document.createElement('div');
  row.className = 'role-row';
  const label = document.createElement('label');
  const reportSelect = document.createElement('input');
  reportSelect.type = 'checkbox';
  reportSelect.value = character.character_id;
  reportSelect.dataset.reportSelect = 'true';
  reportSelect.checked = selectedCharacterIds.size ? selectedCharacterIds.has(String(character.character_id)) : character.enabled;
  const text = document.createElement('span');
  text.textContent = `${character.name} (${character.character_id})`;
  const status = document.createElement('small');
  status.textContent = roleStatus(character);
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'text-action status-action';
  toggle.textContent = character.enabled ? '停止统计' : '启用统计';
  toggle.setAttribute('aria-label', `${toggle.textContent}：${character.name}`);
  toggle.addEventListener('click', () => setEnabled(character.character_id, !character.enabled, toggle));
  label.append(reportSelect, text);
  row.append(label, status, toggle);
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'text-action membership-action';
  remove.textContent = '移回未分组';
  remove.setAttribute('aria-label', `将 ${character.name} 移回未分组`);
  remove.addEventListener('click', () => assignCharacterGroup(character.character_id, null, remove));
  row.append(remove);
  target.append(row);
}

function renderUngroupedCharacter(character, target) {
  const row = document.createElement('div');
  row.className = 'role-row ungrouped-row';
  const identity = document.createElement('span');
  identity.className = 'role-identity';
  identity.textContent = `${character.name} (${character.character_id})`;
  const status = document.createElement('small');
  status.textContent = roleStatus(character);
  const join = document.createElement('button');
  join.type = 'button';
  join.className = 'text-action membership-action';
  join.textContent = '加入当前组';
  join.disabled = !selectedGroupId;
  join.setAttribute('aria-label', `将 ${character.name} 加入当前角色组`);
  join.addEventListener('click', () => assignCharacterGroup(character.character_id, activeGroup().group_id, join));
  row.append(identity, status, join);
  target.append(row);
}

function renderRoles() {
  const group = activeGroup();
  const characters = [...charactersById.values()];
  const members = group ? characters.filter((character) => String(character.group_id) === selectedGroupId) : [];
  const ungrouped = characters.filter((character) => character.group_id === null);
  $('active-group-name').textContent = group ? `${group.name} · ${members.length} 名` : '尚未创建角色组';
  $('monitor-group-name').textContent = group ? group.name : '暂无角色组';
  $('monitor-character-count').textContent = `${members.length} 名角色`;

  const memberRegion = $('enable-region');
  memberRegion.replaceChildren();
  if (!group) {
    emptyState(memberRegion, '先创建角色组，再把已授权角色加入组内。');
  } else if (!members.length) {
    emptyState(memberRegion, '这个角色组还没有成员，请从下方未分组角色中添加。');
  } else {
    members.forEach((character) => renderMember(character, memberRegion));
  }

  const ungroupedRegion = $('ungrouped-region');
  ungroupedRegion.replaceChildren();
  if (!ungrouped.length) {
    emptyState(ungroupedRegion, '当前没有未分组角色。');
  } else {
    ungrouped.forEach((character) => renderUngroupedCharacter(character, ungroupedRegion));
  }
}

async function roles({ captureSelection = true } = {}) {
  if (captureSelection) {
    const currentSelection = checkedCharacterIds();
    if (currentSelection.size) selectedCharacterIds = currentSelection;
  }
  const [groupData, characterData] = await Promise.all([get('/api/groups'), get('/api/characters')]);
  groups = groupData.groups;
  charactersById = new Map(characterData.characters.map((character) => [String(character.character_id), character]));
  if (!selectedGroupId || !groups.some((group) => String(group.group_id) === selectedGroupId)) {
    selectedGroupId = groups.length ? String(groups[0].group_id) : null;
    selectedCharacterIds.clear();
  }
  renderGroupOptions();
  renderRoles();
  setWorkspaceAvailability();
  if (!selectedGroupId) message('先创建角色组，再把已授权角色加入组内。');
}

function enteredGroupName() {
  const name = $('group-name').value.trim();
  if (!name) {
    message('请输入角色组名称。', true);
    $('group-name').focus();
    return null;
  }
  return name;
}

async function createGroup() {
  const name = enteredGroupName();
  if (!name) return;
  const button = $('create-group-button');
  button.disabled = true;
  try {
    const payload = await request('/api/groups', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ name }),
    });
    selectedGroupId = String(payload.group.group_id);
    selectedCharacterIds.clear();
    $('group-name').value = '';
    await roles({ captureSelection: false });
    await load();
    message(`角色组“${payload.group.name}”已创建。`);
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function renameGroup() {
  const name = enteredGroupName();
  if (!name || !selectedGroupId) return;
  const button = $('rename-group-button');
  button.disabled = true;
  try {
    const payload = await request(`/api/groups/${selectedGroupId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ name }),
    });
    $('group-name').value = '';
    await roles();
    await load();
    message(`角色组已重命名为“${payload.group.name}”。`);
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function deleteGroup() {
  const group = activeGroup();
  if (!group || !confirm('删除后，组内角色将移回未分组，钱包流水不会被删除。确定删除吗？')) return;
  const button = $('delete-group-button');
  button.disabled = true;
  try {
    await request(`/api/groups/${selectedGroupId}`, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': csrf },
    });
    selectedGroupId = null;
    selectedCharacterIds.clear();
    await roles({ captureSelection: false });
    await load();
    message(`角色组“${group.name}”已删除，原成员已移回未分组。`);
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function assignCharacterGroup(characterId, groupId, button) {
  button.disabled = true;
  try {
    await request(`/api/characters/${characterId}/group`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ group_id: groupId }),
    });
    selectedCharacterIds = checkedCharacterIds();
    if (groupId === null) selectedCharacterIds.delete(String(characterId));
    else if (charactersById.get(String(characterId))?.enabled) selectedCharacterIds.add(String(characterId));
    await roles({ captureSelection: false });
    await load();
    message(groupId === null ? '角色已移回未分组。' : '角色已加入当前组。');
  } catch (error) {
    button.disabled = false;
    message(error.message, true);
  }
}

function renderSyncResults(results) {
  const region = $('sync-results');
  region.replaceChildren();
  if (!results.length) {
    const empty = document.createElement('p');
    empty.textContent = '没有可同步的已启用角色。';
    region.append(empty);
    return;
  }
  const list = document.createElement('ul');
  results.forEach((result) => {
    const role = charactersById.get(String(result.character_id));
    const item = document.createElement('li');
    const outcome = result.error ? `失败：${result.error.message}` : '成功';
    item.textContent = `${role ? role.name : '角色'} (${result.character_id})：${outcome}；新增 ${result.new_count} 条；完成于 ${result.completed_at}`;
    if (result.lp) {
      const loyalty = result.lp;
      const lpOutcome = loyalty.error?.message || (loyalty.baseline_created
        ? 'LP 基线已建立' : `LP 新增 ${lp(loyalty.net)} LP`);
      const warnings = (loyalty.warnings || []).map(warning => warning.message).join('；');
      item.textContent += `；${lpOutcome}${warnings ? '；' + warnings : ''}`;
    }
    list.append(item);
  });
  region.append(list);
}

async function sync() {
  if (!selectedGroupId) return;
  const button = $('sync-button');
  button.disabled = true;
  try {
    const payload = await request(`/api/sync?group_id=${selectedGroupId}`, {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrf },
    });
    renderSyncResults(payload.results || []);
    await roles();
    await load();
    message('同步完成；各角色结果见同步状态。');
  } catch (error) {
    message(error.message || '同步失败；当前报表已保留。', true);
  } finally {
    button.disabled = !selectedGroupId;
  }
}


function formatBytes(value) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return unit === 0 ? `${size} B` : `${size.toFixed(1)} ${units[unit]}`;
}

function bindDataSettings() {
  if (document.body.dataset.runtimeMode !== 'desktop' || !$('settings-button')) return;
  const dialog = $('data-settings-dialog');
  let busy = false;
  let status = null;
  let estimate = null;
  let targetParent = null;
  let returnFocus = null;
  const operations = ['storage-open', 'storage-change', 'storage-delete-recovery',
    'storage-confirm', 'storage-cancel', 'storage-create-recovery', 'storage-restart'];

  function availability() {
    const blocked = busy || status?.busy === true;
    const recoveryPending = status?.migration_recovery_pending === true;
    dialog.setAttribute('aria-busy', String(busy));
    operations.forEach((id) => { $(id).disabled = blocked; });
    $('storage-open').disabled = blocked || !status;
    $('storage-change').disabled = blocked || recoveryPending || !status || status.pending_migration || status.pending_cleanup;
    $('storage-delete-recovery').disabled = blocked || !status?.recovery || status.pending_migration;
    $('storage-confirm').disabled = blocked || recoveryPending || !estimate || estimate.space_sufficient !== true || status?.pending_migration;
    $('storage-create-recovery').disabled = blocked || recoveryPending;
    $('storage-restart').disabled = blocked || !status?.pending_migration;
    $('storage-restart').hidden = !status?.pending_migration;
  }

  function showError(error, fallback = '无法完成操作，请稍后重试。') {
    $('storage-error').textContent = error?.message || fallback;
    $('storage-error').hidden = false;
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    availability();
    $('storage-error').hidden = true;
    try { await action(); } catch (error) { showError(error); }
    finally { busy = false; availability(); }
  }

  function storageRequest(suffix, method, payload) {
    return request('/api/desktop/data-location' + suffix, {
      method, headers: { 'X-CSRF-Token': csrf, ...(payload ? { 'Content-Type': 'application/json' } : {}) },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
  }

  function renderStatus() {
    $('storage-recovery-warning').hidden = status.migration_recovery_pending !== true;
    $('storage-current-path').textContent = status.current_path;
    $('storage-volume').textContent = status.volume;
    ['database', 'logs', 'backups', 'webview', 'config', 'other', 'total'].forEach((key) => {
      $('storage-' + key).textContent = formatBytes(status.usage?.[key]);
    });
    $('storage-recovery-usage').textContent = formatBytes(status.usage?.recovery);
    const cleanupPath = status.pending_cleanup && typeof status.pending_cleanup_path === 'string'
      ? status.pending_cleanup_path : '';
    $('storage-cleanup-path').textContent = cleanupPath;
    $('storage-cleanup-notice').hidden = !cleanupPath;
    const recovery = status.recovery;
    $('storage-recovery-details').textContent = recovery
      ? `创建时间：${recovery.created_at}；实际大小：${formatBytes(recovery.size_bytes)}；成功启动：${recovery.successful_starts} 次。`
      : '当前没有迁移恢复副本。';
    $('storage-status').textContent = status.pending_migration
      ? '已安排迁移，请重启应用完成。'
      : status.pending_cleanup ? '原数据目录尚未清理完成，将在下次启动安全重试。' : '';
  }

  async function refreshStatus() {
    status = await get('/api/desktop/data-location');
    renderStatus();
  }

  async function updateEstimate() {
    if (status?.migration_recovery_pending === true) return;
    estimate = null;
    const result = await storageRequest('/estimate', 'POST', {
      target_parent: targetParent, create_recovery: $('storage-create-recovery').checked,
    });
    estimate = result;
    $('storage-target-path').textContent = result.target_path;
    $('storage-copy-bytes').textContent = formatBytes(result.copy_bytes);
    $('storage-recovery-bytes').textContent = formatBytes(result.recovery_bytes);
    $('storage-free-bytes').textContent = formatBytes(result.free_bytes);
    $('storage-space-warning').hidden = result.space_sufficient === true;
    $('storage-confirmation').hidden = false;
  }

  async function restart() {
    try {
      const restarted = await window.pywebview.api.restart_for_migration();
      if (restarted !== true) throw new Error();
    } catch {
      throw new Error('迁移已安排，但无法重启。请关闭应用后重新打开以完成迁移。');
    }
  }

  $('settings-button').addEventListener('click', async () => {
    returnFocus = document.activeElement;
    if (!dialog.open) dialog.showModal();
    $('data-settings-close').focus();
    await run(refreshStatus);
  });
  $('data-settings-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { (returnFocus || $('settings-button')).focus(); });
  // Native modal dialog handles Escape, Tab containment and background inertness.
  $('storage-open').addEventListener('click', () => run(async () => {
    try {
      if (await window.pywebview.api.open_data_directory() !== true) throw new Error();
    } catch { throw new Error('无法打开当前数据目录，请检查磁盘连接后重试。'); }
  }));
  $('storage-change').addEventListener('click', () => run(async () => {
    if (status?.migration_recovery_pending === true) return;
    let selected;
    try { selected = await window.pywebview.api.choose_data_parent(); }
    catch { throw new Error('无法选择目录，请重试。'); }
    if (selected === null || selected === undefined) return;
    if (typeof selected !== 'string' || !selected.trim()) throw new Error('所选目录无效，请重新选择。');
    targetParent = selected;
    estimate = null;
    $('storage-confirmation').hidden = true;
    $('storage-create-recovery').checked = true;
    await updateEstimate();
    // Enable before moving focus; disabled controls cannot receive focus.
    busy = false;
    availability();
    if (dialog.open) {
      (estimate.space_sufficient === true ? $('storage-confirm') : $('storage-create-recovery')).focus();
    }
  }));
  $('storage-create-recovery').addEventListener('change', () => run(updateEstimate));
  $('storage-cancel').addEventListener('click', () => {
    estimate = null;
    targetParent = null;
    $('storage-confirmation').hidden = true;
    availability();
    $('storage-change').focus();
  });
  $('storage-confirm').addEventListener('click', () => {
    if (status?.migration_recovery_pending === true) return;
    if (!estimate || estimate.space_sufficient !== true || !targetParent) return;
    return run(async () => {
      const scheduled = await storageRequest('/migrations', 'POST', {
        target_parent: targetParent, create_recovery: $('storage-create-recovery').checked,
      });
      if (scheduled.scheduled !== true) throw new Error('迁移未安排，请重新确认。');
      status.pending_migration = true;
      estimate = null;
      $('storage-confirmation').hidden = true;
      $('storage-status').textContent = '迁移已安排，应用即将重启。';
      await restart();
    });
  });
  $('storage-restart').addEventListener('click', () => run(restart));
  $('storage-delete-recovery').addEventListener('click', () => run(async () => {
    const deleted = await storageRequest('/recovery', 'DELETE');
    await refreshStatus();
    $('storage-status').textContent = `已释放 ${formatBytes(deleted.freed_bytes)}。`;
  }));
  availability();
}

document.addEventListener('DOMContentLoaded', () => {
  bindDesktopAuthorization();
  bindDataSettings();
  if (document.body.dataset.authenticated !== 'true') return;
  bindEntryDisclosure();
  bindMonitorDisclosure();
  updatePeriodControls();
  $('ledger-month').value = ledgerMonth;
  $('ledger-month').max = beijingDate().slice(0, 7);
  $('period-prev').addEventListener('click', () => movePeriod(-1));
  $('period-next').addEventListener('click', () => movePeriod(1));
  $('period-mode').addEventListener('click', cyclePeriod);
  $('ledger-toggle').addEventListener('click', () => {
    ledgerExpanded = !ledgerExpanded;
    renderLedger();
  });
  $('ledger-month').addEventListener('change', event => selectLedgerMonth(event.target.value));
  $('ledger-prev').addEventListener('click', () => selectLedgerMonth(shiftMonth(ledgerMonth, -1)));
  $('ledger-next').addEventListener('click', () => selectLedgerMonth(shiftMonth(ledgerMonth, 1)));
  $('role-filters').addEventListener('submit', (event) => { event.preventDefault(); load(); });
  $('sync-button').addEventListener('click', sync);
  $('create-group-button').addEventListener('click', createGroup);
  $('rename-group-button').addEventListener('click', renameGroup);
  $('delete-group-button').addEventListener('click', deleteGroup);
  $('group-name').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      createGroup();
    }
  });
  $('group-select').addEventListener('change', async (event) => {
    selectedGroupId = event.target.value || null;
    selectedCharacterIds.clear();
    renderRoles();
    setWorkspaceAvailability();
    await load();
  });
  roles().then(load).catch((error) => message(error.message || '无法加载角色，请稍后再试。', true));
});
