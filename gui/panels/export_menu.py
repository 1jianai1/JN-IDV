"""右侧数据显示区右上角的统一「三点菜单」。

三个页面共用同一个菜单实例（通过 QTabWidget.setCornerWidget），
位置和行为保持一致；导出、调试会话、下位机示例代码、离线加载都在这里。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QToolButton, QWidget


def _make_more_icon() -> QIcon:
    """三点菜单图标（用 QPainter 绘制，避免字体缺少 ⋮ 字形）。"""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#c3ccda"))
    for y in (10.0, 20.0, 30.0):
        painter.drawEllipse(QPointF(20, y), 2.6, 2.6)
    painter.end()
    return QIcon(pixmap)


class ExportMenu(QToolButton):
    save_png = Signal()
    save_jpg = Signal()
    export_raw = Signal()
    export_hex = Signal()
    export_metadata = Signal()
    save_session = Signal()
    copy_device_code = Signal()
    save_device_c = Signal()
    save_device_h = Signal()
    load_raw = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("more")
        self.setText("")
        self.setIcon(_make_more_icon())
        self.setIconSize(QSize(20, 20))
        self.setToolTip("更多操作：导出 / 调试会话 / 下位机示例代码 / 加载原始数据")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFixedSize(34, 26)

        self.menu = QMenu(self)
        self._frame_actions: list[QAction] = []
        self._code_actions: list[QAction] = []
        self._has_frame = False

        self._add_section(self.menu, "导出 / 保存")
        self.act_png = self._add_action(self.menu, "保存图像为 PNG…", self.save_png.emit, "当前解析后的图像")
        self.act_jpg = self._add_action(self.menu, "保存图像为 JPG…", self.save_jpg.emit, "当前解析后的图像")
        self.act_raw = self._add_action(
            self.menu, "导出原始 Buffer…", self.export_raw.emit,
            "下位机实际发送的完整原始 Buffer（不含 64 字节协议头）",
        )
        self.act_hex = self._add_action(
            self.menu, "导出结构化 Hex…", self.export_hex.emit,
            "按当前接收协议的行 / 像素组结构组织的十六进制文本",
        )
        self.menu.addSeparator()
        self.act_metadata = self._add_action(
            self.menu, "导出帧元数据…", self.export_metadata.emit, "JSON：参数、协议头、布局、耗时"
        )
        self.act_session = self._add_action(
            self.menu, "保存调试会话…", self.save_session.emit,
            "一次保存 raw + PNG + Hex + 元数据 + 日志 + 示例代码",
        )
        self.menu.addSeparator()

        self._add_section(self.menu, "下位机上传示例代码")
        self.act_copy_code = self._add_action(
            self.menu, "复制 C 源码到剪贴板", self.copy_device_code.emit,
            "按当前图像/串口参数生成的参考实现，可直接粘贴到工程", category="code",
        )
        self.act_save_c = self._add_action(
            self.menu, "导出 C 源码文件 (.c)…", self.save_device_c.emit,
            "生成 camera_upload.c", category="code",
        )
        self.act_save_h = self._add_action(
            self.menu, "导出 C 头文件 (.h)…", self.save_device_h.emit,
            "生成 camera_upload.h", category="code",
        )
        self.menu.addSeparator()

        self._add_section(self.menu, "离线数据")
        self.act_load = self._add_action(
            self.menu, "加载原始 Buffer (.bin)…", self.load_raw.emit,
            "离线分析已经保存的下位机 payload", category="load",
        )

        self.setMenu(self.menu)
        self.set_has_frame(False)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _add_section(menu: QMenu, text: str) -> None:
        action = menu.addAction(text)
        action.setEnabled(False)

    def _add_action(
        self, menu: QMenu, text: str, slot, tooltip: str = "", category: str = "frame"
    ) -> QAction:
        action = QAction(text, menu)
        if tooltip:
            action.setToolTip(tooltip)
        action.triggered.connect(slot)
        menu.addAction(action)
        if category == "frame":
            self._frame_actions.append(action)
        elif category == "code":
            self._code_actions.append(action)
        return action

    def set_has_frame(self, has_frame: bool) -> None:
        self._has_frame = has_frame
        for action in self._frame_actions:
            action.setEnabled(has_frame)

    def set_busy(self, busy: bool) -> None:
        self.act_load.setEnabled(not busy)
        for action in self._frame_actions:
            action.setEnabled(not busy and self._has_frame)
