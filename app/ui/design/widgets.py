"""Shared Design System widgets.

Every widget takes its look from the global stylesheet through ``objectName`` and
dynamic properties; none of them embeds a colour. Pixel sizes derive from
``QFontMetrics`` so they follow the font (and therefore the DPI) instead of being
hard-coded.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget)

from app.ui.design import fonts, icons, tokens
from app.ui.design.tokens import (COLORS as C, EPISTEMIC_BY_KEY, LAYER_BY_KEY, SPACE, STATE_COPY, STATUSES)


def repolish(widget: QWidget) -> None:
    """Re-evaluate the stylesheet after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_property(widget: QWidget, name: str, value) -> None:
    widget.setProperty(name, value)
    repolish(widget)


def icon_side(widget: QWidget, factor: float = 1.2) -> int:
    """Icon edge in logical px, derived from the widget's font height."""
    return max(16, round(widget.fontMetrics().height() * factor))


def plain_text_label(text: str = "", *, object_name: str | None = None, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if object_name:
        label.setObjectName(object_name)
    return label


# ------------------------------------------------------------------ layer badge
class LayerBadge(QWidget):
    """Icon + short tag of one layer (Entity / Link / Occurrence / Proof / Inference)."""

    def __init__(self, layer_key: str, parent=None, *, show_text: bool = True):
        super().__init__(parent)
        self.setObjectName("layerBadge")
        layer = LAYER_BY_KEY[layer_key]
        self.layer = layer
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["xs"])
        self.icon_label = QLabel()
        side = icon_side(self)
        ratio = self.devicePixelRatioF()
        self.icon_label.setPixmap(icons.pixmap(layer.icon, layer.color, side, ratio))
        self.icon_label.setFixedSize(side, side)
        self.icon_label.setAccessibleName(f"{layer.label} 层")
        row.addWidget(self.icon_label)
        self.text_label = QLabel(layer.label)
        self.text_label.setObjectName("layerBadgeText")
        self.text_label.setProperty("layer", layer.key)
        self.text_label.setVisible(show_text)
        row.addWidget(self.text_label)
        self.setToolTip(f"{layer.label} · {layer.name}")
        self.setAccessibleName(f"{layer.label}（{layer.name}）")


class LayerCard(QFrame):
    """Card with the layer's identity stripe on the left and a vertical body."""

    def __init__(self, layer_key: str, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("layerCard")
        self.layer = LAYER_BY_KEY[layer_key]
        self.setProperty("layer", self.layer.key)
        outer = QVBoxLayout(self)
        margin = SPACE["md"]
        outer.setContentsMargins(margin, SPACE["sm"], margin, SPACE["sm"])
        outer.setSpacing(SPACE["sm"])
        header = QHBoxLayout()
        header.setSpacing(SPACE["sm"])
        self.badge = LayerBadge(layer_key, self)
        header.addWidget(self.badge)
        self.title_label = plain_text_label(title, object_name="sectionTitle")
        header.addWidget(self.title_label, 1)
        outer.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(SPACE["sm"])
        outer.addLayout(self.body)


# -------------------------------------------------------------------- epistemic
class EpistemicTag(QLabel):
    """Monospace FACT / USER / DERIVED tag."""

    def __init__(self, epistemic_key: str, parent=None):
        super().__init__(EPISTEMIC_BY_KEY[epistemic_key].tag, parent)
        self.setObjectName("epistemicTag")
        self.setProperty("epistemic", epistemic_key)
        self.setFont(fonts.mono_font("small"))
        self.setAccessibleName(EPISTEMIC_BY_KEY[epistemic_key].caption)
        self.setToolTip(EPISTEMIC_BY_KEY[epistemic_key].caption)


class EpistemicFrame(QFrame):
    """Frame whose border states what kind of content it holds.

    FACT = solid border, read-only; USER = user marker, editable; DERIVED = dashed
    border + DERIVED tag, with an explicit Sources line supplied by the caller.
    """

    def __init__(self, epistemic_key: str, title: str = "", parent=None, *, source: str = ""):
        super().__init__(parent)
        self.setObjectName("epistemicFrame")
        self.epistemic = EPISTEMIC_BY_KEY[epistemic_key]
        self.setProperty("epistemic", epistemic_key)
        outer = QVBoxLayout(self)
        margin = SPACE["md"]
        outer.setContentsMargins(margin, SPACE["sm"], margin, SPACE["sm"])
        outer.setSpacing(SPACE["sm"])
        header = QHBoxLayout()
        header.setSpacing(SPACE["sm"])
        self.tag = EpistemicTag(epistemic_key, self)
        header.addWidget(self.tag)
        self.title_label = plain_text_label(title, object_name="sectionTitle")
        header.addWidget(self.title_label, 1)
        self.caption_label = plain_text_label(self.epistemic.caption, object_name="epistemicCaption", wrap=False)
        header.addWidget(self.caption_label)
        outer.addLayout(header)
        self.source_label = plain_text_label(source, object_name="epistemicCaption")
        self.source_label.setVisible(bool(source))
        outer.addWidget(self.source_label)
        self.body = QVBoxLayout()
        self.body.setSpacing(SPACE["sm"])
        outer.addLayout(self.body)

    def set_source(self, text: str) -> None:
        self.source_label.setText(text)
        self.source_label.setVisible(bool(text))


# ----------------------------------------------------------------------- status
class StatusPill(QLabel):
    """Monospace status tag (ACTIVE / PAUSED / ENDED / ARCHIVED / DELETED / ...)."""

    def __init__(self, status: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statusPill")
        self.setFont(fonts.mono_font("small"))
        self.set_status(status)

    def set_status(self, status: str) -> None:
        if status not in STATUSES:
            raise KeyError(f"unknown status: {status}")
        self.status = status
        self.setText(STATUSES[status][1])
        self.setAccessibleName(f"状态 {STATUSES[status][1]}")
        set_property(self, "status", status)


# ------------------------------------------------------------------------ state
class StateNotice(QFrame):
    """One state message (Empty / Loading / Error / Conflict / Archived / ...)."""

    def __init__(self, state: str, parent=None):
        super().__init__(parent)
        self.setObjectName("stateNotice")
        row = QVBoxLayout(self)
        margin = SPACE["lg"]
        row.setContentsMargins(margin, SPACE["md"], margin, SPACE["md"])
        row.setSpacing(SPACE["xs"])
        self.title_label = plain_text_label(object_name="stateTitle", wrap=False)
        self.message_label = plain_text_label(object_name="stateMessage")
        row.addWidget(self.title_label)
        row.addWidget(self.message_label)
        self.set_state(state)

    def set_state(self, state: str, message: str | None = None) -> None:
        if state not in STATE_COPY:
            raise KeyError(f"state has no notice: {state}")
        self.state = state
        title, default, _token = STATE_COPY[state]
        self.title_label.setText(title)
        self.message_label.setText(message if message else default)
        self.setAccessibleName(f"{title}：{self.message_label.text()}")
        set_property(self, "state", state)


class ConflictBanner(StateNotice):
    """Shown for a ``ConflictError``. The text is the Service's own user message."""

    def __init__(self, parent=None):
        super().__init__(tokens.CONFLICT, parent)

    def show_conflict(self, message: str | None = None) -> None:
        self.set_state(tokens.CONFLICT, message or tokens.CONFLICT_MESSAGE)
        self.setVisible(True)


class StateStack(QWidget):
    """Content area that can swap to a state notice without losing its content.

    * NORMAL                         -> the content;
    * EMPTY / LOADING / ERROR / UNAVAILABLE -> the notice replaces the content;
    * CONFLICT / ARCHIVED / DELETED  -> a banner on top of the (still readable) content.
    """

    REPLACING = (tokens.EMPTY, tokens.LOADING, tokens.ERROR, tokens.UNAVAILABLE)
    BANNER = (tokens.CONFLICT, tokens.ARCHIVED, tokens.DELETED)

    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        self.setObjectName("stateStack")
        self.content = content
        self.state = tokens.NORMAL
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE["sm"])
        self.banner = StateNotice(tokens.ARCHIVED, self)
        self.banner.setVisible(False)
        column.addWidget(self.banner)
        self.pages = QStackedWidget()
        self.notice = StateNotice(tokens.EMPTY, self)
        self.pages.addWidget(content)
        self.pages.addWidget(self.notice)
        column.addWidget(self.pages, 1)

    def show_state(self, state: str, message: str | None = None) -> None:
        if state != tokens.NORMAL and state not in STATE_COPY:
            raise KeyError(f"unknown page state: {state}")
        self.state = state
        self.banner.setVisible(False)
        if state == tokens.NORMAL:
            self.pages.setCurrentWidget(self.content)
        elif state in self.REPLACING:
            self.notice.set_state(state, message)
            self.pages.setCurrentWidget(self.notice)
        else:
            self.banner.set_state(state, message)
            self.banner.setVisible(True)
            self.pages.setCurrentWidget(self.content)

    def show_empty(self, message: str | None = None) -> None:
        self.show_state(tokens.EMPTY, message)


class SectionHeader(QWidget):
    """Section title with an optional layer badge and epistemic tag."""

    def __init__(self, title: str, parent=None, *, layer_key: str | None = None,
                 epistemic_key: str | None = None):
        super().__init__(parent)
        self.setObjectName("sectionHeader")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["sm"])
        self.badge = LayerBadge(layer_key, self) if layer_key else None
        if self.badge is not None:
            row.addWidget(self.badge)
        self.title_label = plain_text_label(title, object_name="sectionTitle", wrap=False)
        row.addWidget(self.title_label)
        row.addStretch(1)
        self.tag = EpistemicTag(epistemic_key, self) if epistemic_key else None
        if self.tag is not None:
            row.addWidget(self.tag)
