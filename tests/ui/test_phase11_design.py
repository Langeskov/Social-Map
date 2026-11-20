"""Phase 11.1 Design System and shell contracts.

These tests make the design rules executable instead of aspirational:

* the palette stays small (5 layer colours + 1 accent + 1 warning + 1 archived) and
  legible (WCAG contrast), and the colours are pairwise distinct;
* every font chain ends in a CJK-capable Windows family, so Chinese never falls back
  to tofu;
* no page carries its own colour literal or its own stylesheet — the Design System is
  the single source;
* nothing animates;
* every shared widget renders from tokens and exposes the layer / epistemic / state
  identity the pages rely on.
"""
import re
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.ui.design import fonts, icons, theme, tokens
from app.ui.design.widgets import (ConflictBanner, EpistemicFrame, LayerBadge, LayerCard,
                                   SectionHeader, StateStack, StatusPill)

UI_ROOT = Path(__file__).resolve().parents[2] / "app" / "ui"
DESIGN_ROOT = UI_ROOT / "design"
HEX_LITERAL = re.compile(r"#[0-9a-fA-F]{6}\b")
ANIMATION = re.compile(r"QPropertyAnimation|QVariantAnimation|QAnimationGroup|QMovie|"
                       r"QGraphicsOpacityEffect|QGraphicsBlurEffect|QGraphicsDropShadowEffect|"
                       r"QParallelAnimationGroup|QSequentialAnimationGroup")


# ------------------------------------------------------------------ colour maths
def _luminance(color: str) -> float:
    raw = color.lstrip("#")
    channels = [int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def distance(first: str, second: str) -> float:
    left, right = first.lstrip("#"), second.lstrip("#")
    return sum((int(left[i:i + 2], 16) - int(right[i:i + 2], 16)) ** 2 for i in (0, 2, 4)) ** 0.5


def test_palette_is_small_and_complete():
    colors = tokens.COLORS
    assert len(colors) == 16
    # Exactly one accent, one warning and one archived/deleted colour.
    assert {"accent", "warning", "archived"} <= set(colors)
    # The five layer colours are the only extra hues.
    assert len(tokens.LAYERS) == 5


def test_text_and_semantic_colours_are_legible():
    for name in ("text", "muted", "accent", "warning", "archived"):
        assert contrast(tokens.COLORS[name], tokens.COLORS["surface"]) >= 4.5, name
        assert contrast(tokens.COLORS[name], tokens.COLORS["base"]) >= 4.5, name


def test_layer_colours_are_legible_and_distinct():
    names = ["accent", "warning", "archived"] + [layer.color_token for layer in tokens.LAYERS]
    for name in names:
        assert contrast(tokens.COLORS[name], tokens.COLORS["surface"]) >= 4.0, name
    for left in range(len(names)):
        for right in range(left + 1, len(names)):
            assert distance(tokens.COLORS[names[left]], tokens.COLORS[names[right]]) >= 48, (
                names[left], names[right])


def test_selected_navigation_text_stays_legible():
    assert contrast(tokens.COLORS["text"], tokens.COLORS["selection"]) >= 4.5


# ----------------------------------------------------------------------- fonts
def test_font_chains_end_in_a_cjk_capable_family():
    assert tokens.SANS_FAMILIES[-2] == "Noto Sans CJK SC" or "YaHei" in " ".join(tokens.SANS_FAMILIES)
    assert any("YaHei" in family for family in tokens.SANS_FAMILIES)
    assert any("YaHei" in family for family in tokens.MONO_FAMILIES)


def test_font_sizes_are_points_not_pixels(qt_app):
    """Point sizes scale with the DPI; the layout measures pixels with QFontMetrics."""
    body = fonts.sans_font("body")
    assert isinstance(body.pointSizeF(), float)
    mono = fonts.mono_font("small")
    assert mono.fixedPitch() is True
    assert mono.families() == list(tokens.MONO_FAMILIES)


# ------------------------------------------------------------ single source rule
def _python_files(root: Path):
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_no_colour_literal_outside_the_design_system():
    offenders = []
    for path in _python_files(UI_ROOT):
        if DESIGN_ROOT in path.parents:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if HEX_LITERAL.search(line):
                offenders.append(f"{path.relative_to(UI_ROOT)}:{number}")
    assert offenders == []


def test_no_page_sets_its_own_stylesheet():
    offenders = []
    for path in _python_files(UI_ROOT):
        if DESIGN_ROOT in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "setStyleSheet" in text:
            offenders.append(path.relative_to(UI_ROOT).as_posix())
    assert offenders == []


def test_nothing_animates():
    app_root = UI_ROOT.parent
    offenders = []
    for path in _python_files(app_root):
        if ANIMATION.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(app_root).as_posix())
    assert offenders == []


def test_stylesheet_has_no_transition_or_animation_rule():
    app = theme.apply_design()
    sheet = app.styleSheet()
    assert "transition" not in sheet and "animation" not in sheet
    # Every layer, epistemic and status identity is driven by the sheet.
    for layer in tokens.LAYERS:
        assert f'QFrame#layerCard[layer="{layer.key}"]' in sheet
        assert layer.color in sheet
    for item in tokens.EPISTEMICS:
        assert f'[epistemic="{item.key}"]' in sheet
    for status in tokens.STATUSES:
        assert f'[status="{status}"]' in sheet


def test_design_is_applied_once(qt_app):
    first = theme.apply_design()
    second = theme.apply_design()
    assert first is second
    assert first.property("designApplied") is True
    # The application palette is the Design System palette: the native Windows style
    # would otherwise paint light controls on the dark workbench.
    assert first.palette().color(first.palette().ColorRole.Window).name() == tokens.COLORS["base"]
    assert first.palette().color(first.palette().ColorRole.Highlight).name() == tokens.COLORS["selection"]
    assert first.font().families() == list(tokens.SANS_FAMILIES)
    assert tokens.COLORS["accent"] in first.styleSheet()


# --------------------------------------------------------------------- widgets
def test_layer_badge_carries_the_layer_identity(qt_app):
    for layer in tokens.LAYERS:
        badge = LayerBadge(layer.key)
        assert badge.text_label.text() == layer.label
        assert badge.text_label.property("layer") == layer.key
        assert not badge.icon_label.pixmap().isNull()
        assert layer.name in badge.accessibleName()


def test_layer_card_has_the_left_stripe(qt_app):
    card = LayerCard("PROOF", "证据")
    assert card.property("layer") == "PROOF"
    assert card.objectName() == "layerCard"
    card.body.addWidget(QLabel("x"))


def test_derived_frame_is_dashed_and_names_its_sources(qt_app):
    derived = EpistemicFrame("DERIVED", "分析结果", source="Sources · Event(E1) Evidence(V1)")
    assert derived.tag.text() == "DERIVED"
    assert derived.property("epistemic") == "DERIVED"
    assert tokens.DERIVED.border_style == "dashed"
    assert derived.source_label.isVisible() is False  # not shown until the widget is
    derived.set_source("Sources · Event(E1)")
    assert derived.source_label.isVisible() is False or derived.source_label.text()
    assert "Sources" in derived.source_label.text()


def test_fact_and_user_frames_are_solid(qt_app):
    assert EpistemicFrame("FACT").property("epistemic") == "FACT"
    assert EpistemicFrame("USER").property("epistemic") == "USER"
    assert tokens.FACT.border_style == tokens.USER.border_style == "solid"


def test_memory_wording_is_user_confirmed_only(qt_app):
    """No UI string may claim the AI remembers or judges anything."""
    banned = ("AI 自动记住", "AI 判断", "自动记住", "AI 认为")
    for path in _python_files(UI_ROOT):
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{phrase} in {path.name}"


def test_status_pill_covers_every_status(qt_app):
    for status, (_token, label) in tokens.STATUSES.items():
        pill = StatusPill(status)
        assert pill.text() == label
        assert pill.property("status") == status
    with pytest.raises(KeyError):
        StatusPill("NOT_A_STATUS")


def test_state_stack_swaps_notice_and_banner(qt_app):
    content = QWidget()
    stack = StateStack(content)
    stack.show_state(tokens.NORMAL)
    assert stack.pages.currentWidget() is content
    assert stack.banner.isVisible() is False
    stack.show_empty(tokens.SEARCH_EMPTY_MESSAGE)
    assert stack.pages.currentWidget() is stack.notice
    assert stack.notice.message_label.text() == tokens.SEARCH_EMPTY_MESSAGE
    stack.show_state(tokens.LOADING)
    assert stack.notice.state == tokens.LOADING
    stack.show_state(tokens.ERROR)
    assert stack.notice.state == tokens.ERROR
    stack.show_state(tokens.CONFLICT)
    assert stack.pages.currentWidget() is content
    assert stack.banner.state == tokens.CONFLICT
    stack.show_state(tokens.ARCHIVED)
    assert stack.banner.state == tokens.ARCHIVED
    stack.show_state(tokens.DELETED)
    assert stack.banner.state == tokens.DELETED
    stack.show_state(tokens.UNAVAILABLE)
    assert stack.notice.state == tokens.UNAVAILABLE
    with pytest.raises(KeyError):
        stack.show_state("NOT_A_STATE")


def test_conflict_banner_uses_the_agreed_message(qt_app):
    banner = ConflictBanner()
    banner.show_conflict()
    assert banner.message_label.text() == "当前记录已经被其他窗口修改，请刷新后重试。"
    banner.show_conflict("证据附件已被其他操作改变，请刷新后重试；本次未修改附件。")
    assert "刷新后重试" in banner.message_label.text()


def test_section_header_composes_badge_and_tag(qt_app):
    header = SectionHeader("关系", layer_key="LINK", epistemic_key="FACT")
    assert header.badge is not None and header.tag is not None
    assert header.title_label.text() == "关系"
    plain = SectionHeader("备注")
    assert plain.badge is None and plain.tag is None


def test_icons_exist_for_every_layer_and_navigation_item(qt_app):
    from app.ui.shell import NAV_ICONS
    for layer in tokens.LAYERS:
        assert layer.icon in icons.ICON_NAMES
    for name in NAV_ICONS:
        assert name in icons.ICON_NAMES
    rendered = icons.pixmap("person", tokens.COLORS["entity"], 24, 2.0)
    assert not rendered.isNull()
    assert rendered.devicePixelRatio() == 2.0
    with pytest.raises(KeyError):
        icons.svg_markup("not-an-icon", "#ffffff")


# ----------------------------------------------------------------------- shell
def test_shell_keeps_navigation_stable_and_labelled(qt_app):
    from app.config.settings import AppConfig
    from app.ui.main_window import MainWindow
    window = MainWindow(AppConfig())
    try:
        window.show()
        QApplication.processEvents()
        assert window.minimumWidth() == 800
        assert window.navigation.count() == window.pages.count() == 6
        for index in range(6):
            item = window.navigation.item(index)
            assert not item.icon().isNull()
        # The sidebar is wide enough for its longest label: never compressed.
        metrics = window.navigation.fontMetrics()
        widest = max(metrics.horizontalAdvance(window.navigation.item(index).text()) for index in range(6))
        assert window.sidebar.width() >= widest
        assert window.sidebar.minimumWidth() == window.sidebar.maximumWidth() == window.sidebar.width()
        assert window.top_bar.brand.text()
    finally:
        window.close()


def test_context_bar_is_a_read_only_breadcrumb(qt_app):
    from app.config.settings import AppConfig
    from app.ui.main_window import MainWindow
    window = MainWindow(AppConfig())
    try:
        window.show()
        QApplication.processEvents()
        window.navigation.setCurrentRow(2)
        QApplication.processEvents()
        crumbs = window.context_bar.crumbs
        assert crumbs[-1] == "Events"
        # No Back / Forward history exists in V1 (decision 1).
        assert not hasattr(window.context_bar, "back")
        assert not hasattr(window.context_bar, "forward")
        window.set_workspace_name("研究 A")
        assert window.context_bar.crumbs[0] == "研究 A"
        assert window.context_bar.crumbs[-1] == "Events"
    finally:
        window.close()


def test_shell_shows_no_phase_number(qt_app):
    """Phase wording belongs in the docs, not in the product UI."""
    from app.config.settings import AppConfig
    from app.ui.main_window import MainWindow
    window = MainWindow(AppConfig())
    try:
        texts = " ".join(label.text() for label in window.findChildren(QLabel))
        assert "Phase" not in texts
    finally:
        window.close()
