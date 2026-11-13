"""图像采集引擎：下发命令 -> 分段接收 -> 用协议判断完整帧。

流程与现有命令行脚本完全一致，只是被封装成可取消、可回调的对象：

    prepare 命令（可选，例如 camera capture 0 1）
        -> camera dump <off> <len>（分块，默认 57600 字节/块）
        -> 每块都是一个完整「64B 头 + payload + 4B 尾」的协议帧
        -> 按 header.total / header.offset 拼回整场 Buffer

既不做图像解析，也不碰 UI。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.config import CaptureConfig, ImageConfig
from core.log import get_logger
from core.protocol.uart_frames import FrameHeader, FramePacket, FrameParser, SYNC
from core.protocol.base import CaptureProtocol, ProtocolError, get_protocol
from core.serial.transport import SerialTransport

log = get_logger("capture")

DEVICE_ERROR_PATTERNS = (
    "capture frame failed",
    "no complete frame",
    "no buffer",
    "send failed",
    "capture failed",
    "bad range",
    "unknown subcommand",
    "command not found",
)


class CaptureCancelled(Exception):
    """用户主动停止采集。"""


class CaptureTimeout(RuntimeError):
    """在超时时间内没有收到完整帧。"""


class DeviceError(RuntimeError):
    """下位机明确报告失败。"""


class TransportError(RuntimeError):
    """串口读写异常。"""


@dataclass
class CaptureProgress:
    phase: str
    received: int = 0
    total: int = 0
    message: str = ""
    chunk: int = 0

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, int(self.received * 100 / self.total)))


@dataclass
class CaptureResult:
    payload: bytes
    header: FrameHeader
    headers: list[FrameHeader] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return float(self.stats.get("duration", 0.0))


ProgressCallback = Callable[[CaptureProgress], None]
LineCallback = Callable[[str], None]
CancelFlag = threading.Event | None


class _LineBuffer:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text.replace("\r\n", "\n").replace("\r", "\n")
        parts = self._buf.split("\n")
        self._buf = parts.pop()
        return [line.strip("\x00 ") for line in parts if line.strip("\x00 ")]

    def flush(self) -> list[str]:
        if self._buf.strip():
            line = self._buf.strip("\x00 ")
            self._buf = ""
            return [line] if line else []
        return []


def _check_cancel(cancel: CancelFlag) -> None:
    if cancel is not None and cancel.is_set():
        raise CaptureCancelled("采集已停止")


def _text_lines(payload: bytes) -> list[str]:
    text = payload.decode("latin-1", errors="replace").replace("\x00", "")
    return [line.strip() for line in text.splitlines() if line.strip()]


class CaptureEngine:
    def __init__(
        self,
        transport: SerialTransport,
        image: ImageConfig,
        capture: CaptureConfig,
        protocol: CaptureProtocol | None = None,
    ) -> None:
        self.transport = transport
        self.image = image
        self.capture = capture
        self.protocol = protocol or get_protocol(image.protocol)

    # ------------------------------------------------------------------ #
    def run(
        self,
        cancel: CancelFlag = None,
        on_progress: ProgressCallback | None = None,
        on_line: LineCallback | None = None,
    ) -> CaptureResult:
        warnings = self.capture.validate() + self.image.validate()
        for warning in warnings:
            log.warning("配置告警：%s", warning)

        if not self.transport.is_open:
            raise ConnectionError("串口未打开")

        t0 = time.monotonic()
        _check_cancel(cancel)
        self._prepare(cancel, on_line)
        if self.capture.mode == "whole":
            result = self._capture_whole(cancel, on_progress, on_line)
        else:
            result = self._capture_chunked(cancel, on_progress, on_line)
        result.stats.setdefault("duration", time.monotonic() - t0)
        result.stats["mode"] = self.capture.mode
        result.stats["protocol"] = self.protocol.key
        result.stats["payload_bytes"] = len(result.payload)
        result.stats["expected_bytes"] = self.image.expected_buffer_size
        result.stats["config_warnings"] = warnings
        result.stats["transport"] = self.transport.config.display()
        log.info(
            "采集完成：%d 字节，用时 %.2f s，分块 %d 个",
            len(result.payload),
            result.stats["duration"],
            result.stats.get("chunks", 1),
        )
        self._emit_progress(
            on_progress,
            CaptureProgress("done", len(result.payload), len(result.payload), "采集完成"),
        )
        # 把设备收尾回显（[uart] end 等）也带入日志
        try:
            tail = self.transport.drain_text(0.12)
            for line in _text_lines(tail[:2048].encode("latin-1", errors="replace")):
                self._emit_line(on_line, line)
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------ #
    def _prepare(self, cancel: CancelFlag, on_line: LineCallback | None) -> None:
        commands = self.protocol.prepare_commands(self.capture)
        if not commands:
            return
        for command in commands:
            _check_cancel(cancel)
            self.transport.reset_input_buffer()
            self._send(command)
            lines = self._drain_lines(
                self.capture.prepare_wait, cancel, on_line, "准备"
            )
            for line in lines:
                lowered = line.lower()
                for pattern in DEVICE_ERROR_PATTERNS:
                    if pattern in lowered:
                        raise DeviceError(f"下位机报告失败：{line}")

    def _capture_chunked(
        self,
        cancel: CancelFlag,
        on_progress: ProgressCallback | None,
        on_line: LineCallback | None,
    ) -> CaptureResult:
        capture = self.capture
        chunk_size = max(1, int(capture.chunk_size))
        total: int | None = None
        data = bytearray()
        headers: list[FrameHeader] = []
        parser: FrameParser | None = None
        offset = 0
        chunks = 0
        discarded = 0
        bad_crc = 0

        while True:
            _check_cancel(cancel)
            if total is not None and offset >= total:
                break
            requested = chunk_size if total is None else min(chunk_size, total - offset)
            if requested <= 0:
                break

            self.transport.reset_input_buffer()
            command = self.protocol.dump_command(capture, offset, requested)
            self._send(command)
            parser = self.protocol.new_parser()
            deadline = time.monotonic() + capture.chunk_timeout
            packet = self._read_packet(
                parser, deadline, cancel, on_progress, on_line, len(data), total, offset
            )
            discarded += parser.discarded_bytes
            bad_crc += parser.bad_crc
            chunks += 1

            header = packet.header
            if total is None:
                total = header.total
                if total <= 0 or total > capture.max_frame_bytes:
                    raise ProtocolError(f"帧总长异常：total={total}")
                log.info("设备报告整帧长度 total=%d 字节", total)
            else:
                if header.total != total:
                    raise ProtocolError(
                        f"帧总长变化：已按 total={total} 接收，本块 total={header.total}"
                    )

            expected = min(requested, total - offset)
            if header.offset != offset:
                raise ProtocolError(
                    f"分块偏移不匹配：期望 {offset}，设备返回 {header.offset}"
                )
            if len(packet.payload) != expected:
                raise ProtocolError(
                    f"分块长度不匹配：期望 {expected}，设备返回 {len(packet.payload)}"
                )
            if header.length != len(packet.payload):
                raise ProtocolError(
                    f"头部 length={header.length} 与 payload={len(packet.payload)} 不一致"
                )

            data += packet.payload
            headers.append(header)
            offset += len(packet.payload)
            log.debug(
                "分块 #%d off=%d len=%d 累计 %d/%d（%.0f%%）",
                chunks,
                header.offset,
                len(packet.payload),
                len(data),
                total,
                len(data) * 100.0 / max(1, total),
            )
            self._emit_progress(
                on_progress,
                CaptureProgress(
                    "chunk",
                    len(data),
                    total,
                    f"分块 {chunks}：off={header.offset} len={len(packet.payload)}",
                    chunks,
                ),
            )

        if total is None:
            raise CaptureTimeout("没有收到任何数据块")
        if len(data) != total:
            raise ProtocolError(
                f"拼接结果不完整：收到 {len(data)} 字节，头部声明 {total} 字节"
            )

        stats = {
            "chunks": chunks,
            "discarded_bytes": discarded,
            "bad_crc": bad_crc,
            "total": total,
        }
        return CaptureResult(payload=bytes(data), header=headers[-1], headers=headers, stats=stats)

    def _capture_whole(
        self,
        cancel: CancelFlag,
        on_progress: ProgressCallback | None,
        on_line: LineCallback | None,
    ) -> CaptureResult:
        self.transport.reset_input_buffer()
        command = self.protocol.whole_command(self.capture)
        self._send(command)
        parser = self.protocol.new_parser()
        deadline = time.monotonic() + self.capture.whole_timeout
        packet = self._read_packet(
            parser, deadline, cancel, on_progress, on_line, 0, None, 0
        )
        header = packet.header
        if header.total <= 0 or header.total > self.capture.max_frame_bytes:
            raise ProtocolError(f"帧总长异常：total={header.total}")
        if header.offset != 0:
            raise ProtocolError(f"整场导出的 offset 应为 0，实际 {header.offset}")
        if len(packet.payload) != header.total:
            raise ProtocolError(
                f"整场长度不完整：payload={len(packet.payload)} total={header.total}"
            )
        stats = {
            "chunks": 1,
            "discarded_bytes": parser.discarded_bytes,
            "bad_crc": parser.bad_crc,
            "total": header.total,
        }
        return CaptureResult(payload=packet.payload, header=header, headers=[header], stats=stats)

    # ------------------------------------------------------------------ #
    def _read_packet(
        self,
        parser: FrameParser,
        deadline: float,
        cancel: CancelFlag,
        on_progress: ProgressCallback | None,
        on_line: LineCallback | None,
        received: int,
        total: int | None,
        offset: int,
    ) -> FramePacket:
        line_buf = _LineBuffer()
        local = bytearray()
        last_progress = 0.0
        last_device_text = time.monotonic()

        while True:
            _check_cancel(cancel)
            now = time.monotonic()
            if now > deadline:
                text = "\n".join(line_buf.flush()[-3:])
                detail = f"已收到 {len(local)} 字节"
                if parser.buffered:
                    detail += f"，解析器缓存 {parser.buffered} 字节"
                if text:
                    detail += f"，最近回显：{text}"
                raise CaptureTimeout(
                    f"等待数据超时（{self.capture.chunk_timeout:.1f}s，"
                    f"请求 off={offset}），{detail}"
                )

            chunk = self.transport.read(8192)
            if not chunk:
                continue
            local += chunk

            # 二进制开始之前，设备打印的文字当作回显显示
            if not parser.seen_sync:
                index = chunk.find(bytes([SYNC[0]]))
                text_part = chunk[:index] if index >= 0 else chunk
                if text_part:
                    for line in line_buf.feed(
                        text_part.decode("latin-1", errors="replace").replace("\x00", "")
                    ):
                        self._emit_line(on_line, line)
                        lowered = line.lower()
                        for pattern in DEVICE_ERROR_PATTERNS:
                            if pattern in lowered:
                                raise DeviceError(f"下位机报告失败：{line}")
                if index >= 0:
                    for line in line_buf.flush():
                        self._emit_line(on_line, line)

            packets = parser.feed(chunk)
            if packets:
                for line in line_buf.flush():
                    self._emit_line(on_line, line)
                return packets[0]

            now = time.monotonic()
            if on_progress and now - last_progress > 0.08:
                last_progress = now
                self._emit_progress(
                    on_progress,
                    CaptureProgress(
                        "chunk",
                        received + len(local),
                        total or 0,
                        f"接收中 off={offset} …",
                    ),
                )

    # ------------------------------------------------------------------ #
    def _drain_lines(
        self,
        wait: float,
        cancel: CancelFlag,
        on_line: LineCallback | None,
        phase: str,
        progress: ProgressCallback | None = None,
    ) -> list[str]:
        deadline = time.monotonic() + max(0.0, wait)
        line_buf = _LineBuffer()
        lines: list[str] = []
        last = 0.0
        while time.monotonic() < deadline:
            _check_cancel(cancel)
            chunk = self.transport.read(4096)
            if chunk:
                for line in line_buf.feed(
                    chunk.decode("latin-1", errors="replace").replace("\x00", "")
                ):
                    self._emit_line(on_line, line)
                    lines.append(line)
            now = time.monotonic()
            if progress and now - last > 0.2:
                last = now
                self._emit_progress(
                    progress,
                    CaptureProgress(phase, 0, 0, f"{phase}：等待下位机响应…"),
                )
        for line in line_buf.flush():
            self._emit_line(on_line, line)
            lines.append(line)
        return lines

    def _send(self, command: str) -> None:
        log.info("TX  → %s", command)
        try:
            self.transport.write_line(command)
            self.transport.flush()
        except Exception as exc:
            raise TransportError(f"串口发送失败：{exc}") from exc

    def _emit_line(self, on_line: LineCallback | None, line: str) -> None:
        line = line.strip()
        if not line:
            return
        log.info("RX  ← %s", line)
        if on_line is not None:
            try:
                on_line(line)
            except Exception:
                log.exception("on_line 回调异常")

    @staticmethod
    def _emit_progress(
        on_progress: ProgressCallback | None, progress: CaptureProgress
    ) -> None:
        if on_progress is not None:
            try:
                on_progress(progress)
            except Exception:
                log.exception("on_progress 回调异常")


def send_commands(
    transport: SerialTransport,
    commands: list[str],
    wait: float = 0.6,
    cancel: CancelFlag = None,
    on_line: LineCallback | None = None,
) -> list[str]:
    """发送若干条 MSH 命令并收集回显（手动命令 / 快捷按钮 / CLI info）。"""
    lines: list[str] = []
    for command in commands:
        _check_cancel(cancel)
        transport.reset_input_buffer()
        log.info("TX  → %s", command)
        transport.write_line(command)
        transport.flush()
        deadline = time.monotonic() + max(0.0, wait)
        line_buf = _LineBuffer()
        while time.monotonic() < deadline:
            _check_cancel(cancel)
            chunk = transport.read(4096)
            if chunk:
                for line in line_buf.feed(
                    chunk.decode("latin-1", errors="replace").replace("\x00", "")
                ):
                    if line:
                        log.info("RX  ← %s", line)
                        lines.append(line)
                        if on_line is not None:
                            on_line(line)
        for line in line_buf.flush():
            log.info("RX  ← %s", line)
            lines.append(line)
            if on_line is not None:
                on_line(line)
    return lines


def wait_quiet(
    transport: SerialTransport, quiet: float = 0.2, timeout: float = 3.0
) -> None:
    """读空串口直到安静，避免上一条命令的残留影响下一次采集。"""
    deadline = time.monotonic() + timeout
    last = time.monotonic()
    while time.monotonic() < deadline:
        chunk = transport.read(4096)
        now = time.monotonic()
        if chunk:
            last = now
        elif now - last >= quiet:
            return
