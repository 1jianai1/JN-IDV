"""Image Preview：真实图像 + 行/像素组「抽出放大」联动高亮。

渲染分三层（互不修改原始数据）：
    Base Image Layer        原始解码图像，始终不变
    Highlight Overlay       其他区域压暗 + 当前行恢复正常亮度
    Extracted Layer         把当前行/像素组单独放大绘制（带阴影、边框、动画）

坐标映射始终基于 `frame.layout`（真实 Buffer offset -> (x, y)），
动画只改变绘制矩形，不参与坐标计算。
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

from core.formats.pixel_formats import get_pixel_format
from core.image.frame import Frame
from gui.focus import ViewFocus
from gui.theme import COLORS, mono_font


class ImageViewer(QWidget):
    hover_changed = Signal(object)   # ViewFocus | None
    clicked = Signal(object)         # ViewFocus

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._frame: Frame | None = None
        self._qimage: QImage | None = None
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)

        self._focus = ViewFocus()
        self._hover = ViewFocus()
        self._anim = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()
        self._clock.start()

        self._drag_start: QPointF | None = None
        self._drag_pan: QPointF | None = None
        self._dragged = False
        self._corner_widget: QWidget | None = None

        self._controls = QWidget(self)
        self._controls.setStyleSheet("background: transparent;")
        row = QHBoxLayout(self._controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        for text, slot, tip in (
            ("Fit", self.fit_to_window, "适应窗口（双击图像也可）"),
            ("1:1", self.actual_size, "按原始像素显示"),
            ("−", lambda: self._zoom_by(1 / 1.25), "缩小"),
            ("+", lambda: self._zoom_by(1.25), "放大"),
        ):
            button = QToolButton(self._controls)
            button.setText(text)
            button.setToolTip(tip)
            button.setFixedSize(30, 22)
            button.clicked.connect(slot)
            row.addWidget(button)
        self._controls.adjustSize()

    # ------------------------------------------------------------------ #
    def set_frame(self, frame: Frame | None) -> None:
        self._frame = frame
        self._focus = ViewFocus()
        self._hover = ViewFocus()
        self._anim = 0.0
        self._timer.stop()
        if frame is None:
            self._qimage = None
        else:
            rgb = np.ascontiguousarray(frame.image, dtype=np.uint8)
            height, width, _ = rgb.shape
            self._qimage = QImage(
                rgb.data, width, height, int(rgb.strides[0]), QImage.Format.Format_RGB888
            ).copy()
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def frame(self) -> Frame | None:
        return self._frame

    def set_focus(self, focus: ViewFocus | None) -> None:
        focus = focus or ViewFocus()
        if focus == self._focus:
            return
        previous = self._focus
        self._focus = focus
        if focus.active and focus.y is not None:
            # 行/像素切换时给一个短促的重新抽出动效
            self._anim = min(self._anim, 0.35) if previous.active else 0.0
        self._timer.start()
        self.update()

    def current_focus(self) -> ViewFocus:
        return self._focus

    def set_corner_widget(self, widget: QWidget | None) -> None:
        """在图像右上角放置一个浮动控件（例如三点导出菜单）。"""
        self._corner_widget = widget
        if widget is not None:
            widget.setParent(self)
            widget.raise_()
        self._place_corner_widget()

    def _place_corner_widget(self) -> None:
        if self._corner_widget is None:
            return
        self._corner_widget.move(
            max(6, self.width() - self._corner_widget.width() - 12), 10
        )

    # ------------------------------------------------------------------ #
    def fit_to_window(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def actual_size(self) -> None:
        base = self._base_scale()
        if base <= 0:
            return
        self._zoom = max(0.02, min(64.0, 1.0 / base))
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def _zoom_by(self, factor: float) -> None:
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        self._zoom_at(center, factor)

    # ------------------------------------------------------------------ #
    def _base_scale(self) -> float:
        if self._qimage is None:
            return 1.0
        width = self._qimage.width()
        height = self._qimage.height()
        if width <= 0 or height <= 0:
            return 1.0
        return min(
            max(32.0, self.width() - 32.0) / width,
            max(32.0, self.height() - 32.0) / height,
        )

    def _scale(self) -> float:
        return self._base_scale() * self._zoom

    def _image_rect(self) -> QRectF:
        if self._qimage is None:
            return QRectF()
        width = self._qimage.width()
        height = self._qimage.height()
        scale = self._scale()
        cx = self.width() / 2.0 + self._pan.x()
        cy = self.height() / 2.0 + self._pan.y()
        return QRectF(
            cx - width * scale / 2.0,
            cy - height * scale / 2.0,
            width * scale,
            height * scale,
        )

    def _zoom_at(self, position: QPointF, factor: float) -> None:
        if self._qimage is None:
            return
        old_scale = self._scale()
        if old_scale <= 0:
            return
        old_rect = self._image_rect()
        image_x = (position.x() - old_rect.left()) / old_scale
        image_y = (position.y() - old_rect.top()) / old_scale
        self._zoom = max(0.02, min(128.0, self._zoom * factor))
        new_scale = self._base_scale() * self._zoom
        width = self._qimage.width()
        height = self._qimage.height()
        cx = position.x() - image_x * new_scale + width * new_scale / 2.0
        cy = position.y() - image_y * new_scale + height * new_scale / 2.0
        self._pan = QPointF(cx - self.width() / 2.0, cy - self.height() / 2.0)
        self.update()

    # ------------------------------------------------------------------ #
    def _focus_at(self, position: QPointF) -> ViewFocus:
        frame = self._frame
        if frame is None or frame.layout is None or not frame.layout.mapped:
            return ViewFocus()
        rect = self._image_rect()
        if not rect.contains(position):
            return ViewFocus()
        scale = self._scale()
        if scale <= 0:
            return ViewFocus()
        x = int(math.floor((position.x() - rect.left()) / scale))
        y = int(math.floor((position.y() - rect.top()) / scale))
        if not (0 <= x < frame.width and 0 <= y < frame.height):
            return ViewFocus()
        layout = frame.layout
        if y >= layout.image_height:
            return ViewFocus()
        fmt = get_pixel_format(frame.pixel_format)
        group = x // 2 if fmt.is_yuv422 else x
        x0 = group * 2 if fmt.is_yuv422 else group
        offset = layout.offset_of(x0, y)
        location = layout.locate(offset) if offset is not None else None
        return ViewFocus(
            kind="group",
            y=y,
            x=x0,
            offset=offset,
            line_index=location.line_index if location else None,
            group=group,
            group_span=2 if fmt.is_yuv422 else 1,
        )

    # ------------------------------------------------------------------ #
    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        if self._drag_start is not None and self._drag_pan is not None:
            delta = position - self._drag_start
            if abs(delta.x()) + abs(delta.y()) > 2:
                self._dragged = True
            self._pan = self._drag_pan + delta
            self.update()
            return
        focus = self._focus_at(position)
        if focus != self._hover:
            self._hover = focus
            self.hover_changed.emit(focus if focus.active else None)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_pan = QPointF(self._pan)
            self._dragged = False

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._dragged:
                focus = self._focus_at(event.position())
                if focus.active:
                    self.clicked.emit(focus)
            self._drag_start = None
            self._drag_pan = None
            self._dragged = False

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_to_window()

    def leaveEvent(self, event) -> None:
        self._hover = ViewFocus()
        self.hover_changed.emit(None)
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        if self._qimage is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._zoom_at(event.position(), 1.25 ** (delta / 120.0))
        event.accept()

    def resizeEvent(self, event) -> None:
        self._controls.adjustSize()
        self._controls.move(
            max(6, self.width() - self._controls.width() - 12),
            max(6, self.height() - self._controls.height() - 10),
        )
        self._place_corner_widget()
        self.update()
        super().resizeEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom_by(1.25)
        elif key == Qt.Key.Key_Minus:
            self._zoom_by(1 / 1.25)
        elif key == Qt.Key.Key_F:
            self.fit_to_window()
        elif key == Qt.Key.Key_1:
            self.actual_size()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        target = 1.0 if (self._focus.active and self._focus.y is not None) else 0.0
        dt = max(1, self._clock.restart())
        step = dt / 130.0
        if self._anim < target:
            self._anim = min(target, self._anim + step)
        elif self._anim > target:
            self._anim = max(target, self._anim - step)
        self.update()
        if abs(self._anim - target) < 1e-3:
            self._anim = target
            self._timer.stop()

    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["bg"]))

        if self._qimage is None or self._frame is None:
            self._paint_empty(painter)
            return

        rect = self._image_rect()
        scale = self._scale()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, scale < 1.0)
        painter.drawImage(rect, self._qimage)
        # 图像边框
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.drawRect(rect.adjusted(-1, -1, 0, 0))

        self._paint_highlight(painter, rect, scale)
        self._paint_hud(painter)

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setPen(QColor(COLORS["muted"]))
        font = QFont("Segoe UI", 11)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(0, 0, 0, -40),
            Qt.AlignmentFlag.AlignCenter,
            "还没有图像数据",
        )
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["dim"]))
        painter.drawText(
            self.rect().adjusted(0, 40, 0, 0),
            Qt.AlignmentFlag.AlignCenter,
            "连接串口后点击左侧 Capture Frame\n"
            "或通过右上角菜单 / 拖入 .bin 文件离线查看",
        )

    # ------------------------------------------------------------------ #
    def _paint_highlight(self, painter: QPainter, rect: QRectF, scale: float) -> None:
        focus = self._focus
        progress = self._anim
        if not focus.active or focus.y is None or progress <= 0.001:
            return
        if self._qimage is None:
            return
        image_width = self._qimage.width()
        image_height = self._qimage.height()
        y = focus.y
        if y < 0 or y >= image_height:
            return
        row_source = QRectF(0, y, image_width, 1)
        row_top = rect.top() + y * scale
        row_height = max(1.0, scale)

        painter.save()
        painter.setClipRect(rect)

        if focus.kind in ("group", "byte") and focus.x is not None:
            span = max(1, focus.group_span)
            x0 = focus.x
            if span == 2:
                x0 = x0 - (x0 % 2)
            span = min(span, image_width - x0)
            if span <= 0:
                painter.restore()
                return

            # 1) 压暗其他区域，但保持当前行正常亮度
            painter.fillRect(rect, QColor(6, 8, 12, int(120 * progress)))
            painter.drawImage(
                QRectF(rect.left(), row_top, rect.width(), row_height),
                self._qimage,
                row_source,
            )

            # 2) 稳定显示一次，再按 1 像素以外的其他行压暗（行内上下文保留）
            source = QRectF(x0, y, span, 1)
            center = QPointF(
                rect.left() + (x0 + span / 2.0) * scale,
                rect.top() + (y + 0.5) * scale,
            )
            grow = 1.0 + 3.2 * progress
            target_width = max(span * scale * grow, 10.0)
            target_height = max(row_height * grow, 8.0)
            target = QRectF(
                center.x() - target_width / 2.0,
                center.y() - target_height / 2.0,
                target_width,
                target_height,
            )

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(150 * progress)))
            painter.drawRoundedRect(target.adjusted(-4, -4, 4, 5), 5, 5)
            painter.drawImage(target, self._qimage, source)

            pen = QPen(QColor(COLORS["accent"]))
            pen.setWidthF(1.4 + 0.6 * progress)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(target)

            if span == 2:
                pen = QPen(QColor(255, 255, 255, int(140 * progress)))
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(target.center().x(), target.top() + 1),
                    QPointF(target.center().x(), target.bottom() - 1),
                )
        else:
            painter.fillRect(rect, QColor(6, 8, 12, int(130 * progress)))
            grow_height = max(
                row_height * (1.0 + 5.0 * progress), row_height + 9 * progress
            )
            target = QRectF(
                rect.left() - 8 * progress,
                row_top - (grow_height - row_height) / 2.0,
                rect.width() + 16 * progress,
                grow_height,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(160 * progress)))
            painter.drawRoundedRect(target.adjusted(-4, -3, 4, 5), 4, 4)
            painter.drawImage(target, self._qimage, row_source)
            color = (
                QColor(COLORS["EAV"])
                if "EAV" in focus.label
                else QColor(COLORS["SAV"])
                if "SAV" in focus.label
                else QColor(COLORS["accent"])
            )
            pen = QPen(color)
            pen.setWidthF(1.2 + 0.6 * progress)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(target)

            if progress > 0.4:
                painter.setFont(mono_font(8, bold=True))
                painter.setPen(color)
                tag = focus.label or f"LINE {focus.line_index}"
                painter.drawText(
                    QPointF(target.left() + 6, max(target.top() - 4, 12)), tag[:48]
                )
        painter.restore()

    # ------------------------------------------------------------------ #
    def _paint_hud(self, painter: QPainter) -> None:
        frame = self._frame
        if frame is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 顶部标签
        chip = f"FRAME #{frame.frame_index}   {frame.width} x {frame.height}   {frame.pixel_format}"
        font = mono_font(9, bold=True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        chip_width = metrics.horizontalAdvance(chip) + 20
        chip_rect = QRectF(12, 10, chip_width, 24)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 15, 20, 210))
        painter.drawRoundedRect(chip_rect, 4, 4)
        painter.setPen(QPen(QColor(COLORS["border_hi"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(chip_rect, 4, 4)
        painter.setPen(QColor(COLORS["accent"]))
        painter.drawText(chip_rect.adjusted(10, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter, chip)

        # 左下角状态
        lines = frame.info_lines()
        font = mono_font(8)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        line_height = metrics.height() + 2
        label_width = max(
            metrics.horizontalAdvance(f"{label}: {value}") for label, value in lines
        )
        panel_width = label_width + 24
        panel_height = line_height * len(lines) + 16
        panel = QRectF(12, self.height() - panel_height - 12, panel_width, panel_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 15, 20, 215))
        painter.drawRoundedRect(panel, 5, 5)
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(panel, 5, 5)

        y = panel.top() + 9
        for label, value in lines:
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QPointF(panel.left() + 12, y + metrics.ascent()), f"{label}:")
            offset_x = 12 + metrics.horizontalAdvance(
                max((f"{lab}:" for lab, _ in lines), key=lambda text: metrics.horizontalAdvance(text))
            ) + 8
            painter.setPen(QColor(COLORS["text"]))
            painter.drawText(QPointF(panel.left() + offset_x, y + metrics.ascent()), value)
            y += line_height

        # 像素检查器（仅像素级高亮时显示，行级高亮不伪造 x 坐标）
        focus = self._focus if self._focus.active else self._hover
        if focus.is_pixel_level and focus.y is not None:
            self._paint_inspector(painter, chip_rect)

        # 右上角缩放比例
        painter.setFont(mono_font(8))
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(
            QRectF(self.width() - 120, self.height() - 30, 108, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"Zoom {self._scale() * 100:.0f}%",
        )
        painter.restore()

    def _paint_inspector(self, painter: QPainter, chip_rect: QRectF) -> None:
        frame = self._frame
        focus = self._focus if self._focus.active else self._hover
        if frame is None or focus.y is None:
            return
        x = focus.x if focus.x is not None else 0
        y = focus.y
        offset = focus.offset
        fmt = get_pixel_format(frame.pixel_format)
        group_bytes = fmt.group_bytes
        raw = b""
        if offset is not None and group_bytes:
            raw = frame.raw_data[offset : offset + group_bytes]

        rows: list[tuple[str, list[tuple[str, QColor]]]] = []
        rows.append(("Position", [(f"x={x}  y={y}  group={focus.group}", QColor(COLORS["text"]))]))
        if offset is not None:
            rows.append(("Offset", [(f"0x{offset:08X}", QColor(COLORS["accent"]))]))
        if raw and fmt.lane_components:
            parts = []
            for label, value in zip(fmt.lane_components, raw):
                parts.append((f"{label}={value:02X} ", QColor(COLORS.get(label[:1], COLORS["raw"]))))
            rows.append(("Raw", parts))
        yuv = frame.pixel_yuv(min(x, frame.width - 1), y)
        if yuv is not None:
            rows.append(
                (
                    "YUV",
                    [
                        (f"Y={yuv[0]} ", QColor(COLORS["Y"])),
                        (f"U={yuv[1]} ", QColor(COLORS["U"])),
                        (f"V={yuv[2]}", QColor(COLORS["V"])),
                    ],
                )
            )
        rgb = frame.pixel_rgb(min(x, frame.width - 1), y)
        if rgb is not None:
            rows.append(
                (
                    "RGB",
                    [
                        (f"R={rgb[0]} ", QColor(COLORS["R"])),
                        (f"G={rgb[1]} ", QColor(COLORS["G"])),
                        (f"B={rgb[2]}", QColor(COLORS["B"])),
                    ],
                )
            )

        font = mono_font(8)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        title = "PIXEL INSPECTOR"
        widths = [metrics.horizontalAdvance(title)]
        for label, parts in rows:
            widths.append(metrics.horizontalAdvance(label) + 10 + sum(
                metrics.horizontalAdvance(text) for text, _ in parts
            ))
        panel_width = max(widths) + 24
        line_height = metrics.height() + 3
        panel_height = 12 + line_height * (len(rows) + 1) + 6
        panel = QRectF(
            chip_rect.left(),
            chip_rect.bottom() + 8,
            panel_width,
            panel_height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 15, 20, 225))
        painter.drawRoundedRect(panel, 5, 5)
        painter.setPen(QPen(QColor(COLORS["border_hi"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(panel, 5, 5)

        y_cursor = panel.top() + 8
        painter.setPen(QColor(COLORS["accent"]))
        painter.setFont(mono_font(8, bold=True))
        painter.drawText(QPointF(panel.left() + 12, y_cursor + metrics.ascent()), title)
        y_cursor += line_height
        painter.setFont(font)

        label_x = panel.left() + 12
        value_x = label_x + max(
            metrics.horizontalAdvance(label) for label, _ in rows
        ) + 10
        for label, parts in rows:
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QPointF(label_x, y_cursor + metrics.ascent()), label)
            cursor_x = value_x
            for text, color in parts:
                painter.setPen(color)
                painter.drawText(QPointF(cursor_x, y_cursor + metrics.ascent()), text)
                cursor_x += metrics.horizontalAdvance(text)
            y_cursor += line_height
