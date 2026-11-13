"""结构化 Hex Viewer：按 BT.656 行 / 像素组展示原始 Buffer，并与图像双向联动。

实现要点：
  * 结构化行由 ``core.hex.formatter`` 生成，这里只负责虚拟绘制（只画可见行）。
  * 字节格按 YUV 分量着色（U 青 / Y 白 / V 粉），并标出 YUV422 的共享色度关系。
  * 鼠标悬停 -> 发出 ViewFocus；图像悬停 -> set_external_focus() 定位并滚动。
  * 支持 Offset 跳转、十六进制搜索、复制、纯 Hex / 结构视图切换。
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.formats.pixel_formats import get_pixel_format
from core.hex.formatter import HexRow, StructuredData, format_plain
from core.image.frame import Frame
from gui.focus import ViewFocus
from gui.theme import COLORS, component_color, mono_font

_ROW_HEIGHTS = {
    "info": 32,
    "line": 28,
    "sync": 28,
    "pixels": 40,
    "blank": 24,
    "raw": 24,
    "hash": 24,
    "warn": 28,
}


@dataclass
class HexMetrics:
    left: float = 10.0
    offset_w: float = 94.0
    label_w: float = 112.0
    cell_w: float = 24.0
    cell_gap: float = 2.0
    group_gap: float = 8.0
    group_bytes: int = 4
    groups_per_row: int = 8

    @property
    def row_x(self) -> float:
        return self.left + self.offset_w + self.label_w

    @property
    def group_width(self) -> float:
        return (
            self.group_bytes * self.cell_w
            + (self.group_bytes - 1) * self.cell_gap
            + self.group_gap
        )

    def cell_x(self, group_index: int, cell_index: int) -> float:
        return (
            self.row_x
            + group_index * self.group_width
            + cell_index * (self.cell_w + self.cell_gap)
        )

    def byte_capacity(self, width: float) -> int:
        return max(1, int((width - self.row_x) // (self.cell_w + self.cell_gap)))


class _HexHeader(QWidget):
    def __init__(self, viewer: "HexViewer") -> None:
        super().__init__()
        self.viewer = viewer
        self.setFixedHeight(32)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["panel"]))
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        metrics = self.viewer.metrics
        painter.setFont(mono_font(8, bold=True))
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(
            QRectF(metrics.left, 0, metrics.offset_w, self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "OFFSET",
        )
        painter.drawText(
            QRectF(metrics.left + metrics.offset_w, 0, metrics.label_w, self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "LINE / Y",
        )
        data = self.viewer.structured
        if data is None:
            return
        fmt = get_pixel_format(data.pixel_format)
        lanes = fmt.lane_components
        painter.setFont(mono_font(8, bold=False))
        for cell_index, component in enumerate(lanes[: metrics.group_bytes]):
            x = metrics.cell_x(0, cell_index)
            painter.setPen(component_color(component))
            painter.drawText(
                QRectF(x, 0, metrics.cell_w, self.height()),
                Qt.AlignmentFlag.AlignCenter,
                component,
            )
        note = (
            "JPEG 压缩码流：无逐像素分组"
            if fmt.is_jpeg
            else "像素组：2 像素共享 U/V"
            if fmt.is_yuv422
            else "每格 1 像素"
        )
        note_x = metrics.cell_x(0, metrics.group_bytes) + 4
        painter.setPen(QColor(COLORS["dim"]))
        painter.drawText(
            QRectF(note_x, 0, max(80.0, self.width() - note_x - 8), self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            note,
        )


class _HexCanvas(QWidget):
    hover_changed = Signal(object, int)   # (ViewFocus, row_index)
    clicked = Signal(object, int)
    context_requested = Signal(QPoint)

    def __init__(self, viewer: "HexViewer") -> None:
        super().__init__()
        self.viewer = viewer
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:
        viewer = self.viewer
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(COLORS["bg"]))
        if viewer.structured is None or not viewer.rows:
            painter.setFont(mono_font(9))
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(
                self.rect().adjusted(20, 20, -20, -20),
                Qt.AlignmentFlag.AlignCenter,
                "还没有原始 Buffer\n\n点击 Capture Frame 采集一帧，或从右上角菜单加载 .bin 文件",
            )
            return

        first = max(0, bisect_right(viewer.tops, event.rect().top()) - 1)
        last = min(len(viewer.rows) - 1, bisect_left(viewer.tops, event.rect().bottom()))
        for index in range(first, last + 1):
            rect = QRect(0, int(viewer.tops[index]), self.width(), int(viewer.heights[index]))
            self._paint_row(painter, index, rect)

    # ------------------------------------------------------------------ #
    def _paint_row(self, painter: QPainter, index: int, rect: QRect) -> None:
        viewer = self.viewer
        row = viewer.rows[index]
        y = rect.top()
        height = rect.height()
        data = viewer.frame.raw_data if viewer.frame else b""
        metrics = viewer.metrics

        hovered = index == viewer.hover_row or index == viewer.external_row
        selected = index == viewer.selection_row
        if hovered:
            painter.fillRect(rect, QColor(79, 195, 247, 16))
        if selected:
            painter.fillRect(rect, QColor(47, 129, 247, 26))
            painter.fillRect(QRect(rect.left(), rect.top(), 3, height), QColor(COLORS["accent"]))

        flash = viewer.flash_range
        if flash:
            start, end = flash
            if row.length and row.offset < end and row.offset + row.length > start:
                painter.fillRect(rect, QColor(255, 214, 102, 28))

        if row.kind in ("info", "hash", "warn", "line"):
            self._paint_text_row(painter, row, rect, data)
            return

        # 偏移
        painter.setFont(mono_font(8))
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(
            QRectF(metrics.left, y, metrics.offset_w - 6, height),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{row.offset:08X}",
        )

        if row.kind == "pixels":
            self._paint_pixel_row(painter, index, row, rect, data)
        elif row.kind == "sync":
            self._paint_sync_row(painter, row, rect, data)
        else:
            self._paint_raw_row(painter, row, rect, data)

    def _paint_text_row(self, painter: QPainter, row: HexRow, rect: QRect, data: bytes) -> None:
        metrics = self.viewer.metrics
        painter.setFont(mono_font(9 if row.kind != "info" else 8))
        if row.kind == "info":
            painter.setPen(QColor(COLORS["accent"]))
            text = "  ·  ".join(self.viewer.summary) if self.viewer.summary else row.label
            elided = QFontMetrics(painter.font()).elidedText(
                text, Qt.TextElideMode.ElideRight, rect.width() - 20
            )
            painter.drawText(
                QRectF(metrics.left, rect.top(), rect.width() - 20, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )
        elif row.kind == "line":
            painter.setPen(QColor(COLORS["accent"]))
            painter.setFont(mono_font(8, bold=True))
            painter.drawText(
                QRectF(metrics.left, rect.top(), 70, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"LINE {row.line_index if row.line_index is not None else '-'}",
            )
            painter.setFont(mono_font(8))
            painter.setPen(QColor(COLORS["text"]))
            detail = row.label
            if detail.startswith("LINE "):
                parts = detail.split("  ", 1)
                detail = parts[1] if len(parts) > 1 else detail
            available = max(80.0, rect.width() - metrics.left - 90 - (170 if row.image_row is not None else 0))
            elided = QFontMetrics(painter.font()).elidedText(
                detail, Qt.TextElideMode.ElideRight, available
            )
            painter.drawText(
                QRectF(metrics.left + 78, rect.top(), available, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )
            if row.image_row is not None:
                painter.setPen(QColor(COLORS["ok"]))
                painter.drawText(
                    QRectF(rect.width() - 164, rect.top(), 150, rect.height()),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"image row {row.image_row}",
                )
            painter.setPen(QPen(QColor(COLORS["border"]), 1))
            painter.drawLine(rect.left() + 8, rect.bottom(), rect.right() - 8, rect.bottom())
        elif row.kind == "warn":
            painter.setPen(QColor(COLORS["warn"]))
            elided = QFontMetrics(painter.font()).elidedText(
                "⚠ " + row.label, Qt.TextElideMode.ElideRight, rect.width() - 24
            )
            painter.drawText(
                QRectF(metrics.left + 4, rect.top(), rect.width() - 28, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )
        else:
            painter.setPen(QColor(COLORS["dim"]))
            elided = QFontMetrics(painter.font()).elidedText(
                row.label, Qt.TextElideMode.ElideRight, rect.width() - 24
            )
            painter.drawText(
                QRectF(metrics.left + 4, rect.top(), rect.width() - 28, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )

    # ------------------------------------------------------------------ #
    def _paint_pixel_row(
        self, painter: QPainter, index: int, row: HexRow, rect: QRect, data: bytes
    ) -> None:
        viewer = self.viewer
        metrics = viewer.metrics
        fmt = get_pixel_format(viewer.structured.pixel_format)
        lanes = fmt.lane_components
        group_bytes = max(1, row.length // max(1, row.groups))
        y = rect.top()

        painter.setFont(mono_font(8))
        painter.setPen(QColor(COLORS["dim"]))
        label = (
            f"y={row.image_row:03d}" if row.image_row is not None else "y=-"
        ) + (f"  g={row.group_start:03d}" if row.group_start is not None else "")
        painter.drawText(
            QRectF(metrics.left + metrics.offset_w, y, metrics.label_w - 8, rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label,
        )

        hover_group = None
        hover_cell = None
        focus = viewer.display_focus
        if focus.active and focus.line_index == row.line_index and focus.y == row.image_row:
            hover_group = focus.group
            hover_cell = focus.component
        hovered_row = index == viewer.hover_row or index == viewer.external_row

        for group_offset in range(row.groups):
            group_index = (row.group_start or 0) + group_offset
            group_x = metrics.row_x + group_offset * metrics.group_width
            group_rect = QRectF(
                group_x, y + 4, metrics.group_width - metrics.group_gap, rect.height() - 8
            )
            if hovered_row and hover_group == group_index:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(79, 195, 247, 30))
                painter.drawRoundedRect(group_rect, 3, 3)
                painter.setPen(QPen(QColor(79, 195, 247, 150), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(group_rect, 3, 3)

            for cell_index in range(group_bytes):
                if cell_index >= len(lanes):
                    break
                component = lanes[cell_index]
                byte_offset = row.offset + group_offset * group_bytes + cell_index
                if byte_offset >= len(data):
                    continue
                value = data[byte_offset]
                cell_x = metrics.cell_x(group_offset, cell_index)
                cell_rect = QRectF(cell_x, y + 4, metrics.cell_w, rect.height() - 8)

                if component in ("U", "V"):
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(component_color(component).red(), component_color(component).green(),
                                            component_color(component).blue(), 20))
                    painter.drawRoundedRect(cell_rect.adjusted(0, 1, 0, -1), 2, 2)

                is_hover_cell = (
                    hovered_row
                    and hover_group == group_index
                    and hover_cell == component
                )
                if is_hover_cell:
                    painter.setPen(QPen(QColor(COLORS["accent"]), 1))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(cell_rect.adjusted(-1, 0, 1, 0), 2, 2)

                painter.setFont(mono_font(9))
                painter.setPen(component_color(component))
                painter.drawText(
                    QRectF(cell_x, y + 1, metrics.cell_w, 22),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{value:02X}",
                )
                painter.setFont(mono_font(7))
                painter.setPen(QColor(component_color(component).red(), component_color(component).green(),
                                      component_color(component).blue(), 170))
                painter.drawText(
                    QRectF(cell_x, y + 23, metrics.cell_w, 13),
                    Qt.AlignmentFlag.AlignCenter,
                    "Y" if component in ("Y0", "Y1") else component,
                )

    def _paint_sync_row(self, painter: QPainter, row: HexRow, rect: QRect, data: bytes) -> None:
        metrics = self.viewer.metrics
        is_eav = "EAV" in row.label
        color = QColor(COLORS["EAV"] if is_eav else COLORS["SAV"])
        painter.setFont(mono_font(8))
        painter.setPen(color)
        painter.drawText(
            QRectF(metrics.left + metrics.offset_w, rect.top(), metrics.label_w, rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "SAV" if not is_eav else "EAV",
        )
        chunk = data[row.offset : row.offset + row.length]
        for cell_index, value in enumerate(chunk):
            x = metrics.cell_x(0, cell_index)
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 160), 1))
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 18))
            cell = QRectF(x, rect.top() + 5, metrics.cell_w, rect.height() - 10)
            painter.drawRoundedRect(cell, 2, 2)
            painter.setFont(mono_font(9, bold=True))
            painter.setPen(color)
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, f"{value:02X}")
        note = row.label
        note_x = metrics.cell_x(0, 4) + 6
        painter.setFont(mono_font(8))
        painter.setPen(QColor(COLORS["muted"]))
        elided = QFontMetrics(painter.font()).elidedText(
            note, Qt.TextElideMode.ElideRight, max(60.0, rect.width() - note_x - 10)
        )
        painter.drawText(
            QRectF(note_x, rect.top(), rect.width() - note_x - 8, rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided,
        )

    def _paint_raw_row(self, painter: QPainter, row: HexRow, rect: QRect, data: bytes) -> None:
        metrics = self.viewer.metrics
        painter.setFont(mono_font(8))
        painter.setPen(QColor(COLORS["dim"]))
        label = row.label or ("行间隙/消隐" if row.kind == "blank" else "原始字节")
        elided = QFontMetrics(painter.font()).elidedText(
            label, Qt.TextElideMode.ElideRight, metrics.label_w - 4
        )
        painter.drawText(
            QRectF(metrics.left + metrics.offset_w, rect.top(), metrics.label_w, rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided,
        )
        chunk = data[row.offset : row.offset + row.length]
        capacity = metrics.byte_capacity(self.width())
        for cell_index, value in enumerate(chunk[:capacity]):
            x = metrics.row_x + cell_index * (metrics.cell_w + metrics.cell_gap)
            painter.setFont(mono_font(8))
            painter.setPen(QColor(COLORS["raw"]))
            painter.drawText(
                QRectF(x, rect.top(), metrics.cell_w, rect.height()),
                Qt.AlignmentFlag.AlignCenter,
                f"{value:02X}",
            )

    # ------------------------------------------------------------------ #
    def row_index_at(self, y: float) -> int:
        viewer = self.viewer
        if not viewer.tops:
            return -1
        index = bisect_right(viewer.tops, y) - 1
        return max(0, min(len(viewer.rows) - 1, index))

    def focus_at(self, position: QPoint) -> tuple[ViewFocus, int]:
        viewer = self.viewer
        index = self.row_index_at(position.y())
        if index < 0 or viewer.structured is None:
            return ViewFocus(), index
        row = viewer.rows[index]
        metrics = viewer.metrics

        if row.kind == "pixels":
            row_height = row.length // max(1, row.groups)
            if position.x() < metrics.row_x:
                if row.image_row is not None:
                    return (
                        ViewFocus(
                            kind="row",
                            y=row.image_row,
                            offset=row.offset,
                            line_index=row.line_index,
                            label="像素行",
                        ),
                        index,
                    )
                return ViewFocus(kind="line", offset=row.offset, label=row.label), index
            group_slot = int((position.x() - metrics.row_x) // metrics.group_width)
            if group_slot >= row.groups:
                return ViewFocus(kind="row", y=row.image_row, line_index=row.line_index), index
            group_index = (row.group_start or 0) + group_slot
            within = position.x() - (metrics.row_x + group_slot * metrics.group_width)
            cell_index = int(within // (metrics.cell_w + metrics.cell_gap))
            cell_index = max(0, min(row_height - 1, cell_index))
            offset = row.offset + group_slot * row_height + cell_index
            if viewer.frame is not None and viewer.frame.layout is not None:
                location = viewer.frame.layout.locate(offset)
                fmt = get_pixel_format(viewer.frame.pixel_format)
                return (
                    ViewFocus(
                        kind="byte",
                        y=row.image_row,
                        x=location.x,
                        offset=offset,
                        component=location.component,
                        line_index=row.line_index,
                        group=group_index if row.image_row is not None else None,
                        group_span=2 if fmt.is_yuv422 else 1,
                    ),
                    index,
                )
            return ViewFocus(kind="byte", offset=offset, line_index=row.line_index), index

        if row.kind == "sync":
            code = row.xy
            label = f"{'EAV' if code is not None and (code & 0x10) else 'SAV'}"
            return (
                ViewFocus(
                    kind="sync",
                    y=row.image_row,
                    offset=row.offset,
                    line_index=row.line_index,
                    label=label,
                ),
                index,
            )

        if row.kind == "line":
            return (
                ViewFocus(
                    kind="row",
                    y=row.image_row,
                    offset=row.offset,
                    line_index=row.line_index,
                    label=row.label[:40],
                ),
                index,
            )

        if row.kind in ("blank", "raw") and row.length:
            return (
                ViewFocus(
                    kind="row" if row.image_row is not None else "line",
                    y=row.image_row,
                    offset=row.offset,
                    line_index=row.line_index,
                    label=row.label,
                ),
                index,
            )
        return ViewFocus(offset=row.offset, label=row.label), index

    # ------------------------------------------------------------------ #
    def mouseMoveEvent(self, event) -> None:
        focus, index = self.focus_at(event.pos())
        viewer = self.viewer
        if index != viewer.hover_row or focus != viewer.hover_focus:
            viewer.hover_row = index
            viewer.hover_focus = focus
            self.update()
            viewer.header.update()
            self.hover_changed.emit(focus if focus.active else None, index)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            focus, index = self.focus_at(event.pos())
            viewer = self.viewer
            viewer.selection_row = index
            viewer.selection = focus
            self.update()
            if focus.active:
                self.clicked.emit(focus, index)
            self.setFocus()

    def mouseDoubleClickEvent(self, event) -> None:
        focus, _ = self.focus_at(event.pos())
        if focus.offset is not None:
            self.viewer.select_offset(focus.offset)

    def leaveEvent(self, event) -> None:
        viewer = self.viewer
        if viewer.hover_row >= 0 or viewer.hover_focus.active:
            viewer.hover_row = -1
            viewer.hover_focus = ViewFocus()
            self.update()
            viewer.header.update()
            self.hover_changed.emit(None, -1)
        super().leaveEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.context_requested.emit(event.pos())

    def wheelEvent(self, event) -> None:
        bar = self.viewer.scroll.verticalScrollBar()
        bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()

    def keyPressEvent(self, event) -> None:
        bar = self.viewer.scroll.verticalScrollBar()
        if event.key() == Qt.Key.Key_Home:
            bar.setValue(0)
        elif event.key() == Qt.Key.Key_End:
            bar.setValue(bar.maximum())
        elif event.key() == Qt.Key.Key_PageDown:
            bar.setValue(bar.value() + bar.pageStep())
        elif event.key() == Qt.Key.Key_PageUp:
            bar.setValue(bar.value() - bar.pageStep())
        else:
            super().keyPressEvent(event)


class HexViewer(QWidget):
    focus_changed = Signal(object)
    selection_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.frame: Frame | None = None
        self.structured: StructuredData | None = None
        self.rows: list[HexRow] = []
        self.tops: list[float] = []
        self.heights: list[float] = []
        self.summary: list[str] = []
        self.metrics = HexMetrics()
        self.hover_row = -1
        self.hover_focus = ViewFocus()
        self.selection_row = -1
        self.selection = ViewFocus()
        self.flash_range: tuple[int, int] | None = None
        self._external = ViewFocus()
        self.external_row = -1
        self._wanted_groups = 8

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(1600)
        self._flash_timer.timeout.connect(self._clear_flash)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(QLabel("Offset"))
        self.goto_edit = QLineEdit()
        self.goto_edit.setPlaceholderText("0x0000 或 12345")
        self.goto_edit.setFixedWidth(120)
        self.goto_edit.returnPressed.connect(self.goto_offset_from_edit)
        toolbar.addWidget(self.goto_edit)
        self.goto_btn = QPushButton("跳转")
        self.goto_btn.clicked.connect(self.goto_offset_from_edit)
        toolbar.addWidget(self.goto_btn)

        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("搜索"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("FF 00 00 AB / 0x9D")
        self.search_edit.setFixedWidth(150)
        self.search_edit.returnPressed.connect(self.find_next)
        toolbar.addWidget(self.search_edit)
        self.find_btn = QPushButton("查找")
        self.find_btn.clicked.connect(self.find_next)
        toolbar.addWidget(self.find_btn)
        self.wrap_check = QCheckBox("循环")
        self.wrap_check.setChecked(True)
        toolbar.addWidget(self.wrap_check)

        self.structured_check = QCheckBox("结构视图")
        self.structured_check.setChecked(True)
        self.structured_check.setToolTip("关闭后显示纯十六进制（16 字节/行）")
        self.structured_check.toggled.connect(self._on_structured_toggle)
        toolbar.addWidget(self.structured_check)

        toolbar.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setMinimumWidth(140)
        toolbar.addWidget(self.status_label)
        layout.addLayout(toolbar)

        self.header = _HexHeader(self)
        layout.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = _HexCanvas(self)
        self.canvas.setMinimumHeight(200)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)

        self.canvas.hover_changed.connect(self._on_hover_changed)
        self.canvas.clicked.connect(self._on_clicked)
        self.canvas.context_requested.connect(self._show_context_menu)

        self._plain_mode = False

    # ------------------------------------------------------------------ #
    def set_frame(self, frame: Frame | None, structured: StructuredData | None = None) -> None:
        self.frame = frame
        self._plain_mode = not self.structured_check.isChecked()
        if structured is not None:
            self._wanted_groups = max(1, int(structured.groups_per_row))
        if frame is not None and self._plain_mode:
            structured = format_plain(frame, 16)
        self.structured = structured
        self.summary = list(structured.summary) if structured else []
        self.rows = list(structured.rows) if structured else []
        self.hover_row = -1
        self.hover_focus = ViewFocus()
        self.selection_row = -1
        self.selection = ViewFocus()
        self._external = ViewFocus()
        self.external_row = -1
        self.flash_range = None
        self._update_metrics()
        self._rebuild_geometry()
        self.scroll.verticalScrollBar().setValue(0)
        self.canvas.update()
        self.header.update()
        self.status_label.setText("")

    def set_plain_mode(self, plain: bool) -> None:
        if plain == self._plain_mode:
            return
        self._plain_mode = plain
        if self.frame is None:
            return
        from core.hex.formatter import build_structured

        self.set_frame(
            self.frame,
            format_plain(self.frame, 16) if plain else build_structured(self.frame, self._groups()),
        )

    def _groups(self) -> int:
        return self._wanted_groups

    def _on_structured_toggle(self, checked: bool) -> None:
        self.set_plain_mode(not checked)

    # ------------------------------------------------------------------ #
    def _update_metrics(self) -> None:
        width = self.scroll.viewport().width() if hasattr(self, "scroll") else self.width()
        width = max(420, width)
        data = self.structured
        fmt = get_pixel_format(data.pixel_format) if data else None
        group_bytes = max(1, fmt.group_bytes) if fmt and fmt.group_bytes else 4
        groups = data.groups_per_row if data else 8
        left = 10.0
        offset_w = 94.0
        label_w = 112.0
        cell_gap = 2.0
        group_gap = 8.0
        available = max(220.0, width - left - offset_w - label_w - 16.0)
        cell_w = 24.0
        group_width = group_bytes * cell_w + (group_bytes - 1) * cell_gap + group_gap
        if groups * group_width > available:
            cell_w = max(
                13.0,
                (available - groups * (group_bytes - 1) * cell_gap - groups * group_gap)
                / (groups * group_bytes),
            )
        self.metrics = HexMetrics(
            left=left,
            offset_w=offset_w,
            label_w=label_w,
            cell_w=cell_w,
            cell_gap=cell_gap,
            group_gap=group_gap,
            group_bytes=group_bytes,
            groups_per_row=groups,
        )
        self.header.update()

    def _rebuild_geometry(self) -> None:
        tops: list[float] = []
        heights: list[float] = []
        cursor = 0.0
        for row in self.rows:
            height = _ROW_HEIGHTS.get(row.kind, 28)
            tops.append(cursor)
            heights.append(float(height))
            cursor += height
        self.tops = tops
        self.heights = heights
        self.canvas.setMinimumHeight(max(200, int(cursor + 8)))

    def resizeEvent(self, event) -> None:
        self._update_metrics()
        self.canvas.update()
        super().resizeEvent(event)

    # ------------------------------------------------------------------ #
    def _on_hover_changed(self, focus, index: int) -> None:
        if focus is not None or index == -1:
            # 鼠标进入 Hex 或离开 Hex：清除来自图像的临时定位高亮
            self._external = ViewFocus()
            self.external_row = -1
            self.canvas.update()
        self.focus_changed.emit(focus)

    def _on_clicked(self, focus, index: int) -> None:
        self.selection_changed.emit(focus)

    def set_external_focus(self, focus: ViewFocus | None, follow: bool = True) -> None:
        focus = focus or ViewFocus()
        if focus == self._external:
            return
        self._external = focus
        self.external_row = -1
        if focus.offset is not None and self.structured is not None:
            index = self.structured.row_index_for_offset(focus.offset)
            if index is not None:
                self.external_row = index
                if follow:
                    self.ensure_visible(index)
        self.canvas.update()

    def clear_hover(self) -> None:
        """离开 Hex 页面时清除临时高亮（不改变用户选中状态）。"""
        self.hover_row = -1
        self.hover_focus = ViewFocus()
        self._external = ViewFocus()
        self.external_row = -1
        self.canvas.update()
        self.header.update()

    @property
    def display_focus(self) -> ViewFocus:
        return self.hover_focus if self.hover_focus.active else self._external

    def ensure_visible(self, index: int) -> None:
        if not self.tops or index < 0 or index >= len(self.tops):
            return
        bar = self.scroll.verticalScrollBar()
        top = self.tops[index]
        height = self.heights[index]
        viewport = self.scroll.viewport().height()
        if top < bar.value():
            bar.setValue(int(top))
        elif top + height > bar.value() + viewport:
            bar.setValue(int(top + height - viewport + 2))

    # ------------------------------------------------------------------ #
    def goto_offset_from_edit(self) -> None:
        text = self.goto_edit.text().strip()
        if not text:
            return
        try:
            offset = int(text, 16) if text.lower().startswith("0x") else int(text, 0)
        except ValueError:
            self._set_status("Offset 格式无效", error=True)
            return
        self.go_to_offset(offset)

    def go_to_offset(self, offset: int, flash_length: int = 1) -> None:
        if self.structured is None:
            return
        if offset < 0 or offset >= self.structured.total_bytes:
            self._set_status(f"Offset 越界：0x{offset:X}", error=True)
            return
        index = self.structured.row_index_for_offset(offset)
        if index is None:
            return
        self.selection_row = index
        self.selection = ViewFocus(
            kind="row", offset=offset, line_index=self.rows[index].line_index
        )
        self.ensure_visible(index)
        self.set_flash(offset, flash_length)
        self._set_status(f"定位 0x{offset:08X}（第 {index} 行）")
        self.canvas.update()

    def set_flash(self, offset: int, length: int) -> None:
        self.flash_range = (offset, offset + max(1, length))
        self._flash_timer.start()
        self.canvas.update()

    def _clear_flash(self) -> None:
        self.flash_range = None
        self.canvas.update()

    def _set_status(self, text: str, error: bool = False) -> None:
        color = COLORS["error"] if error else COLORS["muted"]
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setText(text)

    def find_next(self) -> None:
        if self.frame is None or self.structured is None:
            return
        pattern = _parse_hex_pattern(self.search_edit.text())
        if not pattern:
            self._set_status("请输入十六进制字节，如 FF 00 00 AB", error=True)
            return
        data = self.frame.raw_data
        start = 0
        if self.selection.offset is not None:
            start = self.selection.offset + 1
        found = data.find(pattern, max(0, start))
        if found < 0 and self.wrap_check.isChecked():
            found = data.find(pattern, 0)
        if found < 0:
            self._set_status(f"未找到 {pattern.hex(' ').upper()}", error=True)
            return
        self.go_to_offset(found, len(pattern))
        self._set_status(f"命中 {pattern.hex(' ').upper()} @ 0x{found:08X}")

    def select_offset(self, offset: int) -> None:
        if self.structured is None:
            return
        index = self.structured.row_index_for_offset(offset)
        if index is None:
            return
        self.select_focus(ViewFocus(kind="row", offset=offset))

    def select_focus(self, focus: ViewFocus) -> None:
        """在 Hex Viewer 中保持某个像素组/字节的选中状态。"""
        if self.structured is None or focus.offset is None:
            return
        index = self.structured.row_index_for_offset(focus.offset)
        if index is None:
            return
        self.selection_row = index
        self.selection = focus
        self.ensure_visible(index)
        self.set_flash(focus.offset, 1)

    # ------------------------------------------------------------------ #
    def _show_context_menu(self, position: QPoint) -> None:
        if self.frame is None or self.structured is None:
            return
        focus, index = self.canvas.focus_at(position)
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        menu = QMenu(self)
        if focus.offset is not None:
            fmt = get_pixel_format(self.frame.pixel_format)
            group_bytes = max(1, fmt.group_bytes)
            action = menu.addAction("复制当前像素组字节（HEX）")
            action.triggered.connect(
                lambda: self._copy_bytes(focus.offset or 0, group_bytes)
            )
        if row is not None and row.length:
            action = menu.addAction("复制当前行 Hex 文本")
            action.triggered.connect(
                lambda: self._copy_row(row)
            )
            action = menu.addAction("复制当前行 C 数组")
            action.triggered.connect(
                lambda: self._copy_row_c(row)
            )
        if focus.offset is not None:
            action = menu.addAction("复制 Offset")
            action.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(f"0x{focus.offset:08X}")
            )
        if row is not None:
            menu.addSeparator()
            action = menu.addAction("复制行信息")
            action.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(
                    row.label or f"offset=0x{row.offset:08X} length={row.length}"
                )
            )
        menu.addSeparator()
        action = menu.addAction("搜索此字节值")
        action.triggered.connect(lambda: self._search_current_byte(focus))
        menu.exec(self.canvas.mapToGlobal(position))

    def _bytes_at(self, offset: int, count: int) -> bytes:
        if self.frame is None:
            return b""
        return self.frame.raw_data[offset : offset + count]

    def _copy_bytes(self, offset: int, count: int) -> None:
        data = self._bytes_at(offset, count)
        text = " ".join(f"{b:02X}" for b in data)
        QGuiApplication.clipboard().setText(text)
        self._set_status(f"已复制 {len(data)} 字节")

    def _copy_row(self, row: HexRow) -> None:
        if self.frame is None:
            return
        data = self.frame.raw_data[row.offset : row.offset + row.length]
        lines = [
            f"{row.offset + index:08X}  "
            + " ".join(f"{b:02x}" for b in data[index : index + 16])
            for index in range(0, len(data), 16)
        ]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self._set_status("已复制行 Hex")

    def _copy_row_c(self, row: HexRow) -> None:
        if self.frame is None:
            return
        data = self.frame.raw_data[row.offset : row.offset + row.length]
        text = ", ".join(f"0x{b:02X}" for b in data)
        QGuiApplication.clipboard().setText(text)
        self._set_status("已复制行 C 数组")

    def _search_current_byte(self, focus: ViewFocus) -> None:
        if self.frame is None or focus.offset is None:
            return
        value = self.frame.raw_data[focus.offset]
        self.search_edit.setText(f"{value:02X}")
        self.find_next()


def _parse_hex_pattern(text: str) -> bytes:
    if not text:
        return b""
    cleaned = re.sub(r"0x|[\s,;:_\-]", "", text, flags=re.IGNORECASE)
    if not cleaned or len(cleaned) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", cleaned):
        return b""
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return b""
