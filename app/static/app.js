const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name=csrf-token]').content;
let report;
let reportRequestGeneration = 0;
let groups = [];
let selectedGroupId = null;
let selectedCharacterIds = new Set();
let charactersById = new Map();

function beijingDate(offset = 0) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + offset);
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(date);
}

function isk(value, fractionDigits = 2) {
  const [whole, rawFraction = ''] = String(value).split('.');
  const sign = whole.startsWith('-') ? '-' : '';
  const digits = whole.replace('-', '');
  const fraction = rawFraction.padEnd(fractionDigits, '0').slice(0, fractionDigits);
  return `${sign}${digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fractionDigits ? `.${fraction}` : ''} ISK`;
}

function cell(value) {
  const td = document.createElement('td');
  td.textContent = isk(value);
  return td;
}

function checkedCharacterIds() {
  return new Set([...document.querySelectorAll('[data-report-select]:checked')].map((input) => input.value));
}

function query() {
  selectedCharacterIds = checkedCharacterIds();
  const params = new URLSearchParams({ from: $('date-from').value, to: $('date-to').value });
  params.set('group_id', String(selectedGroupId));
  selectedCharacterIds.forEach((characterId) => params.append('character_id', characterId));
  return params;
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

function breakdown(target, title, items, label) {
  target.replaceChildren();
  const marker = document.createElement('span');
  marker.className = 'section-index';
  marker.textContent = title === '类型分项' ? '来源' : '舰长';
  const heading = document.createElement('h2');
  heading.textContent = title;
  target.append(marker, heading);
  if (!items.length) {
    const empty = document.createElement('p');
    empty.textContent = '暂无数据';
    target.append(empty);
    return;
  }
  items.forEach((item) => {
    const line = document.createElement('p');
    line.textContent = `${item[label]}：${isk(item.net)}`;
    target.append(line);
  });
}

function decimalPart(value) {
  const match = /^(-?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(String(value));
  if (!match) return { value: 0n, scale: 0n };
  const digits = `${match[2]}${match[3] || ''}`.replace(/^0+(?=\d)/, '') || '0';
  const scale = BigInt((match[3] || '').length) - BigInt(match[4] || '0');
  return { value: (match[1] === '-' ? -1n : 1n) * BigInt(digits), scale };
}

function chartScale(days) {
  const parts = days.flatMap((day) => ['income', 'tax', 'net'].map((key) => decimalPart(day[key])));
  const scale = parts.reduce((largest, part) => (part.scale > largest ? part.scale : largest), 0n);
  const values = parts.map((part) => part.value * (10n ** (scale - part.scale)));
  const maximum = values.reduce((largest, value) => {
    const absolute = value < 0n ? -value : value;
    return absolute > largest ? absolute : largest;
  }, 0n);
  return { scale, maximum };
}

function normalizedRatios(days, key, chart) {
  const parts = days.map((day) => decimalPart(day[key]));
  const values = parts.map((part) => part.value * (10n ** (chart.scale - part.scale)));
  const maximum = chart.maximum;
  if (maximum === 0n) return values.map(() => 0);
  return values.map((value) => {
    const negative = value < 0n;
    const scaled = ((negative ? -value : value) * 1000000n) / maximum;
    const integer = scaled / 1000000n;
    const fraction = scaled % 1000000n;
    const ratio = `${integer}.${fraction.toString().padStart(6, '0')}`;
    return Number(`${negative ? '-' : ''}${ratio}`);
  });
}

function drawSeries(context, ratios, color, width, height) {
  if (!ratios.length) return;
  const left = 10;
  const right = width - 10;
  const top = 16;
  const bottom = height - 30;
  const center = top + ((bottom - top) / 2);
  context.beginPath();
  ratios.forEach((ratio, index) => {
    const x = ratios.length === 1 ? width / 2 : left + ((index / (ratios.length - 1)) * (right - left));
    const y = center - ratio * ((bottom - top) / 2);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = color;
  context.lineWidth = color === '#e6eef2' ? 2.4 : 1.6;
  context.lineJoin = 'round';
  context.lineCap = 'round';
  context.stroke();
}

function draw(days) {
  const canvas = $('trend-canvas');
  const fallback = $('trend-fallback');
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    fallback.textContent = '图表不可用；请查看下方每日明细表中的收入、税费和税后收入。';
    fallback.classList.add('chart-fallback-visible');
    return;
  }
  fallback.classList.remove('chart-fallback-visible');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.font = '11px "Microsoft YaHei UI", sans-serif';
  ctx.fillStyle = 'rgba(230, 238, 242, 0.42)';
  ctx.textBaseline = 'bottom';
  ctx.setLineDash([3, 7]);
  [16, height / 2 - 7, height - 30].forEach((y, index) => {
    ctx.beginPath();
    ctx.moveTo(10, y);
    ctx.lineTo(width - 10, y);
    ctx.strokeStyle = index === 1 ? 'rgba(114, 214, 207, 0.28)' : 'rgba(41, 64, 77, 0.72)';
    ctx.lineWidth = 1;
    ctx.stroke();
  });
  ctx.setLineDash([]);

  if (days.length) {
    const labelStep = Math.max(1, Math.ceil((days.length - 1) / 4));
    days.forEach((day, index) => {
      if (index % labelStep !== 0 && index !== days.length - 1) return;
      const x = days.length === 1 ? width / 2 : 10 + ((index / (days.length - 1)) * (width - 20));
      ctx.textAlign = index === 0 ? 'left' : (index === days.length - 1 ? 'right' : 'center');
      ctx.fillText(day.date.slice(5), x, height - 5);
    });
  }

  const chart = chartScale(days);
  drawSeries(ctx, normalizedRatios(days, 'income', chart), '#72d6cf', width, height);
  drawSeries(ctx, normalizedRatios(days, 'tax', chart), '#e9a95f', width, height);
  drawSeries(ctx, normalizedRatios(days, 'net', chart), '#e6eef2', width, height);
  canvas.setAttribute('aria-label', '收入（青色）、税费（琥珀色）和税后收入（白色）趋势图；下方每日明细表提供等价文本数据。');
  fallback.textContent = `图表文本降级：${days.map((day) => `${day.date}，到账 ${isk(day.income)}，税费 ${isk(day.tax)}，税后 ${isk(day.net)}`).join('；') || '暂无数据。'}`;
}

function render(data, entries) {
  report = data;
  $('summary-income').textContent = isk(data.income, 0);
  $('summary-tax').textContent = isk(data.tax, 0);
  $('summary-net').textContent = isk(data.net, 0);
  rows($('daily-table').tBodies[0], data.daily, [
    { key: 'date' }, { key: 'income', money: true }, { key: 'tax', money: true }, { key: 'net', money: true },
  ]);
  breakdown($('category-breakdown'), '类型分项', data.categories, 'category');
  breakdown($('character-breakdown'), '角色分项', data.characters, 'name');
  rows($('entry-details').querySelector('tbody'), entries.entries, [
    { key: 'date' }, { key: 'character_name' }, { key: 'category' }, { key: 'amount', format: (x) => isk(x.amount) }, { key: 'description' },
  ]);
  draw(data.daily);
}

async function load() {
  const generation = ++reportRequestGeneration;
  const requestedGroupId = selectedGroupId;
  if (!requestedGroupId) {
    $('report-deck').hidden = true;
    return;
  }
  $('report-deck').hidden = true;
  $('report-deck').setAttribute('aria-busy', 'true');
  message('正在加载当前角色组报表…');
  try {
    const parameters = query();
    const [data, entries] = await Promise.all([
      get(`/api/dashboard?${parameters}`), get(`/api/entries?${parameters}`),
    ]);
    if (generation !== reportRequestGeneration || requestedGroupId !== selectedGroupId) return;
    render(data, entries);
    $('report-deck').hidden = false;
    message('报表已更新。');
  } catch (error) {
    if (generation !== reportRequestGeneration || requestedGroupId !== selectedGroupId) return;
    message(error.message || '无法加载报表，请稍后再试。', true);
  } finally {
    if (generation === reportRequestGeneration) $('report-deck').removeAttribute('aria-busy');
  }
}

function roleStatus(character) {
  if (character.authorization_required) return '需重新授权';
  return character.last_synced_at ? `上次同步：${character.last_synced_at}` : '未同步';
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
  const title = document.createElement('h2');
  title.textContent = '同步状态';
  region.append(title);
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
  $('date-to').value = beijingDate();
  $('date-from').value = beijingDate(-29);
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
  addEventListener('resize', () => report && draw(report.daily));
});
