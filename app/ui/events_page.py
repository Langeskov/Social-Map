"""Workspace-scoped Event CRUD and explicit participant/history association."""
from uuid import UUID
from PySide6.QtCore import Signal, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QSplitter, QTextEdit, QVBoxLayout, QWidget)
from app.domain.errors import ConflictError, DomainError
from app.ui.analysis_dialog import ANALYZE_LABELS, analysis_center
from app.ui.event_dialog import EventDialog
from app.ui.evidence_panel import EvidencePanel
from app.ui.event_detail import EventDetailView, event_time, history_text, plain_label, run_operation, ROLE


class ParticipantDialog(QDialog):
    def __init__(self, people, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加参与者")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.person = QComboBox()
        for person in people:
            self.person.addItem(f"{person.display_name} · {str(person.id)[:8]}", person.id)
        self.role = QLineEdit()
        self.note = QTextEdit()
        form.addRow("人物", self.person)
        form.addRow("角色", self.role)
        form.addRow("备注", self.note)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(people))
        layout.addWidget(buttons)

    def values(self):
        if self.person.currentData() is None:
            raise DomainError("请选择当前 Workspace 的人物。")
        return dict(person_id=self.person.currentData(), role=self.role.text().strip() or None,
                    note=self.note.toPlainText().strip() or None)


class HistoryAttachDialog(QDialog):
    def __init__(self, histories, parent=None):
        super().__init__(parent)
        self.setWindowTitle("明确关联既有 RelationshipHistory")
        self.resize(850, 170)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("仅关联已有 History；不会创建关系或修改状态。已有事件引用将被替换。"))
        self.history = QComboBox()
        for link in histories:
            associated = getattr(link.history, "event_id", None)
            self.history.addItem(history_text(link) + (f" · 已关联 {associated}" if associated else ""), link.history.id)
        layout.addWidget(self.history)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(histories))
        layout.addWidget(buttons)

    def values(self):
        if self.history.currentData() is None:
            raise DomainError("没有可选择的关系 History。")
        return self.history.currentData()


class EventsPage(QWidget):
    events_changed = Signal(object)

    def __init__(self, services, parent=None, *, detail=None):
        super().__init__(parent)
        self.setObjectName("events")
        self.services = services
        self.service = getattr(services, "events", None)
        self._detail_event_id = detail.event.id if detail else None
        self.workspace_id = detail.event.workspace_id if detail else services.initial_workspace.id
        self.current_detail = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = plain_label("Events · 事件")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        top = QHBoxLayout()
        top.addWidget(QLabel("当前 Workspace（本页独立选择）"))
        self.workspace_combo = QComboBox()
        self.workspace_combo.setObjectName("eventWorkspaceSelector")
        self.workspace_combo.currentIndexChanged.connect(self.change_workspace)
        top.addWidget(self.workspace_combo, 1)
        layout.addLayout(top)
        splitter = QSplitter()
        left = QWidget()
        column = QVBoxLayout(left)
        self.search = QLineEdit()
        self.search.setObjectName("eventSearch")
        self.search.setPlaceholderText("搜索事件标题、类型或描述")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(self.refresh_events)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.returnPressed.connect(self.refresh_events)
        column.addWidget(self.search)
        self.include_deleted = QCheckBox("显示已删除事件")
        self.include_deleted.toggled.connect(lambda _: self.refresh_events())
        column.addWidget(self.include_deleted)
        row = QHBoxLayout()
        self.create_button = self.button(row, "新建事件", self.create_event)
        self.button(row, "刷新", self.activate)
        column.addLayout(row)
        self.event_list = QListWidget()
        self.event_list.setObjectName("eventList")
        self.event_list.currentItemChanged.connect(self.select_event)
        column.addWidget(self.event_list)
        splitter.addWidget(left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        right = QWidget()
        detail_layout = QVBoxLayout(right)
        self.detail_view = EventDetailView(self)
        detail_layout.addWidget(self.detail_view)
        # The Evidence panel follows the detail view, so the single-event detail
        # opened from the Person Timeline shows it too.
        self.evidence_panel = EvidencePanel(services, self)
        self.detail_view.detail_changed.connect(self.show_evidence)
        detail_layout.addWidget(self.evidence_panel)
        row = QHBoxLayout()
        self.edit_button = self.button(row, "编辑事件", self.edit_event)
        self.delete_button = self.button(row, "删除事件", self.delete_event)
        self.restore_button = self.button(row, "恢复事件", self.restore_event)
        detail_layout.addLayout(row)
        row = QHBoxLayout()
        self.add_participant_button = self.button(row, "添加参与者", self.add_participant)
        self.remove_participant_button = self.button(row, "移除所选参与者", self.remove_participant)
        detail_layout.addLayout(row)
        row = QHBoxLayout()
        self.attach_button = self.button(row, "关联既有 History", self.attach_history)
        self.detach_button = self.button(row, "解除所选 History 关联", self.detach_history)
        detail_layout.addLayout(row)
        row = QHBoxLayout()
        # Phase 8: an Event analysis is started only by this click, never by opening
        # the page or selecting a row.
        self.analyze_button = self.button(row, ANALYZE_LABELS["EVENT"], self.analyze_event)
        detail_layout.addLayout(row)
        self.detail_view.participants.itemSelectionChanged.connect(self.update_buttons)
        self.detail_view.histories.itemSelectionChanged.connect(self.update_buttons)
        scroll.setWidget(right)
        splitter.addWidget(scroll)
        splitter.setSizes([320, 620])
        layout.addWidget(splitter, 1)
        self.status = plain_label("没有加载或生成演示数据。")
        layout.addWidget(self.status)
        if detail is not None:
            title.hide()
            left.hide()
            self.status.hide()
            for index in range(top.count()):
                widget = top.itemAt(index).widget()
                if widget is not None:
                    widget.hide()
            self.include_deleted.blockSignals(True)
            self.include_deleted.setChecked(True)
            self.include_deleted.blockSignals(False)
        else:
            self.refresh_workspaces()
        self.refresh_events()

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def run_operation(self, callback):
        return run_operation(self, callback)

    def refresh_workspaces(self):
        def load():
            workspaces = self.services.workspaces.list()
            self.workspace_combo.blockSignals(True)
            try:
                self.workspace_combo.clear()
                for workspace in workspaces:
                    self.workspace_combo.addItem(workspace.name, str(workspace.id))
                index = self.workspace_combo.findData(str(self.workspace_id))
                self.workspace_combo.setCurrentIndex(index if index >= 0 else 0)
                data = self.workspace_combo.currentData()
                self.workspace_id = UUID(data) if data else None
            finally:
                self.workspace_combo.blockSignals(False)
        self.run_operation(load)

    def change_workspace(self, index):
        self.search_timer.stop()
        self.workspace_id = UUID(self.workspace_combo.itemData(index)) if index >= 0 else None
        self.current_detail = None
        self.detail_view.set_detail(None)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh_events()

    def activate(self):
        if self._detail_event_id is None:
            self.refresh_workspaces()
        self.refresh_events()

    def select_workspace(self, workspace_id):
        """Point this page at one Workspace (used by cross-page navigation)."""
        if workspace_id is None or workspace_id == self.workspace_id:
            return self.workspace_id
        index = self.workspace_combo.findData(str(workspace_id))
        if index < 0:
            return None
        self.workspace_combo.setCurrentIndex(index)
        return self.workspace_id

    def focus_event(self, event_id):
        """Select one Event after switching Workspace when necessary."""
        def locate():
            for workspace in self.services.workspaces.list():
                for detail in self.service.list_events(workspace.id, include_deleted=True):
                    if detail.event.id == event_id:
                        return workspace.id
            return None
        if self._detail_event_id is not None:
            # A detached single-Event window already shows exactly this Event.
            return self.current_detail
        workspace_id = self.run_operation(locate)
        if workspace_id is not None:
            self.select_workspace(workspace_id)
        self.refresh_events(event_id)
        return self.current_detail

    # ---------------------------------------------------------- evidence focus
    def evidence_workspace(self, evidence_id):
        """The Workspace that owns one Evidence, by walking its Events.

        ``EvidenceService`` has no by-id Workspace lookup on the page side, so the
        owning Event is found through the ordinary read service first.
        """
        service = getattr(self.services, "evidence", None)
        if service is None:
            return None
        def locate():
            for workspace in self.services.workspaces.list():
                for detail in self.service.list_events(workspace.id, include_deleted=True):
                    for found in service.list_by_event(workspace.id, detail.event.id,
                                                       include_deleted=True):
                        if found.evidence.id == evidence_id:
                            return workspace.id, detail.event.id
            return None
        return self.run_operation(locate)

    def focus_evidence(self, evidence_id):
        """Select the Event that owns an Evidence, then select that very Evidence.

        Used by the Analysis Sources list: an Evidence has no page of its own, so
        the jump lands on its Event with the evidence row selected.
        """
        if evidence_id is None:
            return None
        if self._detail_event_id is not None:
            # A detached single-Event window already shows exactly this Event.
            self.evidence_panel.set_event(self.current_detail.event if self.current_detail else None,
                                          include_deleted_event=True)
            return self.evidence_panel.focus_evidence(evidence_id)
        found = self.evidence_workspace(evidence_id)
        if found is None:
            self.status.setText("找不到该证据所属的事件：它可能已被删除，或不属于任何 Workspace。")
            return None
        workspace_id, event_id = found
        if workspace_id is not None:
            self.select_workspace(workspace_id)
        self.refresh_events(event_id)
        self.evidence_panel.include_deleted.setChecked(True)
        return self.evidence_panel.focus_evidence(evidence_id)

    def refresh_events(self, selected_id=None):
        self.search_timer.stop()
        if isinstance(selected_id, bool):
            selected_id = None
        if selected_id is None and self.current_detail:
            selected_id = self.current_detail.event.id
        self.current_detail = None
        self.detail_view.set_detail(None)
        self.event_list.blockSignals(True)
        self.event_list.clear()
        self.event_list.blockSignals(False)
        self.update_buttons()
        if self.service is None or self.workspace_id is None:
            self.status.setText("事件服务尚未装配。")
            return
        if self._detail_event_id is not None:
            def load_detail():
                detail = self.service.detail(self.workspace_id, self._detail_event_id, include_deleted=True)
                self.current_detail = detail
                self.detail_view.set_detail(detail)
                self.update_buttons()
            self.run_operation(load_detail)
            return
        def load():
            details = self.service.list_events(self.workspace_id, self.search.text().strip(),
                                               include_deleted=self.include_deleted.isChecked())
            self.event_list.blockSignals(True)
            try:
                target = None
                for detail in details:
                    event = detail.event
                    text = (f"{event.title} · {event.event_type} · {event.record_kind} · "
                            f"{len(detail.participants)} 参与者\n{event_time(event)} · "
                            + ("已删除" if event.deleted_at else "有效记录"))
                    item = QListWidgetItem(text)
                    item.setData(ROLE, event.id)
                    self.event_list.addItem(item)
                    if event.id == selected_id:
                        target = item
                target = target or (self.event_list.item(0) if details else None)
                self.event_list.setCurrentItem(target)
            finally:
                self.event_list.blockSignals(False)
            self.status.setText(f"当前 Workspace · {len(details)} 个事件（搜索结果）")
            self.select_event(target)
        self.run_operation(load)

    def select_event(self, item, previous=None):
        self.current_detail = None
        self.detail_view.set_detail(None)
        self.update_buttons()
        if item is None:
            return
        def load():
            detail = self.service.detail(self.workspace_id, item.data(ROLE), include_deleted=True)
            self.current_detail = detail
            self.detail_view.set_detail(detail)
            self.update_buttons()
        self.run_operation(load)

    @staticmethod
    def selected_row(table):
        item = table.item(table.currentRow(), 0)
        return item.data(ROLE) if item else None

    def show_evidence(self, detail):
        """Mirror the displayed Event into the Evidence panel; None clears it.

        A soft-deleted Event keeps its Evidence hidden unless deleted records are
        explicitly requested, so the page checkbox is forwarded as context.
        """
        self.evidence_panel.set_event(detail.event if detail is not None else None,
                                      include_deleted_event=self.include_deleted.isChecked())

    def update_buttons(self):
        available = self.service is not None and self.workspace_id is not None
        active = available and self.current_detail is not None and self.current_detail.event.deleted_at is None
        self.create_button.setEnabled(available)
        for button in (self.edit_button, self.delete_button, self.add_participant_button):
            button.setEnabled(active)
        historical = available and self.current_detail is not None
        self.attach_button.setEnabled(historical)
        self.analyze_button.setEnabled(historical)
        self.restore_button.setEnabled(bool(available and self.current_detail and self.current_detail.event.deleted_at))
        self.remove_participant_button.setEnabled(bool(active and self.selected_row(self.detail_view.participants)))
        self.detach_button.setEnabled(bool(historical and self.selected_row(self.detail_view.histories)))

    def analyze_event(self):
        """Phase 8 entry: options dialog only; the model runs after confirmation."""
        if self.current_detail is None:
            return None
        return analysis_center(self).analyze_target(
            "EVENT", self.current_detail.event.id, workspace_id=self.workspace_id,
            target_label=self.current_detail.event.title)

    def changed(self, event_id=None):
        self.refresh_events(event_id)
        self.events_changed.emit(self.workspace_id)

    def create_event(self):
        if self.service is None or self.workspace_id is None:
            return
        def create():
            dialog = EventDialog(self.services.people.list(self.workspace_id), parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                event = self.service.create_event(self.workspace_id, **dialog.values())
                self.changed(event.id)
        self.run_operation(create)

    def edit_event(self):
        if self.current_detail is None or self.current_detail.event.deleted_at:
            return
        def edit():
            id = self.current_detail.event.id
            dialog = EventDialog(self.services.people.list(self.workspace_id), self.current_detail, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.service.update_event(self.workspace_id, id, **dialog.values())
                self.changed(id)
        self.run_operation(edit)

    def delete_event(self):
        if self.current_detail is None or self.current_detail.event.deleted_at:
            return
        if QMessageBox.question(self, "删除事件", "将软删除事件，参与者和 History 引用会保留。是否继续？") != QMessageBox.StandardButton.Yes:
            return
        def delete():
            self.service.soft_delete_event(self.workspace_id, self.current_detail.event.id)
            self.changed()
        self.run_operation(delete)

    def restore_event(self):
        if self.current_detail is None or not self.current_detail.event.deleted_at:
            return
        def restore():
            id = self.current_detail.event.id
            self.service.restore_event(self.workspace_id, id)
            self.changed(id)
        self.run_operation(restore)

    def add_participant(self):
        if self.current_detail is None or self.current_detail.event.deleted_at:
            return
        def add():
            ids = {view.person.id for view in self.current_detail.participants}
            people = [person for person in self.services.people.list(self.workspace_id) if person.id not in ids]
            dialog = ParticipantDialog(people, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                id = self.current_detail.event.id
                self.service.add_participant(self.workspace_id, id, **dialog.values())
                self.changed(id)
        self.run_operation(add)

    def remove_participant(self):
        view = self.selected_row(self.detail_view.participants)
        if self.current_detail is None or self.current_detail.event.deleted_at or view is None:
            return
        def remove():
            id = self.current_detail.event.id
            self.service.remove_participant(self.workspace_id, id, view.person.id)
            self.changed(id)
        self.run_operation(remove)

    def attach_history(self):
        if self.current_detail is None:
            return
        def attach():
            id = self.current_detail.event.id
            links = self.service.list_attachable_histories(self.workspace_id)
            # Snapshot the real linkage at dialog time so a stale detail replaces
            # only the reference the user actually saw.
            expected = {link.history.id: link.history.event_id for link in links}
            dialog = HistoryAttachDialog(links, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                chosen = dialog.values()
                try:
                    self.service.attach_event_to_history(self.workspace_id, chosen, id,
                                                         expected_event_id=expected.get(chosen))
                except ConflictError:
                    self.refresh_events(id)
                    raise
                self.changed(id)
        self.run_operation(attach)

    def detach_history(self):
        link = self.selected_row(self.detail_view.histories)
        if self.current_detail is None or link is None:
            return
        def detach():
            id = self.current_detail.event.id
            try:
                self.service.attach_event_to_history(self.workspace_id, link.history.id, None,
                                                     expected_event_id=id)
            except ConflictError:
                self.refresh_events(id)
                raise
            self.changed(id)
        self.run_operation(detach)
