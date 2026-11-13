"""采集引擎测试：不接硬件，用 FakeTransport 模拟下位机。"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

from core.acquisition.capture import (
    CaptureCancelled,
    CaptureEngine,
    CaptureTimeout,
    DeviceError,
    ProtocolError,
    send_commands,
)
from core.config import CaptureConfig, ImageConfig
from core.protocol.uart_frames import build_packet


class FakeTransport:
    def __init__(self) -> None:
        self.is_open = True
        self.config = SimpleNamespace(display=lambda: "FAKE @ 1000000")
        self._rx = bytearray()
        self.writes: list[str] = []
        self.on_write = None
        self.closed = False

    def write_line(self, command: str) -> None:
        self.writes.append(command)
        if self.on_write is not None:
            self.on_write(command, self)

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
        self.closed = True
        self.is_open = False


def make_device(payload: bytes, fail_prepare: bool = False, wrong_offset: bool = False):
    def handler(command: str, transport: FakeTransport) -> None:
        if command.startswith("camera capture"):
            if fail_prepare:
                transport._rx += b"[show] capture frame failed\r\n"
            else:
                transport._rx += b"[show] ok\r\n"
            return
        if command.startswith("camera raw") or (command.startswith("camera dump") and len(command.split()) == 2):
            transport._rx += b"[raw] begin\r\n" + build_packet(
                payload, total=len(payload), offset=0, length=len(payload), flags=1
            )
            return
        if command.startswith("camera dump"):
            parts = command.split()
            offset = int(parts[2])
            length = min(int(parts[3]), len(payload) - offset)
            if wrong_offset:
                offset += 1
                length = min(length, len(payload) - offset)
            transport._rx += b"[uart] begin\r\n" + build_packet(
                payload[offset : offset + length],
                total=len(payload),
                offset=offset,
                length=length,
                flags=1,
            )

    return handler


class CaptureEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = bytes(range(256)) * 47  # 12032 字节
        self.image = ImageConfig(width=64, height=94, pixel_format="UYVY", stride=128)
        self.capture = CaptureConfig(
            mode="chunked",
            prepare_command="camera capture 0 1",
            prepare_wait=0.01,
            chunk_size=5000,
            chunk_timeout=2.0,
            whole_command="camera raw 800",
            whole_timeout=2.0,
        )

    def test_chunked_capture_reassembles(self) -> None:
        transport = FakeTransport()
        transport.on_write = make_device(self.payload)
        engine = CaptureEngine(transport, self.image, self.capture)
        progress = []
        result = engine.run(on_progress=progress.append)
        self.assertEqual(result.payload, self.payload)
        self.assertEqual(result.stats["chunks"], 3)
        self.assertEqual(result.header.total, len(self.payload))
        self.assertEqual(len(transport.writes), 4)  # prepare + 3 chunks
        self.assertTrue(any("5000" in command for command in transport.writes))
        self.assertTrue(progress)

    def test_chunk_sizes_are_clamped_to_total(self) -> None:
        payload = b"x" * 7000
        transport = FakeTransport()
        transport.on_write = make_device(payload)
        self.capture.chunk_size = 5000
        engine = CaptureEngine(transport, self.image, self.capture)
        result = engine.run()
        self.assertEqual(result.payload, payload)
        self.assertIn("camera dump 5000 2000", transport.writes)

    def test_whole_capture(self) -> None:
        transport = FakeTransport()
        transport.on_write = make_device(self.payload)
        self.capture.mode = "whole"
        self.capture.prepare_command = ""
        engine = CaptureEngine(transport, self.image, self.capture)
        result = engine.run()
        self.assertEqual(result.payload, self.payload)
        self.assertEqual(result.stats["chunks"], 1)
        self.assertEqual(transport.writes, ["camera raw 800"])

    def test_prepare_failure_raises_device_error(self) -> None:
        transport = FakeTransport()
        transport.on_write = make_device(self.payload, fail_prepare=True)
        engine = CaptureEngine(transport, self.image, self.capture)
        with self.assertRaises(DeviceError):
            engine.run()

    def test_wrong_offset_raises_protocol_error(self) -> None:
        transport = FakeTransport()
        transport.on_write = make_device(self.payload, wrong_offset=True)
        engine = CaptureEngine(transport, self.image, self.capture)
        with self.assertRaises(ProtocolError):
            engine.run()

    def test_cancel(self) -> None:
        transport = FakeTransport()
        transport.on_write = make_device(self.payload)
        engine = CaptureEngine(transport, self.image, self.capture)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CaptureCancelled):
            engine.run(cancel=cancel)

    def test_timeout(self) -> None:
        transport = FakeTransport()
        self.capture.chunk_timeout = 0.05
        engine = CaptureEngine(transport, self.image, self.capture)
        with self.assertRaises(CaptureTimeout):
            engine.run()

    def test_send_commands(self) -> None:
        transport = FakeTransport()

        def handler(command: str, transport: FakeTransport) -> None:
            transport._rx += f"echo: {command}\r\nresult ok\r\n".encode()

        transport.on_write = handler
        lines = send_commands(transport, ["camera info"], wait=0.05)
        self.assertIn("result ok", lines)
        self.assertEqual(transport.writes, ["camera info"])


if __name__ == "__main__":
    unittest.main()
