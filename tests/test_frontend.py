from fastapi.testclient import TestClient

from app.config import Settings
from app.storage import CharacterRepository, Database, JournalRepository
from app.web import AppServices, Clock, create_app


def client(tmp_path):
    database = Database(tmp_path / 'income.sqlite3')
    services = AppServices(CharacterRepository(database), JournalRepository(database), None, None, None)
    settings = Settings(_env_file=None)
    return TestClient(create_app(settings, services, Clock()))


def test_landing_page_explains_scope_beijing_day_and_first_authorization_cutoff(tmp_path):
    with client(tmp_path) as browser:
        response = browser.get('/')

    assert response.status_code == 200
    assert 'esi-wallet.read_character_wallet.v1' in response.text
    assert '北京时间' in response.text
    assert '首次授权' in response.text


def test_dashboard_shell_contains_semantic_regions_and_local_assets(tmp_path):
    with client(tmp_path) as browser:
        page = browser.get('/')
        css = browser.get('/static/app.css')
        js = browser.get('/static/app.js')

    assert page.status_code == 200
    for token in ['role-filters', 'date-from', 'date-to', 'sync-button', 'add-role-link', 'reauthorize-link', 'enable-region', 'summary-income', 'summary-tax', 'summary-net', 'trend-canvas', 'category-breakdown', 'character-breakdown', 'daily-table', 'entry-details']:
        assert token in page.text
    assert 'canvas' in page.text and '收入趋势图' in page.text
    assert css.headers['content-type'].startswith('text/css')
    assert js.headers['content-type'].startswith('text/javascript')
    assert 'https://' not in page.text
    assert 'http://' not in page.text


def test_dashboard_script_contract_covers_enable_sync_status_decimal_details_and_canvas_fallback(tmp_path):
    with client(tmp_path) as browser:
        script = browser.get('/static/app.js').text
        page = browser.get('/').text

    for token in [
        '/api/characters/${characterId}/enabled',
        'Content-Type',
        'JSON.stringify',
        'X-CSRF-Token',
        'sync-results',
        'last_synced_at',
        'isk(x.amount)',
        'getContext(\'2d\')',
        'if (!ctx)',
        'BigInt',
        'drawSeries',
        'roles().then(load).catch',
        'data.income',
        'name',
        'data-report-select',
        'result.error.message',
        'button.disabled = true',
        'trend-fallback',
    ]:
        assert token in script
    assert 'sync-results' in page
    assert 'Number(x.net)' not in script
    assert 'innerHTML' not in script


def test_dashboard_script_keeps_maximum_chart_ratio_and_role_controls_separate(tmp_path):
    with client(tmp_path) as browser:
        script = browser.get('/static/app.js').text

    assert 'const integer = scaled / 1000000n;' in script
    assert 'const fraction = scaled % 1000000n;' in script
    assert r'''const row = document.createElement('div');''' in script
    assert 'label.append(reportSelect, text);' in script
    assert 'row.append(label, status, toggle);' in script
    assert 'label.append(reportSelect, text, status, toggle);' not in script
