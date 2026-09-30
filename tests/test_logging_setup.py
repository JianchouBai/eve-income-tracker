import importlib
import importlib.util
import logging
from logging.handlers import RotatingFileHandler


def logging_module():
    assert importlib.util.find_spec("app.logging_setup") is not None, (
        "app.logging_setup is missing"
    )
    return importlib.import_module("app.logging_setup")


def test_desktop_log_redacts_credentials_callback_queries_and_wallet_text(tmp_path):
    module = logging_module()
    log_path = tmp_path / "日志" / "eve-income-tracker.log"
    sentinels = [
        "private-code",
        "private-state",
        "private-verifier",
        "private-access",
        "private-refresh",
        "private-client-secret",
        "private-encryption-key",
        "private-basic",
        "private-bearer",
        "private-cookie",
        "private-wallet-description",
    ]

    module.configure_desktop_logging(log_path)
    logger = logging.getLogger("app.desktop.redaction_test")
    logger.info(
        "stage=token_exchange status=failed "
        "url=http://localhost:8000/auth/callback?code=private-code&state=private-state "
        "code_verifier=private-verifier access_token=private-access "
        "refresh_token=private-refresh client_secret=private-client-secret "
        "app_encryption_key=private-encryption-key "
        "Authorization: Basic private-basic Bearer private-bearer "
        "Cookie: session=private-cookie description=private-wallet-description"
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    rendered = log_path.read_text(encoding="utf-8")
    assert "stage=token_exchange" in rendered
    assert "status=failed" in rendered
    assert "[REDACTED]" in rendered
    for sentinel in sentinels:
        assert sentinel not in rendered

    module.close_desktop_logging()


def test_desktop_logging_is_one_utf8_rotating_handler_when_reconfigured(tmp_path):
    module = logging_module()
    first_path = tmp_path / "first.log"
    current_path = tmp_path / "current.log"

    module.configure_desktop_logging(first_path)
    module.configure_desktop_logging(current_path)

    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_eve_desktop_handler", False)
    ]
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.baseFilename == str(current_path.resolve())
    assert handler.encoding.lower().replace("-", "") == "utf8"
    assert handler.maxBytes > 0
    assert handler.backupCount > 0

    module.close_desktop_logging()
