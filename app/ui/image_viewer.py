"""Original-file viewer: display-only zoom/pan, no rewriting image bytes."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from app.domain.errors import MediaValidationError

class ZoomView(QGraphicsView):
    def wheelEvent(self, event):
        self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()

    def zoom(self, multiplier):
        current = self.transform().m11()
        if (multiplier > 1 and current >= 20) or (multiplier < 1 and current <= 0.005):
            return
        target = max(0.005, min(20.0, current * multiplier))
        if current:
            self.scale(target / current, target / current)

class ImageViewer(QDialog):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            raise MediaValidationError("当前 Qt 无法解码这张原图。原文件未被修改。")
        self.setWindowTitle("原图 · 滚轮缩放 / 拖动平移")
        self.resize(900, 680)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        for text, callback in [("缩小", lambda: self.view.zoom(1 / 1.2)),
                               ("放大", lambda: self.view.zoom(1.2)),
                               ("原始尺寸", lambda: self.view.resetTransform()),
                               ("适应窗口", self.fit)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        row.addWidget(QLabel(f"{pixmap.width()} × {pixmap.height()}"))
        layout.addLayout(row)
        self.scene = QGraphicsScene(self)
        self.scene.addPixmap(pixmap)
        self.view = ZoomView(self.scene, self)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        layout.addWidget(self.view)
        self._initial_fit = False

    def fit(self):
        self.view.fitInView(self.scene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_fit:
            self.fit()
            self._initial_fit = True
