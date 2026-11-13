"""Analysis UI: the explicit, user-triggered path from a fact to a model answer.

Four dialogs live here, and every one of them obeys the same three rules:

* **no automatic call.** Nothing in this module runs at import time, on page
  construction or on selection change: a model request exists only after the user
  presses a button in a dialog.
* **no network on the GUI thread.** ``AnalysisWorker`` is a ``QThread`` that owns
  the blocking ``AnalysisService.create_run`` call; the GUI only ever sees Qt
  signals, a progress dialog and a Cancel button that flips the very
  ``CancelToken`` passed to the service as ``cancel=``.
* **no secret anywhere.** Only ``credential_ref`` (``env:NAME`` / ``os:NAME``) is
  entered, shown, stored or logged; an API key value is never read by this module.

The service interfaces used here are frozen: ``AnalysisService.create_run`` /
``build_snapshot`` / ``get``, ``LLMGateway`` config and ``test_provider``, and
``app.services.prompt.split_result_sections``.
"""
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app.domain.errors import DomainError
from app.domain.validation import normalize_datetime
from app.services.context import (ALL_TIME, CUSTOM, LAST_7_DAYS, LAST_30_DAYS, LAST_90_DAYS,
                                 AnalysisRequest)
from app.services.errors import DatabaseError
from app.services.llm import CancelToken, LLMConfigError, LLMError, LLMProviderConfig
from app.services.prompt import split_result_sections

logger = logging.getLogger(__name__)
ROLE = Qt.ItemDataRole.UserRole

#: Ordered so the dialog and the tests agree on one source of truth.
TIME_SCOPE_LABELS = ((LAST_7_DAYS, "最近 7 天"), (LAST_30_DAYS, "最近 30 天"),
                     (LAST_90_DAYS, "最近 90 天"), (ALL_TIME, "全部时间（不设起点）"),
                     (CUSTOM, "自定义区间（需带时区）"))
TIME_SCOPE_CHOICES = tuple(scope for scope, _ in TIME_SCOPE_LABELS)
TARGET_LABELS = {"PERSON": "人物", "RELATIONSHIP": "关系", "EVENT": "事件"}
ANALYZE_LABELS = {"PERSON": "Analyze Person · 分析人物",
                  "RELATIONSHIP": "Analyze Relationship · 分析关系",
                  "EVENT": "Analyze Event · 分析事件"}
#: ``split_result_sections`` keys in display order, with the plain-text headings.
RESULT_SECTIONS = (("facts", "Facts · 事实"), ("observations", "Observations · 观察"),
                   ("interpretations", "Interpretations · 推断"),
                   ("uncertainties", "Uncertainties · 不确定"),
                   ("other", "其他（未识别的段落）"))
RESULT_KEYS = tuple(key for key, _ in RESULT_SECTIONS)
TIME_HINT = "ISO 时间必须带时区，例如 2026-10-03T18:00:00+08:00 或 2026-10-03T10:00:00Z。"
CREDENTIAL_HINT = ("只填写凭据引用：env:NAME（环境变量）或 os:NAME（系统凭据库）。"
                   "API Key 本身不要填在这里，界面与日志都不会显示或记录它。")
SECRET_MARKERS = ("authorization", "api_key", "apikey", "api-key", "secret", "token", "password")

# ---------------------------------------------------------------- Phase 9 values
#: The four source groups of one Analysis run, in the order the detail lists them.
SOURCE_KINDS = (("PERSON", "Person · 人物"), ("RELATIONSHIP", "Relationship · 关系"),
                ("EVENT", "Event · 事件"), ("EVIDENCE", "Evidence · 证据"))
SOURCE_LABELS = dict(SOURCE_KINDS)
#: The Memory kinds; ``app.ui.memory_panel`` keeps the same order for its editor.
MEMORY_KINDS = (("FACT", "FACT · 事实"), ("PREFERENCE", "PREFERENCE · 偏好"),
                ("CONTEXT", "CONTEXT · 背景"))
MEMORY_VALIDITY_HINT = "有效期必须带时区，例如 2026-10-03T18:00:00+08:00；留空表示不设边界。"
MEMORY_PRIVACY_HINT = ("内容只保存在本地数据库与本对话框内；界面与日志都不会打印它。"
                       "确认保存会新建一条 Memory，不会修改任何事实数据。")
#: How many characters of the model output a new Memory suggests by default.
MEMORY_SUGGESTION_CHARS = 1200
MEMORY_SUGGESTION_LINES = 8


def result_section_text(run, key) -> str:
    """One of the four result sections, or "" when the run has no such section."""
    if key not in RESULT_KEYS or not (getattr(run, "result", None) or ""):
        return ""
    return split_result_sections(run.result).get(key, "")


def selected_section_text(page) -> str:
    """The section body the user is currently looking at, when there is one.

    Only a section that really owns focus counts; with focus anywhere else the
    caller falls back to the whole result.
    """
    views = getattr(page, "section_views", None) or {}
    for key, _ in RESULT_SECTIONS:
        widget = views.get(key)
        if widget is not None and hasattr(widget, "hasFocus") and widget.hasFocus():
            return result_section_text(page.current_view.run, key) if page.current_view else ""
    return ""


def suggested_memory_content(page) -> str:
    """Editable default for "Save as Memory": the focused section, else the result.

    The user always edits this text before anything is written; the suggestion is
    truncated so a long answer does not turn into an accidental dump.
    """
    if page is None or getattr(page, "current_view", None) is None:
        return ""
    text = selected_section_text(page)
    if not text:
        text = page.current_view.run.result or ""
    text = text.strip()
    if not text:
        return ""
    lines = text.splitlines()[:MEMORY_SUGGESTION_LINES]
    shortened = "\n".join(lines)
    if len(shortened) > MEMORY_SUGGESTION_CHARS:
        shortened = shortened[:MEMORY_SUGGESTION_CHARS] + "…"
    elif len(lines) < len(text.splitlines()):
        shortened += "…"
    return shortened


# --------------------------------------------------------------------- helpers

def parse_optional_datetime(text, *, field="时间") -> datetime | None:
    """Parse one ISO timestamp; a naive value is rejected, never guessed."""
    raw = (text or "").strip()
    if not raw:
        return None
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(f"{field}必须包含时区，例如 2026-10-03T18:00:00+08:00。")
    return value


def validate_credential_ref(value) -> str | None:
    """``env:NAME`` / ``os:NAME`` / empty only. The secret itself is never accepted."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" not in text:
        raise LLMConfigError("credential_ref 必须形如 env:NAME 或 os:NAME；API Key 不写入配置。")
    scheme, _, name = text.partition(":")
    scheme, name = scheme.strip().lower(), name.strip()
    if scheme not in {"env", "os"} or not name:
        raise LLMConfigError("credential_ref 只支持 env:NAME（环境变量）或 os:NAME（系统凭据）。")
    return f"{scheme}:{name}"


def parse_extra_headers(text) -> dict:
    """``Key: Value`` per line; a credential-looking header name is refused."""
    headers = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise LLMConfigError(f"extra_headers 每行必须是 Key: Value 形式（无法解析：{line}）。")
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            raise LLMConfigError(f"extra_headers 的字段名不能为空（无法解析：{line}）。")
        if any(marker in key.lower() for marker in SECRET_MARKERS):
            raise LLMConfigError(f"extra_headers 不能携带凭据字段（{key}）；请使用 credential_ref。")
        if key in headers:
            raise LLMConfigError(f"extra_headers 出现重复字段：{key}。")
        headers[key] = value
    return headers


def parse_optional_float(text, *, field, minimum, maximum):
    raw = (text or "").strip()
    if not raw:
        return None
    value = float(raw)
    if not minimum <= value <= maximum:
        raise LLMConfigError(f"{field} 必须在 {minimum:g} 到 {maximum:g} 之间。")
    return value


def parse_timeout(text, *, default=30.0):
    value = parse_optional_float(text, field="timeout", minimum=0.001, maximum=600)
    return default if value is None else value


def format_extra_headers(headers) -> str:
    return "\n".join(f"{key}: {value}" for key, value in (headers or {}).items())


def utc_text(value) -> str:
    if value is None:
        return "—"
    try:
        return value.astimezone().isoformat(timespec="seconds")
    except (AttributeError, ValueError, OSError):
        return str(value)


def duration_text(milliseconds) -> str:
    if milliseconds is None:
        return "—"
    seconds = milliseconds / 1000
    return f"{seconds:.2f}s" if seconds < 60 else f"{seconds / 60:.1f}min"


def time_text(value) -> str:
    """A UTC timestamp in one canonical shape; the Memory list uses it everywhere."""
    if value is None:
        return "—"
    if hasattr(value, "tzinfo"):
        try:
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        except (AttributeError, ValueError, OSError):
            return str(value)
    return value.isoformat()


def plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def read_only_text(minimum_height=120):
    """A read-only multi-line view: model output is data, never markup."""
    widget = QPlainTextEdit()
    widget.setReadOnly(True)
    widget.setMinimumHeight(minimum_height)
    widget.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    return widget


def track_dialog(owner, dialog):
    """Dialog convention used across the UI: delete on close, hold a reference."""
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    owner._dialogs.append(dialog)
    dialog.finished.connect(
        lambda _: owner._dialogs.remove(dialog) if dialog in owner._dialogs else None)
    return dialog


from app.ui.operations import run_operation as _run_operation


def run_operation(parent, callback):
    """Delegates to the shared UI operation boundary (``app.ui.operations``)."""
    return _run_operation(parent, callback, context="Analysis UI", title="分析操作未完成")


# ------------------------------------------------------------------- provider

class ProviderDialog(QDialog):
    """Add / edit one Provider; only non-sensitive fields are ever written."""

    def __init__(self, parent=None, *, provider=None):
        super().__init__(parent)
        self.original_name = provider.provider_name if provider is not None else None
        self.setWindowTitle("编辑 Provider" if provider is not None else "新增 Provider")
        self.resize(560, 460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(self.original_name or "")
        self.name.setPlaceholderText("例如 local-llama / deepseek")
        self.base_url = QLineEdit(provider.base_url if provider is not None else "")
        self.base_url.setPlaceholderText("http://127.0.0.1:8080/v1（必须 http:// 或 https://）")
        self.model = QLineEdit(provider.model if provider is not None else "")
        self.model.setPlaceholderText("模型名称，例如 deepseek-chat")
        self.credential_ref = QLineEdit(provider.credential_ref or "" if provider is not None else "")
        self.credential_ref.setPlaceholderText("留空 = 无凭据；或 env:OPENAI_API_KEY")
        self.temperature = QLineEdit(
            "" if provider is None or provider.temperature is None else f"{provider.temperature:g}")
        self.temperature.setPlaceholderText("留空 = 使用服务端默认；范围 0–2")
        self.timeout = QLineEdit(f"{(provider.timeout if provider is not None else 30.0):g}")
        self.extra_headers = QPlainTextEdit(
            format_extra_headers(provider.extra_headers) if provider is not None else "")
        self.extra_headers.setPlaceholderText("每行一个：Key: Value（可留空）")
        self.extra_headers.setMaximumHeight(110)
        form.addRow("provider_name", self.name)
        form.addRow("base_url", self.base_url)
        form.addRow("model", self.model)
        form.addRow("credential_ref", self.credential_ref)
        form.addRow("temperature", self.temperature)
        form.addRow("timeout（秒）", self.timeout)
        form.addRow("extra_headers", self.extra_headers)
        layout.addLayout(form)
        hint = plain_label(CREDENTIAL_HINT)
        hint.setObjectName("subtitle")
        layout.addWidget(hint)
        if self.original_name:
            layout.addWidget(plain_label(
                "改名会新增一个 Provider，不会自动删除旧名称对应的配置。"))
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self) -> LLMProviderConfig:
        return LLMProviderConfig(
            provider_name=self.name.text(), base_url=self.base_url.text(), model=self.model.text(),
            credential_ref=validate_credential_ref(self.credential_ref.text()),
            temperature=parse_optional_float(self.temperature.text(), field="temperature",
                                             minimum=0.0, maximum=2.0),
            timeout=parse_timeout(self.timeout.text()),
            extra_headers=parse_extra_headers(self.extra_headers.toPlainText()))

    def accept(self):
        try:
            self.values()
        except (LLMConfigError, ValueError) as exc:
            QMessageBox.warning(self, "请检查 Provider 配置", str(exc))
            return
        super().accept()


class ProviderSettingsDialog(QDialog):
    """List, edit, delete, default and test Providers; never shows a secret."""

    HEADERS = ["Provider", "base_url", "model", "credential_ref", "temperature", "timeout", "默认"]

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        self.gateway = getattr(services, "llm", None)
        self._dialogs = []
        self.setWindowTitle("Provider 设置 · 模型连接")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(
            "本页只写入非敏感字段（base_url / model / credential_ref 等）。"
            "API Key 请在环境变量或系统凭据中提供，interface 与日志都不会显示或记录它。"))
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setObjectName("providerTable")
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.itemDoubleClicked.connect(lambda _: self.edit_provider())
        layout.addWidget(self.table, 1)
        self.status = plain_label()
        layout.addWidget(self.status)
        rows = QHBoxLayout()
        self.add_button = self.button(rows, "新增", self.add_provider)
        self.edit_button = self.button(rows, "编辑", self.edit_provider)
        self.delete_button = self.button(rows, "删除", self.delete_provider)
        self.default_button = self.button(rows, "设为默认", self.set_default)
        self.test_button = self.button(rows, "测试连接", self.test_provider)
        layout.addLayout(rows)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()
        self.update_buttons()

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def run_operation(self, callback):
        return run_operation(self, callback)

    # ------------------------------------------------------------------ listing
    def config(self):
        return None if self.gateway is None else self.gateway.config()

    def refresh(self):
        self.table.setRowCount(0)
        config = self.config()
        if config is None:
            self.status.setText("LLM 网关尚未装配：无法配置 Provider。")
            self.update_buttons()
            return None
        for entry in config.providers:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [entry.provider_name, entry.base_url, entry.model, entry.credential_ref or "（无凭据）",
                      "服务端默认" if entry.temperature is None else f"{entry.temperature:g}",
                      f"{entry.timeout:g}s",
                      "★ 默认" if entry.provider_name == config.default_provider else ""]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(ROLE, entry.provider_name)
                self.table.setItem(row, column, item)
        self.update_buttons()
        self.status.setText(f"已配置 Provider：{len(config.providers)} 个"
                            + (f" · 默认 {config.default_provider}" if config.default_provider else "")
                            + "。配置只保存在本地 llm-providers.toml。")
        return config

    def selected_name(self):
        item = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return item.data(ROLE) if item is not None else None

    def selected_provider(self):
        name = self.selected_name()
        config = self.config()
        if name is None or config is None:
            return None
        return next((entry for entry in config.providers if entry.provider_name == name), None)

    def update_buttons(self):
        available = self.config() is not None
        chosen = self.selected_name() is not None
        for button in (self.add_button, self.test_button):
            button.setEnabled(available)
        for button in (self.edit_button, self.delete_button, self.default_button):
            button.setEnabled(available and chosen)

    def select(self, name):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(ROLE) == name:
                self.table.selectRow(row)
                return row
        return -1

    # ------------------------------------------------------------------- writes
    def save_provider(self, entry):
        """One write path shared by the dialog and the tests."""
        config = self.gateway.save_provider(entry)
        self.refresh()
        self.select(entry.provider_name)
        return config

    def add_provider(self):
        if self.gateway is None:
            return None
        def add():
            dialog = ProviderDialog(self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.save_provider(dialog.values())
        return self.run_operation(add)

    def edit_provider(self):
        provider = self.selected_provider()
        if provider is None or self.gateway is None:
            return None
        def edit():
            dialog = ProviderDialog(self, provider=provider)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.save_provider(dialog.values())
        return self.run_operation(edit)

    def delete_provider(self):
        name = self.selected_name()
        if name is None or self.gateway is None:
            return None
        if QMessageBox.question(self, "删除 Provider",
                                f"删除配置 {name}？不会删除任何事实数据。") != QMessageBox.StandardButton.Yes:
            return None
        def remove():
            self.gateway.remove_provider(name)
            self.refresh()
        return self.run_operation(remove)

    def set_default(self):
        name = self.selected_name()
        if name is None or self.gateway is None:
            return None
        def choose():
            self.gateway.set_default(name)
            self.refresh()
        return self.run_operation(choose)

    def test_provider(self, name=None):
        """Connection test: only the reference and the reply are ever shown."""
        if self.gateway is None:
            return None
        target = name or self.selected_name()
        def test():
            status = self.gateway.test_provider(target)
            reference = self.gateway.credential_reference(target)
            if status.ok:
                models = f"模型 {len(status.models)} 个" if status.models else "未返回模型列表"
                self.status.setText(f"连接成功 · {target} · {status.message} · {models}"
                                    f" · 凭据引用 {reference}")
            else:
                self.status.setText(f"连接失败 · {target} · {status.message} · 凭据引用 {reference}")
            return status
        return self.run_operation(test)


# ------------------------------------------------------------------ run worker

class AnalysisWorker(QThread):
    """The only place a model call runs: a worker thread, never the GUI thread.

    The worker owns nothing but the request and the ``CancelToken``; the token is
    the *same object* the progress dialog's Cancel button flips, and it is passed
    to ``AnalysisService.create_run`` as ``cancel=``. Failed runs are reported as
    data (status + safe message), not as an exception across the thread boundary.
    """

    completed = Signal(object)
    failed = Signal(str, str)

    def __init__(self, service, request, *, provider_name=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.request = request
        self.provider_name = provider_name
        self.token = CancelToken()

    def cancel(self):
        self.token.cancel()

    def run(self):
        try:
            run = self.service.create_run(self.request, provider_name=self.provider_name,
                                          cancel=self.token)
        except Exception as exc:
            # Only the exception class is logged; the message is shown, never logged.
            logger.warning("Analysis worker failed: type=%s", type(exc).__name__)
            self.failed.emit(type(exc).__name__, _safe_message(exc))
            return
        self.completed.emit(run)


def _safe_message(exc) -> str:
    if isinstance(exc, (LLMError, DomainError, DatabaseError, OSError, ValueError)):
        return str(exc)
    return "分析未完成：发生技术错误，请查看本地错误日志。"


class AnalysisProgressDialog(QDialog):
    """Progress + Cancel while the worker runs; it never starts a second request."""

    elapsed_tick = Signal(int)

    def __init__(self, services, request, *, provider_name=None, on_result=None, on_failed=None,
                 parent=None):
        super().__init__(parent)
        self.services = services
        self.request = request
        self.on_result = on_result
        self.on_failed = on_failed
        self.final_run = None
        self.final_error = None
        self._seconds = 0
        self._closing = False
        self.setWindowTitle("正在分析 · 模型调用在你点击后才会发生")
        self.setModal(True)
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        self.status = plain_label("正在调用模型…可随时取消。")
        layout.addWidget(self.status)
        self.detail = plain_label(
            f"目标类型 {TARGET_LABELS.get(request.target_type, request.target_type)}"
            f" · 时间范围 {request.time_scope}"
            f" · 包含证据原文 {'是' if request.include_evidence else '否'}")
        layout.addWidget(self.detail)
        self.elapsed = plain_label("已等待 0 秒")
        layout.addWidget(self.elapsed)
        layout.addWidget(plain_label("取消只会停止这次分析，不会修改任何事实数据。"))
        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton("取消分析")
        self.cancel_button.setObjectName("analysisCancelRun")
        self.cancel_button.clicked.connect(self.cancel_requested)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self.worker = AnalysisWorker(services.analysis, request, provider_name=provider_name, parent=self)
        self.worker.completed.connect(self.handle_completed)
        self.worker.failed.connect(self.handle_failed)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)

    # ------------------------------------------------------------------ control
    def start(self):
        self._seconds = 0
        self.timer.start()
        self.worker.start()
        return self.worker

    def cancel_requested(self):
        """One click = one CancelToken.cancel(); the token is shared with the service."""
        if self.worker.token.cancelled:
            return self.worker.token
        self.worker.cancel()
        self.cancel_button.setEnabled(False)
        self.status.setText("已请求取消；正在等待当前请求结束。不会修改任何事实数据。")
        return self.worker.token

    def tick(self):
        self._seconds += 1
        self.elapsed.setText(f"已等待 {self._seconds} 秒")
        self.elapsed_tick.emit(self._seconds)

    def stop_timer(self):
        self.timer.stop()

    def finish(self, run):
        """Close the dialog and hand the finished run to the caller."""
        self._closing = True
        self.final_run = run
        self.stop_timer()
        self.accept()
        if self.on_result is not None:
            self.on_result(run)
        return run

    def finish_error(self, error_type, message):
        self._closing = True
        self.final_error = SimpleNamespace(error_type=error_type, message=message)
        self.stop_timer()
        self.reject()
        if self.on_failed is not None:
            self.on_failed(self.final_error)
        return self.final_error

    # ------------------------------------------------------------------- signals
    def handle_completed(self, run):
        if self._closing:
            return None
        return self.finish(run)

    def handle_failed(self, error_type, message):
        if self._closing:
            return None
        return self.finish_error(error_type, message)

    def closeEvent(self, event):
        """Closing the window means "cancel", not "abandon a running thread"."""
        if self._closing or self.worker.isFinished():
            return super().closeEvent(event)
        self.cancel_requested()
        event.ignore()
        return None

    def wait_for_worker(self, timeout_ms=30000):
        """Blocking wait used by tests and by a UI shutdown; the GUI thread only."""
        return self.worker.wait(timeout_ms)


def analysis_center(widget):
    """The Analysis page that owns every run, found without an import cycle.

    Every entry point (People / Relationship / Event) resolves the *same* page so
    a run always lands in one history list; outside the shell a private instance
    is created lazily and kept alive on the caller.
    """
    window = widget.window() if hasattr(widget, "window") else None
    page = getattr(window, "analysis_page", None)
    if page is not None:
        return page
    holder = getattr(widget, "_analysis_page", None)
    if holder is not None:
        return holder
    from app.ui.analysis_page import AnalysisPage
    page = AnalysisPage(widget.services, widget)
    page.hide()                      # it is a run holder here, not a visible page
    widget._analysis_page = page
    return page


def run_analysis(services, workspace_id, target_type, target_id, *, target_label=None,
                 on_result=None, on_failed=None, parent=None, owner=None, dialog=None):
    """Open the options dialog for one fact and, if confirmed, run one analysis.

    Returns the options dialog, or the existing one when it is already open (so a
    second click cannot start a second model call). ``None`` means the Analysis
    service is not wired at all.
    """
    if getattr(services, "analysis", None) is None:
        QMessageBox.warning(parent, "分析不可用", "分析服务尚未装配：无法发起分析。")
        return None
    if dialog is not None and dialog.isVisible():
        dialog.raise_()
        return dialog
    options = AnalysisOptionsDialog(services, workspace_id, target_type, target_id,
                                    target_label=target_label, parent=parent)
    confirmed = {"request": None, "provider": None}

    def confirm():
        confirmed["request"] = options.build_request()
        confirmed["provider"] = options.provider_name()

    options.confirmed.connect(confirm)
    options.cancelled.connect(options.on_cancel_requested)
    if owner is not None:
        owner._dialogs.append(options)
        options.finished.connect(
            lambda _: owner._dialogs.remove(options) if options in owner._dialogs else None)
    options.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    options.show()
    options.confirmed.connect(lambda: launch_analysis(
        services, confirmed["request"], provider_name=confirmed["provider"], on_result=on_result,
        on_failed=on_failed, parent=parent, dialog=options))
    return options


def _invoke_guard(callback):
    """Call a Qt object's method, tolerating a parent widget deleted meanwhile."""
    def guarded():
        try:
            return callback()
        except RuntimeError:                    # the owning C++ object is gone
            return None
    return guarded


def launch_analysis(services, request, *, provider_name=None, on_result=None, on_failed=None,
                    parent=None, dialog=None):
    """Start one worker thread. No model call happens before this is invoked.

    The progress dialog is parented to the *stable* window (the page), never to the
    options dialog: the options dialog is deleted as soon as it is accepted, and a
    running worker must outlive it.
    """
    analysis = getattr(services, "analysis", None)
    if analysis is None:
        QMessageBox.warning(parent, "分析不可用", "分析服务尚未装配：无法发起分析。")
        return None
    if parent is None and dialog is not None:
        parent = dialog.parentWidget()
    progress = AnalysisProgressDialog(services, request, provider_name=provider_name,
                                      on_result=on_result, on_failed=on_failed, parent=parent)
    if dialog is not None:
        dialog.progress = progress
        if hasattr(dialog, "_dialogs"):
            progress.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog._dialogs.append(progress)
            progress.finished.connect(
                lambda _: dialog._dialogs.remove(progress) if progress in dialog._dialogs else None)
        progress.cancel_button.clicked.connect(_invoke_guard(dialog.on_cancel_requested))
    progress.start()
    return progress


# ------------------------------------------------------------------- options UI

class AnalysisOptionsDialog(QDialog):
    """Where one analysis is configured - and where it is explicitly confirmed.

    Construction touches no model: it reads ``LLMGateway.config()`` only. The
    "上下文预览" button calls ``AnalysisService.build_snapshot`` (database reads,
    no network) and prints the exact text that would be sent.

    ``confirmed`` fires exactly once, after validation and after the user pressed
    OK, and that is the only trigger for a model call anywhere in the UI.
    """

    confirmed = Signal(object, str)
    cancelled = Signal()

    def __init__(self, services, workspace_id, target_type, target_id, *, target_label=None,
                 parent=None):
        super().__init__(parent)
        self.services = services
        self.workspace_id = workspace_id
        self.target_type = target_type
        self.target_id = target_id
        self.analysis = getattr(services, "analysis", None)
        self.analysis_type = (self.analysis.analysis_type_for(target_type)
                              if self.analysis is not None else "")
        self.target_label = target_label
        self.progress = None
        self.confirmed_request = None
        self.analysis_available = self.analysis is not None
        gateway = getattr(services, "llm", None)
        self.provider_count = 0 if gateway is None else len(gateway.config().providers)
        self.last_snapshot = None
        self.setWindowTitle(ANALYZE_LABELS.get(target_type, "分析") + " · 分析选项")
        self.resize(680, 620)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(
            f"目标：{TARGET_LABELS.get(target_type, target_type)}"
            + (f" · {target_label}" if target_label else "")
            + f"\n分析类型：{self.analysis_type}"))
        form = QFormLayout()
        self.time_scope = QComboBox()
        self.time_scope.setObjectName("analysisTimeScope")
        for scope, label in TIME_SCOPE_LABELS:
            self.time_scope.addItem(f"{scope} · {label}", scope)
        self.time_scope.setCurrentIndex(max(0, self.time_scope.findData(LAST_30_DAYS)))
        self.time_scope.currentIndexChanged.connect(self.update_custom_state)
        form.addRow("时间范围", self.time_scope)
        self.custom_start = QLineEdit()
        self.custom_start.setObjectName("analysisCustomStart")
        self.custom_start.setPlaceholderText("起点，可留空；例如 2026-01-01T00:00:00+08:00")
        self.custom_end = QLineEdit()
        self.custom_end.setObjectName("analysisCustomEnd")
        self.custom_end.setPlaceholderText("终点，可留空（默认=现在）；必须带时区")
        form.addRow("自定义起点", self.custom_start)
        form.addRow("自定义终点", self.custom_end)
        layout.addLayout(form)
        layout.addWidget(plain_label(TIME_HINT))
        box = QGroupBox("包含内容")
        box_column = QVBoxLayout(box)
        self.include_relationships = QCheckBox("包含关系（当前关系）")
        self.include_history = QCheckBox("包含关系历史（effective_at）")
        self.include_observations = QCheckBox("包含观察（OBSERVATION 记录）")
        self.include_evidence = QCheckBox("包含证据原文（元数据始终包含）")
        for widget in (self.include_relationships, self.include_history,
                       self.include_observations, self.include_evidence):
            widget.setChecked(True)
            box_column.addWidget(widget)
        self.include_evidence.setChecked(False)
        # Phase 9: Memory is user-confirmed text and stays out of the context unless
        # this box is ticked; opening the dialog never reads or writes a Memory.
        self.include_memories = QCheckBox("包含你确认过的 Memory（默认不包含）")
        self.include_memories.setObjectName("analysisIncludeMemories")
        self.include_memories.setChecked(False)
        box_column.addWidget(self.include_memories)
        box_column.addWidget(plain_label("默认不发送证据原文与 Memory：只有勾选后原文才会进入上下文。"))
        layout.addWidget(box)
        provider_form = QFormLayout()
        self.provider = QComboBox()
        self.provider.setObjectName("analysisProvider")
        names = () if gateway is None else gateway.config().names()
        for name in names:
            self.provider.addItem(name, name)
        default = None if gateway is None else gateway.config().default_provider
        index = self.provider.findData(default)
        self.provider.setCurrentIndex(max(0, index))
        provider_form.addRow("Provider", self.provider)
        layout.addLayout(provider_form)
        self.provider_note = plain_label()
        self.provider_note.setObjectName("subtitle")
        layout.addWidget(self.provider_note)
        layout.addWidget(plain_label("上下文预览（不调用模型）"))
        self.preview = read_only_text(150)
        self.preview.setObjectName("analysisContextPreview")
        layout.addWidget(self.preview, 1)
        rows = QHBoxLayout()
        self.preview_button = QPushButton("上下文预览（不调用模型）")
        self.preview_button.setObjectName("analysisPreviewButton")
        self.preview_button.clicked.connect(self.preview_context)
        rows.addWidget(self.preview_button)
        rows.addStretch()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始分析")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        rows.addWidget(self.buttons)
        layout.addLayout(rows)
        self.update_custom_state()
        self.update_provider_state()

    # -------------------------------------------------------------------- state
    def current_scope(self):
        return self.time_scope.currentData()

    def update_custom_state(self):
        custom = self.current_scope() == CUSTOM
        self.custom_start.setEnabled(custom)
        self.custom_end.setEnabled(custom)
        return custom

    def update_provider_state(self):
        if not self.analysis_available:
            self.provider_note.setText("分析服务尚未装配：无法发起分析。")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return False
        if self.provider.count() == 0:
            self.provider_note.setText(
                "尚未配置任何 LLM Provider：请先在本页或 Analysis 页的「Provider 设置」中添加。")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return False
        self.provider_note.setText(f"当前 Provider：{self.provider.currentText()}（模型调用只发生在点击「开始分析」之后）")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        return True

    def values(self):
        """Validate and build the frozen ``AnalysisRequest`` (raises DomainError)."""
        request = self.build_request()
        return {"analysis_type": request.analysis_type, "time_scope": request.time_scope,
                "as_of": request.as_of, "custom_start": request.custom_start,
                "custom_end": request.custom_end, "provider_name": self.provider_name(),
                "include_relationships": request.include_relationships,
                "include_history": request.include_history,
                "include_observations": request.include_observations,
                "include_evidence": request.include_evidence,
                "include_memories": request.include_memories}

    def build_request(self):
        scope = self.current_scope()
        start = end = None
        if scope == CUSTOM:
            start = parse_optional_datetime(self.custom_start.text(), field="自定义起点")
            end = parse_optional_datetime(self.custom_end.text(), field="自定义终点")
            if start is None and end is None:
                raise DomainError("自定义区间至少需要起点或终点；两点都为空请改用 ALL。")
            if start is not None and end is not None and start > end:
                raise DomainError("自定义区间的起点不能晚于终点。")
        return AnalysisRequest(workspace_id=self.workspace_id, target_type=self.target_type,
                               target_id=self.target_id, analysis_type=self.analysis_type,
                               as_of=normalize_datetime(datetime.now().astimezone(), "as_of"),
                               time_scope=scope, custom_start=start, custom_end=end,
                               include_relationships=self.include_relationships.isChecked(),
                               include_history=self.include_history.isChecked(),
                               include_observations=self.include_observations.isChecked(),
                               include_evidence=self.include_evidence.isChecked(),
                               # Phase 9: user-confirmed Memory is opt-in, exactly like
                               # raw evidence text; the default context never reads it.
                               include_memories=self.include_memories.isChecked())

    def provider_name(self):
        return self.provider.currentData()

    # ------------------------------------------------------------------ actions
    def preview_context(self):
        """``build_snapshot`` only: database reads, no model call, no run record."""
        def build():
            snapshot = self.analysis.build_snapshot(self.build_request())
            self.show_preview(snapshot)
            return snapshot
        return run_operation(self, build)

    def show_preview(self, snapshot):
        self.last_snapshot = snapshot
        self.preview.setPlainText(snapshot.rendered_context)
        self.preview.setToolTip(f"估算 tokens：{snapshot.estimated_tokens}")
        return snapshot

    def accept(self):
        try:
            request = self.build_request()
        except (DomainError, ValueError) as exc:
            QMessageBox.warning(self, "请检查分析选项", str(exc))
            return
        if not self.update_provider_state():
            return
        self.confirmed_request = request
        # Emitted before the dialog closes, so a listener can parent its own
        # widgets to a window that still exists.
        self.confirmed.emit(request, self.provider_name())
        super().accept()

    def reject(self):
        super().reject()
        self.cancelled.emit()

    def on_cancel_requested(self, progress=None):
        """Cancel hook: the page may cancel an in-flight run when this is closed."""
        return None


# -------------------------------------------------------------------- results UI

class AnalysisResultDialog(QDialog):
    """Read-only display of one finished run: four sections, metadata, snapshot."""

    def __init__(self, view, *, parent=None):
        super().__init__(parent)
        self.view = view
        self.run = view.run
        self._dialogs = []
        self.sections = split_result_sections(self.run.result or "")
        self.setWindowTitle("分析结果 · " + self.run.analysis_type)
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        meta = QFormLayout()
        self.fields = {}
        for key, label, value in self.metadata_values(self.run):
            self.fields[key] = plain_label(value)
            meta.addRow(label, self.fields[key])
        layout.addLayout(meta)
        self.status = plain_label(self.status_text(self.run))
        layout.addWidget(self.status)
        self.summary = {"sections": {key: len(self.sections[key]) for key in RESULT_KEYS},
                        "sources": view.sources.total()}
        self.section_views = {}
        if self.run.status == "SUCCEEDED":
            for key, title in RESULT_SECTIONS:
                box = QGroupBox(title)
                box.setObjectName(f"analysisSection_{key}")
                column = QVBoxLayout(box)
                text = self.sections[key] or "（本段没有内容）"
                if key == "other" and not self.sections["other"]:
                    box.hide()
                widget = read_only_text(110)
                widget.setObjectName(f"analysisText_{key}")
                widget.setPlainText(text)
                column.addWidget(widget)
                layout.addWidget(box)
                self.section_views[key] = widget
        else:
            layout.addWidget(plain_label(
                "本次运行未成功，因此不显示模型输出正文。以上错误信息为安全文本（不含密钥）。"))
        rows = QHBoxLayout()
        self.snapshot_button = QPushButton("查看上下文快照")
        self.snapshot_button.setObjectName("analysisSnapshotButton")
        self.snapshot_button.clicked.connect(self.show_snapshot)
        self.snapshot_button.setEnabled(view.snapshot is not None)
        rows.addWidget(self.snapshot_button)
        rows.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        rows.addWidget(buttons)
        layout.addLayout(rows)

    @staticmethod
    def metadata_values(run):
        try:
            scope = json.loads(run.scope)
        except (ValueError, TypeError):
            scope = {}
        include = scope.get("include", {})
        return [("分析类型", "分析类型", run.analysis_type),
                ("目标", "目标", f"{TARGET_LABELS.get(run.target_type, run.target_type)} · {run.target_id}"),
                ("Provider", "Provider", run.provider_name),
                ("模型", "模型", run.model_name),
                ("状态", "状态", run.status),
                ("创建时间", "创建时间（本地时区）", utc_text(run.created_at)),
                ("完成时间", "完成时间（本地时区）", utc_text(run.completed_at)),
                ("耗时", "耗时", duration_text(run.duration_ms)),
                ("时间范围", "时间范围", str(scope.get("time_scope", "—"))),
                ("包含", "包含", "、".join(f"{key}={'是' if value else '否'}"
                                          for key, value in include.items()) or "—")]

    @staticmethod
    def status_text(run):
        if run.status == "SUCCEEDED":
            return "状态 SUCCEEDED · 结果为模型输出，不是数据库事实；事实层未被修改。"
        if run.status == "CANCELLED":
            return f"状态 CANCELLED · {run.error_message or '分析已取消。'}"
        return f"状态 {run.status} · {run.error_message or '分析未完成。'}"

    def referenced_counts(self):
        sources = self.view.sources
        return {"PERSON": len(sources.people), "RELATIONSHIP": len(sources.relationships),
                "EVENT": len(sources.events), "EVIDENCE": len(sources.evidence),
                "total": sources.total()}

    def section_keys(self):
        return [key for key, _ in RESULT_SECTIONS if key in self.section_views]

    # ------------------------------------------------------------------ snapshot
    def snapshot_dialog(self, parent=None):
        view = self.view.snapshot
        if view is None:
            return None
        return ContextSnapshotDialog(view, parent=parent or self)

    def show_snapshot(self):
        dialog = self.snapshot_dialog()
        if dialog is None:
            return None
        track_dialog(self, dialog)
        dialog.show()
        return dialog


class ContextSnapshotDialog(QDialog):
    """The exact text the model was given, read-only, straight from the run."""

    def __init__(self, snapshot, *, parent=None, title=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.setWindowTitle(title or "上下文快照 · 发送给模型的原文（只读）")
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(
            f"生成时间 {utc_text(snapshot.generated_at)} · 估算 tokens {snapshot.estimated_tokens}"
            f" · 分节 {len(snapshot.sections)} 个。以下是模型当时收到的原始文本；事实改变后它不会被重写。"))
        self.text = read_only_text(560)
        self.text.setObjectName("analysisSnapshotText")
        self.text.setPlainText(snapshot.rendered_context)
        layout.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ------------------------------------------------------------- Phase 9: sources

def source_line(workspace_id, kind, target_id, resolvers) -> tuple[str, str, str]:
    """``(name, short id, reason)`` for one source row - never a fact body.

    ``resolvers`` is the page's ``target_name``-style lookup keyed by kind. Only
    referencing information is produced: a display name, a relationship type, an
    event title, an evidence title, or a short id when the fact is gone.
    """
    resolver = (resolvers or {}).get(kind)
    try:
        name = resolver(workspace_id, target_id) if resolver is not None else None
    except Exception as exc:                      # a missing fact must not break the list
        logger.debug("Analysis source lookup failed: kind=%s type=%s", kind, type(exc).__name__)
        name = None
    short = str(target_id)[:8]
    text = str(name).strip() if name else ""
    if text:
        return text, short, ""
    if kind == "PERSON":
        return "", short, "（人物不可见或已归档）"
    if kind == "RELATIONSHIP":
        return "", short, "（关系不可见）"
    if kind == "EVENT":
        return "", short, "（事件不可见）"
    if kind == "EVIDENCE":
        return "", short, "（证据不可见：可能已删除，或其事件已删除）"
    return "", short, ""


def analysis_source_resolvers(page) -> dict:
    """Name lookups for the four source groups, all read-only service calls."""
    return {kind: (lambda workspace_id, target_id, kind=kind:
                   page.target_name(kind, target_id)) for kind, _ in SOURCE_KINDS}


class AnalysisSourceRows(QTableWidget):
    """One group's table: name / kind / short id, double-click jumps to the fact.

    The rows hold only reference information. A row whose fact cannot be read is
    shown with an explicit reason instead of a guessed name.
    """

    activated = Signal(str, object)

    COLUMNS = ["来源", "短 id", "说明"]

    def __init__(self, kind, page, parent=None):
        super().__init__(0, len(self.COLUMNS), parent)
        self.kind = kind
        self.page = page
        self.setObjectName(f"analysisSourceRows_{kind}")
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setMinimumHeight(84)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.itemDoubleClicked.connect(lambda _: self.activate_current())

    def fill(self, workspace_id, targets, resolvers):
        self.setRowCount(0)
        for target_id in targets or ():
            name, short, reason = source_line(workspace_id, self.kind, target_id, resolvers)
            row = self.rowCount()
            self.insertRow(row)
            for column, value in enumerate([name or "—", short, reason or "可跳转"]):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(ROLE, target_id)
                self.setItem(row, column, item)
        return self.rowCount()

    def current_target(self):
        item = self.item(self.currentRow(), 0) if self.currentRow() >= 0 else None
        return item.data(ROLE) if item is not None else None

    def activate_current(self):
        target_id = self.current_target()
        if target_id is None:
            return None
        self.activated.emit(self.kind, target_id)
        return target_id


class MemoryConfirmDialog(QDialog):
    """The explicit confirmation step in front of ``create_from_analysis``.

    Opening this dialog writes nothing, and ``cancel`` writes nothing: the Memory
    exists only after the user presses "确认保存", which sets ``self.parsed`` and
    then emits ``confirmed`` - the single signal the Analysis page listens to.
    The suggested text is editable, the kind is chosen by the user, the person
    binding is optional (empty = Workspace level) and the validity window may stay
    empty (a naive timestamp is refused, never guessed).
    """

    confirmed = Signal(object)

    def __init__(self, services, workspace_id, run, suggested_text="", *, parent=None):
        super().__init__(parent)
        self.services = services
        self.workspace_id = workspace_id
        self.run = run
        self.parsed = None
        self.created_memory = None
        self.setWindowTitle("保存为 Memory · 需要你确认")
        self.resize(720, 620)
        layout = QVBoxLayout(self)
        summary = QFormLayout()
        self.fields = {}
        for key, label, value in self.source_values(run):
            self.fields[key] = plain_label(value)
            summary.addRow(label, self.fields[key])
        layout.addLayout(summary)
        layout.addWidget(plain_label(
            "下面显示的是模型输出，不是数据库事实。保存后会新建一条 Memory；"
            "人物、关系、事件与证据都不会被修改。"))
        form = QFormLayout()
        self.content = QPlainTextEdit()
        self.content.setObjectName("memoryConfirmContent")
        self.content.setMinimumHeight(190)
        self.content.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.content.setPlainText((suggested_text or "").strip())
        form.addRow("内容（可编辑）", self.content)
        self.kind = QComboBox()
        self.kind.setObjectName("memoryConfirmKind")
        for value, label in MEMORY_KINDS:
            self.kind.addItem(label, value)
        form.addRow("类型", self.kind)
        self.person = QComboBox()
        self.person.setObjectName("memoryConfirmPerson")
        self.person.addItem("（空 = Workspace 级记忆）", None)
        for person in self.people():
            self.person.addItem(f"{person.display_name} · {str(person.id)[:8]}", person.id)
        form.addRow("绑定人物（可选）", self.person)
        self.valid_from = QLineEdit()
        self.valid_from.setObjectName("memoryConfirmValidFrom")
        self.valid_from.setPlaceholderText("可留空；例如 2026-10-03T18:00:00+08:00")
        self.valid_until = QLineEdit()
        self.valid_until.setObjectName("memoryConfirmValidUntil")
        self.valid_until.setPlaceholderText("可留空；必须带时区且不早于起点")
        form.addRow("valid_from", self.valid_from)
        form.addRow("valid_until", self.valid_until)
        layout.addLayout(form)
        layout.addWidget(plain_label(MEMORY_VALIDITY_HINT))
        privacy = plain_label(MEMORY_PRIVACY_HINT)
        privacy.setObjectName("subtitle")
        layout.addWidget(privacy)
        self.buttons = QDialogButtonBox()
        self.save_button = self.buttons.addButton("确认保存", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setObjectName("memoryConfirmSave")
        self.cancel_button = self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setObjectName("memoryConfirmCancel")
        self.save_button.clicked.connect(self.confirm)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.buttons)

    @staticmethod
    def source_values(run):
        return [("analysis_type", "分析类型", run.analysis_type),
                ("analysis_run_id", "Source Analysis", str(run.id)),
                ("created_at", "分析时间（本地时区）", utc_text(run.created_at)),
                ("provider", "Provider", run.provider_name),
                ("model", "模型", run.model_name),
                ("status", "状态", run.status)]

    def people(self):
        """The current Workspace's people (read-only); never a fact write."""
        lister = getattr(self.services, "people", None)
        if lister is None or self.workspace_id is None:
            return []
        return list(lister.list(self.workspace_id))

    def select_person(self, person_id):
        """Choose one bindable person (or None for the Workspace scope).

        The combo is searched by comparing the stored value itself: ``findData``
        compares QVariants, which is not reliable for a UUID that did not come
        through ``addItem`` as the very same object.
        """
        for index in range(self.person.count()):
            if self.person.itemData(index) == person_id:
                self.person.setCurrentIndex(index)
                return index
        self.person.setCurrentIndex(0)
        return -1

    def select_kind(self, kind):
        for index in range(self.kind.count()):
            if self.kind.itemData(index) == kind:
                self.kind.setCurrentIndex(index)
                return index
        return -1

    # -------------------------------------------------------------------- values
    def selected_person(self):
        return self.person.currentData()

    def current_kind(self):
        return self.kind.currentData()

    def boundaries(self):
        start = parse_optional_datetime(self.valid_from.text(), field="valid_from")
        end = parse_optional_datetime(self.valid_until.text(), field="valid_until")
        if start is not None and end is not None and end < start:
            raise DomainError("valid_until 不能早于 valid_from。")
        return start, end

    def values(self) -> dict:
        content = self.content.toPlainText().strip()
        if not content:
            raise DomainError("Memory 内容不能为空。")
        start, end = self.boundaries()
        return {"content": content, "kind": self.current_kind(),
                "person_id": self.selected_person(), "valid_from": start, "valid_until": end}

    def confirm(self):
        """The one and only confirmation: validate, then ask the page to write."""
        try:
            self.parsed = self.values()
        except (DomainError, ValueError) as exc:
            failure = exc if isinstance(exc, DomainError) else DomainError(str(exc))
            QMessageBox.warning(self, "请检查 Memory 内容", str(failure))
            return None
        self.confirmed.emit(self.parsed)
        super().accept()
        return self.parsed


def show_run_result(services, workspace_id, run_id, *, parent=None, dialog=None):
    """Open the read-only result dialog for one run id."""
    analysis = getattr(services, "analysis", None)
    if analysis is None:
        return None
    def load():
        view = analysis.get(workspace_id, run_id)
        result = AnalysisResultDialog(view, parent=parent)
        if dialog is not None:
            dialog._dialogs.append(result)
            result.finished.connect(
                lambda _: dialog._dialogs.remove(result) if result in dialog._dialogs else None)
        result.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        result.show()
        return result
    return run_operation(parent, load)


__all__ = ["ANALYZE_LABELS", "AnalysisOptionsDialog", "AnalysisProgressDialog", "AnalysisResultDialog",
           "AnalysisSourceRows", "AnalysisWorker", "CREDENTIAL_HINT", "ContextSnapshotDialog",
           "MEMORY_KINDS", "MEMORY_PRIVACY_HINT", "MEMORY_SUGGESTION_CHARS", "MEMORY_SUGGESTION_LINES",
           "MEMORY_VALIDITY_HINT", "MemoryConfirmDialog", "ProviderDialog",
           "ProviderSettingsDialog", "RESULT_KEYS", "RESULT_SECTIONS", "SOURCE_KINDS", "SOURCE_LABELS",
           "TARGET_LABELS", "TIME_SCOPE_CHOICES", "TIME_SCOPE_LABELS", "analysis_center",
           "analysis_source_resolvers", "duration_text", "format_extra_headers", "launch_analysis",
           "parse_extra_headers", "parse_optional_datetime", "parse_optional_float", "parse_timeout",
           "plain_label", "read_only_text", "result_section_text", "run_analysis", "run_operation",
           "selected_section_text", "show_run_result", "source_line", "suggested_memory_content",
           "time_text", "track_dialog", "utc_text", "validate_credential_ref"]
