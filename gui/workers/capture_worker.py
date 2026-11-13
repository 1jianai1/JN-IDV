"""后台线程：采集、离线文件解析、手动命令。

GUI 主线程只更新界面；串口 I/O、BT.656 解析、图像解码、结构化 Hex 组织
全部在这些 QThread 里完成。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.acquisition.capture import (
    CaptureCancelled,
    CaptureEngine,
    CaptureProgress,
    send_commands,
)
from core.config import CaptureConfig, ImageConfig
from core.log import get_logger
from core.pipeline import build_frame, build_structured, load_raw_file
from core.serial.transport import SerialTransport

log = get_logger("gui.worker")


class CaptureWorker(QThread):
    progress = Signal(object)          # CaptureProgress
    device_line = Signal(str)
    frame_ready = Signal(object)       # (Frame, StructuredData)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        transport: SerialTransport,
        image: ImageConfig,
        capture: CaptureConfig,
        frame_index: int,
        groups_per_row: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._image = image
        self._capture = capture
        self._frame_index = frame_index
        self._groups_per_row = groups_per_row
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_cancelling(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            log.info(
                "开始采集：%s，%dx%d %s stride=%d，模式=%s",
                self._transport.config.display(),
                self._image.width,
                self._image.height,
                self._image.pixel_format,
                self._image.effective_stride(),
                self._capture.mode,
            )
            engine = CaptureEngine(self._transport, self._image, self._capture)
            result = engine.run(
                cancel=self._cancel,
                on_progress=self._emit_progress,
                on_line=self.device_line.emit,
            )
            if self._cancel.is_set():
                raise CaptureCancelled("采集已停止")

            frame = build_frame(
                result.payload,
                self._image,
                header=result.header.to_dict(),
                frame_index=self._frame_index,
                source="live",
                extra_metadata={"capture": result.stats},
            )
            structured = build_structured(frame, self._groups_per_row)
            self.frame_ready.emit((frame, structured))
        except CaptureCancelled:
            log.warning("采集已取消")
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 需要把真实原因带回 GUI
            log.exception("采集失败")
            self.failed.emit(str(exc))

    def _emit_progress(self, progress: CaptureProgress) -> None:
        self.progress.emit(progress)


class BuildFrameWorker(QThread):
    """离线加载 .bin，解码 + 结构化，整个过程不阻塞 GUI。"""

    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        path: str,
        image: ImageConfig,
        frame_index: int,
        groups_per_row: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._image = image
        self._frame_index = frame_index
        self._groups_per_row = groups_per_row

    def run(self) -> None:
        try:
            log.info("加载离线 Buffer：%s", self._path)
            path = Path(self._path)
            if not path.is_file():
                raise FileNotFoundError(f"文件不存在：{path}")
            frame = load_raw_file(path, self._image, frame_index=self._frame_index)
            structured = build_structured(frame, self._groups_per_row)
            frame.metadata.setdefault("source_file", str(path))
            self.frame_ready.emit((frame, structured))
        except Exception as exc:  # noqa: BLE001
            log.exception("加载离线 Buffer 失败")
            self.failed.emit(str(exc))


class CommandWorker(QThread):
    """手动命令 / 快捷命令：发送并收集回显。"""

    line = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        transport: SerialTransport,
        commands: list[str],
        wait: float = 0.8,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._commands = list(commands)
        self._wait = wait
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            send_commands(
                self._transport,
                self._commands,
                wait=self._wait,
                cancel=self._cancel,
                on_line=self.line.emit,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("命令发送失败")
            self.failed.emit(str(exc))
