import re

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.storage import (
    CharacterGroupRepository,
    CharacterRepository,
    Database,
    JournalRepository,
)
from app.web import AppServices, Clock, create_app


def client(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    services = AppServices(
        CharacterRepository(database),
        CharacterGroupRepository(database),
        JournalRepository(database),
        None,
        None,
        None,
    )
    settings = Settings(
        _env_file=None,
        eve_client_id="public-client-id",
        app_session_secret="frontend-session-secret-" * 3,
        app_encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "income.sqlite3",
        eve_user_agent="eve-income-tracker/0.1 (admin@example.com)",
    )
    return TestClient(create_app(settings, services, Clock()))


def test_landing_page_explains_scope_beijing_day_and_first_authorization_cutoff(
    tmp_path,
):
    with client(tmp_path) as browser:
        response = browser.get("/")

    assert response.status_code == 200
    assert "esi-wallet.read_character_wallet.v1" in response.text
    assert "北京时间" in response.text
    assert "首次授权" in response.text


def test_dashboard_shell_contains_semantic_regions_and_local_assets(tmp_path):
    with client(tmp_path) as browser:
        page = browser.get("/")
        css = browser.get("/static/app.css")
        js = browser.get("/static/app.js")

    assert page.status_code == 200
    for token in [
        "role-filters",
        "group-select",
        "group-name",
        "create-group-button",
        "rename-group-button",
        "delete-group-button",
        "active-group-name",
        "date-from",
        "date-to",
        "sync-button",
        "add-role-link",
        "reauthorize-link",
        "enable-region",
        "ungrouped-region",
        "summary-income",
        "summary-tax",
        "summary-net",
        "trend-canvas",
        "category-breakdown",
        "character-breakdown",
        "daily-table",
        "entry-details",
    ]:
        assert token in page.text
    assert "canvas" in page.text and "收入趋势图" in page.text
    assert css.headers["content-type"].startswith("text/css")
    assert js.headers["content-type"].startswith("text/javascript")
    assert "https://" not in page.text
    assert "http://" not in page.text


def test_dashboard_exposes_financial_hierarchy_and_chart_legend(tmp_path):
    with client(tmp_path) as browser:
        page = browser.get("/").text

    assert "id='page-title'" in page
    assert "<aside class='command-rail' aria-label='角色组、角色与日期控制'>" in page
    assert "<section class='summary-ledger' aria-label='收益汇总'>" in page
    assert "aria-describedby='trend-legend trend-fallback'" in page
    assert "id='trend-legend'" in page


def test_dashboard_script_contract_covers_enable_sync_status_decimal_details_and_canvas_fallback(
    tmp_path,
):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text
        page = browser.get("/").text

    for token in [
        "/api/characters/${characterId}/enabled",
        "Content-Type",
        "JSON.stringify",
        "X-CSRF-Token",
        "sync-results",
        "last_synced_at",
        "isk(x.amount)",
        "getContext('2d')",
        "if (!ctx)",
        "BigInt",
        "drawSeries",
        "roles().then(load).catch",
        "data.income",
        "name",
        "data-report-select",
        "result.error.message",
        "button.disabled = true",
        "trend-fallback",
    ]:
        assert token in script
    assert "sync-results" in page
    assert "Number(x.net)" not in script
    assert "innerHTML" not in script


def test_group_workspace_has_accessible_plain_language_controls(tmp_path):
    with client(tmp_path) as browser:
        page = browser.get("/").text

    for token in [
        "<label for='group-select'>角色组</label>",
        "<label for='group-name'>组名</label>",
        "maxlength='50'",
        ">创建组</button>",
        ">重命名</button>",
        ">删除组</button>",
        "当前角色组",
        "未分组角色",
    ]:
        assert token in page


def test_group_workspace_script_calls_group_and_membership_apis(tmp_path):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text

    for token in [
        "get('/api/groups')",
        "request('/api/groups',",
        "request(`/api/groups/${selectedGroupId}`",
        "request(`/api/characters/${characterId}/group`",
        "method: 'POST'",
        "method: 'PATCH'",
        "method: 'DELETE'",
        "JSON.stringify({ name })",
        "JSON.stringify({ group_id: groupId })",
        "params.set('group_id', String(selectedGroupId))",
        "`/api/sync?group_id=${selectedGroupId}`",
    ]:
        assert token in script
    assert "innerHTML" not in script


def test_group_workspace_script_preserves_data_and_guides_empty_states(tmp_path):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text

    assert "删除后，组内角色将移回未分组，钱包流水不会被删除。确定删除吗？" in script
    assert (
        "selectedGroupId = groups.length ? String(groups[0].group_id) : null;" in script
    )
    assert "先创建角色组，再把已授权角色加入组内。" in script
    assert "这个角色组还没有成员，请从下方未分组角色中添加。" in script
    assert "当前没有未分组角色。" in script
    assert "setWorkspaceAvailability" in script
    availability = script.split("function setWorkspaceAvailability() {", 1)[1].split(
        "function renderGroupOptions", 1
    )[0]
    assert "$('report-button').disabled = !hasGroup;" in availability
    assert "$('sync-button').disabled = !hasGroup;" in availability
    assert "$('report-deck').hidden = !hasGroup;" in availability


def test_group_workspace_keeps_feedback_visible_without_a_group(tmp_path):
    with client(tmp_path) as browser:
        page = browser.get("/").text

    status_position = page.index("id='status-message'")
    report_position = page.index("id='report-deck'")
    assert status_position < report_position


def test_report_load_rejects_out_of_order_group_responses(tmp_path):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text

    load = script.split("async function load() {", 1)[1].split(
        "function roleStatus", 1
    )[0]
    assert "let reportRequestGeneration = 0;" in script
    assert "const generation = ++reportRequestGeneration;" in load
    assert "const requestedGroupId = selectedGroupId;" in load
    assert "generation !== reportRequestGeneration" in load
    assert "requestedGroupId !== selectedGroupId" in load
    assert "$('report-deck').hidden = true;" in load
    assert "$('report-deck').hidden = false;" in load
    assert "正在加载当前角色组报表" in load


def test_dashboard_chart_contract_uses_one_exact_scale_for_all_money_series(tmp_path):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text

    # The served asset must pool all three monetary series before finding its
    # exact maximum; otherwise income=100, tax=10, net=90 each peak at 1.
    scale = script.split("function chartScale(days) {", 1)[1].split(
        "function normalizedRatios", 1
    )[0]
    assert "days.flatMap" in scale
    assert "['income', 'tax', 'net'].map((key) => decimalPart(day[key]))" in scale
    assert "part.value * (10n ** (scale - part.scale))" in scale
    assert "const absolute = value < 0n ? -value : value;" in scale
    assert "absolute > largest ? absolute : largest" in scale
    assert "return { scale, maximum };" in scale

    normalize = script.split("function normalizedRatios", 1)[1].split(
        "function drawSeries", 1
    )[0]
    assert "chart.scale - part.scale" in normalize
    assert "const maximum = chart.maximum;" in normalize
    assert "reduce(" not in normalize
    assert "if (maximum === 0n) return values.map(() => 0);" in normalize
    assert "const chart = chartScale(days);" in script
    for key in ("income", "tax", "net"):
        assert f"normalizedRatios(days, '{key}', chart)" in script

    # Audit every Number call, so a new raw monetary expression cannot slip
    # through a blacklist that checks only particular variable names.
    assert re.findall(r"\bNumber\s*\((.*?)\)", script, re.DOTALL) == [
        "`${negative ? '-' : ''}${ratio}`"
    ]


def test_dashboard_script_keeps_maximum_chart_ratio_and_role_controls_separate(
    tmp_path,
):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text

    assert "const integer = scaled / 1000000n;" in script
    assert "const fraction = scaled % 1000000n;" in script
    assert r"""const row = document.createElement('div');""" in script
    assert "label.append(reportSelect, text);" in script
    assert "row.append(label, status, toggle);" in script
    assert "label.append(reportSelect, text, status, toggle);" not in script


def test_desktop_authorization_script_uses_only_allowlisted_bridge_and_bounded_poll(
    tmp_path,
):
    with client(tmp_path) as browser:
        script = browser.get("/static/app.js").text
        page = browser.get("/").text

    for token in [
        "/api/authorization-attempts",
        "window.pywebview.api.open_authorization_url",
        "X-CSRF-Token",
        "authorization_url",
        "attempt_id",
        "location.reload()",
        "授权未完成，请重试。",
    ]:
        assert token in script
    assert "started.expires_in_seconds" in script
    assert "Date.now() < deadline" in script
    assert "for (let poll = 0; poll < 120; poll += 1)" not in script
    assert "window.open(" not in script
    assert "href='/auth/login'" in page


def test_storage_dialog_is_desktop_only_and_versions_are_dynamic(tmp_path, monkeypatch):
    from app import web
    from app.auth_attempts import AuthorizationAttemptStore
    from app.desktop_session import DesktopLaunchTokenStore
    from app.web import RuntimeContext

    monkeypatch.setattr(web, "APP_VERSION", "8.7.6", raising=False)
    with client(tmp_path) as browser:
        assert "id='data-settings-dialog'" not in browser.get("/").text
    settings = Settings(
        _env_file=None,
        runtime_mode="desktop",
        eve_client_id="public",
        app_session_secret="s" * 48,
        app_encryption_key=Fernet.generate_key().decode(),
    )
    db = Database(tmp_path / "desktop.sqlite3")
    services = AppServices(
        CharacterRepository(db),
        CharacterGroupRepository(db),
        JournalRepository(db),
        None,
        None,
        None,
    )
    runtime = RuntimeContext(
        "desktop", AuthorizationAttemptStore(), DesktopLaunchTokenStore()
    )
    with TestClient(create_app(settings, services, Clock(), runtime)) as browser:
        page = browser.get("/").text
    assert "V8.7" in page and "V8.7.6" in page
    assert "<dialog id='data-settings-dialog'" in page
    assert "aria-labelledby='data-settings-title'" in page
    for label in (
        "数据存储",
        "数据库",
        "日志",
        "备份",
        "WebView",
        "总占用",
        "原数据目录",
        "七天",
        "三次",
    ):
        assert label in page
    assert "id='storage-create-recovery' type='checkbox' checked" in page
    assert "id='storage-error'" in page and "role='alert'" in page
    assert page.count("id='storage-cleanup-path'") == 1
    assert "待清理的原数据目录" in page


def test_storage_javascript_flow_focus_failures_and_byte_formatting():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for executable frontend contract")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(id) { this.id = id; this.listeners = {}; this.disabled = false; this.hidden = false;
    this.textContent = ''; this.checked = true; this.open = false; this.dataset = {}; this.className = ''; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
  async fire(name = 'click') { return this.listeners[name]?.({preventDefault() {}, currentTarget: this, key: 'Escape'}); }
}
const nodes = new Map();
const element = id => { if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id); };
global.document = { getElementById: element, querySelector: () => ({content: 'csrf-known'}),
  querySelectorAll: () => [], body: {dataset: {runtimeMode:'desktop', authenticated:'true'}},
  addEventListener() {}, activeElement: element('settings-button') };
const calls = [];
let failure = null, selected = 'E:/', pending = false;
const status = {current_path:'D:/<unsafe>', volume:'D:', usage:{database:100, logs:20, backups:30,
  webview:10, config:5, other:5, recovery:40, total:210},
  recovery:{created_at:'2026-10-01T00:00:00Z', size_bytes:40, successful_starts:2,
  retention_days:7, required_starts:3}, pending_migration:false, pending_cleanup:true, pending_cleanup_path:'F:/<leftover>/EVE Income Tracker', busy:false};
global.window = {pywebview:{api:{
  async choose_data_parent() { calls.push('picker'); return selected; },
  async open_data_directory() { calls.push('open'); return true; },
  async restart_for_migration() { assert.equal(pending,true); calls.push('restart'); return true; }
}}};
global.fetch = async (path, options={}) => {
  calls.push([path, options]);
  if (options.method && options.method !== 'GET') assert.equal(options.headers['X-CSRF-Token'], 'csrf-known');
  if (failure && path.endsWith(failure)) return {ok:false,json:async()=>({error:{message:'持久错误'}})};
  let data = status;
  if (path.endsWith('/estimate')) {
    const payload=JSON.parse(options.body); assert.equal(payload.target_parent,'E:/');
    data={target_path:'E:/EVE Income Tracker', copy_bytes:100,recovery_bytes:payload.create_recovery?40:0,
      free_bytes:200000,space_sufficient:true};
  }
  if (path.endsWith('/migrations')) { pending=true; data={scheduled:true,target_path:'E:/EVE Income Tracker'}; }
  if (path.endsWith('/recovery')) data={freed_bytes:40};
  return {ok:true,json:async()=>data};
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  assert.equal(formatBytes(-1), '—'); assert.equal(formatBytes(NaN), '—');
  assert.equal(formatBytes('123'), '—'); assert.equal(formatBytes(1024), '1.0 KiB');
  assert.equal(formatBytes(0), '0 B');
  bindDataSettings();
  await element('settings-button').fire();
  assert.equal(element('data-settings-dialog').open,true);
  assert.equal(document.activeElement.id,'data-settings-close');
  assert.equal(element('storage-current-path').textContent,'D:/<unsafe>');
  assert.equal(element('storage-cleanup-path').textContent,'F:/<leftover>/EVE Income Tracker');
  assert.equal(element('storage-cleanup-notice').hidden,false);
  status.pending_cleanup=false;
  status.pending_cleanup_path=null;
  element('data-settings-dialog').close();
  await element('settings-button').fire();
  assert.equal(element('storage-cleanup-path').textContent,'');
  assert.equal(element('storage-cleanup-notice').hidden,true);
  await element('storage-open').fire();
  assert(calls.includes('open'));
  selected=null;
  await element('storage-change').fire();
  assert.equal(calls.filter(x=>Array.isArray(x)&&x[0].endsWith('/estimate')).length,0);
  selected='E:/';
  await element('storage-change').fire();
  assert.equal(element('storage-confirmation').hidden,false);
  assert.equal(element('storage-target-path').textContent,'E:/EVE Income Tracker');
  assert.equal(document.activeElement.id,'storage-confirm');
  element('storage-create-recovery').checked=false;
  await element('storage-create-recovery').fire('change');
  assert.equal(element('storage-recovery-bytes').textContent,'0 B');
  failure='/migrations';
  await element('storage-confirm').fire();
  assert.equal(calls.includes('restart'),false);
  assert.equal(element('storage-error').hidden,false);
  assert.equal(element('storage-error').textContent,'持久错误');
  assert.equal(element('storage-confirm').disabled,false);
  failure=null;
  await element('storage-confirm').fire();
  assert.equal(calls.at(-1),'restart');
  element('data-settings-dialog').close();
  assert.equal(document.activeElement.id,'settings-button');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script, str(Path(__file__).parents[1] / "app/static/app.js")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
