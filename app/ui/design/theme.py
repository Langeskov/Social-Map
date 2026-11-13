"""Install the Design System on the application (style, palette, fonts, no effects).

``apply_design`` is idempotent and application-wide, so detached dialogs (which have
no styled parent) look like the main window. Animation is disabled twice: Qt's UI
effects (menu / tooltip fades) are switched off here, and the stylesheet contains no
transition rule.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from app.ui.design import fonts
from app.ui.design.qss import build_qss
from app.ui.design.tokens import COLORS as C

_EFFECTS = (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateTooltip, Qt.UIEffect.UI_FadeTooltip, Qt.UIEffect.UI_AnimateToolBox)


def build_palette() -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: C["base"], QPalette.ColorRole.WindowText: C["text"],
        QPalette.ColorRole.Base: C["surface"], QPalette.ColorRole.AlternateBase: C["raised"],
        QPalette.ColorRole.Text: C["text"], QPalette.ColorRole.Button: C["raised"],
        QPalette.ColorRole.ButtonText: C["text"], QPalette.ColorRole.ToolTipBase: C["raised"],
        QPalette.ColorRole.ToolTipText: C["text"], QPalette.ColorRole.Highlight: C["selection"],
        QPalette.ColorRole.HighlightedText: C["text"], QPalette.ColorRole.Link: C["accent"],
        QPalette.ColorRole.PlaceholderText: C["muted"], QPalette.ColorRole.BrightText: C["text"],
        QPalette.ColorRole.Mid: C["border"], QPalette.ColorRole.Dark: C["sidebar"],
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(C["archived"]))
    return palette


def apply_design(app: QApplication | None = None) -> QApplication:
    app = app or QApplication.instance()
    if app is None:
        raise RuntimeError("apply_design needs a QApplication")
    if app.property("designApplied"):
        return app
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    app.setFont(fonts.sans_font("body"))
    for effect in _EFFECTS:
        app.setEffectEnabled(effect, False)
    app.setStyleSheet(build_qss())
    app.setProperty("designApplied", True)
    return app
