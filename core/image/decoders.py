"""图像解码器：把「有效像素区原始字节」解码为 RGB / 亮度平面。

扩展方式：实现 ``ImageDecoder`` 并注册到 ``DECODERS``。
本次实现 YUV422（UYVY/YUYV/YVYU/VYUY）、RGB565、RGB888、JPEG，
采用常用的 BT.601 有限范围 YUV->RGB 公式。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO

import numpy as np

from core.config import ImageConfig
from core.formats.pixel_formats import PixelFormat, get_pixel_format


class DecodeError(RuntimeError):
    pass


@dataclass
class DecodedImage:
    rgb: np.ndarray
    width: int
    height: int
    luma: np.ndarray | None = None
    yuv: np.ndarray | None = None
    note: str = ""


class ImageDecoder(ABC):
    key: str = ""

    @abstractmethod
    def decode(
        self, data: bytes, width: int, height: int, config: ImageConfig
    ) -> DecodedImage:
        raise NotImplementedError


def _raw_array(data: bytes, count: int) -> np.ndarray:
    available = min(len(data), count)
    array = np.frombuffer(data, dtype=np.uint8, count=available)
    if available < count:
        array = np.concatenate([array, np.zeros(count - available, dtype=np.uint8)])
    return array


class YUV422Decoder(ImageDecoder):
    """YUV422 packed（2 像素/4 字节）-> RGB888。"""

    def __init__(self, pixel_format: PixelFormat) -> None:
        self.format = pixel_format
        self.key = pixel_format.key
        self._lanes = {name: index for index, name in enumerate(pixel_format.lane_components)}

    def decode(
        self, data: bytes, width: int, height: int, config: ImageConfig
    ) -> DecodedImage:
        width = max(0, int(width))
        height = max(0, int(height))
        even_width = width - (width % 2)
        if even_width <= 0 or height <= 0:
            raise DecodeError(f"无效图像尺寸：{width} x {height}")
        note = ""
        if even_width != width:
            note = "YUV422 宽度必须为偶数，最后一列已丢弃"

        needed = height * even_width * 2
        raw = _raw_array(data, needed)
        if len(data) < needed:
            note = (note + "；" if note else "") + (
                f"有效像素数据不足（需要 {needed}，实际 {len(data)}），尾部补 0"
            )

        groups = raw.reshape(height, even_width // 2, 4)
        lanes = self._lanes
        u = groups[..., lanes["U"]].astype(np.int32)
        v = groups[..., lanes["V"]].astype(np.int32)
        y0 = groups[..., lanes["Y0"]].astype(np.int32)
        y1 = groups[..., lanes["Y1"]].astype(np.int32)

        y_plane = np.empty((height, even_width), dtype=np.uint8)
        y_plane[:, 0::2] = y0
        y_plane[:, 1::2] = y1
        u_plane = np.repeat(u, 2, axis=1).astype(np.uint8)
        v_plane = np.repeat(v, 2, axis=1).astype(np.uint8)

        c = y_plane.astype(np.int32) - 16
        d = u_plane.astype(np.int32) - 128
        e = v_plane.astype(np.int32) - 128
        red = (298 * c + 409 * e + 128) >> 8
        green = (298 * c - 100 * d - 208 * e + 128) >> 8
        blue = (298 * c + 516 * d + 128) >> 8
        rgb = np.stack(
            [
                np.clip(red, 0, 255),
                np.clip(green, 0, 255),
                np.clip(blue, 0, 255),
            ],
            axis=-1,
        ).astype(np.uint8)
        yuv = np.stack([y_plane, u_plane, v_plane], axis=-1)
        return DecodedImage(
            rgb=rgb, width=even_width, height=height, luma=y_plane, yuv=yuv, note=note
        )


class RGB565Decoder(ImageDecoder):
    key = "RGB565"

    def decode(
        self, data: bytes, width: int, height: int, config: ImageConfig
    ) -> DecodedImage:
        width = max(0, int(width))
        height = max(0, int(height))
        if width <= 0 or height <= 0:
            raise DecodeError(f"无效图像尺寸：{width} x {height}")
        needed = width * height * 2
        raw = _raw_array(data, needed)
        note = ""
        if len(data) < needed:
            note = f"数据不足（需要 {needed}，实际 {len(data)}），尾部补 0"
        pairs = raw.reshape(height, width, 2)
        if config.byte_order == "big":
            value = (pairs[..., 0].astype(np.uint16) << 8) | pairs[..., 1]
        else:
            value = pairs[..., 0].astype(np.uint16) | (pairs[..., 1].astype(np.uint16) << 8)
        r5 = ((value >> 11) & 0x1F).astype(np.uint8)
        g6 = ((value >> 5) & 0x3F).astype(np.uint8)
        b5 = (value & 0x1F).astype(np.uint8)
        red = (r5 << 3) | (r5 >> 2)
        green = (g6 << 2) | (g6 >> 4)
        blue = (b5 << 3) | (b5 >> 2)
        rgb = np.stack([red, green, blue], axis=-1).astype(np.uint8)
        luma = (
            0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        ).astype(np.uint8)
        return DecodedImage(
            rgb=rgb, width=width, height=height, luma=luma, note=note
        )


class RGB888Decoder(ImageDecoder):
    key = "RGB888"

    def decode(
        self, data: bytes, width: int, height: int, config: ImageConfig
    ) -> DecodedImage:
        width = max(0, int(width))
        height = max(0, int(height))
        if width <= 0 or height <= 0:
            raise DecodeError(f"无效图像尺寸：{width} x {height}")
        needed = width * height * 3
        raw = _raw_array(data, needed)
        note = ""
        if len(data) < needed:
            note = f"数据不足（需要 {needed}，实际 {len(data)}），尾部补 0"
        rgb = raw.reshape(height, width, 3).copy()
        luma = (
            0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        ).astype(np.uint8)
        return DecodedImage(
            rgb=rgb, width=width, height=height, luma=luma, note=note
        )


class JPEGDecoder(ImageDecoder):
    key = "JPEG"

    def decode(
        self, data: bytes, width: int, height: int, config: ImageConfig
    ) -> DecodedImage:
        if not data:
            raise DecodeError("JPEG 数据为空")
        try:
            from PIL import Image  # type: ignore

            image = Image.open(BytesIO(data)).convert("RGB")
            rgb = np.asarray(image, dtype=np.uint8)
        except ImportError:
            rgb = _decode_jpeg_with_qt(data)
        luma = (
            0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        ).astype(np.uint8)
        return DecodedImage(
            rgb=rgb,
            width=int(rgb.shape[1]),
            height=int(rgb.shape[0]),
            luma=luma,
            note="JPEG 尺寸来自码流本身",
        )


def _decode_jpeg_with_qt(data: bytes) -> np.ndarray:
    """没有 Pillow 时用 Qt 解码（GUI 环境一定有 Qt）。"""
    try:
        from PySide6.QtGui import QImage
    except ImportError as exc:  # pragma: no cover
        raise DecodeError(
            "解码 JPEG 需要 Pillow 或 PySide6：pip install pillow"
        ) from exc
    image = QImage.fromData(data)
    if image.isNull():
        raise DecodeError("JPEG 解码失败（Qt 无法识别码流）")
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    buffer = np.frombuffer(image.constBits(), dtype=np.uint8, count=stride * height)
    rgb = buffer.reshape(height, stride)[:, : width * 3].reshape(height, width, 3)
    return np.ascontiguousarray(rgb)


DECODERS: dict[str, ImageDecoder] = {
    "UYVY": YUV422Decoder(get_pixel_format("UYVY")),
    "YUYV": YUV422Decoder(get_pixel_format("YUYV")),
    "YVYU": YUV422Decoder(get_pixel_format("YVYU")),
    "VYUY": YUV422Decoder(get_pixel_format("VYUY")),
    "RGB565": RGB565Decoder(),
    "RGB888": RGB888Decoder(),
    "JPEG": JPEGDecoder(),
}


def get_decoder(pixel_format: str) -> ImageDecoder:
    key = pixel_format.upper()
    try:
        return DECODERS[key]
    except KeyError as exc:
        raise KeyError(f"未实现的像素格式解码器：{pixel_format}") from exc


def decode_image(
    data: bytes,
    width: int,
    height: int,
    config: ImageConfig,
) -> DecodedImage:
    return get_decoder(config.pixel_format).decode(data, width, height, config)
