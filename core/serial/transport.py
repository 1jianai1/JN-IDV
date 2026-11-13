"""串口传输层（pyserial 封装）。

只做字节收发与端口枚举，不包含任何协议/图像逻辑。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from core.config import SerialConfig
from core.log import get_logger

log = get_logger("serial")

_PARITY_MAP = {
    "none": "PARITY_NONE",
    "even": "PARITY_EVEN",
    "odd": "PARITY_ODD",
    "mark": "PARITY_MARK",
    "space": "PARITY_SPACE",
}


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str = ""
    hwid: str = ""

    def display(self) -> str:
        if self.description and self.description != "n/a":
            return f"{self.device} — {self.description}"
        return self.device


def _pyserial():
    try:
        import serial  # noqa: F401
        from serial.tools import list_ports  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError(
            "缺少 pyserial，请先执行：pip install pyserial（或 pip install -r requirements.txt）"
        ) from exc
    import serial
    from serial.tools import list_ports

    return serial, list_ports


def list_serial_ports() -> list[PortInfo]:
    """枚举当前可用串口，按端口号自然排序。"""
    serial, list_ports = _pyserial()
    ports: list[PortInfo] = []
    for info in list_ports.comports():
        ports.append(
            PortInfo(
                device=info.device,
                description=info.description or "",
                hwid=info.hwid or "",
            )
        )

    def sort_key(port: PortInfo):
        device = port.device.upper()
        digits = "".join(ch for ch in device if ch.isdigit())
        return (0, int(digits)) if digits else (1, device)

    ports.sort(key=sort_key)
    return ports


class SerialTransport:
    """线程安全的串口包装：同一时刻只允许一个采集任务使用。"""

    def __init__(self, config: SerialConfig) -> None:
        self.config = config
        self._serial = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    @property
    def is_open(self) -> bool:
        return self._serial is not None and bool(getattr(self._serial, "is_open", False))

    @property
    def port(self) -> str:
        return self.config.port

    def open(self) -> None:
        with self._lock:
            if self.is_open:
                return
            serial, _ = _pyserial()
            kwargs = {
                "port": self.config.port,
                "baudrate": int(self.config.baudrate),
                "bytesize": int(self.config.bytesize),
                "stopbits": float(self.config.stopbits),
                "timeout": float(self.config.read_timeout),
                "write_timeout": 2.0,
            }
            parity = _PARITY_MAP.get(self.config.parity.lower(), "PARITY_NONE")
            kwargs["parity"] = getattr(serial, parity)
            if self.config.flow == "rtscts":
                kwargs["rtscts"] = True
            elif self.config.flow == "xonxoff":
                kwargs["xonxoff"] = True

            log.info("打开串口 %s（%s）", self.config.port, self.config.display())
            self._serial = serial.Serial(**kwargs)
            # 打开瞬间的线路抖动/复位信息丢弃
            time.sleep(0.15)
            try:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            except Exception:
                pass
            log.info("串口已连接：%s", self.config.display())

    def close(self) -> None:
        with self._lock:
            if self._serial is None:
                return
            try:
                self._serial.close()
            finally:
                self._serial = None
            log.info("串口已关闭：%s", self.config.port)

    # ------------------------------------------------------------------ #
    def reset_input_buffer(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.reset_input_buffer()
                except Exception:
                    pass

    def flush(self) -> None:
        with self._lock:
            if self._serial is not None:
                self._serial.flush()

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._serial is None:
                raise ConnectionError("串口未打开")
            return int(self._serial.write(data))

    def write_line(self, command: str) -> None:
        """发送一行 MSH 命令（自动补 CRLF，与现有脚本一致）。"""
        self.write((command + "\r\n").encode("ascii", errors="replace"))

    def read(self, size: int = 8192) -> bytes:
        with self._lock:
            if self._serial is None:
                raise ConnectionError("串口未打开")
            try:
                return self._serial.read(size)
            except Exception as exc:
                raise ConnectionError(f"串口读取失败：{exc}") from exc

    def drain_text(self, wait: float, read_size: int = 4096) -> str:
        """在 ``wait`` 秒内读取并返回可打印文本（用于命令回显）。"""
        deadline = time.monotonic() + max(0.0, wait)
        data = bytearray()
        while time.monotonic() < deadline:
            chunk = self.read(read_size)
            if chunk:
                data += chunk
            else:
                time.sleep(0.005)
        text = data.decode("latin-1", errors="replace")
        return text.replace("\x00", "")
