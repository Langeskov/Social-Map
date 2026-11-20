"""Minimal real People UI. Only application Services and Domain/DTO values."""
import logging
from dataclasses import dataclass
from uuid import UUID
from pathlib import Path
from PySide6.QtCore import Qt, QSize, QTimer, Signal
from app.ui.operations import run_operation as _run_operation
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)
from app.domain.errors import DomainError, NotFoundError
from app.services.errors import DatabaseError
from app.ui.analysis_dialog import ANALYZE_LABELS, analysis_center
from app.ui.context import ContextPage
from app.ui.design.fonts import mono_font
from app.ui.design.tokens import SPACE
from app.ui.design.widgets import LayerCard, StatusPill
from app.ui.person_dialog import AliasDialog, FIELD_LABELS, IMAGE_FILTER, PersonDialog
from app.ui.image_viewer import ImageViewer
from app.ui.relationship_panel import RelationshipPanel
from app.ui.memory_panel import MemoryPanel, MemoryWorkspaceDialog
from app.ui.search_panel import SearchPanel
from app.ui.timeline_panel import MODE_PERSON, RelationshipDetailDialog, TimelinePanel

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole


def plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


@dataclass(frozen=True)
class ArchivedPersonSnapshot:
    """Identity of an archived person, read from the list projection.

    ``PersonService.detail`` only reads live people, so an archived person is shown
    from the management list instead: identity plus its history stay visible, while
    the alias / image references that only the detail query returns are reported as
    unavailable rather than invented. This is a UI-side degradation, not a schema or
    Domain change.
    """

    person: object
    aliases: tuple = ()
    images: tuple = ()
    degraded: bool = True

class PeoplePage(ContextPage, QWidget):
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
        margin = SPACE["md"]
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE["sm"])
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
        # Phase 11: unified search moved to the TopBar (Ctrl+F); this page keeps its
        # own *filter* box, and the embedded panel is no longer a visible duplicate.
        self.search_panel.hide()
        splitter = QSplitter()
        left = QWidget()
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索显示姓名、真实姓名、别称")
        self.search.setObjectName("peopleSearchInput")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(self.refresh_people)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.returnPressed.connect(self.refresh_people)
        column.addWidget(self.search)
        filters = QHBoxLayout()
        self.include_archived = QCheckBox("显示已归档")
        self.include_archived.setObjectName("peopleIncludeArchived")
        self.include_archived.toggled.connect(lambda _: self.refresh_people())
        filters.addWidget(self.include_archived)
        filters.addStretch(1)
        column.addLayout(filters)
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
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(SPACE["md"])
        # ---------------------------------------------------------- Identity
        identity = LayerCard("ENTITY", "Identity · 身份", right)
        identity.setObjectName("personIdentityCard")
        body = identity.body
        avatar_row = QHBoxLayout()
        self.avatar = QLabel("未设置头像")
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setMinimumSize(120, 120)
        self.avatar.setMaximumWidth(160)
        avatar_row.addWidget(self.avatar)
        facts = QVBoxLayout()
        facts.setSpacing(SPACE["xs"])
        self.identity_name = plain_label("—")
        self.identity_name.setObjectName("personIdentityName")
        facts.addWidget(self.identity_name)
        self.status_pill = StatusPill("ACTIVE")
        self.status_pill.setVisible(False)
        facts.addWidget(self.status_pill)
        self.field_labels = {}
        self.field_rows = {}
        self.fields_form = QFormLayout()
        self.fields_form.setContentsMargins(0, 0, 0, 0)
        for key, text in FIELD_LABELS.items():
            # display_name is the card heading and notes has its own block.
            if key in {"notes", "display_name"}:
                continue
            value = plain_label("—")
            self.field_labels[key] = value
            self.fields_form.addRow(text, value)
            self.field_rows[key] = self.fields_form.rowCount() - 1
        facts.addLayout(self.fields_form)
        self.identity_uuid = plain_label("")
        self.identity_uuid.setObjectName("personIdentityUUID")
        self.identity_uuid.setFont(mono_font("small"))
        facts.addWidget(self.identity_uuid)
        avatar_row.addLayout(facts, 1)
        body.addLayout(avatar_row)
        row = QHBoxLayout()
        self.edit_button = self.button(row, "编辑人物", self.edit_person)
        self.archive_button = self.button(row, "归档人物", self.archive_person)
        # Phase 8: the model is called only when this button is clicked, never when
        # the page opens or a person is selected.
        self.analyze_button = self.button(row, ANALYZE_LABELS["PERSON"], self.analyze_person)
        body.addLayout(row)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(90)
        body.addWidget(QLabel("备注"))
        body.addWidget(self.notes)
        body.addWidget(QLabel("别称"))
        self.alias_list = QListWidget()
        self.alias_list.setMaximumHeight(90)
        body.addWidget(self.alias_list)
        row = QHBoxLayout()
        self.add_alias_button = self.button(row, "添加别称", self.add_alias)
        self.remove_alias_button = self.button(row, "删除所选别称", self.remove_alias)
        body.addLayout(row)
        body.addWidget(QLabel("头像 / Gallery（删除引用不会删除原图）"))
        self.image_list = QListWidget()
        self.image_list.setIconSize(QSize(64, 64))
        self.image_list.setMinimumHeight(120)
        self.image_list.itemDoubleClicked.connect(lambda _: self.open_image())
        body.addWidget(self.image_list)
        row = QHBoxLayout()
        self.avatar_button = self.button(row, "选择头像", lambda: self.choose_images("AVATAR"))
        self.gallery_button = self.button(row, "添加 Gallery", lambda: self.choose_images("GALLERY"))
        body.addLayout(row)
        row = QHBoxLayout()
        self.remove_image_button = self.button(row, "删除所选引用", self.remove_image)
        self.open_image_button = self.button(row, "查看原图", self.open_image)
        self.promote_button = self.button(row, "设为头像", self.promote_image)
        body.addLayout(row)
        detail.addWidget(identity)
        self.identity_card = identity
        # ----------------------------------------------------- Relationships
        relationships = LayerCard("LINK", "Relationships · 关系", right)
        self.relationship_panel = RelationshipPanel(services, self)
        relationships.body.addWidget(self.relationship_panel)
        detail.addWidget(relationships)
        self.relationships_card = relationships
        # ---------------------------------------------------------- Timeline
        timeline_card = LayerCard("OCCURRENCE", "Timeline · 时间线（Event + 关系状态变更）", right)
        timeline_card.setObjectName("peopleTimelineGroup")
        self.full_timeline = TimelinePanel(services, self)
        self.full_timeline.mode = MODE_PERSON
        self.full_timeline.mode_combo.setCurrentIndex(
            max(0, self.full_timeline.mode_combo.findData(MODE_PERSON)))
        self.full_timeline.mode_combo.hide()
        timeline_card.body.addWidget(self.full_timeline)
        detail.addWidget(timeline_card)
        self.timeline_card = timeline_card
        # Phase 11: one Timeline only. The historic Event-only EventTimeline was a
        # second, duplicate view of the same facts; TimelinePanel already carries both
        # Events and relationship state changes, so the duplicate widget is gone.
        # ------------------------------------------- Evidence / Memory / Analysis
        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("personDetailTabs")
        evidence_tab = QWidget()
        evidence_column = QVBoxLayout(evidence_tab)
        evidence_column.setContentsMargins(0, 0, 0, 0)
        evidence_card = LayerCard("PROOF", "Evidence · 与该人物事件相关的原始证据", evidence_tab)
        self.evidence_list = QListWidget()
        self.evidence_list.setObjectName("personEvidenceList")
        self.evidence_list.setMinimumHeight(150)
        self.evidence_list.itemDoubleClicked.connect(lambda _: self.open_person_evidence())
        evidence_card.body.addWidget(self.evidence_list)
        self.evidence_status = plain_label("尚未选择人物。")
        self.evidence_status.setObjectName("personEvidenceStatus")
        evidence_card.body.addWidget(self.evidence_status)
        evidence_column.addWidget(evidence_card)
        self.detail_tabs.addTab(evidence_tab, "Evidence · 证据")
        memory_tab = QWidget()
        memory_column = QVBoxLayout(memory_tab)
        memory_column.setContentsMargins(0, 0, 0, 0)
        # Phase 9 + decision 3: Memory is a user-confirmed semantic layer, so it has
        # its own tab instead of sitting inside the Fact -> Evidence -> Analysis chain.
        self.memory_panel = MemoryPanel(services, self, allow_person=True,
                                        name="personMemoryPanel",
                                        title="User-confirmed Memory · 你确认过的内容")
        memory_column.addWidget(self.memory_panel)
        memory_row = QHBoxLayout()
        self.workspace_memory_button = self.button(memory_row, "Workspace 记忆（person 为空）",
                                                   self.open_workspace_memories)
        memory_column.addLayout(memory_row)
        self.detail_tabs.addTab(memory_tab, "Memory · 记忆")
        analysis_tab = QWidget()
        analysis_column = QVBoxLayout(analysis_tab)
        analysis_column.setContentsMargins(0, 0, 0, 0)
        self.analysis_layer = LayerCard("INFERENCE", "Analysis · 关于该人物的分析记录", analysis_tab)
        self.analysis_list = QListWidget()
        self.analysis_list.setObjectName("personAnalysisList")
        self.analysis_list.setMinimumHeight(150)
        self.analysis_list.itemDoubleClicked.connect(lambda _: self.open_person_analysis())
        self.analysis_layer.body.addWidget(self.analysis_list)
        self.analysis_status = plain_label("尚未选择人物。")
        self.analysis_status.setObjectName("personAnalysisStatus")
        self.analysis_layer.body.addWidget(self.analysis_status)
        analysis_column.addWidget(self.analysis_layer)
        self.detail_tabs.addTab(analysis_tab, "Analysis · 分析")
        detail.addWidget(self.detail_tabs)
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
        if self.workspace_context is not None:
            # A bound page never picks its own Workspace: the context is the source.
            self.workspace_id = self.workspace_context.workspace_id
        self.workspace_combo.blockSignals(True)
        self.workspace_combo.clear()
        for workspace in workspaces:
            self.workspace_combo.addItem(workspace.name, str(workspace.id))
        selected = next((index for index, item in enumerate(workspaces) if item.id == self.workspace_id), 0)
        self.workspace_combo.setCurrentIndex(selected)
        self.workspace_combo.blockSignals(False)
        if self.workspace_context is None:
            data = self.workspace_combo.currentData()
            self.workspace_id = UUID(data) if data else None

    def change_workspace(self, index):
        if index < 0:
            return
        workspace_id = UUID(self.workspace_combo.itemData(index))
        if self.workspace_context is not None:
            # The global selector owns the context; every page resets together.
            self.workspace_context.set_workspace(workspace_id)
            return
        self.workspace_id = workspace_id
        self.clear_context()
        self.refresh_people()

    # ------------------------------------------------- Phase 11: context protocol
    def context_object(self):
        """Breadcrumb object segment: the selected person, when there is one."""
        if self.current_detail is None:
            return ""
        return self.current_detail.person.display_name

    def clear_context(self):
        """Drop the selected person and every panel that followed it."""
        self.clear_detail_view()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.search_panel.clear()
        self.notify_context()

    def load_workspace(self, workspace_id):
        if self.search_panel is not None:
            self.search_panel.set_workspace(workspace_id)
        self.refresh_people()

    def sync_workspace_combo(self):
        index = self.workspace_combo.findData(str(self.workspace_id))
        if index < 0:
            return
        self.workspace_combo.blockSignals(True)
        try:
            self.workspace_combo.setCurrentIndex(index)
        finally:
            self.workspace_combo.blockSignals(False)

    def create_workspace_named(self, name):
        workspace = self.services.workspaces.create(name)
        if self.workspace_context is not None:
            self.workspace_context.refresh()
            self.workspace_context.set_workspace(workspace.id)
            self.refresh_workspaces()
            return workspace
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
            people = self.services.people.list(self.workspace_id, self.search.text().strip(),
                                               include_deleted=self.include_archived.isChecked())
            self.people_list.blockSignals(True)
            self.people_list.clear()
            target = None
            for person in people:
                label = person.display_name + (" · ARCHIVED" if person.deleted_at is not None else "")
                item = QListWidgetItem(label)
                item.setData(ROLE, person.id)
                if person.deleted_at is not None:
                    item.setToolTip("该人物已归档；历史引用仍可查看。")
                self.people_list.addItem(item)
                if person.id == selected_id:
                    target = item
            target = target or (self.people_list.item(0) if people else None)
            self.people_list.setCurrentItem(target)
            self.people_list.blockSignals(False)
            scope = "（含已归档）" if self.include_archived.isChecked() else ""
            self.status.setText(f"当前 Workspace · {len(people)} 个人物{scope}")
            self.select_person(target, None)
        self.run_operation(refresh)

    @staticmethod
    def short_uuid(value) -> str:
        """Display-only short UUID (decision 8): no schema field is added."""
        text = str(value)
        return text[:8] + "…" + text[-4:] if len(text) > 14 else text

    def clear_detail_view(self):
        """Empty every widget of the Person Detail (used before a new selection)."""
        self.current_detail = None
        self.relationship_panel.set_person(None, None)
        self.full_timeline.set_person(None, None)
        self.memory_panel.set_person(None, None)
        self.alias_list.clear()
        self.image_list.clear()
        self.evidence_list.clear()
        self.analysis_list.clear()
        self.avatar.clear()
        self.avatar.setText("未设置头像")
        self.notes.clear()
        self.identity_name.setText("—")
        self.identity_uuid.setText("")
        self.status_pill.setVisible(False)
        self.evidence_status.setText("尚未选择人物。")
        self.analysis_status.setText("尚未选择人物。")
        for key, label in self.field_labels.items():
            label.setText("—")
            self._show_field(key, False)
        enabled = False
        for button in (self.edit_button, self.archive_button, self.add_alias_button,
                       self.remove_alias_button, self.avatar_button, self.gallery_button,
                       self.remove_image_button, self.open_image_button, self.promote_button,
                       self.analyze_button):
            button.setEnabled(enabled)
        self.notify_context()

    def _show_field(self, key, visible):
        """Hide an empty identity field instead of painting an empty box."""
        row = self.field_rows.get(key)
        if row is None:
            return
        self.fields_form.setRowVisible(row, bool(visible))

    def load_person_detail(self, person_id):
        """The detail snapshot, or an honest archived view when the person is archived.

        ``PersonService.detail`` reads live people only; an archived person is shown
        from the management list so identity and history stay readable.
        """
        try:
            return self.services.people.detail(self.workspace_id, person_id)
        except NotFoundError:
            for person in self.services.people.list(self.workspace_id, include_deleted=True):
                if person.id == person_id:
                    return ArchivedPersonSnapshot(person=person)
            raise

    def select_person(self, item, previous=None):
        self.clear_detail_view()
        enabled = item is not None
        for button in (self.edit_button, self.archive_button, self.add_alias_button,
                       self.remove_alias_button, self.avatar_button, self.gallery_button,
                       self.remove_image_button, self.open_image_button, self.promote_button,
                       self.analyze_button):
            button.setEnabled(enabled)
        if item is None:
            return
        def populate():
            snapshot = self.load_person_detail(item.data(ROLE))
            self.current_detail = snapshot
            person = snapshot.person
            self.identity_name.setText(person.display_name)
            self.identity_uuid.setText(f"ID · {self.short_uuid(person.id)}")
            self.identity_uuid.setToolTip(str(person.id))
            archived = person.deleted_at is not None
            self.status_pill.set_status("ARCHIVED" if archived else "ACTIVE")
            self.status_pill.setVisible(True)
            self.archive_button.setEnabled(not archived)
            for key, label in self.field_labels.items():
                value = getattr(person, key)
                text = value.isoformat() if hasattr(value, "isoformat") else (value or "")
                label.setText(text or "—")
                self._show_field(key, bool(text))
            self.notes.setPlainText(person.notes or "")
            if getattr(snapshot, "degraded", False):
                self.alias_list.addItem("已归档人物：别称引用需要读取归档详情，当前不可用。")
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
            self.relationship_panel.set_person(self.workspace_id, person.id)
            self.full_timeline.set_person(self.workspace_id, person.id)
            self.memory_panel.set_person(self.workspace_id, person.id)
            self.reload_person_evidence(person.id)
            self.reload_person_analysis(person.id)
            self.notify_context()
        self.run_operation(populate)

    # ------------------------------------------- Evidence / Analysis per person
    def reload_person_evidence(self, person_id):
        """Evidence of the Events this person took part in (read-only projection)."""
        self.evidence_list.clear()
        service = getattr(self.services, "evidence", None)
        events = getattr(self.services, "events", None)
        if service is None or events is None:
            self.evidence_status.setText("证据服务尚未装配。")
            return []
        found = []
        seen = set()
        for detail in events.list_events_by_person(self.workspace_id, person_id, include_deleted=True):
            for evidence in service.list_by_event(self.workspace_id, detail.event.id, include_deleted=True):
                if evidence.evidence.id in seen:
                    continue
                seen.add(evidence.evidence.id)
                found.append((detail.event, evidence.evidence))
        found.sort(key=lambda pair: pair[1].captured_at or pair[1].created_at, reverse=True)
        for event, evidence in found:
            entry = QListWidgetItem(f"{evidence.title} · {evidence.kind}")
            entry.setData(ROLE, (event.id, evidence.id))
            entry.setToolTip(f"事件：{event.title}")
            self.evidence_list.addItem(entry)
        self.evidence_status.setText(
            f"{len(found)} 条证据（来自该人物的 {len({event.id for event, _ in found})} 个事件）；双击打开所属事件。"
            if found else "该人物的事件还没有关联证据。")
        return found

    def reload_person_analysis(self, person_id):
        """Recorded analysis runs about this person; DERIVED, never a fact."""
        self.analysis_list.clear()
        service = getattr(self.services, "analysis", None)
        if service is None:
            self.analysis_status.setText("分析服务尚未装配。")
            return []
        runs = list(service.history(self.workspace_id, target_type="PERSON", target_id=person_id))
        for view in runs:
            run = view.run
            entry = QListWidgetItem(f"{run.status} · {run.analysis_type} · {self.short_uuid(run.id)}")
            entry.setData(ROLE, run.id)
            entry.setToolTip(f"{run.provider_name or '—'} · {run.model_name or '—'}")
            self.analysis_list.addItem(entry)
        self.analysis_status.setText(
            f"{len(runs)} 条分析记录（模型输出，不是数据库事实）；双击打开结果。" if runs
            else "尚无关于该人物的分析记录。分析只在你点击分析按钮后运行。")
        return runs

    def open_person_evidence(self):
        item = self.evidence_list.currentItem()
        if item is None:
            return None
        event_id, _evidence_id = item.data(ROLE)
        self.navigate_requested.emit("event", event_id)
        return event_id

    def open_person_analysis(self):
        item = self.analysis_list.currentItem()
        if item is None:
            return None
        run_id = item.data(ROLE)
        self.navigate_requested.emit("analysis_run", run_id)
        return run_id

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
