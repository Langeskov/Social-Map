"""Static SVG icons rendered at the screen's device pixel ratio.

Icons are tiny stroke-only SVGs tinted at render time, so one definition serves
every DPI and every colour token. ``icon()`` returns a multi-size ``QIcon`` so Qt can
pick the sharpest raster for the widget; there is no raster asset and no animation.
"""
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
         'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">')

# 24x24 line icons, one per layer plus the navigation / status glyphs.
_PATHS = {
    "person": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/>',
    "link": '<circle cx="6" cy="12" r="3"/><circle cx="18" cy="12" r="3"/><path d="M9 12h6"/>',
    "event": '<circle cx="12" cy="12" r="3"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>',
    "evidence": '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/>',
    "analysis": '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="12" r="2.5"/>'
                '<circle cx="6" cy="18" r="2.5"/><path d="M8.3 7l7.4 4M8.3 17l7.4-4"/>',
    "dashboard": '<rect x="3" y="3" width="8" height="8"/><rect x="13" y="3" width="8" height="5"/>'
                 '<rect x="13" y="10" width="8" height="11"/><rect x="3" y="13" width="8" height="8"/>',
    "people": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.8 2.9-6 6.5-6s6.5 2.2 6.5 6"/>'
              '<path d="M16 5a3.5 3.5 0 0 1 0 6.5M18 14c2.2.6 3.5 2.6 3.5 6"/>',
    "events": '<rect x="3" y="5" width="18" height="16" rx="1"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    "graph": '<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="19" r="2.5"/>'
             '<path d="M7.5 6h9M6.5 8l4 9M17.5 8l-4 9"/>',
    "settings": '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
    "workspace": '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="M3 9h18"/>',
    "memory": '<path d="M5 4h14v16l-7-4-7 4z"/>',
    "warning": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "lock": '<rect x="5" y="11" width="14" height="9" rx="1"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
}
ICON_NAMES = tuple(_PATHS)
ICON_SIZES = (16, 20, 24, 32, 48, 64)


def svg_markup(name: str, color: str) -> str:
    if name not in _PATHS:
        raise KeyError(f"unknown icon: {name}")
    return _HEAD.format(color=color) + _PATHS[name] + "</svg>"


@lru_cache(maxsize=512)
def _renderer(name: str, color: str) -> QSvgRenderer:
    return QSvgRenderer(QByteArray(svg_markup(name, color).encode("utf-8")))


def pixmap(name: str, color: str, size: int, ratio: float = 1.0) -> QPixmap:
    """A ``size`` x ``size`` (logical px) pixmap rendered at ``ratio`` for sharp HiDPI."""
    side = max(1, round(size * ratio))
    image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        _renderer(name, color).render(painter, QRectF(0, 0, side, side))
    finally:
        painter.end()
    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(ratio)
    return result


def icon(name: str, color: str, *, selected_color: str | None = None) -> QIcon:
    """Multi-size ``QIcon``; ``selected_color`` is used for the Selected/On state."""
    result = QIcon()
    for size in ICON_SIZES:
        result.addPixmap(pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
        if selected_color is not None:
            result.addPixmap(pixmap(name, selected_color, size), QIcon.Mode.Selected, QIcon.State.Off)
            result.addPixmap(pixmap(name, selected_color, size), QIcon.Mode.Normal, QIcon.State.On)
    return result
