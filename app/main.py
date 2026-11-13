"""Launch with python -m app.main."""
import argparse
import logging
from pathlib import Path
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from app.config.settings import ConfigError, load_config
from app.logging_setup import configure_logging
from app.services.database import initialize_database
from app.services.application import create_application_services
from app.domain.errors import DomainError
from app.storage.database import DatabaseError
from app.ui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relationship Intelligence System — Phase 10")
    parser.add_argument("--config", type=Path, help="自定义 TOML 配置文件")
    parser.add_argument("--smoke-test", action="store_true", help="显示窗口并验证所有页面后自动退出")
    args = parser.parse_args(argv)
    qt_app = QApplication.instance() or QApplication([sys.argv[0]])
    qt_app.setApplicationName("Relationship Intelligence System")
    qt_app.setOrganizationName("RelationshipIntelligence")
    try:
        config = load_config(args.config)
        logger = configure_logging(config)
    except (ConfigError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        if not args.smoke_test:
            QMessageBox.critical(None, "启动失败", str(exc))
        return 1

    previous_hook = sys.excepthook
    def report_error(exc_type, value, traceback):
        if isinstance(value, (DatabaseError, DomainError)):
            logger.critical("Unhandled application error: type=%s; private values omitted", exc_type.__name__)
        else:
            logger.critical("Unhandled exception", exc_info=(exc_type, value, traceback))
        if args.smoke_test:
            qt_app.exit(1)
        else:
            QMessageBox.critical(None, "程序错误", "发生未处理错误，请查看本地错误日志。")
    sys.excepthook = report_error
    database = None
    try:
        database = initialize_database(config)
        services = create_application_services(config, database.sessions)
        window = MainWindow(config, services=services)
        window.show()
        logger.info("Application started (Phase 10)")
        if args.smoke_test:
            def verify():
                try:
                    assert window.isVisible()
                    page_count = window.pages.count()
                    assert page_count == 6, f"expected 6 pages, found {page_count}"
                    for index in range(page_count):
                        window.navigation.setCurrentRow(index)
                        assert window.pages.currentIndex() == index
                    window.navigation.setCurrentRow(0)
                    logger.info("GUI smoke test passed: window visible and %s pages navigable; Qt platform=%s",
                                page_count, qt_app.platformName())
                    window.close()
                    qt_app.exit(0)
                except Exception:
                    logger.exception("GUI smoke test failed")
                    qt_app.exit(1)
            QTimer.singleShot(700, verify)
        result = qt_app.exec()
        logger.info("Application stopped: %s", result)
        return result
    except (DatabaseError, DomainError) as exc:
        logger.error("Application startup stopped: type=%s", type(exc).__name__)
        if not args.smoke_test:
            QMessageBox.critical(None, "本地数据不可用", str(exc))
        return 1
    except Exception:
        logger.exception("Application startup failed")
        return 1
    finally:
        if database is not None:
            database.close()
        sys.excepthook = previous_hook
        logging.shutdown()

if __name__ == "__main__":
    raise SystemExit(main())
