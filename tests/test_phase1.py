from dataclasses import replace
import logging
from pathlib import Path
import pytest
from app.config.settings import AppConfig, ConfigError, load_config
from app.logging_setup import configure_logging
from app.ui.main_window import MainWindow


def test_defaults():
    config = load_config()
    assert config.title == "Relationship Intelligence System"
    assert (config.width, config.height, config.log_level) == (1120, 760, "INFO")
    assert config.log_dir.is_absolute()


def test_custom_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[app]\ntitle="测试窗口"\nwidth=900\nheight=600\n[logging]\nlevel="debug"\ndirectory="logs"\n', encoding="utf-8")
    config = load_config(path)
    assert config.title == "测试窗口"
    assert config.log_level == "DEBUG"
    assert config.log_dir == tmp_path / "logs"


@pytest.mark.parametrize("content", [
    "[app", '[app]\nwidth="900"', '[app]\nwidth=true',
    '[app]\nheight=100', '[app]\ntitle=" "',
    '[logging]\nlevel="NOPE"', '[logging]\ndirectory=""',
    '[app]\nunknown=1', 'app=1', '[llm]\nmodel="test"',
])
def test_invalid_config(tmp_path, content):
    path = tmp_path / "bad.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_explicit_config(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "missing.toml")


def test_logging(tmp_path):
    config = replace(AppConfig(), log_dir=tmp_path)
    logger = configure_logging(config)
    configure_logging(config)
    assert len(logger.handlers) == 3
    logger.info("test-start")
    logger.error("test-error")
    for handler in logger.handlers:
        handler.flush()
    application = (tmp_path / "application.log").read_text(encoding="utf-8")
    errors = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert application.count("test-start") == 1
    assert "test-error" in application and "test-error" in errors
    assert "test-start" not in errors
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_window_and_navigation(qt_app):
    window = MainWindow(AppConfig())
    try:
        window.show()
        qt_app.processEvents()
        assert window.isVisible()
        assert window.windowTitle() == AppConfig.title
        assert window.minimumWidth() == 800
        # Phase 7 added the Graph page: six pages, six navigation items, in order.
        assert window.pages.count() == window.navigation.count() == 6
        assert [window.navigation.item(index).text() for index in range(6)] == [
            "Dashboard · 工作台", "People · 人物", "Events · 事件",
            "Graph · 关系图", "Analysis · 分析", "Settings · 设置"]
        assert window.pages.currentIndex() == 0
        for index, name in enumerate(["dashboard", "people", "events", "graph", "analysis", "settings"]):
            window.navigation.setCurrentRow(index)
            qt_app.processEvents()
            assert window.pages.currentIndex() == index
            assert window.pages.currentWidget().objectName() == name
        window.select_page(-1)
        window.select_page(50)
        assert window.pages.currentIndex() == 5
    finally:
        window.close()


def test_custom_window(qt_app):
    window = MainWindow(replace(AppConfig(), title="Custom", width=900, height=600))
    assert window.windowTitle() == "Custom"
    assert (window.width(), window.height()) == (900, 600)
    window.close()
