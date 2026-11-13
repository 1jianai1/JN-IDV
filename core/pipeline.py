"""核心流水线：原始 Buffer -> BT.656 布局 -> 有效像素 -> 解码 -> Frame。

GUI 的采集线程、CLI、离线文件加载都走这一条路径，保证结果一致。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import ImageConfig
from core.formats.bt656 import analyze_layout, build_bt656_layout
from core.image.decoders import DecodeError, decode_image
from core.image.frame import Frame
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.hex.formatter import StructuredData

log = get_logger("pipeline")


def build_frame(
    payload: bytes,
    image: ImageConfig,
    header: dict | None = None,
    *,
    frame_index: int = 0,
    protocol: str = "bt656",
    source: str = "live",
    extra_metadata: dict | None = None,
) -> Frame:
    if not payload:
        raise DecodeError("原始 Buffer 为空，无法解析图像")

    started = time.perf_counter()
    if protocol != "bt656":
        raise DecodeError(f"暂未实现协议 {protocol} 的图像解析")

    layout = build_bt656_layout(payload, image)
    layout_done = time.perf_counter()

    if layout.mapped:
        pixels = layout.packed_rows()
        width, height = layout.width, layout.height
        if width <= 0 or height <= 0:
            raise DecodeError(
                f"数据布局无法形成图像（{len(payload)} 字节，Stride={layout.stride}）"
            )
    else:
        pixels = payload
        width, height = image.width, image.height

    decoded = decode_image(pixels, width, height, image)
    decode_done = time.perf_counter()

    metadata = {
        "layout_ms": round((layout_done - started) * 1000.0, 2),
        "decode_ms": round((decode_done - layout_done) * 1000.0, 2),
        "build_ms": round((decode_done - started) * 1000.0, 2),
        "decode_note": decoded.note,
        "warnings": list(layout.warnings),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    if layout.mapped and (
        decoded.width != layout.width or decoded.height != layout.height
    ):
        metadata.setdefault("warnings", []).append(
            f"解码尺寸 {decoded.width}x{decoded.height} 与布局 {layout.width}x{layout.height} 不同"
        )

    frame = Frame(
        raw_data=bytes(payload),
        image=decoded.rgb,
        width=decoded.width,
        height=decoded.height,
        pixel_format=image.pixel_format,
        protocol=protocol,
        frame_index=frame_index,
        timestamp=time.time(),
        luma=decoded.luma,
        yuv=decoded.yuv,
        layout=layout,
        config=image,
        header=header,
        metadata=metadata,
        source=source,
    )
    log.info(
        "Frame #%d 解析完成：%dx%d %s，原始 %d 字节，布局=%s，用时 %.1f ms",
        frame_index,
        frame.width,
        frame.height,
        image.pixel_format,
        len(payload),
        layout.mode_label,
        metadata["build_ms"],
    )
    for warning in metadata["warnings"]:
        log.warning("解析告警：%s", warning)
    if decoded.note:
        log.info("解码说明：%s", decoded.note)
    return frame


def build_structured(frame: Frame, groups_per_row: int = 8) -> "StructuredData":
    from core.hex.formatter import format_frame

    return format_frame(frame, groups_per_row=groups_per_row)


def analyze_frame(frame: Frame) -> list[str]:
    """给 CLI / Log 用的完整分析报告。"""
    lines = [
        f"Frame #{frame.frame_index}  {frame.width}x{frame.height}  "
        f"{frame.pixel_format}  {frame.source}",
        f"Buffer: {frame.raw_size} 字节",
    ]
    if frame.layout is not None:
        lines.extend(analyze_layout(frame.layout, frame.header))
    for label, value in frame.info_lines():
        lines.append(f"{label}: {value}")
    note = frame.metadata.get("decode_note")
    if note:
        lines.append("解码说明: " + note)
    return lines


def load_raw_file(
    path: str | Path,
    image: ImageConfig,
    *,
    frame_index: int = 0,
) -> Frame:
    """离线加载下位机保存的 payload（不含 64 字节协议头）。"""
    data = Path(path).read_bytes()
    log.info("加载原始 Buffer：%s（%d 字节）", path, len(data))
    return build_frame(
        data,
        image,
        header={"source_file": str(path), "length": len(data), "offset": 0, "total": len(data)},
        frame_index=frame_index,
        protocol=image.protocol,
        source="file",
    )
