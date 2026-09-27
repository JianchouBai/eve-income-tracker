import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / '.env',
        env_file_encoding='utf-8',
        extra='ignore',
        validate_default=True,
    )

    eve_client_id: str = ''
    eve_client_secret: str = ''
    eve_callback_url: str = 'http://localhost:8000/auth/callback'
    app_session_secret: str = ''
    app_encryption_key: str = ''
    database_path: Path | None = Path('data/eve_income.sqlite3')
    eve_user_agent: str = 'eve-income-tracker/0.1 (your-email@example.com)'
    esi_compatibility_date: str = '2026-05-19'

    @field_validator('database_path', mode='before')
    @classmethod
    def preserve_empty_database_path(cls, path: str | Path | None) -> str | Path | None:
        if isinstance(path, str) and not path.strip():
            return None
        return path

    @field_validator('database_path', mode='after')
    @classmethod
    def resolve_database_path(cls, path: Path | None) -> Path | None:
        if path is None:
            return None
        if path.is_absolute():
            return path
        return PROJECT_ROOT / path

    def configuration_issues(self) -> tuple[str, ...]:
        issues = []
        if not self.eve_client_id.strip():
            issues.append('EVE_CLIENT_ID')
        if not self.eve_client_secret.strip():
            issues.append('EVE_CLIENT_SECRET')
        if not self._has_valid_callback_url():
            issues.append('EVE_CALLBACK_URL')
        if len(self.app_session_secret) < 32:
            issues.append('APP_SESSION_SECRET')
        if not self._has_valid_fernet_key():
            issues.append('APP_ENCRYPTION_KEY')
        if not self._has_valid_database_path():
            issues.append('DATABASE_PATH')
        if not self._has_valid_user_agent():
            issues.append('EVE_USER_AGENT')
        if not self._has_valid_esi_compatibility_date():
            issues.append('ESI_COMPATIBILITY_DATE')
        return tuple(issues)

    @property
    def is_esi_ready(self) -> bool:
        return not self.configuration_issues()

    def _has_valid_fernet_key(self) -> bool:
        try:
            Fernet(self.app_encryption_key.encode('utf-8'))
        except (TypeError, ValueError):
            return False
        return True

    def _has_valid_callback_url(self) -> bool:
        parsed = urlparse(self.eve_callback_url)
        return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)

    def _has_valid_database_path(self) -> bool:
        return self.database_path is not None

    def _has_valid_esi_compatibility_date(self) -> bool:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', self.esi_compatibility_date):
            return False
        try:
            date.fromisoformat(self.esi_compatibility_date)
        except ValueError:
            return False
        return True

    def _has_valid_user_agent(self) -> bool:
        normalized = self.eve_user_agent.lower()
        return (
            'eve-income-tracker' in normalized
            and 'your-email@example.com' not in normalized
            and re.search(
                r'\b[A-Za-z0-9!#$%&*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&*+/=?^_`{|}~-]+)*@'
                r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+'
                r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\b',
                self.eve_user_agent,
            )
            is not None
        )
