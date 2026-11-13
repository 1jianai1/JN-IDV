#!/usr/bin/env python3
"""GUI 冒烟测试：离屏启动主窗口，载入合成帧，检查联动并输出截图。

用途：
  * CI / Agent 验证 GUI 能启动、能渲染、坐标联动不抛异常；
  * 生成 out/gui_*.png 供人工检查界面布局。

    python tests/gui_smoke.py [--out out] [--samples]

注意：合成帧只用于验证渲染与映射，不会被标记成设备采集结果。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# 默认离屏（无桌面/CI 安全）；--native 时使用系统平台插件（可加载系统字体，便于人工看截图）
if "--native" not in sys.argv and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import APP_NAME  # noqa: E402
from core.config import ImageConfig  # noqa: E402
from core.log import get_logger  # noqa: E402
from core.pipeline import build_frame, build_structured  # noqa: E402
from core.protocol.uart_frames import build_packet  # noqa: E402
from gui.focus import ViewFocus  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.theme import apply_theme  # noqa: E402
from tests.synthetic import make_uyvy  # noqa: E402

log = get_logger("gui.smoke")


class _FakeTransport:
    """最小串口替身：GUI 采集线程端到端测试用。"""

    def __init__(self, payload: bytes) -> None:
        self.is_open = True
        self.config = SimpleNamespace(display=lambda: "FAKE @ 1000000")
        self._rx = bytearray()
        self._payload = payload
        self.writes: list[str] = []

    def write_line(self, command: str) -> None:
        self.writes.append(command)
        if command.startswith("camera capture"):
            self._rx += b"[show] ok\r\n"
        elif command.startswith("camera dump"):
            parts = command.split()
            offset = int(parts[2])
            length = min(int(parts[3]), len(self._payload) - offset)
            self._rx += b"[uart] begin\r\n" + build_packet(
                self._payload[offset : offset + length],
                total=len(self._payload),
                offset=offset,
                length=length,
                flags=1,
            )

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def read(self, size: int = 8192) -> bytes:
        if self._rx:
            chunk = bytes(self._rx[:size])
            del self._rx[:size]
            return chunk
        time.sleep(0.001)
        return b""

    def drain_text(self, wait: float) -> str:
        text = bytes(self._rx).decode("latin-1", errors="replace")
        self._rx.clear()
        return text

    def close(self) -> None:
        self.is_open = False


def _install_frame(window: MainWindow, payload: bytes, index: int) -> tuple:
    image = ImageConfig(width=720, height=240, pixel_format="UYVY", stride=1440)
    frame = build_frame(
        payload,
        image,
        header={"total": len(payload), "offset": 0, "length": len(payload)},
        frame_index=index,
        source="demo",
    )
    structured = build_structured(frame, image.groups_per_row)
    window._on_frame_ready((frame, structured))
    return frame, structured


def _pixel_row(structured, image_row: int, group: int):
    for index, row in enumerate(structured.rows):
        if row.kind == "pixels" and row.image_row == image_row:
            if row.group_start is not None and row.group_start <= group < row.group_start + row.groups:
                return index, row
    return None, None


def run(out_dir: Path, write_samples: bool, size: tuple[int, int] = (1500, 940)) -> int:
    app = QApplication([])
    app.setApplicationName(APP_NAME)
    apply_theme(app)
    QSettings("CameraTools", "CameraImageDebugViewer").clear()  # 每次从默认配置开始

    window = MainWindow()
    window.move(-8000, -8000)
    window.resize(*size)
    window.show()
    app.processEvents()
    assert window.tabs.count() == 2, "下部分应为 Hex Viewer / Log 两个页签"
    assert window.tabs.tabText(0) == "Hex Viewer"
    assert window.tabs.tabText(1) == "Log"

    payload = make_uyvy(720, 240, with_codes=True)
    frame, structured = _install_frame(window, payload, 0)
    app.processEvents()
    assert window.image_viewer._qimage is not None, "image viewer has no QImage"
    assert structured.rows, "structured rows empty"
    log.info("同步测试：帧 %dx%d，原始 %d 字节", frame.width, frame.height, frame.raw_size)

    # -------- Image Preview：像素级联动 --------
    offset = frame.location_to_offset(320, 120)
    assert offset is not None
    location = frame.offset_to_location(offset)
    assert location.x == 320 and location.y == 120, (location.x, location.y)
    pixel_focus = ViewFocus(
        kind="group",
        y=120,
        x=320,
        offset=offset,
        line_index=location.line_index,
        group=location.pixel_group_index,
        group_span=2,
    )
    window.image_viewer.set_focus(pixel_focus)
    window.image_viewer._anim = 1.0
    window.hex_viewer.set_external_focus(pixel_focus, follow=True)
    window.status_hover.setText(pixel_focus.describe())
    window.tabs.setCurrentWidget(window.hex_viewer)
    app.processEvents()
    window.grab().save(str(out_dir / "gui_image_group.png"))

    # -------- Image Preview：行级联动 --------
    row_focus = ViewFocus(kind="row", y=120, line_index=location.line_index, label="LINE 120")
    window.image_viewer.set_focus(row_focus)
    window.image_viewer._anim = 1.0
    app.processEvents()
    window.grab().save(str(out_dir / "gui_image_row.png"))

    # -------- Hex Viewer：定位到同一像素组 --------
    row_index, _ = _pixel_row(structured, 120, 160)
    assert row_index is not None
    window.hex_viewer.hover_row = row_index
    window.hex_viewer.external_row = row_index
    window.hex_viewer.hover_focus = pixel_focus
    window.hex_viewer.set_external_focus(pixel_focus, follow=True)
    window.hex_viewer.ensure_visible(row_index)
    window.hex_viewer.canvas.update()
    window.tabs.setCurrentWidget(window.hex_viewer)
    app.processEvents()
    window.grab().save(str(out_dir / "gui_hex.png"))

    # -------- Log --------
    for level in ("info", "warning"):
        getattr(log, level)("日志渲染测试：%s", level)
    window.tabs.setCurrentWidget(window.log_viewer)
    app.processEvents()
    window.grab().save(str(out_dir / "gui_log.png"))

    # -------- 第二帧：无定时码（uniform 布局），验证切换 --------
    plain = make_uyvy(720, 240)
    _install_frame(window, plain, 1)
    window.tabs.setCurrentWidget(window.hex_viewer)
    app.processEvents()
    assert window.hex_viewer.structured is not None
    assert window.hex_viewer.structured.rows
    window.grab().save(str(out_dir / "gui_offline.png"))

    # -------- 端到端：GUI CaptureWorker 走完整采集链路（FakeTransport） --------
    transport = _FakeTransport(payload)
    window.transport = transport
    window.config_panel.set_connected(True)
    window.config_panel.prepare_wait_spin.setValue(0.05)
    window.config_panel.timeout_spin.setValue(2.0)
    before = len(window.frames)
    window._capture()
    deadline = time.time() + 15
    while time.time() < deadline:
        app.processEvents()
        if len(window.frames) > before and not window._busy:
            break
        time.sleep(0.01)
    app.processEvents()
    assert len(window.frames) == before + 1, "GUI 采集未产生新帧"
    assert not window._busy, "采集完成后仍处于 busy 状态"
    assert window._current_frame() is not None
    log.info("GUI 采集链路测试通过：总帧数 %d", len(window.frames))

    if write_samples:
        (out_dir / "sample_uyvy_720x240.bin").write_bytes(plain)
        (out_dir / "sample_uyvy_720x240_codes.bin").write_bytes(payload)

    window.close()
    app.processEvents()
    print("GUI smoke OK")
    print(f"截图输出目录：{out_dir}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(PROJECT_DIR / "out"))
    parser.add_argument(
        "--size",
        default="1500x940",
        help="截图窗口逻辑尺寸，格式 WxH（默认 1500x940）",
    )
    parser.add_argument("--samples", action="store_true", help="同时写出合成 .bin 示例")
    parser.add_argument(
        "--native",
        action="store_true",
        help="使用系统 GUI 平台插件（窗口移到屏幕外），可渲染系统字体",
    )
    args = parser.parse_args(argv)
    try:
        width, height = (int(part) for part in args.size.lower().split("x"))
    except ValueError:
        parser.error(f"--size 需要 WxH 格式，例如 1500x940（收到 {args.size!r}）")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    return run(out_dir, args.samples, (width, height))


if __name__ == "__main__":
    sys.exit(main())
