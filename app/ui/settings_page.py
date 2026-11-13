"""Settings · Data Management: four local data operations, and nothing else.

The page owns exactly four actions - integrity check, backup, restore
verification and workspace export - and every one of them obeys the same rules:

* **nothing runs by itself.** Opening, re-activating or switching to this page
  reads the Workspace list and nothing more. ``DataIntegrityService.check``,
  ``BackupService.backup``, ``BackupVerificationService.verify`` and
  ``WorkspaceExportService.export`` are reached from one button handler each:
  never on construction, never on ``activate()``, never on a selection change.
  There is no schedule, no retry and no upload - a local backup is the only
  destination this page knows about.
* **slow work leaves the GUI thread.** A full check hashes every media file, a
  backup copies them and a verification re-reads the whole copy, so all three
  run inside :class:`OperationWorker` (a ``QThread``). While one runs, all four
  buttons are disabled and the status line names the operation in progress.
* **one operation boundary.** Every service call is routed through
  ``app.ui.operations.run_operation`` with ``context="Settings UI"`` and
  ``title="数据管理操作未完成"``. A worker cannot open a dialog itself, so it
  hands the *exception object* back to the GUI thread, where
  :meth:`SettingsPage.handle_failure` re-enters the very same boundary - one
  error contract, and only the exception class name ever reaches the log.
* **no secret, no body text.** An API key is never read, shown or logged, and
  ``credential_ref`` is not displayed either. Memory and Evidence body text never
  appears here: a result is a status, a count, a code or a path. The integrity
  table shows exactly ``severity / code / message / entity``, which the audit
  service documents as privacy-safe; the raw ``detail`` string is only a tooltip.
"""
import logging
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget)

from app.ui.analysis_dialog import duration_text, plain_label, read_only_text, utc_text
from app.ui.operations import run_operation as _run_operation

logger = logging.getLogger(__name__)

#: The one log prefix and dialog title this page uses, so the boundary cannot drift.
CONTEXT = "Settings UI"
TITLE = "数据管理操作未完成"

#: Which action is running; the label is set before the worker starts.
INTEGRITY_BUSY = "检查中…"
BACKUP_BUSY = "备份中…"
VERIFY_BUSY = "校验中…"
EXPORT_BUSY = "导出中…"

CHECK_PASSED = "通过"
CHECK_FAILED = "失败"
ERROR_SEVERITY = "ERROR"
WARNING_SEVERITY = "WARNING"

ERROR_COLOR = QColor("#ff9d9d")
WARNING_COLOR = QColor("#ffd479")
PASS_COLOR = QColor("#9fe3a8")

INTEGRITY_HEADERS = ["严重度", "代码", "说明", "对象"]
VERIFY_HEADERS = ["检查名", "结果", "说明"]

DATABASE_ONLY_LABEL = "仅检查数据库（跳过媒体文件 hash）"
CURRENT_WORKSPACE_LABEL = "仅当前 Workspace（默认全库）"
INTEGRITY_HINT = ("完整检查会读取每个媒体文件并重新计算 sha256；媒体库很大时会比较慢，"
                  "因此在后台线程执行。审计只读：不会修改数据，孤儿文件也只报告、不删除。")
BACKUP_HINT = ("备份目标必须是数据目录之外的目录；数据目录内的位置会被服务拒绝。"
               "这里选择的是父目录，本次备份会写入其下新建的 backup-YYYYmmdd-HHMMSS 目录。")
VERIFY_HINT = ("校验会在独立临时副本上重新检查 manifest、数据库 sha256、schema、外键、媒体文件"
               "与完整性审计；文件存在本身不算通过。")
EXPORT_PRIVACY_HINT = ("「包含分析原始结果」与「包含分析上下文快照」默认不勾选：这两项可能包含"
                       "私人文本（模型原始回答与当时发送的上下文），只有显式勾选才会写入 workspace.json。")
LOCAL_ONLY_HINT = ("所有操作都在本机完成：不上传、不同步、不自动重试，也不会新建或修改任何事实数据。"
                   "界面与日志都不显示 API Key 或 credential_ref。")


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context=CONTEXT, title=TITLE)


def _reraise(error):
    """Re-raise a worker exception inside the boundary callback (GUI thread)."""
    raise error


def human_size(value) -> str:
    """A byte count in one readable unit; never a bare 10-digit number."""
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GiB"


def bytes_text(value) -> str:
    return f"{human_size(value)}（{int(value or 0):,} 字节）"


def backup_directory_name(moment=None) -> str:
    """``backup-YYYYmmdd-HHMMSS`` for the directory this run will create."""
    return "backup-" + (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")


def unique_directory(parent, name) -> Path:
    """``parent/name``, or the first free ``name-2``, ``name-3``, ... beside it."""
    candidate = Path(parent) / str(name)
    counter = 2
    while candidate.exists():
        candidate = Path(parent) / f"{name}-{counter}"
        counter += 1
    return candidate


def counts_line(counts) -> str:
    return " · ".join(f"{key}={value}" for key, value in sorted((counts or {}).items()))


def object_text(issue) -> str:
    """The entity a finding is about: an identifier and a type, never a body."""
    if not issue.entity_type and not issue.entity_id:
        return "—"
    return f"{issue.entity_type or '—'} · {issue.entity_id or '—'}"


class OperationWorker(QThread):
    """The only place a Data Management service call runs: never the GUI thread.

    A failure is emitted as the exception *object* (not a message string) so the
    GUI thread can hand it to the shared boundary, which is the single place that
    decides what a user may read and what the log may contain.
    """

    completed = Signal(object)
    failed = Signal(object)

    def __init__(self, callback, label="", parent=None):
        super().__init__(parent)
        self.callback = callback
        self.label = label

    def run(self):
        try:
            result = self.callback()
        except Exception as exc:                      # noqa: BLE001 - boundary by design
            logger.warning("Settings worker failed: type=%s", type(exc).__name__)
            self.failed.emit(exc)
            return
        self.completed.emit(result)


class SettingsPage(QWidget):
    """Data Management for one local installation: four explicit, manual actions."""

    def __init__(self, config, services=None, parent=None):
        super().__init__(parent)
        self.setObjectName("settings")
        self.config = config
        self.services = services
        self.integrity = getattr(services, "integrity", None)
        self.backups = getattr(services, "backups", None)
        self.verifier = (getattr(services, "backup_verification", None)
                         or getattr(services, "backups", None))
        self.exports = getattr(services, "exports", None)
        self.workspaces = getattr(services, "workspaces", None)
        self.workspace_id = getattr(getattr(services, "initial_workspace", None), "id", None)
        self.busy = False
        self.worker = None
        self._workers = []
        self.last_report = None
        self.last_backup = None
        self.last_verification = None
        self.last_export = None
        self.verify_time = None
        self.export_time = None
        self._build()
        self.update_buttons()

    # ------------------------------------------------------------------- build
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setObjectName("settingsScroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        title = plain_label("Data Management · 数据管理")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = plain_label(
            "本机数据的完整性检查、备份、恢复校验与可阅读导出。四个操作都只在你点击按钮后执行。")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        top = QHBoxLayout()
        top.addWidget(QLabel("当前 Workspace"))
        self.workspace_combo = QComboBox()
        self.workspace_combo.setObjectName("settingsWorkspaceSelector")
        self.workspace_combo.setMinimumWidth(200)
        self.workspace_combo.currentIndexChanged.connect(self.change_workspace)
        top.addWidget(self.workspace_combo, 1)
        note = plain_label("「仅当前 Workspace」与导出都使用这里的选项。")
        note.setObjectName("subtitle")
        top.addWidget(note)
        layout.addLayout(top)
        self.busy_label = plain_label("")
        self.busy_label.setObjectName("settingsBusy")
        layout.addWidget(self.busy_label)
        self.service_note = plain_label("")
        self.service_note.setObjectName("settingsServiceNote")
        self.service_note.setVisible(False)
        layout.addWidget(self.service_note)
        self._build_integrity(layout)
        self._build_backup(layout)
        self._build_verify(layout)
        self._build_export(layout)
        self._build_storage(layout)
        layout.addStretch()
        area.setWidget(container)
        outer.addWidget(area)
        self.update_workspace_labels()

    @staticmethod
    def _table(headers, name, *, minimum_height=130):
        table = QTableWidget(0, len(headers))
        table.setObjectName(name)
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setMinimumHeight(minimum_height)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        return table

    @staticmethod
    def _button(layout, text, name, callback):
        button = QPushButton(text)
        button.setObjectName(name)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build_integrity(self, layout):
        box = QGroupBox("Integrity Check · 完整性检查（只读审计）")
        box.setObjectName("integrityGroup")
        column = QVBoxLayout(box)
        self.database_only = QCheckBox(DATABASE_ONLY_LABEL)
        self.database_only.setObjectName("integrityDatabaseOnly")
        self.database_only.setChecked(False)
        self.database_only.setToolTip("勾选后不读取任何媒体文件；默认执行完整检查（含 sha256）。")
        column.addWidget(self.database_only)
        self.current_workspace_only = QCheckBox(CURRENT_WORKSPACE_LABEL)
        self.current_workspace_only.setObjectName("integrityCurrentWorkspaceOnly")
        self.current_workspace_only.setChecked(False)
        column.addWidget(self.current_workspace_only)
        column.addWidget(plain_label(INTEGRITY_HINT))
        row = QHBoxLayout()
        self.integrity_button = self._button(row, "Run Integrity Check", "runIntegrityCheck",
                                             self.run_integrity_check)
        row.addStretch()
        column.addLayout(row)
        self.integrity_status = plain_label("尚未运行完整性检查（点击按钮后才会执行）。")
        self.integrity_status.setObjectName("integrityStatus")
        column.addWidget(self.integrity_status)
        self.integrity_summary = read_only_text(76)
        self.integrity_summary.setObjectName("integritySummary")
        column.addWidget(self.integrity_summary)
        self.integrity_table = self._table(INTEGRITY_HEADERS, "integrityIssues")
        column.addWidget(self.integrity_table)
        layout.addWidget(box)

    def _build_backup(self, layout):
        box = QGroupBox("Create Backup · 创建备份（数据库一致快照 + 媒体副本）")
        box.setObjectName("backupGroup")
        column = QVBoxLayout(box)
        column.addWidget(plain_label(
            f"默认父目录：{self.config.storage_paths.backups}（仅作为选择起点）"))
        column.addWidget(plain_label(BACKUP_HINT))
        row = QHBoxLayout()
        self.backup_button = self._button(row, "Create Backup", "createBackup", self.create_backup)
        row.addStretch()
        column.addLayout(row)
        self.backup_status = plain_label("尚未创建备份（点击按钮后才会执行）。")
        self.backup_status.setObjectName("backupStatus")
        column.addWidget(self.backup_status)
        self.backup_details = read_only_text(104)
        self.backup_details.setObjectName("backupDetails")
        column.addWidget(self.backup_details)
        layout.addWidget(box)

    def _build_verify(self, layout):
        box = QGroupBox("Verify Backup · 校验备份（在临时副本上重新检查）")
        box.setObjectName("verifyGroup")
        column = QVBoxLayout(box)
        column.addWidget(plain_label(VERIFY_HINT))
        row = QHBoxLayout()
        self.verify_button = self._button(row, "Verify Backup", "verifyBackup", self.verify_backup)
        row.addStretch()
        column.addLayout(row)
        self.verify_status = plain_label("尚未校验备份（点击按钮后才会执行）。")
        self.verify_status.setObjectName("verifyStatus")
        column.addWidget(self.verify_status)
        self.verify_summary = read_only_text(120)
        self.verify_summary.setObjectName("verifySummary")
        column.addWidget(self.verify_summary)
        self.verify_table = self._table(VERIFY_HEADERS, "verifyChecks")
        column.addWidget(self.verify_table)
        layout.addWidget(box)

    def _build_export(self, layout):
        box = QGroupBox("Export Workspace · 导出工作区（可阅读副本，不是备份）")
        box.setObjectName("exportGroup")
        column = QVBoxLayout(box)
        self.export_workspace_label = plain_label("")
        self.export_workspace_label.setObjectName("exportWorkspaceLabel")
        column.addWidget(self.export_workspace_label)
        self.include_results = QCheckBox("包含分析原始结果")
        self.include_results.setObjectName("exportIncludeResults")
        self.include_results.setChecked(False)
        column.addWidget(self.include_results)
        self.include_snapshots = QCheckBox("包含分析上下文快照")
        self.include_snapshots.setObjectName("exportIncludeSnapshots")
        self.include_snapshots.setChecked(False)
        column.addWidget(self.include_snapshots)
        column.addWidget(plain_label(EXPORT_PRIVACY_HINT))
        column.addWidget(plain_label(
            "导出目录必须是空目录或不存在的目录；导出写入 workspace.json、media/ 与 README.txt，"
            "不能用于覆盖生产数据库。"))
        row = QHBoxLayout()
        self.export_button = self._button(row, "Export Workspace", "exportWorkspace",
                                          self.export_workspace)
        row.addStretch()
        column.addLayout(row)
        self.export_status = plain_label("尚未导出（点击按钮后才会执行）。")
        self.export_status.setObjectName("exportStatus")
        column.addWidget(self.export_status)
        self.export_details = read_only_text(120)
        self.export_details.setObjectName("exportDetails")
        column.addWidget(self.export_details)
        layout.addWidget(box)

    def _build_storage(self, layout):
        box = QGroupBox("本地存储与日志（只读）")
        box.setObjectName("storageGroup")
        column = QVBoxLayout(box)
        paths = self.config.storage_paths
        column.addWidget(plain_label(
            f"数据目录：{paths.data_dir}\n"
            f"数据库：{paths.database}\n"
            f"媒体目录：{paths.media}\n"
            f"日志目录：{self.config.log_dir}（级别 {self.config.log_level}）\n"
            f"备份默认父目录：{paths.backups}"))
        column.addWidget(plain_label(LOCAL_ONLY_HINT))
        layout.addWidget(box)

    # --------------------------------------------------------------- lifecycle
    def activate(self):
        """Re-read the Workspace list only; no check, backup or export runs here."""
        return self.load_workspaces()

    def load_workspaces(self):
        """Read ``WorkspaceService.list`` - a plain read, never a data operation."""
        if self.workspaces is None or not hasattr(self.workspaces, "list"):
            self.workspace_combo.setEnabled(False)
            return []

        def load():
            workspaces = list(self.workspaces.list())
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
            self.update_workspace_labels()
            return workspaces
        return self.run_operation(load)

    def change_workspace(self, index):
        if index < 0:
            return None
        data = self.workspace_combo.itemData(index)
        self.workspace_id = UUID(data) if data else None
        self.update_workspace_labels()
        return self.workspace_id

    def update_workspace_labels(self):
        name = self.workspace_combo.currentText() or "—"
        self.export_workspace_label.setText(
            f"导出 Workspace：{name} · {self.workspace_id or '—'}（在上方「当前 Workspace」切换）")
        # Without a Workspace there is nothing to scope a single-Workspace audit to.
        self.current_workspace_only.setEnabled(self.workspace_id is not None)
        return name

    def run_operation(self, callback):
        return run_operation(self, callback)

    # ----------------------------------------------------------- busy contract
    def update_buttons(self):
        pairs = (("完整性检查服务", self.integrity, self.integrity_button),
                 ("备份服务", self.backups, self.backup_button),
                 ("备份校验服务", self.verifier, self.verify_button),
                 ("导出服务", self.exports, self.export_button))
        missing = []
        for label, service, button in pairs:
            available = service is not None
            button.setEnabled(available and not self.busy)
            button.setToolTip("" if available else f"{label}尚未装配：该操作不可用。")
            if not available:
                missing.append(label)
        self.service_note.setText(
            ("尚未装配：" + "、".join(missing) + "。对应按钮不可用，其余操作不受影响。")
            if missing else "")
        self.service_note.setVisible(bool(missing))
        return not missing

    def set_busy(self, busy, message=""):
        """Disable every action while one runs, and say which one it is."""
        self.busy = bool(busy)
        self.busy_label.setText(str(message) if busy else "")
        self.update_buttons()
        return self.busy

    def finish_busy(self):
        """Restore the buttons once; a repeated call (completed + finished) is a no-op."""
        if not self.busy and not self.busy_label.text():
            return False
        return self.set_busy(False)

    def start_operation(self, callback, on_success, *, busy_text):
        """Run one service call on a worker thread and render its result here.

        ``callback`` performs exactly one service call (the shared boundary is
        re-entered by :meth:`handle_failure` if it raises). At most one operation
        runs at a time: a second click while ``busy`` starts nothing. Every
        handler is bound to *its own* worker, so a signal left over by the
        previous operation can never unlock the one that is running now.
        """
        if self.busy:
            return None
        self.set_busy(True, busy_text)
        worker = OperationWorker(callback, busy_text, parent=self)
        worker.completed.connect(partial(self.handle_result, worker, on_success))
        worker.failed.connect(partial(self.handle_failure, worker))
        worker.finished.connect(partial(self.finish_operation, worker))
        self.worker = worker
        self._workers.append(worker)
        worker.start()
        return worker

    def finish_operation(self, worker):
        """End the busy state for the worker that owns it; a stale signal is ignored.

        A finished worker can leave one queued signal behind after its result has
        already been rendered. If the next operation has started by then, that
        stale signal must not re-enable the buttons of the running one.
        """
        if worker is not self.worker:
            return False
        return self.finish_busy()

    def handle_result(self, worker, on_success, result):
        # Rendering goes through the same boundary as the call: a formatting bug
        # becomes one readable dialog instead of an exception escaping a Qt slot.
        self.finish_operation(worker)
        return self.run_operation(partial(on_success, result))

    def handle_failure(self, worker, error):
        """Re-enter the shared boundary on the GUI thread with the real exception."""
        self.finish_operation(worker)
        return self.run_operation(partial(_reraise, error))

    def running_workers(self):
        return [worker for worker in list(self._workers) if worker.isRunning()]

    def wait_for_worker(self, timeout_ms=30000):
        """Blocking join used by shutdown (and by tests); the GUI thread only."""
        done = True
        for worker in list(self._workers):
            if worker.isRunning():
                done = bool(worker.wait(timeout_ms)) and done
        return done

    # -------------------------------------------------------------- integrity
    def integrity_options(self):
        """``(verify_media_files, workspace_id)`` from the two checkboxes."""
        verify_media_files = not self.database_only.isChecked()
        workspace_id = self.workspace_id if self.current_workspace_only.isChecked() else None
        return verify_media_files, workspace_id

    def run_integrity_check(self):
        """The button handler: read the two options, then audit in the background."""
        if self.integrity is None:
            return None
        self.integrity_table.setRowCount(0)
        self.integrity_summary.setPlainText("")
        verify_media_files, workspace_id = self.integrity_options()
        scope = "完整检查（含媒体文件 sha256）" if verify_media_files else "仅数据库"
        where = "全库" if workspace_id is None else f"仅当前 Workspace {workspace_id}"
        return self.start_operation(
            lambda: self.integrity.check(workspace_id, verify_media_files=verify_media_files),
            self.show_integrity_report, busy_text=f"{INTEGRITY_BUSY}{scope} · {where}")

    def integrity_scope_text(self, report):
        counts = report.counts or {}
        scope = "仅当前 Workspace" if counts.get("workspace_filtered") else "全库"
        checked = int(counts.get("media_files_checked") or 0)
        skipped = int(counts.get("media_files_skipped") or 0)
        files = f"媒体文件已校验 {checked} 个" + (f"、跳过 {skipped} 个" if skipped else "")
        return f"{scope} · {files} · 孤儿文件 {int(counts.get('orphan_files') or 0)} 个"

    def show_integrity_report(self, report):
        """Render one audit result: the service's summary, then every finding."""
        self.last_report = report
        if report is None:
            self.integrity_status.setText("完整性检查没有返回结果。")
            return None
        errors, warnings = len(report.errors), len(report.warnings)
        state = "OK" if report.ok else "发现问题"
        self.integrity_summary.setPlainText("\n".join([
            report.summary(),
            f"检查时间：{utc_text(report.checked_at)}",
            f"范围：{self.integrity_scope_text(report)}",
            f"结果：{errors} 项 error · {warnings} 项 warning（只读审计，未修改任何数据）"]))
        self.fill_issues(report)
        self.integrity_status.setText(
            f"{state} · {utc_text(report.checked_at)} · {errors} error / {warnings} warning"
            + ("（errors 必须处理，warnings 仅供参考）" if errors else ""))
        return report

    def fill_issues(self, report):
        """errors first, then warnings; each row carries its severity colour."""
        entries = [(issue, ERROR_COLOR) for issue in report.errors]
        entries += [(issue, WARNING_COLOR) for issue in report.warnings]
        self.integrity_table.setRowCount(0)
        for issue, color in entries:
            row = self.integrity_table.rowCount()
            self.integrity_table.insertRow(row)
            for column, value in enumerate([issue.severity, issue.code, issue.message,
                                            object_text(issue)]):
                item = QTableWidgetItem(str(value))
                item.setForeground(QBrush(color))
                if issue.detail:
                    item.setToolTip(str(issue.detail))
                self.integrity_table.setItem(row, column, item)
        return self.integrity_table.rowCount()

    def issue_rows(self, code=None, severity=None):
        """The rendered findings, optionally filtered by code and/or severity."""
        rows = []
        for row in range(self.integrity_table.rowCount()):
            values = [self.integrity_table.item(row, column).text() for column in range(4)]
            if (code is None or values[1] == code) and (severity is None or values[0] == severity):
                rows.append(values)
        return rows

    # ------------------------------------------------------------------ backup
    def choose_directory(self, title, start=None):
        """The one directory chooser; an empty answer means the user cancelled."""
        chosen = QFileDialog.getExistingDirectory(self, title, str(start) if start else "")
        return Path(chosen) if chosen else None

    def default_backup_start(self):
        backups = self.config.storage_paths.backups
        return backups if backups.is_dir() else backups.parent

    def create_backup(self):
        """The button handler: pick a parent directory, then back up in the background."""
        if self.backups is None:
            return None
        parent = self.choose_directory("选择备份父目录（将在其中新建 backup-… 目录）",
                                       self.default_backup_start())
        if parent is None:
            self.backup_status.setText("已取消：未选择备份父目录，没有创建任何内容。")
            return None
        destination = unique_directory(parent, backup_directory_name())
        self.backup_details.setPlainText("")
        return self.start_operation(
            lambda: self.backups.backup(destination), self.show_backup_result,
            busy_text=f"{BACKUP_BUSY}正在写入 {destination}")

    def show_backup_result(self, result):
        self.last_backup = result
        if result is None:
            self.backup_status.setText("备份没有返回结果。")
            return None
        if not result.ok:
            self.backup_status.setText(f"备份失败 · 未生成备份 · {result.directory}")
            self.backup_details.setPlainText("\n".join([
                f"目标目录：{result.directory}",
                f"错误：{result.error}",
                "没有生成半成品备份：未通过检查的临时目录已被删除，已有备份未被覆盖。"]))
            return result
        manifest = result.manifest
        self.backup_status.setText(f"备份完成 · {result.directory}")
        self.backup_details.setPlainText("\n".join([
            f"备份目录：{result.directory}",
            f"备份时间：{utc_text(manifest.created_at)}",
            f"数据库：{result.database_path} · sha256 {manifest.database_sha256[:12]}…",
            f"媒体文件：{manifest.media_file_count} 个 · {bytes_text(manifest.media_total_bytes)}",
            f"schema 版本：{manifest.schema_revision} · 应用版本 {manifest.application_version}",
            f"耗时：{duration_text(result.duration_ms)}",
            f"manifest：{result.manifest_path}"]))
        return result

    # ------------------------------------------------------------------ verify
    def verify_backup(self):
        """The button handler: pick an existing backup, then verify it in the background."""
        if self.verifier is None:
            return None
        directory = self.choose_directory("选择要校验的备份目录（应包含 manifest.json）",
                                          self.default_backup_start())
        if directory is None:
            self.verify_status.setText("已取消：未选择备份目录，没有校验任何内容。")
            return None
        self.verify_table.setRowCount(0)
        self.verify_summary.setPlainText("")
        return self.start_operation(
            lambda: self.verifier.verify(directory), self.show_verification,
            busy_text=f"{VERIFY_BUSY}在临时副本上重新检查 {directory}")

    def show_verification(self, verification):
        """A backup counts as good only when every named check passed."""
        self.last_verification = verification
        if verification is None:
            self.verify_status.setText("备份校验没有返回结果。")
            return None
        self.verify_time = datetime.now(timezone.utc)
        checks = verification.checks
        passed = sum(1 for check in checks if check.ok)
        total = len(checks)
        if verification.ok:
            self.verify_status.setText(f"备份验证通过 · {passed}/{total} 项检查通过 · "
                                       f"{verification.directory}")
        else:
            failure = verification.failed[0].message if verification.failed else verification.error
            self.verify_status.setText(
                f"备份验证失败 · {passed}/{total} 项检查通过 · {failure}")
        self.verify_summary.setPlainText("\n".join([
            verification.summary(),
            f"校验时间：{utc_text(self.verify_time)}"
            + (f" · schema 版本：{verification.schema_revision}"
               if verification.schema_revision else "")]))
        self.fill_checks(verification)
        return verification

    def fill_checks(self, verification):
        self.verify_table.setRowCount(0)
        for check in verification.checks:
            row = self.verify_table.rowCount()
            self.verify_table.insertRow(row)
            color = PASS_COLOR if check.ok else ERROR_COLOR
            name = QTableWidgetItem(str(check.name))
            self.verify_table.setItem(row, 0, name)
            for column, value in enumerate([CHECK_PASSED if check.ok else CHECK_FAILED,
                                            check.message], start=1):
                item = QTableWidgetItem(str(value))
                item.setForeground(QBrush(color))
                self.verify_table.setItem(row, column, item)
        return self.verify_table.rowCount()

    def check_rows(self):
        return [[self.verify_table.item(row, column).text() for column in range(3)]
                for row in range(self.verify_table.rowCount())]

    # ------------------------------------------------------------------ export
    def export_workspace(self):
        """The button handler: pick a directory, then export in the background."""
        if self.exports is None or self.workspace_id is None:
            return None
        directory = self.choose_directory("选择导出目录（必须是空目录）",
                                          self.config.storage_paths.data_dir)
        if directory is None:
            self.export_status.setText("已取消：未选择导出目录，没有写入任何内容。")
            return None
        include_results = self.include_results.isChecked()
        include_snapshots = self.include_snapshots.isChecked()
        self.export_details.setPlainText("")
        return self.start_operation(
            lambda: self.exports.export(self.workspace_id, directory,
                                        include_analysis_results=include_results,
                                        include_analysis_snapshots=include_snapshots),
            self.show_export_result,
            busy_text=(f"{EXPORT_BUSY}正在写入 {directory}"
                       + ("（含私人文本：分析原始结果/上下文快照）"
                          if include_results or include_snapshots else "")))

    def show_export_result(self, result):
        self.last_export = result
        if result is None:
            self.export_status.setText("导出没有返回结果。")
            return None
        self.export_time = datetime.now(timezone.utc)
        if not result.ok:
            self.export_status.setText(f"导出失败 · 未写入导出内容 · {result.directory}")
            self.export_details.setPlainText("\n".join([
                f"目标目录：{result.directory}",
                f"错误：{result.error}"]))
            return result
        self.export_status.setText(f"导出完成 · {result.json_path}")
        self.export_details.setPlainText("\n".join([
            f"JSON：{result.json_path}",
            f"导出时间：{utc_text(self.export_time)}",
            f"媒体目录：{result.media_dir}",
            f"媒体文件：{result.media_file_count} 个 · {bytes_text(result.media_total_bytes)}",
            f"counts：{counts_line(result.counts)}",
            result.summary()]))
        return result
