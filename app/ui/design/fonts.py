"""Application fonts: one sans chain for text, one monospace chain for identifiers.

Sans is used for body copy; monospace is reserved for IDs, timestamps and status
tags. Both chains end in CJK-capable Windows families so Chinese never falls back to
tofu. Sizes are points (DPI aware); the layout derives pixel metrics from
``QFontMetrics`` instead of hard-coding pixels.
"""
from PySide6.QtGui import QFont

from app.ui.design import tokens


def _font(families, point_size, *, fixed=False) -> QFont:
    font = QFont()
    font.setFamilies(list(families))
    font.setPointSizeF(float(point_size))
    font.setStyleHint(QFont.StyleHint.TypeWriter if fixed else QFont.StyleHint.SansSerif)
    font.setFixedPitch(fixed)
    return font


def sans_font(role: str = "body") -> QFont:
    return _font(tokens.SANS_FAMILIES, tokens.FONT_PT[role])


def mono_font(role: str = "small") -> QFont:
    return _font(tokens.MONO_FAMILIES, tokens.FONT_PT[role], fixed=True)
