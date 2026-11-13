"""The single UI operation boundary.

Every page and panel routes user-triggered work through this one function so the
error contract cannot drift between screens:

* a *known* failure (DomainError, DatabaseError, OSError, ValueError) keeps its
  message, because those messages are written for the user;
* anything else is reported generically while only the exception **class name**
  reaches the log — messages may quote private user data;
* the callback's value is returned unchanged, or ``None`` when it failed.

Pages pass their own `context` (log prefix) and `title` (dialog title); nothing
else differs, which is why there is exactly one implementation.
"""
import logging

from PySide6.QtWidgets import QMessageBox

from app.domain.errors import DomainError
from app.storage.database import DatabaseError

logger = logging.getLogger(__name__)

KNOWN_FAILURES = (DomainError, DatabaseError, OSError, ValueError)
DEFAULT_TITLE = "操作未完成"
UNKNOWN_MESSAGE = "发生技术错误，请查看本地错误日志。"


def run_operation(parent, callback, *, context: str = "UI", title: str = DEFAULT_TITLE):
    """Run `callback`, turning failures into user-facing dialogs and log lines."""
    try:
        return callback()
    except KNOWN_FAILURES as exc:
        logger.warning("%s operation failed: type=%s", context, type(exc).__name__)
        QMessageBox.warning(parent, title, str(exc))
    except Exception as exc:                      # noqa: BLE001 - boundary by design
        logger.error("%s unexpected failure: type=%s", context, type(exc).__name__)
        QMessageBox.critical(parent, title, UNKNOWN_MESSAGE)
    return None
