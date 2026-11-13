import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import logging
import pytest
from PySide6.QtWidgets import QApplication

@pytest.fixture(autouse=True)
def isolated_app_logging():
    logger = logging.getLogger("app")
    previous = (logger.level, logger.propagate, list(logger.handlers))
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(previous[0])
    logger.propagate = previous[1]
    logger.handlers[:] = previous[2]


@pytest.fixture(scope="session")
def qt_app():
    instance = QApplication.instance() or QApplication([])
    yield instance
