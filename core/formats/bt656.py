"""BT.656 / ITU-R BT.656 数据结构解析与坐标映射。

职责边界：
  * 只负责「原始 Buffer 里的字节属于哪一行、哪一组、哪个分量」。
  * 不做颜色转换（那是 ``core.image.decoders``）。
  * 不假设一定存在定时码：部分采集接口在裁剪 / 嵌入式同步模式下
    通常会把 SAV/EAV 吃掉，因此提供两种布局：
      - ``codes``：识别到规则 SAV/EAV 行结构（真·BT.656 带同步码）
      - ``uniform``：没有同步码，按 Stride 均匀切行（常见）
      - ``raw``：无法建立行列映射（例如 JPEG）
  * 两种布局都提供 字节偏移 <-> (x, y) 的精确映射。
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import dataclass

from core.config import ImageConfig
from core.formats.pixel_formats import PixelFormat, get_pixel_format

# XY 字节：1 F V H P3P2P1P0（F=场，V=消隐，H=SAV/EAV）
VALID_XY: dict[int, str] = {
    0x80: "F0 V0 H0 场1 有效行 SAV",
    0x9D: "F0 V0 H1 场1 有效行 EAV",
    0xAB: "F0 V1 H0 场1 消隐行 SAV",
    0xB6: "F0 V1 H1 场1 消隐行 EAV",
    0xC7: "F1 V0 H0 场2 有效行 SAV",
    0xDA: "F1 V0 H1 场2 有效行 EAV",
    0xEC: "F1 V1 H0 场2 消隐行 SAV",
    0xF1: "F1 V1 H1 场2 消隐行 EAV",
}


def xy_description(xy: int) -> str:
    return VALID_XY.get(xy, "未知定时码")


@dataclass(frozen=True)
class TimingCode:
    offset: int
    xy: int

    @property
    def f(self) -> int:
        return (self.xy >> 6) & 1

    @property
    def v(self) -> int:
        return (self.xy >> 5) & 1

    @property
    def h(self) -> int:
        return (self.xy >> 4) & 1

    @property
    def is_sav(self) -> bool:
        return self.h == 0

    @property
    def is_eav(self) -> bool:
        return self.h == 1

    @property
    def field(self) -> int:
        return self.f

    @property
    def is_active(self) -> bool:
        return self.v == 0

    @property
    def kind(self) -> str:
        return "SAV" if self.is_sav else "EAV"

    @property
    def description(self) -> str:
        return xy_description(self.xy)


def scan_timing_codes(data: bytes | bytearray) -> list[TimingCode]:
    """扫描 ``FF 00 00 XY`` 定时码；XY 必须落在 BT.656 合法集合内。"""
    codes: list[TimingCode] = []
    start = 0
    finder = b"\xff\x00\x00"
    view = bytes(data)
    while True:
        index = view.find(finder, start)
        if index < 0 or index + 4 > len(view):
            break
        xy = view[index + 3]
        if xy in VALID_XY:
            codes.append(TimingCode(index, xy))
            start = index + 4
        else:
            start = index + 1
    return codes


@dataclass(frozen=True)
class LineSpan:
    """一对 SAV..EAV 之间的行（不包含码字本身）。"""

    index: int
    sav_offset: int
    sav_xy: int
    data_start: int
    data_end: int
    eav_offset: int
    eav_xy: int
    image_row: int | None = None
    field_id: int | None = None

    @property
    def data_len(self) -> int:
        return max(0, self.data_end - self.data_start)

    @property
    def is_active(self) -> bool:
        return (self.sav_xy & 0x20) == 0 and self.data_len > 0

    @property
    def kind_label(self) -> str:
        return "有效行" if self.is_active else "消隐行"

    @property
    def summary(self) -> str:
        return (
            f"LINE {self.index:03d}  {self.kind_label}  "
            f"SAV@0x{self.sav_offset:08X} (0x{self.sav_xy:02X})  "
            f"data={self.data_len} bytes  EAV@0x{self.eav_offset:08X}"
        )


@dataclass(frozen=True)
class ActiveRow:
    y: int
    data_start: int
    data_len: int
    usable_bytes: int
    line_index: int | None = None
    field_id: int | None = None
    sav_offset: int | None = None
    eav_offset: int | None = None

    @property
    def data_end(self) -> int:
        return self.data_start + self.data_len


@dataclass(frozen=True)
class PixelLocation:
    offset: int
    y: int | None = None
    x: int | None = None
    line_index: int | None = None
    pixel_group_index: int | None = None
    component: str | None = None
    pixel_in_group: int | None = None
    shared_pixels: int = 1
    in_padding: bool = False
    in_gap: bool = False

    def describe(self) -> str:
        parts = [f"offset=0x{self.offset:08X} ({self.offset})"]
        if self.y is not None:
            parts.append(f"y={self.y}")
        if self.x is not None:
            parts.append(f"x={self.x}")
        if self.pixel_group_index is not None:
            parts.append(f"group={self.pixel_group_index}")
        if self.component:
            parts.append(f"component={self.component}")
        if self.in_padding:
            parts.append("行尾填充")
        if self.in_gap:
            parts.append("非有效图像区")
        return "  ".join(parts)


class BT656Layout:
    """一帧原始 Buffer 的结构描述 + 坐标双向映射。"""

    def __init__(
        self,
        data: bytes,
        pixel_format: PixelFormat,
        mode: str,
        rows: list[ActiveRow],
        lines: list[LineSpan],
        codes: list[TimingCode],
        width: int,
        height: int,
        stride: int,
        warnings: list[str] | None = None,
    ) -> None:
        self.data = data
        self.pixel_format = pixel_format
        self.mode = mode
        self.rows = rows
        self.lines = lines
        self.codes = codes
        self.width = width
        self.height = height
        self.stride = stride
        self.warnings = warnings or []
        self._starts = [row.data_start for row in rows]

    # ------------------------------------------------------------------ #
    @property
    def has_codes(self) -> bool:
        return bool(self.codes)

    @property
    def mapped(self) -> bool:
        return bool(self.rows) and self.width > 0

    @property
    def total_bytes(self) -> int:
        return len(self.data)

    @property
    def group_bytes(self) -> int:
        return self.pixel_format.group_bytes

    @property
    def bytes_per_pixel(self) -> int:
        return self.pixel_format.bytes_per_pixel

    @property
    def image_height(self) -> int:
        return len(self.rows)

    @property
    def mode_label(self) -> str:
        return {
            "codes": "带定时码行结构",
            "uniform": "Stride 均匀切行",
            "raw": "原始字节（无行列映射）",
        }.get(self.mode, self.mode)

    def row(self, y: int) -> ActiveRow | None:
        if 0 <= y < len(self.rows):
            return self.rows[y]
        return None

    def group_count(self, y: int) -> int:
        row = self.row(y)
        if row is None or self.group_bytes <= 0:
            return 0
        return row.usable_bytes // self.group_bytes

    def group_offset(self, y: int, group: int) -> int | None:
        row = self.row(y)
        if row is None:
            return None
        offset = row.data_start + group * self.group_bytes
        if offset + self.group_bytes > row.data_start + row.usable_bytes:
            return None
        return offset

    # ------------------------------------------------------------------ #
    def locate(self, offset: int) -> PixelLocation:
        """Buffer 偏移 -> 像素位置（若落在行尾填充/行间空隙则给出说明）。"""
        if offset < 0 or offset >= len(self.data):
            return PixelLocation(offset=offset, in_gap=True)
        if not self.rows:
            return PixelLocation(offset=offset, in_gap=True)

        index = bisect_right(self._starts, offset) - 1
        if index < 0:
            return PixelLocation(offset=offset, in_gap=True)
        row = self.rows[index]
        if offset >= row.data_end:
            return PixelLocation(offset=offset, in_gap=True)

        col = offset - row.data_start
        if self.group_bytes <= 0 or col >= row.usable_bytes:
            return PixelLocation(
                offset=offset,
                y=row.y,
                line_index=row.line_index,
                in_padding=True,
            )

        group = col // self.group_bytes
        within = col % self.group_bytes
        fmt = self.pixel_format
        if fmt.is_yuv422:
            component = fmt.lane_components[within]
            if component in ("Y0", "Y1"):
                x = group * 2 + (1 if component == "Y1" else 0)
                pixel_in_group = 0 if component == "Y0" else 1
                shared = 1
            else:
                x = group * 2
                pixel_in_group = None
                shared = 2
            return PixelLocation(
                offset=offset,
                y=row.y,
                x=x,
                line_index=row.line_index,
                pixel_group_index=group,
                component=component,
                pixel_in_group=pixel_in_group,
                shared_pixels=shared,
            )
        if fmt.kind in ("rgb565", "rgb888"):
            return PixelLocation(
                offset=offset,
                y=row.y,
                x=group,
                line_index=row.line_index,
                pixel_group_index=group,
                component=fmt.lane_components[within],
                pixel_in_group=0,
                shared_pixels=1,
            )
        return PixelLocation(
            offset=offset,
            y=row.y,
            line_index=row.line_index,
            pixel_group_index=group,
        )

    def offset_of(
        self, x: int, y: int, component: str | None = None
    ) -> int | None:
        """(x, y, 分量) -> Buffer 偏移；越界返回 None。"""
        row = self.row(y)
        if row is None or self.group_bytes <= 0:
            return None
        fmt = self.pixel_format
        if fmt.is_yuv422:
            if x < 0 or x >= self.width:
                return None
            group = x // 2
            base = row.data_start + group * self.group_bytes
            if base + self.group_bytes > row.data_start + row.usable_bytes:
                return None
            lanes = fmt.lane_components
            if component in (None, ""):
                lane = 0
            elif component == "Y":
                lane = lanes.index("Y0" if x % 2 == 0 else "Y1")
            elif component in lanes:
                lane = lanes.index(component)
            else:
                lane = 0
            return base + lane
        if fmt.kind in ("rgb565", "rgb888"):
            if x < 0 or x >= self.width:
                return None
            base = row.data_start + x * self.group_bytes
            if base + self.group_bytes > row.data_start + row.usable_bytes:
                return None
            lanes = fmt.lane_components
            lane = lanes.index(component) if component in lanes else 0
            return base + lane
        return None

    # ------------------------------------------------------------------ #
    def packed_rows(self) -> bytes:
        """提取有效行并压紧成连续图像数据（每行恰好 width × bpp 字节）。"""
        row_bytes = self.width * self.bytes_per_pixel
        if row_bytes <= 0 or not self.rows:
            return b""
        out = bytearray(row_bytes * len(self.rows))
        for y, row in enumerate(self.rows):
            take = min(row.usable_bytes, row_bytes)
            start = y * row_bytes
            out[start : start + take] = self.data[row.data_start : row.data_start + take]
        return bytes(out)

    def summary(self) -> dict:
        lengths = [row.data_len for row in self.rows]
        fields: dict[int, int] = {}
        for row in self.rows:
            if row.field_id is not None:
                fields[row.field_id] = fields.get(row.field_id, 0) + 1
        return {
            "mode": self.mode,
            "mode_label": self.mode_label,
            "total_bytes": len(self.data),
            "width": self.width,
            "height": self.height,
            "stride": self.stride,
            "bytes_per_pixel": self.bytes_per_pixel,
            "has_codes": self.has_codes,
            "code_count": len(self.codes),
            "line_count": len(self.lines),
            "active_rows": len(self.rows),
            "row_length_min": min(lengths) if lengths else 0,
            "row_length_max": max(lengths) if lengths else 0,
            "row_length_median": int(statistics.median(lengths)) if lengths else 0,
            "fields": {str(k): v for k, v in sorted(fields.items())},
            "warnings": list(self.warnings),
        }


def build_bt656_layout(data: bytes, image: ImageConfig) -> BT656Layout:
    """根据原始 Buffer + 配置建立布局；自动在 codes / uniform / raw 间选择。"""
    data = bytes(data)
    fmt = get_pixel_format(image.pixel_format)
    warnings: list[str] = []

    if fmt.kind == "jpeg":
        warnings.append("JPEG 为压缩码流：尺寸由码流决定，不做逐像素 Buffer 映射")
        return BT656Layout(
            data=data,
            pixel_format=fmt,
            mode="raw",
            rows=[],
            lines=[],
            codes=[],
            width=0,
            height=0,
            stride=0,
            warnings=warnings,
        )

    bpp = fmt.bytes_per_pixel
    configured_width = max(0, int(image.width))
    configured_height = max(0, int(image.height))
    stride = image.effective_stride()
    codes = scan_timing_codes(data)

    # ---- 尝试按真实 SAV/EAV 建立行结构 -------------------------------- #
    pairs: list[tuple[TimingCode, TimingCode]] = []
    for i in range(len(codes) - 1):
        sav, eav = codes[i], codes[i + 1]
        if sav.is_sav and eav.is_eav and eav.offset >= sav.offset + 4:
            pairs.append((sav, eav))
    active_pairs = [
        (s, e) for s, e in pairs if s.is_active and (e.offset - s.offset - 4) > 0
    ]
    expected_height = configured_height or 240
    if active_pairs:
        lengths = [e.offset - s.offset - 4 for s, e in active_pairs]
        median_len = statistics.median(lengths)
        uniform = sum(1 for length in lengths if abs(length - median_len) <= 8)
        enough_rows = len(active_pairs) >= max(4, int(expected_height * 0.5))
        if enough_rows and uniform >= len(lengths) * 0.9 and median_len >= 16:
            min_line = (configured_width * bpp) if configured_width else 16
            width = (
                min(configured_width, int(median_len) // bpp)
                if configured_width
                else int(median_len) // bpp
            )
            if width <= 0:
                width = int(median_len) // max(1, bpp)
            rows: list[ActiveRow] = []
            lines: list[LineSpan] = []
            image_row = 0
            pair_index = 0
            pair_set = {(s.offset, e.offset) for s, e in active_pairs}
            for sav, eav in pairs:
                data_start, data_end = sav.offset + 4, eav.offset
                is_active = (sav.offset, eav.offset) in pair_set
                row_index = image_row if is_active else None
                span = LineSpan(
                    index=pair_index,
                    sav_offset=sav.offset,
                    sav_xy=sav.xy,
                    data_start=data_start,
                    data_end=data_end,
                    eav_offset=eav.offset,
                    eav_xy=eav.xy,
                    image_row=row_index,
                    field_id=sav.field,
                )
                lines.append(span)
                if is_active:
                    usable = min(data_end - data_start, width * bpp)
                    rows.append(
                        ActiveRow(
                            y=image_row,
                            data_start=data_start,
                            data_len=data_end - data_start,
                            usable_bytes=usable,
                            line_index=pair_index,
                            field_id=sav.field,
                            sav_offset=sav.offset,
                            eav_offset=eav.offset,
                        )
                    )
                    image_row += 1
                pair_index += 1
            if len(rows) != expected_height:
                warnings.append(
                    f"检测到 {len(rows)} 行有效图像（配置为 {configured_height} 行）"
                )
            if median_len < min_line:
                warnings.append(
                    f"行内有效数据 {int(median_len)} 字节，小于配置宽度所需 {min_line} 字节"
                )
            return BT656Layout(
                data=data,
                pixel_format=fmt,
                mode="codes",
                rows=rows,
                lines=lines,
                codes=codes,
                width=width,
                height=len(rows),
                stride=int(median_len),
                warnings=warnings,
            )
        if active_pairs:
            warnings.append(
                f"检测到 {len(codes)} 个定时码，但行长度不一致/行数不足，"
                "改用 Stride 均匀切行（定时码仍会在 Hex Viewer 标出）"
            )

    # ---- 回退：按 Stride 均匀切行 ------------------------------------ #
    if stride <= 0 or bpp <= 0:
        warnings.append("无法确定 Stride，按原始字节显示")
        return BT656Layout(
            data=data,
            pixel_format=fmt,
            mode="raw",
            rows=[],
            lines=[],
            codes=codes,
            width=0,
            height=0,
            stride=0,
            warnings=warnings,
        )

    full_rows = len(data) // stride
    height = min(configured_height, full_rows) if configured_height > 0 else full_rows
    width = min(configured_width, stride // bpp) if configured_width > 0 else stride // bpp
    if width <= 0:
        width = stride // max(1, bpp)
    rows = []
    for y in range(height):
        start = y * stride
        available = min(stride, len(data) - start)
        usable = min(available, width * bpp)
        rows.append(
            ActiveRow(
                y=y,
                data_start=start,
                data_len=available,
                usable_bytes=usable,
                line_index=y,
            )
        )
    if full_rows == 0:
        warnings.append(
            f"数据只有 {len(data)} 字节，不足一行 Stride={stride}，无法显示图像"
        )
    elif len(data) % stride:
        warnings.append(f"末尾有 {len(data) % stride} 字节不足一行，未参与图像显示")
    if stride > width * bpp:
        warnings.append(
            f"Stride({stride}) 比一行有效像素多 {stride - width * bpp} 字节"
            "（行尾填充，不计入图像）"
        )
    if configured_height and full_rows < configured_height:
        warnings.append(
            f"按 Stride 只切出 {full_rows} 行，配置高度为 {configured_height} 行"
            "（常见于单场行数不足 / 采集未搬满）"
        )
    if codes:
        warnings.append(
            f"缓冲内检测到 {len(codes)} 个 BT.656 定时码（码未完全被采集接口吃掉），"
            "已按 Stride 切行，码字会在 Hex Viewer 中单独标出"
        )
    if height <= 0:
        height = 0
    return BT656Layout(
        data=data,
        pixel_format=fmt,
        mode="uniform",
        rows=rows,
        lines=[],
        codes=codes,
        width=width,
        height=height,
        stride=stride,
        warnings=warnings,
    )


def analyze_layout(layout: BT656Layout, header: dict | None = None) -> list[str]:
    """生成给 CLI / Log 用的结构分析文本。"""
    lines: list[str] = []
    if header:
        lines.append(
            "帧头: total=%s offset=%s length=%s flags=0x%02X(%s)"
            % (
                header.get("total"),
                header.get("offset"),
                header.get("length"),
                header.get("flags", 0),
                header.get("flags_text", "-"),
            )
        )
        lines.append(
            "状态快照: state0=0x%08X state1=0x%08X state2=0x%08X state3=0x%08X state4=%d"
            % (
                header.get("state0", 0),
                header.get("state1", 0),
                header.get("state2", 0),
                header.get("state3", 0),
                header.get("state4", 0),
            )
        )
    lines.append(
        "Buffer: %d 字节  %s  %dx%d  pixel=%s  stride=%d  bpp=%d"
        % (
            layout.total_bytes,
            layout.mode_label,
            layout.width,
            layout.height,
            layout.pixel_format.key,
            layout.stride,
            layout.bytes_per_pixel,
        )
    )
    if layout.codes:
        hist: dict[int, int] = {}
        for code in layout.codes:
            hist[code.xy] = hist.get(code.xy, 0) + 1
        lines.append("定时码: 共 %d 个" % len(layout.codes))
        for xy in sorted(hist):
            lines.append(
                "  0x%02X  %-26s x%d" % (xy, xy_description(xy), hist[xy])
            )
        gaps: dict[int, int] = {}
        for i in range(1, len(layout.codes)):
            gap = layout.codes[i].offset - layout.codes[i - 1].offset
            gaps[gap] = gaps.get(gap, 0) + 1
        top = sorted(gaps.items(), key=lambda item: -item[1])[:8]
        lines.append("码间距 Top: " + "  ".join(f"{gap}:{count}" for gap, count in top))
    else:
        lines.append("定时码: 无（采集接口已吃掉 SAV/EAV，按 Stride 切行）")
    if layout.rows:
        lengths = [row.data_len for row in layout.rows]
        lines.append(
            "行长: min=%d median=%d max=%d  有效行=%d"
            % (
                min(lengths),
                int(statistics.median(lengths)),
                max(lengths),
                len(layout.rows),
            )
        )
        fields: dict[int, int] = {}
        for row in layout.rows:
            if row.field_id is not None:
                fields[row.field_id] = fields.get(row.field_id, 0) + 1
        if fields:
            lines.append(
                "按场统计: " + "  ".join(f"F{k}:{v}行" for k, v in sorted(fields.items()))
            )
    for warning in layout.warnings:
        lines.append("⚠ " + warning)
    return lines
