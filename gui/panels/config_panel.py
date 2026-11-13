"""左侧配置与调试操作面板。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.acquisition.capture import CaptureProgress
from core.config import (
    PROTOCOLS,
    PROTOCOL_FUTURE,
    CaptureConfig,
    ImageConfig,
    SerialConfig,
)
from core.formats.pixel_formats import PIXEL_FORMATS, get_pixel_format, resolve_stride
from core.log import get_logger

log = get_logger("gui.config")

_BAUDS = [
    9600,
    19200,
    38400,
    57600,
    115200,
    230400,
    460800,
    921600,
    1000000,
    1500000,
    2000000,
    3000000,
]
_PARITIES = [("无 (None)", "none"), ("偶 (Even)", "even"), ("奇 (Odd)", "odd"),
             ("标记 (Mark)", "mark"), ("空格 (Space)", "space")]
_FLOW = [("无流控", "none"), ("RTS/CTS", "rtscts"), ("XON/XOFF", "xonxoff")]

_QUICK_COMMANDS = [
    ("设备信息", "camera info", "打印设备状态（示例命令，按实际设备修改）"),
    ("采集一帧", "camera capture 1000", "触发一次采集"),
    ("导出整场", "camera dump", "一次导出整块 Buffer"),
    ("帮助", "help", "列出设备支持的命令"),
]


class ConfigPanel(QWidget):
    connect_requested = Signal(object)      # SerialConfig
    disconnect_requested = Signal()
    ports_refresh_requested = Signal()
    capture_requested = Signal()
    stop_requested = Signal()
    clear_requested = Signal()
    command_requested = Signal(str)
    image_config_changed = Signal(object)   # ImageConfig
    load_raw_requested = Signal()
    frame_nav_requested = Signal(int)       # -1 / +1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = True
        self._connected = False
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        title = QLabel("JN-IDV")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("通用摄像头图像调试上位机")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        layout.addWidget(self._build_serial_group())
        layout.addWidget(self._build_actions_group())
        layout.addWidget(self._build_image_group())
        layout.addWidget(self._build_transfer_group())
        layout.addWidget(self._build_command_group())
        layout.addWidget(self._build_frame_group())
        layout.addStretch(1)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        status_row.addWidget(self.progress, 1)
        layout.addLayout(status_row)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._loading = False
        self.stride_spin.setEnabled(not self.auto_stride_check.isChecked())
        self._refresh_stride()
        self._refresh_derived()
        self._apply_enabled()
        self._on_mode_changed()

    # ================================================================== #
    # 构建 UI
    # ================================================================== #
    def _build_serial_group(self) -> QGroupBox:
        group = QGroupBox("串口配置")
        form = QFormLayout(group)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        port_row = QHBoxLayout()
        port_row.setSpacing(4)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(140)
        self.port_combo.setToolTip("可用串口列表；也可以直接输入 COMx")
        self.refresh_ports_btn = QToolButton()
        self.refresh_ports_btn.setObjectName("refresh")
        self.refresh_ports_btn.setText("⟳")
        self.refresh_ports_btn.setToolTip("重新扫描可用串口")
        self.refresh_ports_btn.setFixedSize(26, 26)
        self.refresh_ports_btn.clicked.connect(self.ports_refresh_requested.emit)
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.refresh_ports_btn)
        form.addRow("端口", port_row)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        for baud in _BAUDS:
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentText("1000000")
        self.baud_combo.setValidator(QIntValidator(1200, 20_000_000, self))
        self.baud_combo.setToolTip("默认 1000000（与现有脚本一致）")
        form.addRow("波特率", self.baud_combo)

        self.data_bits_combo = QComboBox()
        for value in (8, 7, 6, 5):
            self.data_bits_combo.addItem(str(value), value)
        form.addRow("数据位", self.data_bits_combo)

        self.stop_bits_combo = QComboBox()
        for text, value in (("1", 1.0), ("1.5", 1.5), ("2", 2.0)):
            self.stop_bits_combo.addItem(text, value)
        form.addRow("停止位", self.stop_bits_combo)

        self.parity_combo = QComboBox()
        for text, value in _PARITIES:
            self.parity_combo.addItem(text, value)
        form.addRow("校验位", self.parity_combo)

        self.flow_combo = QComboBox()
        for text, value in _FLOW:
            self.flow_combo.addItem(text, value)
        form.addRow("流控", self.flow_combo)

        connect_row = QHBoxLayout()
        connect_row.setSpacing(6)
        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        self.conn_status = QLabel("● 未连接")
        self.conn_status.setObjectName("statusErr")
        connect_row.addWidget(self.connect_btn, 1)
        connect_row.addWidget(self.conn_status)
        form.addRow(connect_row)
        return group

    def _build_image_group(self) -> QGroupBox:
        group = QGroupBox("图像接收配置")
        form = QFormLayout(group)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.protocol_combo = QComboBox()
        for key, label in PROTOCOLS.items():
            self.protocol_combo.addItem(label, key)
        for key, label in PROTOCOL_FUTURE.items():
            self.protocol_combo.addItem(label, key)
            item = self.protocol_combo.model().item(self.protocol_combo.count() - 1)
            item.setEnabled(False)
        form.addRow("接收协议", self.protocol_combo)

        size_row = QHBoxLayout()
        size_row.setSpacing(4)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 8192)
        self.width_spin.setValue(720)
        self.width_spin.setSuffix(" px")
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, 8192)
        self.height_spin.setValue(240)
        self.height_spin.setSuffix(" px")
        size_row.addWidget(self.width_spin, 1)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_spin, 1)
        form.addRow("分辨率", size_row)

        self.format_combo = QComboBox()
        for key, fmt in PIXEL_FORMATS.items():
            self.format_combo.addItem(fmt.label, key)
        self.format_combo.setToolTip(
            "YUV422 系列为 2 像素/4 字节；RGB565/888 为逐像素；JPEG 尺寸由码流决定"
        )
        form.addRow("像素格式", self.format_combo)

        self.byte_order_combo = QComboBox()
        self.byte_order_combo.addItem("小端 (Little)", "little")
        self.byte_order_combo.addItem("大端 (Big)", "big")
        self.byte_order_combo.setEnabled(False)
        form.addRow("字节序", self.byte_order_combo)

        stride_row = QHBoxLayout()
        stride_row.setSpacing(4)
        self.stride_spin = QSpinBox()
        self.stride_spin.setRange(0, 1_000_000)
        self.stride_spin.setSingleStep(4)
        self.stride_spin.setValue(1440)
        self.auto_stride_check = QCheckBox("自动")
        self.auto_stride_check.setChecked(True)
        self.auto_stride_check.setToolTip("按 宽度 × 每像素字节数 自动计算")
        stride_row.addWidget(self.stride_spin, 1)
        stride_row.addWidget(self.auto_stride_check)
        form.addRow("Stride", stride_row)

        self.buffer_label = QLabel("—")
        self.buffer_label.setObjectName("value")
        self.buffer_label.setToolTip("期望的原始 Buffer 字节数；实际以帧头 total 为准")
        form.addRow("Buffer", self.buffer_label)

        self.groups_combo = QComboBox()
        for value in (4, 6, 8, 10):
            self.groups_combo.addItem(f"{value} 组/行", value)
        self.groups_combo.setCurrentIndex(2)
        self.groups_combo.setToolTip("Hex Viewer 每个显示行包含多少个像素组")
        form.addRow("Hex 分组", self.groups_combo)

        self.image_warning = QLabel("")
        self.image_warning.setObjectName("hint")
        self.image_warning.setWordWrap(True)
        self.image_warning.setStyleSheet(f"color: #ffb74d;")
        form.addRow(self.image_warning)

        self.protocol_combo.currentIndexChanged.connect(self._emit_image_config)
        self.width_spin.valueChanged.connect(self._on_size_changed)
        self.height_spin.valueChanged.connect(self._on_size_changed)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        self.byte_order_combo.currentIndexChanged.connect(self._emit_image_config)
        self.stride_spin.valueChanged.connect(self._on_manual_stride)
        self.auto_stride_check.toggled.connect(self._on_auto_stride_toggled)
        self.groups_combo.currentIndexChanged.connect(self._on_groups_changed)
        return group

    def _build_transfer_group(self) -> QGroupBox:
        group = QGroupBox("采集与传输")
        form = QFormLayout(group)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("分块导出（推荐）", "chunked")
        self.mode_combo.addItem("整场单条命令", "whole")
        self.mode_combo.setToolTip(
            "分块：先执行采集命令，再按 offset/length 逐块导出\n"
            "整场：用一条命令采集并导出整块 Buffer"
        )
        form.addRow("采集方式", self.mode_combo)

        self.prepare_edit = QLineEdit("camera capture 0 1")
        self.prepare_edit.setPlaceholderText("留空表示不执行前置命令")
        self.prepare_edit.setToolTip("分块模式下，先让设备采集一帧（示例命令，按实际设备修改）")
        form.addRow("前置命令", self.prepare_edit)

        self.prepare_wait_spin = QDoubleSpinBox()
        self.prepare_wait_spin.setRange(0.0, 30.0)
        self.prepare_wait_spin.setSingleStep(0.1)
        self.prepare_wait_spin.setValue(1.5)
        self.prepare_wait_spin.setSuffix(" s")
        form.addRow("等待回显", self.prepare_wait_spin)

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1024, 1_000_000)
        self.chunk_spin.setSingleStep(1024)
        self.chunk_spin.setValue(57600)
        self.chunk_spin.setToolTip("默认 57600，是现有脚本实测安全值")
        form.addRow("分块大小", self.chunk_spin)

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(1.0, 300.0)
        self.timeout_spin.setValue(20.0)
        self.timeout_spin.setSuffix(" s")
        form.addRow("块超时", self.timeout_spin)

        self.whole_edit = QLineEdit("camera raw 800")
        self.whole_edit.setPlaceholderText("整场模式命令")
        form.addRow("整场命令", self.whole_edit)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._on_mode_changed()
        return group

    def _build_actions_group(self) -> QGroupBox:
        group = QGroupBox("调试操作")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(6)

        self.capture_btn = QPushButton("Capture Frame")
        self.capture_btn.setObjectName("primary")
        self.capture_btn.setMinimumHeight(34)
        self.capture_btn.setToolTip("请求下位机采集一帧并接收完整原始 Buffer")
        self.capture_btn.clicked.connect(self.capture_requested.emit)
        layout.addWidget(self.capture_btn)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        self.clear_btn = QPushButton("Clear Data")
        self.clear_btn.clicked.connect(self.clear_requested.emit)
        row.addWidget(self.stop_btn)
        row.addWidget(self.clear_btn)
        layout.addLayout(row)
        return group

    def _build_command_group(self) -> QGroupBox:
        group = QGroupBox("串口指令")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(6)

        send_row = QHBoxLayout()
        send_row.setSpacing(4)
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("输入 MSH 命令，回车发送")
        self.command_edit.returnPressed.connect(self._on_send_command)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self._on_send_command)
        send_row.addWidget(self.command_edit, 1)
        send_row.addWidget(self.send_btn)
        layout.addLayout(send_row)

        grid = QGridLayout()
        grid.setSpacing(4)
        self._quick_buttons: list[QPushButton] = []
        for index, (text, command, tip) in enumerate(_QUICK_COMMANDS):
            button = QPushButton(text)
            button.setObjectName("mini")
            button.setToolTip(f"{command}\n{tip}")
            button.clicked.connect(lambda _=False, cmd=command: self.command_requested.emit(cmd))
            grid.addWidget(button, index // 2, index % 2)
            self._quick_buttons.append(button)
        layout.addLayout(grid)
        return group

    def _build_frame_group(self) -> QGroupBox:
        group = QGroupBox("帧历史")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.prev_btn = QPushButton("◀ 上一帧")
        self.prev_btn.setEnabled(False)
        self.prev_btn.clicked.connect(lambda: self.frame_nav_requested.emit(-1))
        self.next_btn = QPushButton("下一帧 ▶")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(lambda: self.frame_nav_requested.emit(1))
        row.addWidget(self.prev_btn)
        row.addWidget(self.next_btn)
        layout.addLayout(row)

        self.frame_label = QLabel("尚无帧")
        self.frame_label.setObjectName("muted")
        layout.addWidget(self.frame_label)

        self.load_btn = QPushButton("加载 .bin 原始 Buffer…")
        self.load_btn.clicked.connect(self.load_raw_requested.emit)
        layout.addWidget(self.load_btn)
        return group

    # ================================================================== #
    # 状态 / 配置读取
    # ================================================================== #
    def serial_config(self) -> SerialConfig:
        try:
            baud = int(self.baud_combo.currentText().strip())
        except ValueError:
            baud = 1000000
        return SerialConfig(
            port=self.port_combo.currentText().strip() or "COM1",
            baudrate=baud,
            bytesize=int(self.data_bits_combo.currentData() or 8),
            stopbits=float(self.stop_bits_combo.currentData() or 1.0),
            parity=str(self.parity_combo.currentData() or "none"),
            flow=str(self.flow_combo.currentData() or "none"),
        )

    def image_config(self) -> ImageConfig:
        stride = 0 if self.auto_stride_check.isChecked() else int(self.stride_spin.value())
        return ImageConfig(
            protocol=str(self.protocol_combo.currentData() or "bt656"),
            width=int(self.width_spin.value()),
            height=int(self.height_spin.value()),
            pixel_format=str(self.format_combo.currentData() or "UYVY"),
            stride=stride,
            byte_order=str(self.byte_order_combo.currentData() or "little"),
            groups_per_row=int(self.groups_combo.currentData() or 8),
        )

    def capture_config(self) -> CaptureConfig:
        chunked = self.mode_combo.currentData() == "chunked"
        return CaptureConfig(
            mode="chunked" if chunked else "whole",
            prepare_command=self.prepare_edit.text().strip() if chunked else "",
            prepare_wait=float(self.prepare_wait_spin.value()),
            chunk_size=int(self.chunk_spin.value()),
            chunk_timeout=float(self.timeout_spin.value()),
            whole_command=self.whole_edit.text().strip() or "camera raw 800",
            whole_timeout=max(30.0, float(self.timeout_spin.value())),
        )

    def set_image_config(self, config: ImageConfig) -> None:
        self._loading = True
        index = self.protocol_combo.findData(config.protocol)
        if index >= 0:
            self.protocol_combo.setCurrentIndex(index)
        index = self.format_combo.findData(config.pixel_format)
        if index >= 0:
            self.format_combo.setCurrentIndex(index)
        self.width_spin.setValue(config.width)
        self.height_spin.setValue(config.height)
        self.auto_stride_check.setChecked(config.stride <= 0)
        self.stride_spin.setValue(config.stride if config.stride > 0 else 0)
        index = self.byte_order_combo.findData(config.byte_order)
        if index >= 0:
            self.byte_order_combo.setCurrentIndex(index)
        index = self.groups_combo.findData(config.groups_per_row)
        if index >= 0:
            self.groups_combo.setCurrentIndex(index)
        self._loading = False
        self.stride_spin.setEnabled(not self.auto_stride_check.isChecked())
        self._refresh_stride()
        self._refresh_derived()

    # ------------------------------------------------------------------ #
    def populate_ports(self, ports: list, keep_text: str | None = None) -> None:
        current = keep_text if keep_text is not None else self.port_combo.currentText().strip()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        for info in ports:
            self.port_combo.addItem(info.device, info.device)
            index = self.port_combo.count() - 1
            description = getattr(info, "description", "")
            if description:
                self.port_combo.setItemData(index, description, Qt.ItemDataRole.ToolTipRole)
        if current:
            if self.port_combo.findText(current) < 0:
                self.port_combo.addItem(current, current)
            self.port_combo.setCurrentText(current)
        elif ports:
            self.port_combo.setCurrentIndex(0)
        self.port_combo.blockSignals(False)

    def set_connected(self, connected: bool, detail: str = "") -> None:
        self._connected = connected
        if connected:
            self.connect_btn.setText("断开")
            self.connect_btn.setObjectName("stop")
            self.conn_status.setText("● 已连接")
            self.conn_status.setObjectName("statusOk")
        else:
            self.connect_btn.setText("连接")
            self.connect_btn.setObjectName("primary")
            self.conn_status.setText("● 未连接")
            self.conn_status.setObjectName("statusErr")
        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)
        self.conn_status.style().unpolish(self.conn_status)
        self.conn_status.style().polish(self.conn_status)
        self._apply_enabled()

    def set_busy(self, busy: bool, phase: str = "", show_progress: bool = True) -> None:
        self._busy = busy
        if busy:
            self.progress.setVisible(show_progress)
            self.progress.setValue(0)
            self.progress.setRange(0, 0 if not show_progress else 100)
            self.status_label.setText(phase or "正在采集…")
        else:
            self.progress.setVisible(False)
        self._apply_enabled()

    def set_progress(self, progress: CaptureProgress) -> None:
        if not self._busy:
            return
        if progress.total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(progress.percent)
        else:
            self.progress.setRange(0, 0)
        if progress.message:
            self.status_label.setText(progress.message)

    def set_status(self, text: str, kind: str = "muted") -> None:
        self.status_label.setText(text)
        if self.status_label.objectName() != kind:
            self.status_label.setObjectName(kind)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)

    def set_frame_nav(self, index: int, total: int) -> None:
        if total <= 0:
            self.frame_label.setText("尚无帧")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return
        self.frame_label.setText(f"Frame {index + 1} / {total}")
        self.prev_btn.setEnabled(index > 0)
        self.next_btn.setEnabled(index < total - 1)

    def _apply_enabled(self) -> None:
        self.connect_btn.setEnabled(not self._busy)
        self.capture_btn.setEnabled(self._connected and not self._busy)
        self.stop_btn.setEnabled(self._busy)
        self.send_btn.setEnabled(self._connected and not self._busy)
        self.command_edit.setEnabled(self._connected and not self._busy)
        self.load_btn.setEnabled(not self._busy)
        self.refresh_ports_btn.setEnabled(not self._busy)
        self.port_combo.setEnabled(not self._connected and not self._busy)
        for button in getattr(self, "_quick_buttons", []):
            button.setEnabled(self._connected and not self._busy)

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
        else:
            self.connect_requested.emit(self.serial_config())

    def _on_send_command(self) -> None:
        command = self.command_edit.text().strip()
        if not command:
            return
        self.command_requested.emit(command)
        self.command_edit.clear()

    # ------------------------------------------------------------------ #
    def _on_mode_changed(self) -> None:
        chunked = self.mode_combo.currentData() == "chunked"
        for widget in (self.prepare_edit, self.prepare_wait_spin, self.chunk_spin):
            widget.setEnabled(chunked)
        self.whole_edit.setEnabled(not chunked)

    def _on_size_changed(self) -> None:
        self._refresh_stride()
        self._refresh_derived()
        self._emit_image_config()

    def _on_format_changed(self) -> None:
        fmt = get_pixel_format(str(self.format_combo.currentData() or "UYVY"))
        self.byte_order_combo.setEnabled(fmt.kind == "rgb565")
        self._refresh_stride()
        self._refresh_derived()
        self._emit_image_config()

    def _on_auto_stride_toggled(self, checked: bool) -> None:
        self.stride_spin.setEnabled(not checked)
        self._refresh_stride()
        self._refresh_derived()
        self._emit_image_config()

    def _on_manual_stride(self) -> None:
        if self.auto_stride_check.isChecked():
            return
        self._refresh_derived()
        self._emit_image_config()

    def _on_groups_changed(self) -> None:
        if self._loading:
            return
        self._emit_image_config()

    def _refresh_stride(self) -> None:
        if not self.auto_stride_check.isChecked():
            return
        config = self.image_config()
        stride = resolve_stride(int(self.width_spin.value()), str(self.format_combo.currentData()))
        self.stride_spin.blockSignals(True)
        self.stride_spin.setValue(stride)
        self.stride_spin.blockSignals(False)
        log.debug("自动 Stride = %d（%dx%s）", stride, self.width_spin.value(), config.pixel_format)

    def _refresh_derived(self) -> None:
        config = self.image_config()
        expected = config.expected_buffer_size
        if expected:
            self.buffer_label.setText(f"{expected:,} Bytes  ({config.effective_stride()} B/line)")
        else:
            self.buffer_label.setText("—（JPEG 由码流决定）")
        warnings = config.validate()
        if warnings:
            self.image_warning.setText("⚠ " + "\n⚠ ".join(warnings))
        else:
            self.image_warning.setText("")

    def _emit_image_config(self) -> None:
        if self._loading:
            return
        self._refresh_derived()
        self.image_config_changed.emit(self.image_config())
