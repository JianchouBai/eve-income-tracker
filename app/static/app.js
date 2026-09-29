const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name=csrf-token]').content;
let report;
let selectedCharacterIds = new Set();
let charactersById = new Map();

function beijingDate(offset = 0) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + offset);
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(date);
}

function isk(value) {
  const [whole, fraction] = String(value).split('.');
  const sign = whole.startsWith('-') ? '-' : '';
  const digits = whole.replace('-', '');
  return `${sign}${digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction ? `.${fraction}` : ''} ISK`;
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
  selectedCharacterIds.forEach((characterId) => params.append('character_id', characterId));
  return params;
}

async function get(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error('request failed');
  return response.json();
}

function message(text, error = false) {
  const element = $('status-message');
  element.textContent = text;
  element.className = `notice status-bar${error ? ' error' : ''}`;
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
  $('summary-income').textContent = isk(data.income);
  $('summary-tax').textContent = isk(data.tax);
  $('summary-net').textContent = isk(data.net);
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
  try {
    const parameters = query();
    const [data, entries] = await Promise.all([
      get(`/api/dashboard?${parameters}`), get(`/api/entries?${parameters}`),
    ]);
    render(data, entries);
    message('报表已更新。');
  } catch {
    message('无法加载报表，请稍后再试。', true);
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
  } catch {
    button.disabled = false;
    message('无法更新角色统计状态，请稍后再试。', true);
  }
}

async function roles() {
  const currentSelection = checkedCharacterIds();
  if (currentSelection.size) selectedCharacterIds = currentSelection;
  const data = await get('/api/characters');
  charactersById = new Map(data.characters.map((character) => [String(character.character_id), character]));
  const box = $('enable-region');
  box.replaceChildren();
  data.characters.forEach((character) => {
    const row = document.createElement('div');
    row.className = 'role-row';
    const label = document.createElement('label');
    const reportSelect = document.createElement('input');
    reportSelect.type = 'checkbox';
    reportSelect.value = character.character_id;
    reportSelect.dataset.reportSelect = 'true';
    reportSelect.checked = selectedCharacterIds.size ? selectedCharacterIds.has(String(character.character_id)) : character.enabled;
    const text = document.createElement('span');
    text.textContent = `报表：${character.name} (${character.character_id})`;
    const status = document.createElement('small');
    status.textContent = roleStatus(character);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.textContent = character.enabled ? '停止统计' : '启用统计';
    toggle.addEventListener('click', () => setEnabled(character.character_id, !character.enabled, toggle));
    label.append(reportSelect, text);
    row.append(label, status, toggle);
    box.append(row);
  });
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
  const button = $('sync-button');
  button.disabled = true;
  try {
    const response = await fetch('/api/sync', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
    if (!response.ok) throw new Error('request failed');
    const payload = await response.json();
    renderSyncResults(payload.results || []);
    await roles();
    await load();
    message('同步完成；各角色结果见同步状态。');
  } catch {
    message('同步失败；当前报表已保留。', true);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (document.body.dataset.authenticated !== 'true') return;
  $('date-to').value = beijingDate();
  $('date-from').value = beijingDate(-29);
  $('role-filters').addEventListener('submit', (event) => { event.preventDefault(); load(); });
  $('sync-button').addEventListener('click', sync);
  roles().then(load).catch(() => message('无法加载角色，请稍后再试。', true));
  addEventListener('resize', () => report && draw(report.daily));
});
