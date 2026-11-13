"""像素格式定义（GUI、解码器、Hex Viewer 共用同一份事实）。

YUV422 系列按「像素组 / 宏像素」描述字节道的含义：
    UYVY: U0 Y0 V0 Y1   （U/V 由两个相邻像素共享）
    YUYV: Y0 U0 Y1 V0
    YVYU: Y0 V0 Y1 U0
    VYUY: V0 Y0 U0 Y1

RGB565 / RGB888 一个像素就是一个组，字节道分别是字节序号或 R/G/B。
JPEG 是压缩码流，没有逐像素的 Buffer 映射。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PixelFormat:
    key: str
    label: str
    kind: str                      # yuv422 / rgb565 / rgb888 / jpeg
    bytes_per_pixel: int           # JPEG 为 0（长度可变）
    lane_components: tuple[str, ...] = ()
    pixels_per_group: int = 1
    group_bytes: int = 0
    description: str = ""

    @property
    def is_yuv422(self) -> bool:
        return self.kind == "yuv422"

    @property
    def is_jpeg(self) -> bool:
        return self.kind == "jpeg"

    @property
    def has_pixel_mapping(self) -> bool:
        """能否把 Buffer 偏移映射到 (x, y)。"""
        return self.kind != "jpeg"

    @property
    def components(self) -> tuple[str, ...]:
        """去重后的分量名，用于 Hex Viewer 图例。"""
        seen: list[str] = []
        for comp in self.lane_components:
            base = "Y" if comp in ("Y0", "Y1") else comp
            if base not in seen:
                seen.append(base)
        return tuple(seen)

    @property
    def legend(self) -> tuple[tuple[str, str], ...]:
        """(颜色键, 文本) 列表；颜色键在 GUI 里映射到颜色。"""
        if self.kind == "yuv422":
            return (
                ("U", "U 色度（2 像素共享）"),
                ("Y", "Y 亮度"),
                ("V", "V 色度（2 像素共享）"),
            )
        if self.kind in ("rgb565", "rgb888"):
            return (("R", "R 红"), ("G", "G 绿"), ("B", "B 蓝"))
        return ()


UYVY = PixelFormat(
    key="UYVY",
    label="YUV422 UYVY",
    kind="yuv422",
    bytes_per_pixel=2,
    lane_components=("U", "Y0", "V", "Y1"),
    pixels_per_group=2,
    group_bytes=4,
    description="U0 Y0 V0 Y1 —— U/V 两个相邻像素共享",
)
YUYV = PixelFormat(
    key="YUYV",
    label="YUV422 YUYV",
    kind="yuv422",
    bytes_per_pixel=2,
    lane_components=("Y0", "U", "Y1", "V"),
    pixels_per_group=2,
    group_bytes=4,
    description="Y0 U0 Y1 V0",
)
YVYU = PixelFormat(
    key="YVYU",
    label="YUV422 YVYU",
    kind="yuv422",
    bytes_per_pixel=2,
    lane_components=("Y0", "V", "Y1", "U"),
    pixels_per_group=2,
    group_bytes=4,
    description="Y0 V0 Y1 U0",
)
VYUY = PixelFormat(
    key="VYUY",
    label="YUV422 VYUY",
    kind="yuv422",
    bytes_per_pixel=2,
    lane_components=("V", "Y0", "U", "Y1"),
    pixels_per_group=2,
    group_bytes=4,
    description="V0 Y0 U0 Y1",
)
RGB565 = PixelFormat(
    key="RGB565",
    label="RGB565",
    kind="rgb565",
    bytes_per_pixel=2,
    lane_components=("B0", "B1"),
    pixels_per_group=1,
    group_bytes=2,
    description="16-bit RGB，字节序可配置",
)
RGB888 = PixelFormat(
    key="RGB888",
    label="RGB888",
    kind="rgb888",
    bytes_per_pixel=3,
    lane_components=("R", "G", "B"),
    pixels_per_group=1,
    group_bytes=3,
    description="24-bit RGB，R G B 顺序",
)
JPEG = PixelFormat(
    key="JPEG",
    label="JPEG",
    kind="jpeg",
    bytes_per_pixel=0,
    lane_components=(),
    pixels_per_group=0,
    group_bytes=0,
    description="压缩码流，尺寸由码流自身决定，无逐像素 Buffer 映射",
)

PIXEL_FORMATS: dict[str, PixelFormat] = {
    fmt.key: fmt for fmt in (UYVY, YUYV, YVYU, VYUY, RGB565, RGB888, JPEG)
}

DEFAULT_PIXEL_FORMAT = "UYVY"


def get_pixel_format(key: str) -> PixelFormat:
    try:
        return PIXEL_FORMATS[key.upper()]
    except KeyError as exc:  # pragma: no cover - 由调用方校验
        raise KeyError(
            "未知像素格式 %r（可用：%s）" % (key, ", ".join(PIXEL_FORMATS))
        ) from exc


def pixel_format_keys() -> list[str]:
    return list(PIXEL_FORMATS)


def resolve_stride(width: int, pixel_format: str) -> int:
    """给定期望宽度和格式，返回一行的字节数（JPEG 返回 0）。"""
    fmt = get_pixel_format(pixel_format)
    if fmt.bytes_per_pixel <= 0:
        return 0
    return int(width) * fmt.bytes_per_pixel
