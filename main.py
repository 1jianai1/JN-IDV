#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JN-IDV (Camera Image Debug Viewer) — GUI 入口。

    python main.py

依赖：PySide6、pyserial、numpy（见 requirements.txt）。
CLI 入口见 cli.py，两者共用 core/ 下的全部业务能力。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import APP_NAME, __version__  # noqa: E402
from core.log import get_logger  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.theme import apply_theme  # noqa: E402


def make_icon() -> QIcon:
    """用 QPainter 画一个简单的相机镜头图标，不依赖外部资源文件。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1b1f26"))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    pen = QPen(QColor("#4fc3f7"), 3)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPointF(32, 32), 18, 18)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4fc3f7"))
    painter.drawEllipse(QPointF(32, 32), 7, 7)
    painter.setBrush(QColor("#ffb74d"))
    painter.drawRoundedRect(QRectF(43, 10, 11, 5), 2, 2)
    painter.end()
    return QIcon(pixmap)


def _install_excepthook(logger) -> None:
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "未捕获异常：\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        app = QApplication.instance()
        if app is not None:
            QMessageBox.critical(
                None,
                "程序异常",
                f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入日志文件。",
            )

    sys.excepthook = hook


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("CameraTools")
    app.setApplicationVersion(__version__)
    apply_theme(app)
    app.setWindowIcon(make_icon())

    logger = get_logger("main")
    _install_excepthook(logger)

    window = MainWindow()
    window.setWindowIcon(make_icon())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
