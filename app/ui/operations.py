"""The single UI operation boundary.

Every page and panel routes user-triggered work through this one function so the
error contract cannot drift between screens:

* a *known* failure (DomainError, DatabaseError, OSError, ValueError) keeps its
  message, because those messages are written for the user;
* a ``ConflictError`` additionally reaches ``on_conflict`` so the page can keep a
  persistent, non-modal ``ConflictBanner`` visible (Phase 11). The dialog is still
  shown: the historical contract of "the Service's text reaches the user" holds, and
  the banner makes the conflict state explicit instead of a transient box;
* anything else is reported generically while only the exception **class name**
  reaches the log — messages may quote private user data;
* the callback's value is returned unchanged, or ``None`` when it failed.

Phase 11 decision: the UI never classifies a conflict by its message text. The banner
shows the Service's own user message (``safe_user_message``), which is already written
for a person; ``tokens.CONFLICT_MESSAGE`` is the fallback for a conflict that carries
no text. There is no ``expected_updated_at`` and no ConflictError taxonomy change, so
only operations that already have an expected-state guard can report a stale snapshot.
"""
import logging

from PySide6.QtWidgets import QMessageBox

from app.domain.errors import ConflictError, DomainError
from app.storage.database import DatabaseError

logger = logging.getLogger(__name__)

KNOWN_FAILURES = (DomainError, DatabaseError, OSError, ValueError)
DEFAULT_TITLE = "操作未完成"
UNKNOWN_MESSAGE = "发生技术错误，请查看本地错误日志。"


def safe_user_message(exc: BaseException) -> str:
    """The message a known failure may show: the Service's own text."""
    text = str(exc).strip()
    if text:
        return text
    return UNKNOWN_MESSAGE


def run_operation(parent, callback, *, context: str = "UI", title: str = DEFAULT_TITLE,
                  on_conflict=None):
    """Run `callback`, turning failures into user-facing dialogs and log lines."""
    try:
        return callback()
    except ConflictError as exc:
        message = safe_user_message(exc)
        logger.warning("%s conflict: type=%s", context, type(exc).__name__)
        if on_conflict is not None:
            on_conflict(message)
        QMessageBox.warning(parent, title, message)
    except KNOWN_FAILURES as exc:
        logger.warning("%s operation failed: type=%s", context, type(exc).__name__)
        QMessageBox.warning(parent, title, str(exc))
    except Exception as exc:                      # noqa: BLE001 - boundary by design
        logger.error("%s unexpected failure: type=%s", context, type(exc).__name__)
        QMessageBox.critical(parent, title, UNKNOWN_MESSAGE)
    return None
