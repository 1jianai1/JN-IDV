"""主窗口：左侧配置栏 + 右侧 Image / Hex / Log 三页，数据全部来自真实采集。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core import APP_NAME, __version__
from core.config import ImageConfig, SerialConfig
from core.device_template import build_device_example_code, device_code_filename
from core.export.exporter import (
    ExportError,
    export_session,
    write_hex_text,
    write_jpeg,
    write_metadata,
    write_png,
    write_raw_buffer,
)
from core.hex.formatter import StructuredData
from core.image.frame import Frame
from core.log import add_handler, get_logger, remove_handler, setup_logging, shutdown_logging
from core.pipeline import build_structured
from core.serial.transport import SerialTransport, list_serial_ports
from gui.log_bridge import LogBridge, QtLogHandler
from gui.panels.config_panel import ConfigPanel
from gui.panels.export_menu import ExportMenu
from gui.panels.hex_viewer import HexViewer
from gui.panels.image_viewer import ImageViewer
from gui.panels.log_viewer import LogViewer
from gui.theme import COLORS
from gui.workers.capture_worker import BuildFrameWorker, CaptureWorker, CommandWorker

log = get_logger("gui")

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT_DIR = PROJECT_DIR / "out"
MAX_FRAMES = 24


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — 通用摄像头图像调试上位机")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)

        self.transport: SerialTransport | None = None
        self.frames: list[Frame] = []
        self.current_index = -1
        self._next_frame_index = 0
        self._structured_cache: dict[tuple[int, int], StructuredData] = {}
        self._image_config = ImageConfig()
        self._busy = False
        self._capture_worker: CaptureWorker | None = None
        self._command_worker: CommandWorker | None = None
        self._load_worker: BuildFrameWorker | None = None
        self._last_dir = str(DEFAULT_EXPORT_DIR)
        self._settings = QSettings("CameraTools", "CameraImageDebugViewer")

        self._setup_logging()
        self._build_ui()
        self._wire_signals()
        self._load_settings()

        QTimer.singleShot(0, self._refresh_ports)
        log.info("%s v%s 启动", APP_NAME, __version__)

    # ================================================================== #
    # 初始化
    # ================================================================== #
    def _setup_logging(self) -> None:
        try:
            setup_logging(
                log_dir=PROJECT_DIR / "logs",
                level=logging.DEBUG,
                console=False,
                file=True,
            )
        except OSError as exc:
            log.warning("日志文件不可用（%s），仅保留界面日志", exc)
            setup_logging(level=logging.DEBUG, console=False, file=False)
        self.log_bridge = LogBridge()
        self.log_handler = QtLogHandler(self.log_bridge, level=logging.DEBUG)
        add_handler(self.log_handler)

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        outer.addWidget(self.splitter)

        self.config_panel = ConfigPanel()
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(self.config_panel)
        left_scroll.setFixedWidth(368)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.splitter.addWidget(left_scroll)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        self.tabs.setMovable(False)
        self.image_viewer = ImageViewer()
        self.hex_viewer = HexViewer()
        self.log_viewer = LogViewer()
        self.log_viewer.attach(self.log_bridge)
        self.tabs.addTab(self.hex_viewer, "Hex Viewer")
        self.tabs.addTab(self.log_viewer, "Log")

        self.export_menu = ExportMenu()
        self.image_viewer.set_corner_widget(self.export_menu)

        # 右上区域上下分布：上半部分单独 Image Preview，下半部分 Hex Viewer / Log
        self.data_splitter = QSplitter(Qt.Orientation.Vertical)
        self.data_splitter.setChildrenCollapsible(False)
        self.data_splitter.addWidget(self.image_viewer)
        self.data_splitter.addWidget(self.tabs)
        self.data_splitter.setStretchFactor(0, 2)
        self.data_splitter.setStretchFactor(1, 3)
        self.data_splitter.setSizes([420, 490])
        self.image_viewer.setMinimumHeight(240)
        self.tabs.setMinimumHeight(220)
        self.splitter.addWidget(self.data_splitter)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([368, 1072])
        self.setCentralWidget(central)

        status = self.statusBar()
        self.status_conn = QLabel("● 未连接")
        self.status_conn.setStyleSheet(f"color: {COLORS['error']};")
        self.status_frame = QLabel("No frame")
        self.status_hover = QLabel("—")
        self.status_hover.setStyleSheet("font-family: Consolas;")
        status.addWidget(self.status_conn)
        status.addWidget(self.status_frame, 1)
        status.addPermanentWidget(self.status_hover)

        self.setAcceptDrops(True)

    def _wire_signals(self) -> None:
        panel = self.config_panel
        panel.connect_requested.connect(self._connect_device)
        panel.disconnect_requested.connect(self._disconnect_device)
        panel.ports_refresh_requested.connect(self._refresh_ports)
        panel.capture_requested.connect(self._capture)
        panel.stop_requested.connect(self._stop)
        panel.clear_requested.connect(self._clear_data)
        panel.command_requested.connect(self._send_command)
        panel.image_config_changed.connect(self._on_image_config_changed)
        panel.load_raw_requested.connect(self._load_raw_dialog)
        panel.frame_nav_requested.connect(self._navigate)

        self.image_viewer.hover_changed.connect(self._on_image_hover)
        self.image_viewer.clicked.connect(self._on_image_clicked)
        self.hex_viewer.focus_changed.connect(self._on_hex_focus)
        self.hex_viewer.selection_changed.connect(self._on_hex_focus)
        self.tabs.currentChanged.connect(self._on_bottom_tab_changed)

        menu = self.export_menu
        menu.save_png.connect(self._save_png)
        menu.save_jpg.connect(self._save_jpg)
        menu.export_raw.connect(self._export_raw)
        menu.export_hex.connect(self._export_hex)
        menu.export_metadata.connect(self._export_metadata)
        menu.save_session.connect(self._save_session)
        menu.copy_device_code.connect(self._copy_device_code)
        menu.save_device_c.connect(lambda: self._save_device_code("c"))
        menu.save_device_h.connect(lambda: self._save_device_code("h"))
        menu.load_raw.connect(self._load_raw_dialog)

    def _refresh_ports(self) -> None:
        try:
            ports = list_serial_ports()
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描串口失败：%s", exc)
            self.config_panel.set_status(f"扫描串口失败：{exc}", "statusWarn")
            return
        self.config_panel.populate_ports(ports)
        if not ports:
            log.info("未发现可用串口（可手动输入 COMx）")
        else:
            log.debug("发现串口：%s", ", ".join(port.device for port in ports))

    # ================================================================== #
    # 连接
    # ================================================================== #
    def _connect_device(self, config: SerialConfig) -> None:
        if self._busy:
            return
        if not config.port:
            QMessageBox.warning(self, "连接失败", "请选择或输入串口端口号。")
            return
        transport = SerialTransport(config)
        try:
            transport.open()
        except Exception as exc:  # noqa: BLE001
            log.error("串口连接失败：%s", exc)
            self.config_panel.set_connected(False, f"连接失败：{exc}")
            self.config_panel.set_status(f"连接失败：{exc}", "statusErr")
            self.status_conn.setText("● 连接失败")
            self.status_conn.setStyleSheet(f"color: {COLORS['error']};")
            QMessageBox.critical(
                self,
                "串口连接失败",
                f"端口：{config.port}\n波特率：{config.baudrate}\n\n{exc}\n\n"
                "请检查：端口是否被其他终端占用 / 板子是否在线 / 波特率是否正确。",
            )
            return
        self.transport = transport
        self.config_panel.set_connected(True, f"已连接 {config.display()}")
        self.config_panel.set_status(f"已连接 {config.display()}", "statusOk")
        self.status_conn.setText(f"● {config.port}")
        self.status_conn.setStyleSheet(f"color: {COLORS['ok']};")
        log.info("串口连接成功：%s", config.display())

    def _disconnect_device(self) -> None:
        if self._busy:
            QMessageBox.information(self, "正在忙", "采集/命令执行中，请先点击 Stop。")
            return
        if self.transport is not None:
            log.info("主动断开串口")
            self.transport.close()
            self.transport = None
        self.config_panel.set_connected(False, "已断开")
        self.config_panel.set_status("串口已断开", "muted")
        self.status_conn.setText("● 未连接")
        self.status_conn.setStyleSheet(f"color: {COLORS['error']};")

    # ================================================================== #
    # 采集
    # ================================================================== #
    def _capture(self) -> None:
        if self._busy:
            return
        if self.transport is None or not self.transport.is_open:
            QMessageBox.warning(self, "未连接", "请先连接串口，再点击 Capture Frame。")
            return
        image_config = self.config_panel.image_config()
        capture_config = self.config_panel.capture_config()
        groups = image_config.groups_per_row
        for warning in image_config.validate() + capture_config.validate():
            log.warning("配置告警：%s", warning)

        self._image_config = image_config
        self._set_busy(True, "准备采集…")
        worker = CaptureWorker(
            self.transport,
            image_config,
            capture_config,
            frame_index=self._next_frame_index,
            groups_per_row=groups,
            parent=self,
        )
        self._capture_worker = worker
        worker.progress.connect(self.config_panel.set_progress)
        worker.device_line.connect(self._on_device_line)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.failed.connect(self._on_worker_failed)
        worker.cancelled.connect(self._on_capture_cancelled)
        worker.finished.connect(self._on_capture_finished)
        worker.start()

    def _stop(self) -> None:
        if self._capture_worker is not None and self._capture_worker.isRunning():
            log.info("用户请求停止采集")
            self.config_panel.set_status("正在停止…", "statusWarn")
            self._capture_worker.cancel()
        elif self._command_worker is not None and self._command_worker.isRunning():
            self._command_worker.cancel()
        elif self._load_worker is not None and self._load_worker.isRunning():
            self.config_panel.set_status("正在停止加载…", "statusWarn")

    def _on_device_line(self, line: str) -> None:
        if line:
            self.config_panel.set_status(line[:160], "muted")

    def _on_frame_ready(self, payload) -> None:
        frame, structured = payload
        self.frames.append(frame)
        while len(self.frames) > MAX_FRAMES:
            removed = self.frames.pop(0)
            for key in [key for key in self._structured_cache if key[0] == removed.frame_index]:
                self._structured_cache.pop(key, None)
        self._next_frame_index += 1
        self.current_index = len(self.frames) - 1
        groups = frame.config.groups_per_row if frame.config else 8
        self._cache_structured((frame.frame_index, groups), structured)
        self._display_current()
        self.config_panel.set_status(
            f"采集完成：{frame.width}x{frame.height}，原始 {frame.raw_size:,} 字节，"
            f"解析 {frame.metadata.get('build_ms', 0):.1f} ms",
            "statusOk",
        )
        self.status_frame.setText(
            f"Frame #{frame.frame_index}  {frame.width}x{frame.height}  "
            f"{frame.raw_size:,} B  {frame.pixel_format}"
        )

    def _on_capture_cancelled(self) -> None:
        self.config_panel.set_status("采集已停止", "statusWarn")

    def _on_capture_finished(self) -> None:
        self._capture_worker = None
        self._set_busy(False)
        if self.transport is not None and self.transport.is_open:
            self.config_panel.set_connected(True, "已连接")
        else:
            self.config_panel.set_connected(False, "已断开")

    def _on_worker_failed(self, message: str) -> None:
        self.config_panel.set_status(f"失败：{message}", "statusErr")
        QMessageBox.warning(self, "操作失败", message)

    # ================================================================== #
    # 手动命令
    # ================================================================== #
    def _send_command(self, command: str) -> None:
        if self._busy:
            return
        if self.transport is None or not self.transport.is_open:
            QMessageBox.warning(self, "未连接", "请先连接串口。")
            return
        self._set_busy(True, f"发送命令：{command}", show_progress=False)
        worker = CommandWorker(self.transport, [command], wait=0.8, parent=self)
        self._command_worker = worker
        worker.line.connect(self._on_device_line)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_command_finished)
        worker.start()

    def _on_command_finished(self) -> None:
        self._command_worker = None
        self._set_busy(False)
        if self.transport is not None and self.transport.is_open:
            self.config_panel.set_connected(True, "已连接")
            self.config_panel.set_status("命令已发送", "muted")
        else:
            self.config_panel.set_connected(False, "已断开")

    # ================================================================== #
    # 离线加载
    # ================================================================== #
    def _load_raw_dialog(self) -> None:
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载原始 Buffer",
            self._last_dir,
            "Raw buffer (*.bin *.raw *.dat);;All files (*)",
        )
        if not path:
            return
        self._last_dir = str(Path(path).parent)
        self._load_raw_file(path)

    def _load_raw_file(self, path: str) -> None:
        if self._busy:
            return
        image_config = self.config_panel.image_config()
        self._image_config = image_config
        self._set_busy(True, f"加载 {Path(path).name}…", show_progress=False)
        worker = BuildFrameWorker(
            path,
            image_config,
            frame_index=self._next_frame_index,
            groups_per_row=image_config.groups_per_row,
            parent=self,
        )
        self._load_worker = worker
        worker.frame_ready.connect(self._on_frame_ready)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_load_finished)
        worker.start()

    def _on_load_finished(self) -> None:
        self._load_worker = None
        self._set_busy(False)
        if self.transport is not None and self.transport.is_open:
            self.config_panel.set_connected(True, "已连接")
        else:
            self.config_panel.set_connected(False, "已断开")

    # ================================================================== #
    # 帧显示
    # ================================================================== #
    def _current_frame(self) -> Frame | None:
        if 0 <= self.current_index < len(self.frames):
            return self.frames[self.current_index]
        return None

    def _current_structured(self) -> StructuredData | None:
        frame = self._current_frame()
        if frame is None:
            return None
        groups = self._image_config.groups_per_row
        cached = self._structured_cache.get((frame.frame_index, groups))
        if cached is None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                cached = build_structured(frame, groups)
            finally:
                QApplication.restoreOverrideCursor()
            self._cache_structured((frame.frame_index, groups), cached)
        return cached

    def _cache_structured(self, key: tuple[int, int], structured: StructuredData) -> None:
        """限制结构化缓存数量，避免反复切换分组导致内存增长。"""
        self._structured_cache.pop(key, None)
        self._structured_cache[key] = structured
        while len(self._structured_cache) > 8:
            oldest = next(iter(self._structured_cache))
            self._structured_cache.pop(oldest, None)

    def _display_current(self) -> None:
        frame = self._current_frame()
        if frame is None:
            self.image_viewer.set_frame(None)
            self.hex_viewer.set_frame(None, None)
            self.config_panel.set_frame_nav(0, 0)
            self.export_menu.set_has_frame(False)
            self.status_frame.setText("No frame")
            return
        structured = self._current_structured()
        self.image_viewer.set_frame(frame)
        self.hex_viewer.set_frame(frame, structured)
        self.status_hover.setText("—")
        self.config_panel.set_frame_nav(self.current_index, len(self.frames))
        self.export_menu.set_has_frame(True)
        self.setWindowTitle(
            f"{APP_NAME} — Frame #{frame.frame_index}  {frame.width}x{frame.height} "
            f"{frame.pixel_format}"
        )
        self.status_frame.setText(
            f"Frame #{frame.frame_index}  {frame.width}x{frame.height}  "
            f"{frame.raw_size:,} B  {frame.pixel_format}  {frame.layout.mode_label if frame.layout else ''}"
        )

    def _navigate(self, delta: int) -> None:
        if not self.frames:
            return
        index = max(0, min(len(self.frames) - 1, self.current_index + delta))
        if index == self.current_index:
            return
        self.current_index = index
        self._display_current()

    def _clear_data(self) -> None:
        self.frames.clear()
        self._structured_cache.clear()
        self.current_index = -1
        self.image_viewer.set_frame(None)
        self.hex_viewer.set_frame(None, None)
        self.config_panel.set_frame_nav(0, 0)
        self.config_panel.set_status("已清空帧数据（日志与文件记录保留）", "muted")
        self.export_menu.set_has_frame(False)
        self.status_frame.setText("No frame")
        self.status_hover.setText("—")
        log.info("清空当前帧数据")

    # ================================================================== #
    # 联调：Image <-> Hex
    # ================================================================== #
    def _on_image_hover(self, focus) -> None:
        if focus is not None:
            self.hex_viewer.set_external_focus(focus, follow=True)
            self.status_hover.setText(focus.describe())
        else:
            self.hex_viewer.set_external_focus(None)
            self.status_hover.setText("—")

    def _on_image_clicked(self, focus) -> None:
        if focus is not None and focus.offset is not None:
            self.hex_viewer.select_focus(focus)
            self.status_hover.setText(focus.describe())

    def _on_hex_focus(self, focus) -> None:
        self.image_viewer.set_focus(focus)
        self.status_hover.setText(focus.describe() if focus is not None else "—")

    def _on_bottom_tab_changed(self, index: int) -> None:
        """切到 Log 等非 Hex 页面时，清除临时联动高亮。"""
        if self.tabs.widget(index) is self.hex_viewer:
            return
        self.hex_viewer.clear_hover()
        self.image_viewer.set_focus(None)
        self.status_hover.setText("—")

    # ================================================================== #
    # 配置变化
    # ================================================================== #
    def _on_image_config_changed(self, config: ImageConfig) -> None:
        old_groups = self._image_config.groups_per_row
        self._image_config = config
        if config.groups_per_row != old_groups and self._current_frame() is not None:
            frame = self._current_frame()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                structured = build_structured(frame, config.groups_per_row)
            finally:
                QApplication.restoreOverrideCursor()
            self._cache_structured((frame.frame_index, config.groups_per_row), structured)
            self.hex_viewer.set_frame(frame, structured)

    # ================================================================== #
    # 导出
    # ================================================================== #
    def _ensure_frame(self) -> Frame | None:
        frame = self._current_frame()
        if frame is None:
            QMessageBox.information(self, "没有图像", "当前没有可导出的帧，请先采集或加载 .bin。")
        return frame

    def _save_path(self, title: str, default_name: str, filters: str) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self, title, str(Path(self._last_dir) / default_name), filters
        )
        if path:
            self._last_dir = str(Path(path).parent)
        return path

    def _run_export(self, label: str, func) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = func()
        except (ExportError, OSError) as exc:
            log.error("%s失败：%s", label, exc)
            QMessageBox.critical(self, f"{label}失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("%s失败", label)
            QMessageBox.critical(self, f"{label}失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        if result:
            log.info("%s成功：%s", label, result)
            self.config_panel.set_status(f"{label}成功：{result}", "statusOk")

    def _save_png(self) -> None:
        frame = self._ensure_frame()
        if frame is None:
            return
        path = self._save_path(
            "保存 PNG", f"frame_{frame.frame_index:03d}.png", "PNG image (*.png)"
        )
        if path:
            self._run_export("保存 PNG", lambda: write_png(path, frame.image))

    def _save_jpg(self) -> None:
        frame = self._ensure_frame()
        if frame is None:
            return
        path = self._save_path(
            "保存 JPG", f"frame_{frame.frame_index:03d}.jpg", "JPEG image (*.jpg *.jpeg)"
        )
        if path:
            self._run_export("保存 JPG", lambda: write_jpeg(path, frame.image))

    def _export_raw(self) -> None:
        frame = self._ensure_frame()
        if frame is None:
            return
        path = self._save_path(
            "导出原始 Buffer", f"frame_{frame.frame_index:03d}.bin",
            "Raw buffer (*.bin);;All files (*)",
        )
        if path:
            self._run_export("导出原始 Buffer", lambda: write_raw_buffer(path, frame))

    def _export_hex(self) -> None:
        frame = self._ensure_frame()
        if frame is None:
            return
        path = self._save_path(
            "导出结构化 Hex", f"frame_{frame.frame_index:03d}_hex.txt",
            "Text (*.txt);;All files (*)",
        )
        if path:
            structured = self._current_structured()
            self._run_export(
                "导出结构化 Hex", lambda: write_hex_text(path, frame, structured)
            )

    def _export_metadata(self) -> None:
        frame = self._ensure_frame()
        if frame is None:
            return
        path = self._save_path(
            "导出帧元数据", f"frame_{frame.frame_index:03d}.json",
            "JSON (*.json);;All files (*)",
        )
        if path:
            self._run_export("导出帧元数据", lambda: write_metadata(path, frame))

    def _save_session(self) -> None:
        frame = self._ensure_frame()
        if frame is None:
            return
        directory = QFileDialog.getExistingDirectory(
            self, "选择调试会话保存目录", self._last_dir
        )
        if not directory:
            return
        self._last_dir = directory
        structured = self._current_structured()
        code = self._device_code("c")
        log_text = self.log_viewer.log_text()
        self._run_export(
            "保存调试会话",
            lambda: export_session(directory, frame, structured, log_text, code)[0],
        )

    def _device_code(self, kind: str) -> str:
        return build_device_example_code(
            self.config_panel.image_config(),
            self.config_panel.serial_config(),
            self.config_panel.capture_config(),
            kind=kind,
        )

    def _copy_device_code(self) -> None:
        code = self._device_code("c")
        QGuiApplication.clipboard().setText(code)
        log.info("已复制下位机上传示例代码（%d 行）", code.count("\n") + 1)
        self.config_panel.set_status("下位机上传示例代码已复制到剪贴板", "statusOk")
        self.statusBar().showMessage("C 源码已复制，可直接粘贴到下位机工程", 4000)

    def _save_device_code(self, kind: str) -> None:
        default_name = device_code_filename(self.config_panel.image_config(), kind)
        filter_text = "C source (*.c)" if kind == "c" else "C header (*.h)"
        path = self._save_path("导出下位机示例代码", default_name, filter_text)
        if not path:
            return
        suffix = Path(path).suffix.lower()
        if suffix in (".c", ".h"):
            kind = "h" if suffix == ".h" else "c"
        else:
            path += ".c" if kind == "c" else ".h"
        code = self._device_code(kind)
        self._run_export(
            "导出下位机示例代码",
            lambda: _write_text(path, code),
        )

    # ================================================================== #
    # 状态 / 设置 / 退出
    # ================================================================== #
    def _set_busy(self, busy: bool, phase: str = "", show_progress: bool = True) -> None:
        self._busy = busy
        self.config_panel.set_busy(busy, phase, show_progress=show_progress)
        self.export_menu.set_busy(busy)
        if busy:
            self.statusBar().showMessage(phase, 0)
        else:
            self.statusBar().clearMessage()

    # ------------------------------------------------------------------ #
    def _load_settings(self) -> None:
        settings = self._settings
        port = str(settings.value("serial/port", "COM12"))
        baud = str(settings.value("serial/baud", "1000000"))
        self.config_panel.port_combo.setCurrentText(port)
        self.config_panel.baud_combo.setCurrentText(baud)
        self.config_panel.populate_ports([], keep_text=port)

        image = ImageConfig(
            width=int(settings.value("image/width", 720)),
            height=int(settings.value("image/height", 240)),
            pixel_format=str(settings.value("image/format", "UYVY")),
            stride=int(settings.value("image/stride", 0)),
            byte_order=str(settings.value("image/byte_order", "little")),
            groups_per_row=int(settings.value("image/groups", 8)),
        )
        self.config_panel.set_image_config(image)
        self._image_config = image

        mode = str(settings.value("capture/mode", "chunked"))
        index = self.config_panel.mode_combo.findData(mode)
        if index >= 0:
            self.config_panel.mode_combo.setCurrentIndex(index)
        self.config_panel.prepare_edit.setText(
            str(settings.value("capture/prepare", "camera capture 0 1"))
        )
        self.config_panel.prepare_wait_spin.setValue(
            float(settings.value("capture/prepare_wait", 1.5))
        )
        self.config_panel.chunk_spin.setValue(int(settings.value("capture/chunk", 57600)))
        self.config_panel.timeout_spin.setValue(
            float(settings.value("capture/timeout", 20.0))
        )
        self.config_panel.whole_edit.setText(
            str(settings.value("capture/whole", "camera raw 800"))
        )
        self._last_dir = str(settings.value("export/dir", DEFAULT_EXPORT_DIR))
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_state = settings.value("window/splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        data_splitter_state = settings.value("window/data_splitter")
        if data_splitter_state is not None:
            self.data_splitter.restoreState(data_splitter_state)

    def _save_settings(self) -> None:
        settings = self._settings
        serial_config = self.config_panel.serial_config()
        settings.setValue("serial/port", serial_config.port)
        settings.setValue("serial/baud", serial_config.baudrate)
        image = self.config_panel.image_config()
        settings.setValue("image/width", image.width)
        settings.setValue("image/height", image.height)
        settings.setValue("image/format", image.pixel_format)
        settings.setValue(
            "image/stride",
            0 if self.config_panel.auto_stride_check.isChecked() else image.stride,
        )
        settings.setValue("image/byte_order", image.byte_order)
        settings.setValue("image/groups", image.groups_per_row)
        settings.setValue("capture/mode", self.config_panel.mode_combo.currentData())
        settings.setValue("capture/prepare", self.config_panel.prepare_edit.text())
        settings.setValue("capture/prepare_wait", self.config_panel.prepare_wait_spin.value())
        settings.setValue("capture/chunk", self.config_panel.chunk_spin.value())
        settings.setValue("capture/timeout", self.config_panel.timeout_spin.value())
        settings.setValue("capture/whole", self.config_panel.whole_edit.text())
        settings.setValue("export/dir", self._last_dir)
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/splitter", self.splitter.saveState())
        settings.setValue("window/data_splitter", self.data_splitter.saveState())

    # ------------------------------------------------------------------ #
    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls and urls[0].toLocalFile().lower().endswith((".bin", ".raw", ".dat")):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path:
                self._load_raw_file(path)
                event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        if self._busy:
            answer = QMessageBox.question(
                self,
                "正在采集",
                "当前还有采集/命令任务，确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for worker in (self._capture_worker, self._command_worker, self._load_worker):
                if worker is not None and worker.isRunning():
                    if hasattr(worker, "cancel"):
                        worker.cancel()
                    worker.wait(3000)
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self._save_settings()
        remove_handler(self.log_handler)
        log.info("退出 %s", APP_NAME)
        shutdown_logging()
        super().closeEvent(event)


def _write_text(path: str, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
