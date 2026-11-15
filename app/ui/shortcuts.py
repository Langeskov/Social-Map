"""Keyboard shortcuts shared by every editing dialog.

Phase 11 §18 defines ``Ctrl+S`` as **save and close** for a dialog: it runs exactly the
same path as the dialog's Accepted button, so validation and the existing error
boundary apply unchanged. Nothing here writes to a Service directly.

``Esc`` and ``Enter`` are Qt's own dialog behaviour and are not re-implemented.
"""
from PySide6.QtGui import QKeySequence, QShortcut


def install_save_shortcut(dialog, callback=None):
    """Bind ``Ctrl+S`` to ``callback`` (default: the dialog's own ``accept``)."""
    action = callback if callback is not None else dialog.accept
    shortcut = QShortcut(QKeySequence.StandardKey.Save, dialog)
    shortcut.setContext(shortcut.context())

    def trigger():
        try:
            return action()
        except RuntimeError:            # the dialog was deleted meanwhile
            return None

    shortcut.activated.connect(trigger)
    dialog.save_shortcut = shortcut
    return shortcut
