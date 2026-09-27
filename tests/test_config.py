from pathlib import Path

from app.config import Settings


def test_configuration_issues_name_missing_fields_without_echoing_values():
    settings = Settings(
        _env_file=None,
        eve_client_id='',
        eve_client_secret='do-not-print',
        app_session_secret='short',
        app_encryption_key='invalid',
        eve_user_agent='eve-income-tracker/0.1 (your-email@example.com)',
    )

    issues = settings.configuration_issues()

    assert 'EVE_CLIENT_ID' in issues
    assert 'APP_SESSION_SECRET' in issues
    assert 'APP_ENCRYPTION_KEY' in issues
    assert 'EVE_USER_AGENT' in issues
    assert 'do-not-print' not in repr(issues)


def test_defaults_use_documented_callback_database_and_compatibility_date():
    settings = Settings(_env_file=None)
    project_root = Path(__file__).resolve().parents[1]

    assert settings.eve_callback_url == 'http://localhost:8000/auth/callback'
    assert settings.database_path == project_root / 'data' / 'eve_income.sqlite3'
    assert settings.esi_compatibility_date == '2026-05-19'


def test_relative_database_path_is_resolved_from_project_root():
    settings = Settings(_env_file=None, database_path='runtime/eve.sqlite3')
    project_root = Path(__file__).resolve().parents[1]

    assert settings.database_path == project_root / 'runtime' / 'eve.sqlite3'


def test_is_esi_ready_with_secure_secrets_and_contact_bearing_user_agent():
    settings = Settings(
        _env_file=None,
        eve_client_id='client-id',
        eve_client_secret='client-secret',
        app_session_secret='s' * 32,
        app_encryption_key='MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=',
        eve_user_agent='eve-income-tracker/0.1 (admin@example.com)',
    )

    assert settings.is_esi_ready is True
