"""运行配置对象：串口 / 图像接收 / 采集流程。

默认值是通用示例；接入具体设备时在界面左侧改成实际命令即可，
命令会保存在本机的 QSettings 中，不影响源码。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from core.formats.pixel_formats import (
    DEFAULT_PIXEL_FORMAT,
    get_pixel_format,
    resolve_stride,
)

DEFAULT_PORT = "COM12"
DEFAULT_BAUD = 1_000_000

PROTOCOLS: dict[str, str] = {
    "bt656": "BT.656（嵌入式同步）",
}
PROTOCOL_FUTURE = {
    "dvp": "DVP（预留）",
    "mipi": "MIPI CSI（预留）",
    "spi": "SPI Camera（预留）",
}


@dataclass
class SerialConfig:
    port: str = DEFAULT_PORT
    baudrate: int = DEFAULT_BAUD
    bytesize: int = 8
    stopbits: float = 1.0
    parity: str = "none"       # none / even / odd / mark / space
    flow: str = "none"         # none / rtscts / xonxoff
    read_timeout: float = 0.05

    def display(self) -> str:
        parity = self.parity[:1].upper() if self.parity else "N"
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        flow = {"none": "无流控", "rtscts": "RTS/CTS", "xonxoff": "XON/XOFF"}.get(
            self.flow, self.flow
        )
        return f"{self.port} @ {self.baudrate} {self.bytesize}{parity}{stop} ({flow})"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImageConfig:
    protocol: str = "bt656"
    width: int = 720
    height: int = 240
    pixel_format: str = DEFAULT_PIXEL_FORMAT
    stride: int = 1440             # 0 表示按 width × bytes_per_pixel 自动计算
    byte_order: str = "little"     # RGB565 用：little / big
    groups_per_row: int = 8        # Hex Viewer 每行显示多少个像素组

    # ------------------------------------------------------------------ #
    @property
    def format(self):
        return get_pixel_format(self.pixel_format)

    @property
    def bytes_per_pixel(self) -> int:
        return self.format.bytes_per_pixel

    @property
    def auto_stride(self) -> int:
        return resolve_stride(self.width, self.pixel_format)

    def effective_stride(self) -> int:
        if self.stride > 0:
            return self.stride
        return self.auto_stride

    @property
    def expected_buffer_size(self) -> int:
        stride = self.effective_stride()
        if stride <= 0 or self.height <= 0:
            return 0
        return stride * self.height

    def display_resolution(self) -> str:
        return f"{self.width} x {self.height}"

    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        """返回配置告警（不是异常）；真正非法的值由 GUI 控件约束。"""
        warnings: list[str] = []
        fmt = self.format
        if self.width <= 0 or self.height <= 0:
            warnings.append("图像宽高必须为正数")
        if self.protocol not in PROTOCOLS:
            warnings.append(f"未知接收协议：{self.protocol}")
        stride = self.effective_stride()
        if fmt.is_yuv422 and self.width % 2:
            warnings.append("YUV422 宽度必须为偶数，最后一列会被丢弃")
        if fmt.bytes_per_pixel > 0:
            minimum = self.width * fmt.bytes_per_pixel
            if stride < minimum:
                warnings.append(
                    f"Stride({stride}) 小于一行有效像素字节数({minimum})，图像会被截断"
                )
            elif stride > minimum:
                warnings.append(
                    f"Stride({stride}) 大于一行有效像素字节数({minimum})，行尾有 {stride - minimum} 字节填充"
                )
        elif fmt.is_jpeg and self.stride not in (0, self.width):
            warnings.append("JPEG 不需要固定 Stride")
        if self.expected_buffer_size > 16 * 1024 * 1024:
            warnings.append("单帧 Buffer 超过 16 MB，串口传输可能非常慢")
        return warnings

    def as_dict(self) -> dict:
        data = asdict(self)
        data["effective_stride"] = self.effective_stride()
        data["expected_buffer_size"] = self.expected_buffer_size
        return data


@dataclass
class CaptureConfig:
    # chunked: 逐块导出，实测最稳；whole: 单条命令发整场
    mode: str = "chunked"
    prepare_command: str = "camera capture 0 1"
    prepare_wait: float = 1.5
    chunk_size: int = 57600
    dump_command_template: str = "camera dump {off} {len}"
    whole_command: str = "camera raw 800"
    chunk_timeout: float = 20.0
    whole_timeout: float = 90.0
    command_gap: float = 0.15
    max_frame_bytes: int = 16 * 1024 * 1024

    def validate(self) -> list[str]:
        warnings: list[str] = []
        if self.mode not in ("chunked", "whole"):
            warnings.append(f"未知采集模式：{self.mode}")
        if self.mode == "chunked":
            if self.chunk_size <= 0:
                warnings.append("分块大小必须大于 0")
            if "{off}" not in self.dump_command_template or "{len}" not in self.dump_command_template:
                warnings.append("分块导出命令模板必须同时包含 {off} 和 {len}")
        if self.chunk_timeout <= 0 or self.whole_timeout <= 0:
            warnings.append("超时时间必须大于 0")
        return warnings

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_IMAGE_CONFIG = ImageConfig()
DEFAULT_SERIAL_CONFIG = SerialConfig()
DEFAULT_CAPTURE_CONFIG = CaptureConfig()
