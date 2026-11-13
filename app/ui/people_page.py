"""Minimal real People UI. Only application Services and Domain/DTO values."""
import logging
from uuid import UUID
from pathlib import Path
from PySide6.QtCore import Qt, QSize, QTimer, Signal
from app.ui.operations import run_operation as _run_operation
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QSplitter, QTextEdit, QVBoxLayout, QWidget)
from app.domain.errors import DomainError
from app.services.errors import DatabaseError
from app.ui.analysis_dialog import ANALYZE_LABELS, analysis_center
from app.ui.person_dialog import AliasDialog, FIELD_LABELS, IMAGE_FILTER, PersonDialog
from app.ui.image_viewer import ImageViewer
from app.ui.relationship_panel import RelationshipPanel
from app.ui.event_timeline import EventTimeline
from app.ui.memory_panel import MemoryPanel, MemoryWorkspaceDialog
from app.ui.search_panel import SearchPanel
from app.ui.timeline_panel import MODE_PERSON, MODE_WORKSPACE, RelationshipDetailDialog, TimelinePanel

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole


def plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label

class PeoplePage(QWidget):
    navigate_requested = Signal(str, object)

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.setObjectName("people")
        self.services = services
        self.workspace_id = services.initial_workspace.id
        self.current_detail = None
        self._viewers = []
        self._dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = plain_label("People · 人物")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        top = QHBoxLayout()
        top.addWidget(QLabel("当前 Workspace"))
        self.workspace_combo = QComboBox()
        self.workspace_combo.setObjectName("workspaceSelector")
        self.workspace_combo.setMinimumWidth(160)
        self.workspace_combo.currentIndexChanged.connect(self.change_workspace)
        top.addWidget(self.workspace_combo, 1)
        self.button(top, "新建 Workspace", self.create_workspace)
        layout.addLayout(top)
        self.search_panel = SearchPanel(services, self, name="peopleSearch",
                                        placeholder="统一搜索：人物 / 别称 / 事件 / 证据（本页过滤见下方）")
        self.search_panel.navigate_requested.connect(self.open_search_result)
        layout.addWidget(self.search_panel)
        splitter = QSplitter()
        left = QWidget()
        column = QVBoxLayout(left)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索显示姓名、真实姓名、别称")
        self.search.setObjectName("peopleSearch")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(self.refresh_people)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.returnPressed.connect(self.refresh_people)
        column.addWidget(self.search)
        row = QHBoxLayout()
        self.button(row, "新建人物", self.create_person)
        self.button(row, "刷新", self.refresh_people)
        column.addLayout(row)
        self.people_list = QListWidget()
        self.people_list.setObjectName("peopleList")
        self.people_list.currentItemChanged.connect(self.select_person)
        column.addWidget(self.people_list)
        splitter.addWidget(left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        right = QWidget()
        detail = QVBoxLayout(right)
        self.avatar = QLabel("未设置头像")
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setMinimumHeight(100)
        detail.addWidget(self.avatar)
        row = QHBoxLayout()
        self.edit_button = self.button(row, "编辑人物", self.edit_person)
        self.archive_button = self.button(row, "归档人物", self.archive_person)
        detail.addLayout(row)
        row = QHBoxLayout()
        # Phase 8: the model is called only when this button is clicked, never when
        # the page opens or a person is selected.
        self.analyze_button = self.button(row, ANALYZE_LABELS["PERSON"], self.analyze_person)
        detail.addLayout(row)
        self.field_labels = {}
        form = QFormLayout()
        for key, text in FIELD_LABELS.items():
            if key == "notes":
                continue
            value = plain_label("—")
            self.field_labels[key] = value
            form.addRow(text, value)
        detail.addLayout(form)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(100)
        detail.addWidget(QLabel("备注"))
        detail.addWidget(self.notes)
        detail.addWidget(QLabel("别称"))
        self.alias_list = QListWidget()
        self.alias_list.setMaximumHeight(100)
        detail.addWidget(self.alias_list)
        row = QHBoxLayout()
        self.add_alias_button = self.button(row, "添加别称", self.add_alias)
        self.remove_alias_button = self.button(row, "删除所选别称", self.remove_alias)
        detail.addLayout(row)
        detail.addWidget(QLabel("头像 / Gallery（删除引用不会删除原图）"))
        self.image_list = QListWidget()
        self.image_list.setIconSize(QSize(64, 64))
        self.image_list.setMinimumHeight(160)
        self.image_list.itemDoubleClicked.connect(lambda _: self.open_image())
        detail.addWidget(self.image_list)
        row = QHBoxLayout()
        self.avatar_button = self.button(row, "选择头像", lambda: self.choose_images("AVATAR"))
        self.gallery_button = self.button(row, "添加 Gallery", lambda: self.choose_images("GALLERY"))
        detail.addLayout(row)
        row = QHBoxLayout()
        self.remove_image_button = self.button(row, "删除所选引用", self.remove_image)
        self.open_image_button = self.button(row, "查看原图", self.open_image)
        self.promote_button = self.button(row, "设为头像", self.promote_image)
        detail.addLayout(row)
        self.relationship_panel = RelationshipPanel(services, self)
        detail.addWidget(self.relationship_panel)
        # Phase 9: the memories of this person, plus the Workspace-level list (a
        # person panel can only bind a Memory to its own person, so the person_id
        # IS NULL scope opens in its own window).
        self.memory_panel = MemoryPanel(services, self, allow_person=True,
                                        name="personMemoryPanel",
                                        title="Memory · 该人物的记忆（只有你确认过的内容）")
        detail.addWidget(self.memory_panel)
        memory_row = QHBoxLayout()
        self.workspace_memory_button = self.button(memory_row, "Workspace 记忆（person 为空）",
                                                   self.open_workspace_memories)
        detail.addLayout(memory_row)
        # Phase 7: the complete Timeline (Events *and* relationship state changes).
        # The embedded instance is always person-scoped; the Workspace mode lives on
        # the Dashboard, so this page never needs a scope switcher.
        timeline_group = QGroupBox("Timeline · 完整时间线（Event + 关系状态变更）")
        timeline_group.setObjectName("peopleTimelineGroup")
        timeline_column = QVBoxLayout(timeline_group)
        self.full_timeline = TimelinePanel(services, self)
        self.full_timeline.mode = MODE_PERSON
        self.full_timeline.mode_combo.setCurrentIndex(
            max(0, self.full_timeline.mode_combo.findData(MODE_PERSON)))
        self.full_timeline.mode_combo.hide()
        timeline_column.addWidget(self.full_timeline)
        detail.addWidget(timeline_group)
        # The historic Event-only timeline stays available unchanged (same widget,
        # same objectName, same row semantics) right below the complete one.
        self.event_timeline = EventTimeline(services, self)
        detail.addWidget(self.event_timeline)
        detail.addStretch()
        scroll.setWidget(right)
        splitter.addWidget(scroll)
        splitter.setSizes([300, 620])
        layout.addWidget(splitter, 1)
        self.status = plain_label()
        layout.addWidget(self.status)
        self.refresh_workspaces()
        self.refresh_people()

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def run_operation(self, callback):
        """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
        return _run_operation(self, callback, context="People UI", title="操作未完成")

    def refresh_workspaces(self):
        workspaces = self.services.workspaces.list()
        self.workspace_combo.blockSignals(True)
        self.workspace_combo.clear()
        for workspace in workspaces:
            self.workspace_combo.addItem(workspace.name, str(workspace.id))
        selected = next((index for index, item in enumerate(workspaces) if item.id == self.workspace_id), 0)
        self.workspace_combo.setCurrentIndex(selected)
        self.workspace_combo.blockSignals(False)
        self.workspace_id = UUID(self.workspace_combo.currentData())

    def change_workspace(self, index):
        if index < 0:
            return
        self.workspace_id = UUID(self.workspace_combo.itemData(index))
        self.current_detail = None
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh_people()

    def create_workspace_named(self, name):
        workspace = self.services.workspaces.create(name)
        self.workspace_id = workspace.id
        self.refresh_workspaces()
        self.refresh_people()
        return workspace

    def create_workspace(self):
        name, accepted = QInputDialog.getText(self, "新建 Workspace", "名称")
        if accepted:
            self.run_operation(lambda: self.create_workspace_named(name))

    def refresh_people(self, selected_id=None):
        self.search_timer.stop()
        # Qt clicked can pass a bool; only preserve a genuine Domain id.
        if isinstance(selected_id, bool):
            selected_id = None
        if selected_id is None and self.current_detail:
            selected_id = self.current_detail.person.id
        def refresh():
            people = self.services.people.list(self.workspace_id, self.search.text().strip())
            self.people_list.blockSignals(True)
            self.people_list.clear()
            target = None
            for person in people:
                item = QListWidgetItem(person.display_name)
                item.setData(ROLE, person.id)
                self.people_list.addItem(item)
                if person.id == selected_id:
                    target = item
            target = target or (self.people_list.item(0) if people else None)
            self.people_list.setCurrentItem(target)
            self.people_list.blockSignals(False)
            self.status.setText(f"当前 Workspace · {len(people)} 个人物（搜索结果）")
            self.select_person(target, None)
        self.run_operation(refresh)

    def select_person(self, item, previous=None):
        self.current_detail = None
        self.relationship_panel.set_person(None, None)
        self.event_timeline.set_person(None, None)
        self.full_timeline.set_person(None, None)
        self.memory_panel.set_person(None, None)
        self.alias_list.clear()
        self.image_list.clear()
        self.avatar.clear()
        self.avatar.setText("未设置头像")
        self.notes.clear()
        for label in self.field_labels.values():
            label.setText("—")
        enabled = item is not None
        for button in (self.edit_button, self.archive_button, self.add_alias_button,
                       self.remove_alias_button, self.avatar_button, self.gallery_button,
                       self.remove_image_button, self.open_image_button, self.promote_button,
                       self.analyze_button):
            button.setEnabled(enabled)
        if item is None:
            return
        def populate():
            snapshot = self.services.people.detail(self.workspace_id, item.data(ROLE))
            self.current_detail = snapshot
            for key, label in self.field_labels.items():
                value = getattr(snapshot.person, key)
                label.setText(value.isoformat() if hasattr(value, "isoformat") else str(value or "—"))
            self.notes.setPlainText(snapshot.person.notes or "")
            for alias in snapshot.aliases:
                entry = QListWidgetItem(f"{alias.alias} · {alias.alias_type}")
                entry.setData(ROLE, alias.id)
                entry.setToolTip(alias.note or "")
                self.alias_list.addItem(entry)
            for image in snapshot.images:
                entry = QListWidgetItem(f"{image.link.role} · {image.media.width} × {image.media.height}")
                entry.setData(ROLE, image)
                try:
                    path = self.services.media.original_path(self.workspace_id, image.media.id)
                    pixmap = QPixmap(str(path))
                    if not pixmap.isNull():
                        entry.setIcon(QIcon(pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
                        if image.link.role == "AVATAR":
                            self.avatar.setPixmap(pixmap.scaled(150, 150, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                except (DomainError, DatabaseError, OSError) as exc:
                    logger.warning("Media thumbnail unavailable: type=%s", type(exc).__name__)
                    entry.setText(entry.text() + " · 文件不可读")
                self.image_list.addItem(entry)
            self.relationship_panel.set_person(self.workspace_id, snapshot.person.id)
            self.event_timeline.set_person(self.workspace_id, snapshot.person.id)
            self.full_timeline.set_person(self.workspace_id, snapshot.person.id)
            self.memory_panel.set_person(self.workspace_id, snapshot.person.id)
        self.run_operation(populate)

    # ------------------------------------------------------- cross-page targets

    def select_workspace(self, workspace_id):
        """Point this page at one Workspace (used by cross-page navigation)."""
        if workspace_id is None or workspace_id == self.workspace_id:
            return self.workspace_id
        index = self.workspace_combo.findData(str(workspace_id))
        if index < 0:
            return None
        self.workspace_combo.setCurrentIndex(index)
        return self.workspace_id

    def focus_person(self, person_id):
        """Select one person after switching Workspace when necessary."""
        if self.select_workspace(self._workspace_of(person_id)) is None:
            return None
        self.refresh_people(person_id)
        return self.current_detail

    def _workspace_of(self, person_id):
        """The Workspace that owns a person, or the current one when unknown.

        ``PersonService.list`` has no ``include_deleted`` switch, so this lookup
        covers the current people; an unknown id leaves the page where it is.
        """
        def lookup():
            for workspace in self.services.workspaces.list():
                for item in self.services.people.list(workspace.id):
                    if item.id == person_id:
                        return workspace.id
            return None
        found = self.run_operation(lookup)
        return found if found is not None else self.workspace_id

    def focus_relationship(self, relationship_id):
        """Select the person of a Relationship, then open its Relationship detail."""
        def locate():
            for workspace in self.services.workspaces.list():
                for view in self.services.relationships.list_by_workspace(workspace.id, include_deleted=True):
                    if view.relationship.id == relationship_id:
                        return workspace.id, view.person_a.id
            return None
        found = self.run_operation(locate)
        if found is None:
            return None
        workspace_id, person_id = found
        self.select_workspace(workspace_id)
        self.refresh_people(person_id)
        return self.open_relationship(relationship_id)

    def open_relationship(self, relationship_id):
        """Open the read-only Relationship detail (its own workspace is re-read)."""
        def show():
            dialog = RelationshipDetailDialog(self.services, self.workspace_id, relationship_id, self)
            dialog.load(self.workspace_id, relationship_id)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._dialogs.append(dialog)
            dialog.finished.connect(lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
            dialog.show()
            return dialog
        return self.run_operation(show)

    def open_search_result(self, result):
        """A unified-search hit: PERSON/ALIAS select here, EVENT/EVIDENCE jump."""
        if result.entity_type in {"PERSON", "ALIAS"}:
            person_id = result.metadata.get("person_id") or result.entity_id
            self.focus_person(person_id)
        elif result.entity_type == "EVENT":
            self.navigate_requested.emit("event", result.entity_id)
        elif result.entity_type == "EVIDENCE":
            event_id = result.metadata.get("event_id")
            if event_id is not None:
                self.navigate_requested.emit("event", event_id)
        return result

    def activate(self):
        self.run_operation(self.refresh_workspaces)
        self.search_panel.set_workspace(self.workspace_id)
        self.refresh_people()

    # ------------------------------------------------------------ Phase 9: memory
    def open_workspace_memories(self):
        """Open the Workspace-level Memory list (``person_id IS NULL``).

        The list itself is read-only until the user adds a Memory there; opening the
        window never writes anything and never binds a Memory to a person.
        """
        if getattr(self.services, "memories", None) is None:
            QMessageBox.warning(self, "Memory 不可用", "Memory 服务尚未装配：无法查看 Workspace 记忆。")
            return None
        dialog = MemoryWorkspaceDialog(self.services, self.workspace_id, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialogs.append(dialog)
        dialog.finished.connect(
            lambda _: self._dialogs.remove(dialog) if dialog in self._dialogs else None)
        dialog.show()
        return dialog

    def create_person(self):
        dialog = PersonDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def create():
                values = dialog.values()
                name = values.pop("display_name")
                person = self.services.people.create(self.workspace_id, name, avatar_path=dialog.avatar_path, **values)
                self.refresh_people(person.id)
            self.run_operation(create)

    def edit_person(self):
        if self.current_detail is None:
            return
        person_id = self.current_detail.person.id
        dialog = PersonDialog(self.current_detail.person, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def edit():
                self.services.people.update(self.workspace_id, person_id, **dialog.values())
                self.refresh_people(person_id)
            self.run_operation(edit)

    def archive_person(self):
        if self.current_detail is None:
            return
        if QMessageBox.question(self, "归档人物", "人物将被软删除，图片文件会保留。是否继续？") != QMessageBox.StandardButton.Yes:
            return
        def archive():
            self.services.people.soft_delete(self.workspace_id, self.current_detail.person.id)
            self.current_detail = None
            self.refresh_people()
        self.run_operation(archive)

    def analyze_person(self):
        """Phase 8 entry: opens the options dialog; no model call happens here."""
        if self.current_detail is None:
            return None
        return analysis_center(self).analyze_target(
            "PERSON", self.current_detail.person.id, workspace_id=self.workspace_id,
            target_label=self.current_detail.person.display_name)

    def add_alias(self):
        if self.current_detail is None:
            return
        dialog = AliasDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def add():
                self.services.people.add_alias(self.workspace_id, self.current_detail.person.id, **dialog.values())
                self.refresh_people()
            self.run_operation(add)

    def remove_alias(self):
        item = self.alias_list.currentItem()
        if item is not None and self.current_detail:
            def remove():
                self.services.people.remove_alias(self.workspace_id, self.current_detail.person.id, item.data(ROLE))
                self.refresh_people()
            self.run_operation(remove)

    def choose_images(self, role):
        if self.current_detail is None:
            return
        if role == "AVATAR":
            filename, _ = QFileDialog.getOpenFileName(self, "选择头像", "", IMAGE_FILTER)
            filenames = [filename] if filename else []
        else:
            filenames, _ = QFileDialog.getOpenFileNames(self, "添加 Gallery", "", IMAGE_FILTER)
        for filename in filenames:
            self.run_operation(lambda filename=filename: self.import_image(Path(filename), role))

    def import_image(self, path, role):
        if self.current_detail is None:
            return None
        media = self.services.media.import_file(self.workspace_id, path,
            person_id=self.current_detail.person.id, role=role)
        self.refresh_people()
        return media

    def remove_image(self):
        item = self.image_list.currentItem()
        if item is not None and self.current_detail:
            def remove():
                image = item.data(ROLE)
                self.services.people.remove_media_reference(self.workspace_id, self.current_detail.person.id, image.link.id)
                self.refresh_people()
            self.run_operation(remove)

    def promote_image(self):
        item = self.image_list.currentItem()
        if item is not None and self.current_detail:
            def promote():
                image = item.data(ROLE)
                self.services.people.set_avatar(self.workspace_id, self.current_detail.person.id, image.media.id)
                self.refresh_people()
            self.run_operation(promote)

    def open_image(self):
        item = self.image_list.currentItem()
        if item is None:
            return
        def open_original():
            image = item.data(ROLE)
            path = self.services.media.original_path(self.workspace_id, image.media.id)
            viewer = ImageViewer(path, self)
            viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._viewers.append(viewer)
            viewer.finished.connect(lambda _: self._viewers.remove(viewer) if viewer in self._viewers else None)
            viewer.show()
            return viewer
        return self.run_operation(open_original)
