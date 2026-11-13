"""Log 页面：显示真实运行日志，支持级别过滤、暂停自动滚动、复制、导出。"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.log_bridge import LogBridge
from gui.theme import COLORS, mono_font

_LEVELS = [
    ("全部", logging.DEBUG),
    ("INFO+", logging.INFO),
    ("WARNING+", logging.WARNING),
    ("ERROR", logging.ERROR),
]

_LEVEL_COLORS = {
    logging.DEBUG: COLORS["dim"],
    logging.INFO: COLORS["text"],
    logging.WARNING: COLORS["warn"],
    logging.ERROR: COLORS["error"],
    logging.CRITICAL: COLORS["error"],
}


class LogViewer(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._min_level = logging.DEBUG
        self._auto_scroll = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(QLabel("级别"))
        self.level_combo = QComboBox()
        for text, level in _LEVELS:
            self.level_combo.addItem(text, level)
        self.level_combo.setCurrentIndex(0)
        self.level_combo.setFixedWidth(92)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)
        toolbar.addWidget(self.level_combo)

        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        self.auto_scroll.toggled.connect(self._on_auto_scroll)
        toolbar.addWidget(self.auto_scroll)

        toolbar.addStretch(1)

        self.count_label = QLabel("0 行")
        self.count_label.setObjectName("muted")
        toolbar.addWidget(self.count_label)

        self.clear_btn = QPushButton("清空视图")
        self.clear_btn.setToolTip("只清空界面显示，日志文件与底层 logging 记录不受影响")
        self.clear_btn.clicked.connect(self.clear_view)
        toolbar.addWidget(self.clear_btn)

        self.copy_btn = QPushButton("复制")
        self.copy_btn.clicked.connect(self.copy_all)
        toolbar.addWidget(self.copy_btn)

        self.export_btn = QPushButton("导出…")
        self.export_btn.clicked.connect(self.export_log)
        toolbar.addWidget(self.export_btn)

        layout.addLayout(toolbar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMaximumBlockCount(200_000)
        self.view.setFont(mono_font(9))
        self.view.setPlaceholderText("运行日志会实时显示在这里（与文件日志、CLI 日志同源）。")
        layout.addWidget(self.view, 1)

        self._line_count = 0

    # ------------------------------------------------------------------ #
    def attach(self, bridge: LogBridge) -> None:
        bridge.record.connect(self.append_record, Qt.ConnectionType.QueuedConnection)

    def append_record(self, payload) -> None:
        levelno, text = payload
        if levelno < self._min_level:
            return
        color = _LEVEL_COLORS.get(levelno, COLORS["text"])
        safe = html.escape(text).replace("  ", "&nbsp;&nbsp;")
        self.view.appendHtml(f'<span style="color:{color}">{safe}</span>')
        self._line_count += 1
        self.count_label.setText(f"{self._line_count} 行")
        if self._auto_scroll:
            scrollbar = self.view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _on_level_changed(self, index: int) -> None:
        self._min_level = int(self.level_combo.itemData(index))

    def _on_auto_scroll(self, enabled: bool) -> None:
        self._auto_scroll = enabled
        if enabled:
            scrollbar = self.view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    # ------------------------------------------------------------------ #
    def clear_view(self) -> None:
        self.view.clear()
        self._line_count = 0
        self.count_label.setText("0 行")

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.view.toPlainText())

    def log_text(self) -> str:
        return self.view.toPlainText()

    def export_log(self) -> None:
        text = self.view.toPlainText()
        if not text:
            QMessageBox.information(self, "导出日志", "当前没有可导出的日志内容。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", "camera_viewer_log.txt", "Text (*.txt);;Log (*.log);;All files (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"{path}\n\n{exc}")
            return
        QMessageBox.information(self, "导出日志", f"已导出到：\n{path}")
