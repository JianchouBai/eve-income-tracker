import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.storage import CharacterRepository, Database, JournalRepository
from app.web import AppServices, Clock, create_app


def client(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    services = AppServices(
        CharacterRepository(database), JournalRepository(database), None, None, None
    )
    settings = Settings(_env_file=None)
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
        "date-from",
        "date-to",
        "sync-button",
        "add-role-link",
        "reauthorize-link",
        "enable-region",
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
    assert "<aside class='command-rail' aria-label='角色与日期控制'>" in page
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
