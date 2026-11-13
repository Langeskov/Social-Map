"""Basic person fields only; saving is delegated to the page/service boundary."""
from datetime import date
from pathlib import Path
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTextEdit, QVBoxLayout)

IMAGE_FILTER = "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tif *.tiff);;所有文件 (*)"
FIELD_LABELS = {
    "display_name": "显示姓名", "real_name": "真实姓名", "gender": "性别",
    "birthday": "生日 (YYYY-MM-DD)", "occupation": "职业", "organization": "组织",
    "location": "地点", "notes": "备注",
}


class PersonDialog(QDialog):
    def __init__(self, person=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑人物" if person else "新建人物")
        self.resize(460, 520)
        self.avatar_path = None
        self.fields = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for key, label in FIELD_LABELS.items():
            widget = QTextEdit() if key == "notes" else QLineEdit()
            widget.setObjectName(key)
            value = getattr(person, key, None)
            text = value.isoformat() if isinstance(value, date) else str(value or "")
            if key == "notes":
                widget.setPlainText(text)
            else:
                widget.setText(text)
            self.fields[key] = widget
            form.addRow(label, widget)
        layout.addLayout(form)
        if person is None:
            row = QHBoxLayout()
            choose = QPushButton("选择头像（可选）")
            choose.clicked.connect(self.choose_avatar)
            self.avatar_label = QLabel("未选择")
            row.addWidget(choose)
            row.addWidget(self.avatar_label, 1)
            layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_avatar(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择头像", "", IMAGE_FILTER)
        if filename:
            self.avatar_path = Path(filename)
            self.avatar_label.setText(self.avatar_path.name)

    def values(self):
        result = {}
        for key, widget in self.fields.items():
            text = widget.toPlainText() if key == "notes" else widget.text()
            text = text.strip()
            if key == "birthday":
                try:
                    result[key] = date.fromisoformat(text) if text else None
                except ValueError as exc:
                    raise ValueError("生日请输入有效 ISO 日期，例如 1990-01-31；未知请留空。") from exc
            else:
                result[key] = text if key == "display_name" else (text or None)
        if not result["display_name"]:
            raise ValueError("显示姓名不能为空。")
        return result

    def accept(self):
        try:
            self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "请检查人物信息", str(exc))
            return
        super().accept()


class AliasDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加别称")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.alias = QLineEdit()
        self.kind = QComboBox()
        self.kind.setEditable(True)
        self.kind.addItems(["NICKNAME", "REAL_NAME", "USERNAME", "ONLINE_ID", "OTHER"])
        self.note = QTextEdit()
        form.addRow("别称", self.alias)
        form.addRow("类型（可自定义）", self.kind)
        form.addRow("备注", self.note)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return dict(alias=self.alias.text().strip(), alias_type=self.kind.currentText().strip(),
                    note=self.note.toPlainText().strip() or None)

    def accept(self):
        values = self.values()
        if not values["alias"] or not values["alias_type"]:
            QMessageBox.warning(self, "请检查别称", "别称和类型不能为空。")
            return
        super().accept()

