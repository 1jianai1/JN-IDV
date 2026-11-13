"""统一 Frame 数据对象：一次采集的所有事实都在这里。

必须保留：
  * 下位机实际发送的完整原始 Buffer（``raw_data``，不含协议头/尾）
  * 解析后的有效图像（``image`` RGB888）
  * 参数/协议元数据（``metadata`` / ``header`` / ``config``）
  * 原始 Buffer 与图像坐标的映射关系（``layout``）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from core.config import PROTOCOLS, ImageConfig
from core.formats.bt656 import BT656Layout, PixelLocation
from core.formats.pixel_formats import get_pixel_format


@dataclass
class Frame:
    raw_data: bytes
    image: np.ndarray
    width: int
    height: int
    pixel_format: str
    protocol: str
    frame_index: int = 0
    timestamp: float = field(default_factory=time.time)
    luma: np.ndarray | None = None
    yuv: np.ndarray | None = None
    layout: BT656Layout | None = None
    config: ImageConfig | None = None
    header: dict | None = None
    metadata: dict = field(default_factory=dict)
    source: str = "live"

    # ------------------------------------------------------------------ #
    @property
    def resolution(self) -> str:
        return f"{self.width} x {self.height}"

    @property
    def format_label(self) -> str:
        try:
            return get_pixel_format(self.pixel_format).label
        except KeyError:
            return self.pixel_format

    @property
    def stride(self) -> int:
        return self.layout.stride if self.layout else 0

    @property
    def raw_size(self) -> int:
        return len(self.raw_data)

    @property
    def timestamp_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))

    def status_text(self) -> str:
        if self.source == "file":
            return f"已加载文件 (#{self.frame_index})"
        if self.source == "demo":
            return f"合成测试帧 (#{self.frame_index})"
        return f"Frame Received (#{self.frame_index})"

    # ------------------------------------------------------------------ #
    def info_lines(self) -> list[tuple[str, str]]:
        """Image Preview 状态面板内容，全部来自真实数据。"""
        lines = [
            ("Resolution", self.resolution),
            ("Format", self.format_label),
            ("Buffer", f"{self.raw_size} Bytes"),
            ("Stride", str(self.stride)),
            ("Protocol", PROTOCOLS.get(self.protocol, self.protocol)),
            ("Status", self.status_text()),
        ]
        if self.layout is not None:
            lines.append(("Layout", self.layout.mode_label))
        return lines

    def summary_lines(self) -> list[str]:
        return [f"{label}: {value}" for label, value in self.info_lines()]

    # ------------------------------------------------------------------ #
    def offset_to_location(self, offset: int) -> PixelLocation | None:
        if self.layout is None or not self.layout.mapped:
            return None
        return self.layout.locate(offset)

    def location_to_offset(self, x: int, y: int, component: str | None = None) -> int | None:
        if self.layout is None or not self.layout.mapped:
            return None
        return self.layout.offset_of(x, y, component)

    def pixel_rgb(self, x: int, y: int) -> tuple[int, int, int] | None:
        if 0 <= y < self.height and 0 <= x < self.width:
            pixel = self.image[y, x]
            return int(pixel[0]), int(pixel[1]), int(pixel[2])
        return None

    def pixel_yuv(self, x: int, y: int) -> tuple[int, int, int] | None:
        if self.yuv is None:
            return None
        if 0 <= y < self.height and 0 <= x < self.width:
            pixel = self.yuv[y, x]
            return int(pixel[0]), int(pixel[1]), int(pixel[2])
        return None

    def raw_bytes_at(self, offset: int, count: int = 8) -> bytes:
        return self.raw_data[offset : offset + count]

    # ------------------------------------------------------------------ #
    def to_metadata(self) -> dict:
        layout = self.layout.summary() if self.layout else None
        return {
            "frame_index": self.frame_index,
            "timestamp": self.timestamp,
            "timestamp_text": self.timestamp_text,
            "source": self.source,
            "protocol": self.protocol,
            "pixel_format": self.pixel_format,
            "width": self.width,
            "height": self.height,
            "raw_bytes": self.raw_size,
            "header": self.header,
            "config": self.config.as_dict() if self.config else None,
            "layout": layout,
            "stats": self.metadata,
        }
