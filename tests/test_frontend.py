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
