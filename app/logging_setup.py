from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DESKTOP_HANDLER_MARKER = "_eve_desktop_handler"
_REDACTED = "[REDACTED]"
_CALLBACK_QUERY = re.compile(r"(?i)(https?://[^\s?#]+/auth/callback)\?[^\s#]*")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:code|state|code_verifier|access_token|refresh_token|"
    r"client_secret|token)=)[^&#\s]*"
)
_NAMED_SECRET = re.compile(
    r"""(?ix)
    (
        ['"]?
        (?:state|code_verifier|access_token|refresh_token|client_secret|
           app_encryption_key|encryption_key|fernet_key|key|token|
           cookie|description|password)
        ['"]?
        \s*[:=]\s*
    )
    (?:"[^"]*"|'[^']*'|[^\s,;&]+)
    """
)
_AUTH_SCHEME = re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_HEADER = re.compile(
    r"(?i)(\b(?:Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*)"
    r"[^\r\n]*"
)


def _redact(text: str) -> str:
    text = _CALLBACK_QUERY.sub(rf"\1?{_REDACTED}", text)
    text = _QUERY_SECRET.sub(rf"\1{_REDACTED}", text)
    text = _NAMED_SECRET.sub(rf"\1{_REDACTED}", text)
    text = _AUTH_SCHEME.sub(rf"\1 {_REDACTED}", text)
    return _SECRET_HEADER.sub(rf"\1{_REDACTED}", text)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def configure_desktop_logging(path: Path) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    close_desktop_logging()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    setattr(handler, _DESKTOP_HANDLER_MARKER, True)
    handler.setFormatter(
        _RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def close_desktop_logging() -> None:
    root_logger = logging.getLogger()
    for handler in tuple(root_logger.handlers):
        if not getattr(handler, _DESKTOP_HANDLER_MARKER, False):
            continue
        root_logger.removeHandler(handler)
        handler.flush()
        handler.close()
