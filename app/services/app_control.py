from __future__ import annotations

import hmac
import logging
import os
import secrets
import signal


logger = logging.getLogger(__name__)

CONTROL_TOKEN_ENV = "STREAMDOCK_CONTROL_TOKEN"
CONTROL_TOKEN_HEADER = "X-StreamDock-Token"
CONTROL_TOKEN_RESPONSE_HEADER = "X-StreamDock-Control-Token"
_RUNTIME_CONTROL_TOKEN = secrets.token_urlsafe(48)


class InvalidControlTokenError(ValueError):
    """Получен неверный секрет управляемого запуска."""


def get_control_token() -> str:
    """Возвращает секрет текущего процесса для локального помощника."""
    return os.environ.get(CONTROL_TOKEN_ENV, "").strip() or _RUNTIME_CONTROL_TOKEN


def validate_control_token(provided_token: str | None) -> None:
    expected_token = get_control_token()
    provided_bytes = (provided_token or "").encode("utf-8")
    expected_bytes = expected_token.encode("utf-8")
    if not hmac.compare_digest(provided_bytes, expected_bytes):
        raise InvalidControlTokenError


def signal_process_shutdown() -> None:
    """Посылает текущему процессу сигнал штатного завершения."""
    logger.info("Получена подтверждённая команда остановки приложения")
    signal.raise_signal(signal.SIGTERM)
