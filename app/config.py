import re
from pathlib import Path

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
    database_path: Path = Path('data/eve_income.sqlite3')
    eve_user_agent: str = 'eve-income-tracker/0.1 (your-email@example.com)'
    esi_compatibility_date: str = '2026-05-19'

    @field_validator('database_path', mode='after')
    @classmethod
    def resolve_database_path(cls, path: Path) -> Path:
        if path.is_absolute():
            return path
        return PROJECT_ROOT / path

    def configuration_issues(self) -> tuple[str, ...]:
        issues = []
        if not self.eve_client_id.strip():
            issues.append('EVE_CLIENT_ID')
        if not self.eve_client_secret.strip():
            issues.append('EVE_CLIENT_SECRET')
        if len(self.app_session_secret) < 32:
            issues.append('APP_SESSION_SECRET')
        if not self._has_valid_fernet_key():
            issues.append('APP_ENCRYPTION_KEY')
        if not self._has_valid_user_agent():
            issues.append('EVE_USER_AGENT')
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

    def _has_valid_user_agent(self) -> bool:
        normalized = self.eve_user_agent.lower()
        return (
            'eve-income-tracker' in normalized
            and 'your-email@example.com' not in normalized
            and re.search(r'[^()\s]+@[^()\s]+\.[^()\s]+', self.eve_user_agent) is not None
        )
