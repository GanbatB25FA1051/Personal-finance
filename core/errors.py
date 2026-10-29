"""Safe identifiers and metadata-only logging for user-facing errors."""

import logging
import secrets
import traceback
from pathlib import Path


def log_ui_error(page: str, error: BaseException) -> str:
    """Log stack locations without exception text, which may contain private data."""
    error_id = secrets.token_hex(4)
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    locations = [
        (Path(frame.filename).name, frame.lineno, frame.name)
        for frame in frames[-8:]
    ]
    logging.getLogger('finance.ui').error(
        'UI page failure error_id=%s page=%s error_type=%s stack=%s',
        error_id, page, type(error).__name__, locations,
    )
    return error_id
