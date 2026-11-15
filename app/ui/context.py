"""Workspace context and the Context Reset protocol.

One ``WorkspaceContext`` is the single source of truth for "which Workspace am I in".
Pages never own a Workspace id of their own once they are bound: they read
``context.workspace_id`` and react to ``changed``.

The reset order is fixed and identical everywhere::

    about_to_change(old)   -> every page closes its non-modal windows and clears
                              selection / detail / search input and results
    changed(new)           -> every page re-reads its data for the new Workspace

A page that is *not* bound (a standalone widget in a focused test) behaves exactly as
before, so the historical per-page tests keep their meaning.
"""
import logging

from PySide6.QtCore import QObject, Signal

from app.domain.errors import DomainError

logger = logging.getLogger(__name__)


class DetailWindowTracker:
    """Closes every non-modal detail window a page opened.

    The page keeps owning its list (``_dialogs`` / ``_viewers``) — the tracker only
    iterates it — so existing lifecycle behaviour (``WA_DeleteOnClose`` + ``finished``)
    is unchanged; the tracker adds the "close everything on context reset" step.
    """

    def __init__(self, owner, dialogs=None, viewers=None):
        self.owner = owner
        self.dialogs = dialogs if dialogs is not None else []
        self.viewers = viewers if viewers is not None else []

    def _all(self):
        return list(self.dialogs) + list(self.viewers)

    def count(self) -> int:
        return len(self._all())

    def close_all(self) -> int:
        closed = 0
        for window in self._all():
            try:
                window.close()
                closed += 1
            except RuntimeError:                  # already deleted by Qt
                continue
        return closed


class WorkspaceContext(QObject):
    """Current Workspace + the two-step reset protocol."""

    about_to_change = Signal(object)              # old workspace id (may be None)
    changed = Signal(object)                      # new workspace id

    def __init__(self, service, initial_workspace=None, parent=None):
        super().__init__(parent)
        self.service = service
        self._workspace_id = getattr(initial_workspace, "id", None)
        self.workspaces: list = []

    # ------------------------------------------------------------------ state
    @property
    def workspace_id(self):
        return self._workspace_id

    def name_of(self, workspace_id=None) -> str:
        wanted = workspace_id if workspace_id is not None else self._workspace_id
        for workspace in self.workspaces:
            if workspace.id == wanted:
                return workspace.name
        return ""

    def current_name(self) -> str:
        return self.name_of()

    # ------------------------------------------------------------------ reads
    def refresh(self) -> list:
        """Re-read the Workspace list; keeps the current id when it still exists."""
        self.workspaces = self.service.list() if self.service is not None else []
        if self._workspace_id not in {workspace.id for workspace in self.workspaces}:
            self._workspace_id = self.workspaces[0].id if self.workspaces else None
        return self.workspaces

    # ----------------------------------------------------------------- writes
    def set_workspace(self, workspace_id, *, force: bool = False) -> bool:
        """Switch the session to ``workspace_id`` and run the reset protocol once."""
        if workspace_id is None:
            return False
        if workspace_id == self._workspace_id and not force:
            return False
        old = self._workspace_id
        self._workspace_id = workspace_id
        logger.debug("Workspace context switch")
        self.about_to_change.emit(old)
        self.changed.emit(workspace_id)
        return True

    def reset(self, workspace_id=None) -> None:
        """Force a full reset, e.g. after the Workspace was renamed or archived."""
        self._workspace_id = workspace_id if workspace_id is not None else self._workspace_id
        self.about_to_change.emit(None)
        self.changed.emit(self._workspace_id)


class ContextPage:
    """Mixin implementing the Context Reset protocol for a page.

    Subclasses implement ``clear_context()`` (drop selection / detail / search) and
    ``load_workspace(workspace_id)`` (re-read data); the mixin guarantees the order and
    that a bound page never keeps showing an object from another Workspace.
    """

    workspace_context = None

    # ------------------------------------------------------------------- wiring
    def bind_workspace_context(self, context: WorkspaceContext, tracker: DetailWindowTracker | None = None):
        self.workspace_context = context
        if tracker is not None:
            self.detail_windows = tracker
        context.changed.connect(self._on_workspace_changed)
        self._adopt_workspace(context.workspace_id)
        hide_duplicate_workspace_controls(self)
        return context

    def _adopt_workspace(self, workspace_id) -> bool:
        """Point the page at ``workspace_id`` without re-emitting the context."""
        if workspace_id == getattr(self, "workspace_id", None):
            return False
        self.workspace_id = workspace_id
        self.sync_workspace_combo()
        return True

    # -------------------------------------------------------------- protocol
    def reset_context(self) -> None:
        """Close the page's non-modal windows and drop every selected object."""
        tracker = getattr(self, "detail_windows", None)
        if tracker is not None:
            tracker.close_all()
        self.clear_context()

    def _on_workspace_changed(self, workspace_id) -> None:
        self.reset_context()
        self._adopt_workspace(workspace_id)
        self.load_workspace(workspace_id)

    # ------------------------------------------------------------- overrides
    def clear_context(self) -> None:              # pragma: no cover - overridden
        """Clear selection / detail / search of this page."""

    def load_workspace(self, workspace_id) -> None:   # pragma: no cover - overridden
        """Re-read this page's data for ``workspace_id``."""

    def sync_workspace_combo(self) -> None:       # pragma: no cover - overridden
        """Keep the page's legacy ``workspace_combo`` showing the current Workspace."""

    # ------------------------------------------------------- context breadcrumb
    def context_object(self) -> str:
        """The object segment of the breadcrumb, or "" when the page has none.

        Pages override this; the shell only renders what it is told, so the
        breadcrumb can never invent an object that is not actually selected.
        """
        return ""

    def notify_context(self) -> None:
        """Ask the shell to re-render the breadcrumb after a selection changed."""
        window = self.window()
        refresh = getattr(window, "refresh_crumbs", None)
        if callable(refresh):
            return refresh()
        return None


def hide_duplicate_workspace_controls(page) -> None:
    """Hide a page's own Workspace selector once the global one owns the context.

    The widget stays alive (and functional) so existing code and tests that address
    ``page.workspace_combo`` keep working; only the duplicate *control* disappears.
    """
    combo = getattr(page, "workspace_combo", None)
    if combo is None:
        return
    combo.setVisible(False)
    parent = combo.parentWidget()
    if parent is None:
        return
    from PySide6.QtWidgets import QLabel
    for label in parent.findChildren(QLabel):
        if label.text().startswith("当前 Workspace"):
            label.setVisible(False)


def request_switch(context, workspace_id, *, page=None) -> bool:
    """Switch through the context when bound, else fall back to the page itself."""
    if context is not None:
        return context.set_workspace(workspace_id)
    if page is not None and hasattr(page, "apply_workspace"):
        page.apply_workspace(workspace_id)
        return True
    return False


def known_failure(exc: BaseException) -> bool:
    """True when the message is written for the user (never logged with content)."""
    from app.storage.database import DatabaseError
    return isinstance(exc, (DomainError, DatabaseError, OSError, ValueError))
