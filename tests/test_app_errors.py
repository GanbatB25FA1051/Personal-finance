"""User-facing app errors must not leak exception contents."""

import logging
import re

from core.errors import log_ui_error


def test_ui_error_log_omits_exception_message_and_returns_short_id(caplog):
    secret_marker = 'private-password-do-not-log'
    error = RuntimeError(secret_marker)
    with caplog.at_level(logging.ERROR, logger='finance.ui'):
        error_id = log_ui_error('Home', error)
    assert re.fullmatch(r'[0-9a-f]{8}', error_id)
    assert secret_marker not in caplog.text
    assert error_id in caplog.text


def test_render_requires_neon_database_but_local_can_use_sqlite():
    from core.deployment import hosted_database_configured

    assert hosted_database_configured({'RENDER': 'true', 'DATABASE_URL': 'postgresql://configured'})
    assert not hosted_database_configured({'RENDER': 'true', 'DATABASE_URL': '  '})
    assert hosted_database_configured({'DATABASE_URL': ''})
