"""文件导出：PNG / JPG / 原始 Buffer / 结构化 Hex / 元数据 / 调试会话。

约束（对应需求 8.3）：
  * PNG/JPG  = 当前解析后的图像（RGB888）
  * Raw      = 下位机实际发送的完整原始 Buffer（payload，不含 64 字节协议头）
  * Hex      = 当前结构化 Hex 视图（带行结构/像素组注释）
  * Metadata = 当前帧的解析参数与协议头
"""

from __future__ import annotations

import json
import struct
import time
import zlib
from pathlib import Path

import numpy as np

from core import APP_NAME
from core.hex.formatter import StructuredData
from core.image.frame import Frame
from core.log import get_logger

log = get_logger("export")


class ExportError(RuntimeError):
    pass


# ---------------------------------------------------------------------- #
# 图像
# ---------------------------------------------------------------------- #
def _prepare_rgb(image: np.ndarray) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ExportError(f"图像数据不是 RGB888：shape={array.shape}")
    return np.ascontiguousarray(array, dtype=np.uint8)


def write_png(path: str | Path, image: np.ndarray) -> Path:
    """纯标准库 PNG 写入（RGB888，无额外依赖）。"""
    path = Path(path)
    rgb = _prepare_rgb(image)
    height, width, _ = rgb.shape

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(height))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    log.info("导出 PNG：%s（%dx%d，%d 字节）", path, width, height, len(png))
    return path


def write_jpeg(path: str | Path, image: np.ndarray, quality: int = 95) -> Path:
    """JPG 需要 Qt（GUI 环境）或 Pillow，二选一即可。"""
    path = Path(path)
    rgb = _prepare_rgb(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PySide6.QtGui import QImage

        height, width, _ = rgb.shape
        qimage = QImage(
            rgb.data, width, height, int(rgb.strides[0]), QImage.Format.Format_RGB888
        ).copy()
        if not qimage.save(str(path), "JPG", quality):
            raise ExportError(f"Qt 保存 JPG 失败：{path}")
    except ImportError:
        try:
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise ExportError(
                "导出 JPG 需要 PySide6 或 Pillow：pip install pillow"
            ) from exc
        Image.fromarray(rgb).save(path, "JPEG", quality=quality)
    log.info("导出 JPG：%s（%dx%d）", path, rgb.shape[1], rgb.shape[0])
    return path


# ---------------------------------------------------------------------- #
# 原始数据
# ---------------------------------------------------------------------- #
def write_raw_buffer(path: str | Path, frame: Frame) -> Path:
    path = Path(path)
    if not frame.raw_data:
        raise ExportError("没有原始 Buffer，无法导出")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame.raw_data)
    log.info("导出原始 Buffer：%s（%d 字节）", path, len(frame.raw_data))
    return path


# ---------------------------------------------------------------------- #
# 结构化 Hex 文本
# ---------------------------------------------------------------------- #
def _hex_line(offset: int, data: bytes) -> str:
    hex_part = " ".join(f"{b:02x}" for b in data)
    if len(data) > 8:
        left, right = hex_part[: (8 * 3) - 1], hex_part[(8 * 3) - 1 :]
        hex_part = left + "  " + right.strip()
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{offset:08X}  {hex_part:<49} |{ascii_part}|"


def build_hex_text(
    frame: Frame,
    structured: StructuredData | None = None,
    per_line: int = 16,
) -> str:
    lines: list[str] = [
        f"# {APP_NAME} — structured hex dump",
        f"# frame: #{frame.frame_index}  {frame.width}x{frame.height}  "
        f"{frame.pixel_format}  {frame.protocol}",
        f"# buffer: {len(frame.raw_data)} bytes  stride: {frame.stride}",
        f"# exported: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# columns: offset  hex bytes  |ascii|",
        "",
    ]
    data = frame.raw_data
    if structured is None:
        for offset in range(0, len(data), per_line):
            lines.append(_hex_line(offset, data[offset : offset + per_line]))
        return "\r\n".join(lines) + "\r\n"

    for row in structured.rows:
        if row.kind in ("info", "warn", "hash"):
            lines.append("")
            lines.append(f"# {row.label}")
            continue
        if row.kind == "line":
            lines.append("")
            lines.append(f"# {row.label}")
            continue
        if row.kind == "sync":
            chunk = data[row.offset : row.offset + row.length]
            lines.append(_hex_line(row.offset, chunk) + f"   # {row.label}")
            continue
        if row.kind == "pixels":
            group_bytes = max(1, (row.length // row.groups) if row.groups else 4)
            for g in range(row.groups):
                offset = row.offset + g * group_bytes
                chunk = data[offset : offset + group_bytes]
                group = (row.group_start or 0) + g
                y_text = f"y={row.image_row}" if row.image_row is not None else ""
                lines.append(
                    _hex_line(offset, chunk)
                    + f"   # pixel-group {group:04d} {y_text}"
                )
            continue
        # raw / blank
        for offset in range(row.offset, row.offset + row.length, per_line):
            chunk = data[offset : min(offset + per_line, row.offset + row.length)]
            lines.append(_hex_line(offset, chunk))
    return "\r\n".join(lines) + "\r\n"


def write_hex_text(
    path: str | Path, frame: Frame, structured: StructuredData | None = None
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = build_hex_text(frame, structured)
    path.write_text(text, encoding="utf-8-sig", newline="")
    log.info("导出结构化 Hex：%s（%d 行）", path, text.count("\n"))
    return path


# ---------------------------------------------------------------------- #
# 元数据 / 会话
# ---------------------------------------------------------------------- #
def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def write_metadata(path: str | Path, frame: Frame) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        frame.to_metadata(), ensure_ascii=False, indent=2, default=_json_default
    )
    path.write_text(text, encoding="utf-8")
    log.info("导出帧元数据：%s", path)
    return path


def export_session(
    directory: str | Path,
    frame: Frame,
    structured: StructuredData | None,
    log_text: str = "",
    device_code: str | None = None,
) -> list[Path]:
    """保存一次完整调试会话（图像 + 原始数据 + Hex + 元数据 + 日志）。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    paths: list[Path] = []
    paths.append(write_raw_buffer(directory / f"frame_{stamp}.bin", frame))
    paths.append(write_png(directory / f"frame_{stamp}.png", frame.image))
    paths.append(
        write_hex_text(directory / f"frame_{stamp}_hex.txt", frame, structured)
    )
    paths.append(write_metadata(directory / f"frame_{stamp}.json", frame))
    if log_text:
        log_path = directory / f"session_{stamp}.log"
        log_path.write_text(log_text, encoding="utf-8")
        paths.append(log_path)
    if device_code:
        code_path = directory / f"device_upload_{stamp}.c"
        code_path.write_text(device_code, encoding="utf-8")
        paths.append(code_path)
    log.info("调试会话已保存到：%s（%d 个文件）", directory, len(paths))
    return paths
