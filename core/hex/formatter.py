"""结构化 Hex 数据组织：把原始 Buffer 变成「按协议结构排列的显示行」。

关键点：
  * 行对象只保存 offset/长度/行号等元信息，不复制每个字节，
    因此 30 万字节的 Buffer 也只有约一万个小对象，Hex Viewer 可以虚拟滚动。
  * 具体协议解析由 Formatter 完成；通用 Hex Viewer 只认识 ``HexRow``。
  * 显示行的 offset 单调递增且完整覆盖原始 Buffer（含消隐/间隙），
    所以搜索、跳转、字节 -> 像素映射都可以精确定位。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from core.formats.bt656 import TimingCode, xy_description
from core.formats.pixel_formats import get_pixel_format
from core.image.frame import Frame

RAW_ROW_BYTES = 16


@dataclass(frozen=True)
class HexRow:
    kind: str                 # info / line / sync / pixels / blank / raw
    offset: int
    length: int = 0
    label: str = ""
    line_index: int | None = None
    image_row: int | None = None
    group_start: int | None = None
    groups: int = 0
    xy: int | None = None       # sync 行的 BT.656 XY 字节
    field_id: int | None = None

    @property
    def end(self) -> int:
        return self.offset + self.length

    @property
    def has_bytes(self) -> bool:
        return self.length > 0

    @property
    def is_pixels(self) -> bool:
        return self.kind == "pixels"


@dataclass
class StructuredData:
    rows: list[HexRow]
    groups_per_row: int
    total_bytes: int
    pixel_format: str
    protocol: str
    image_width: int
    image_height: int
    legend: list[tuple[str, str]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    _data_offsets: list[int] = field(default_factory=list, repr=False)
    _data_rows: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        self._data_offsets = []
        self._data_rows = []
        for index, row in enumerate(self.rows):
            if row.length > 0:
                self._data_offsets.append(row.offset)
                self._data_rows.append(index)

    # ------------------------------------------------------------------ #
    def row_index_for_offset(self, offset: int) -> int | None:
        """返回包含该 offset 的显示行；越界时返回最近一行。"""
        if not self._data_offsets:
            return None
        index = bisect_right(self._data_offsets, offset) - 1
        if index < 0:
            index = 0
        return self._data_rows[index]

    def row_at(self, index: int) -> HexRow | None:
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def locate_offset_in_row(self, row_index: int, offset: int) -> int:
        """返回 offset 在显示行内的列号（按字节）。"""
        row = self.rows[row_index]
        return max(0, min(row.length - 1, offset - row.offset)) if row.length else 0

    def line_row_index(self, line_index: int) -> int | None:
        for index, row in enumerate(self.rows):
            if row.kind == "line" and row.line_index == line_index:
                return index
        return None

    def first_row_of_image(self, image_row: int) -> int | None:
        for index, row in enumerate(self.rows):
            if row.kind == "pixels" and row.image_row == image_row:
                return index
        return None

    @property
    def data_row_count(self) -> int:
        return len(self._data_rows)


class DataFormatter(ABC):
    key: str = ""

    @abstractmethod
    def format(self, frame: Frame, groups_per_row: int = 8) -> StructuredData:
        raise NotImplementedError


class BT656Formatter(DataFormatter):
    key = "bt656"

    # ------------------------------------------------------------------ #
    def format(self, frame: Frame, groups_per_row: int = 8) -> StructuredData:
        groups_per_row = max(1, min(64, int(groups_per_row)))
        layout = frame.layout
        data = frame.raw_data
        pf = get_pixel_format(frame.pixel_format)
        legend = list(pf.legend)

        rows: list[HexRow] = []
        summary = frame.summary_lines()
        if layout is not None:
            summary.append(f"Layout: {layout.mode_label}")
        rows.append(HexRow("info", 0, 0, "  ·  ".join(summary)))

        if layout is None or not layout.mapped or layout.mode == "raw":
            self._format_raw(rows, data)
        else:
            code_map = {code.offset: code for code in layout.codes}
            code_offsets = sorted(code_map)
            if layout.mode == "codes":
                self._format_codes(
                    rows, data, layout, pf, groups_per_row, code_map, code_offsets
                )
            else:
                self._format_uniform(
                    rows, data, layout, pf, groups_per_row, code_map, code_offsets
                )
            if layout.warnings:
                for warning in layout.warnings:
                    rows.append(HexRow("warn", 0, 0, warning))

        structured = StructuredData(
            rows=rows,
            groups_per_row=groups_per_row,
            total_bytes=len(data),
            pixel_format=frame.pixel_format,
            protocol=frame.protocol,
            image_width=frame.width,
            image_height=frame.height,
            legend=legend,
            summary=summary,
        )
        return structured

    # ------------------------------------------------------------------ #
    def _format_raw(self, rows: list[HexRow], data: bytes) -> None:
        for offset in range(0, len(data), RAW_ROW_BYTES):
            rows.append(
                HexRow("raw", offset, min(RAW_ROW_BYTES, len(data) - offset))
            )

    def _format_uniform(
        self,
        rows: list[HexRow],
        data: bytes,
        layout,
        pf,
        groups_per_row: int,
        code_map: dict[int, TimingCode],
        code_offsets: list[int],
    ) -> None:
        group_bytes = pf.group_bytes
        for row in layout.rows:
            rows.append(
                HexRow(
                    "line",
                    row.data_start,
                    0,
                    self._row_label(row, layout),
                    line_index=row.line_index,
                    image_row=row.y,
                )
            )
            self._emit_data(
                rows,
                data,
                row.data_start,
                row.data_start + row.usable_bytes,
                row.y,
                row.line_index,
                group_bytes,
                groups_per_row,
                code_map,
                code_offsets,
            )
            padding_start = row.data_start + row.usable_bytes
            padding_end = row.data_start + row.data_len
            if padding_end > padding_start:
                rows.append(
                    HexRow(
                        "hash",
                        padding_start,
                        0,
                        f"行尾填充 {padding_end - padding_start} 字节（Stride 内、超出有效宽度）",
                        image_row=row.y,
                        line_index=row.line_index,
                    )
                )
                for offset in range(padding_start, padding_end, RAW_ROW_BYTES):
                    rows.append(
                        HexRow(
                            "raw",
                            offset,
                            min(RAW_ROW_BYTES, padding_end - offset),
                            "行尾填充",
                            image_row=row.y,
                            line_index=row.line_index,
                        )
                    )
        # 末尾不足一行的数据
        covered = layout.rows[-1].data_start + layout.rows[-1].data_len if layout.rows else 0
        if covered < len(data):
            rows.append(
                HexRow("hash", covered, 0, f"尾部残余 {len(data) - covered} 字节（不足一行）")
            )
            for offset in range(covered, len(data), RAW_ROW_BYTES):
                rows.append(HexRow("raw", offset, min(RAW_ROW_BYTES, len(data) - offset)))

    def _format_codes(
        self,
        rows: list[HexRow],
        data: bytes,
        layout,
        pf,
        groups_per_row: int,
        code_map: dict[int, TimingCode],
        code_offsets: list[int],
    ) -> None:
        group_bytes = pf.group_bytes
        position = 0
        for span in layout.lines:
            if span.sav_offset > position:
                self._emit_gap(
                    rows, position, span.sav_offset, code_map, code_offsets, RAW_ROW_BYTES
                )
            active = span.data_len > 0 and not (span.sav_xy & 0x20)
            label = (
                f"LINE {span.index:03d}  {'有效行' if active else '消隐行'}"
                f"  field=F{span.field_id if span.field_id is not None else '-'}"
                f"  data={span.data_len} B"
            )
            if span.image_row is not None:
                label += f"  → 图像第 {span.image_row} 行"
            rows.append(
                HexRow(
                    "line",
                    span.sav_offset,
                    0,
                    label,
                    line_index=span.index,
                    image_row=span.image_row,
                    field_id=span.field_id,
                )
            )
            code = code_map.get(span.sav_offset)
            if code is not None:
                rows.append(self._sync_row(code, span.image_row, span.index))
            position = span.sav_offset + 4
            if span.data_end > position:
                if active:
                    self._emit_data(
                        rows,
                        data,
                        position,
                        span.data_end,
                        span.image_row,
                        span.index,
                        group_bytes,
                        groups_per_row,
                        code_map,
                        code_offsets,
                    )
                else:
                    for offset in range(position, span.data_end, RAW_ROW_BYTES):
                        rows.append(
                            HexRow(
                                "blank",
                                offset,
                                min(RAW_ROW_BYTES, span.data_end - offset),
                                "消隐数据",
                                line_index=span.index,
                            )
                        )
                position = span.data_end
            code = code_map.get(span.eav_offset)
            if code is not None:
                rows.append(self._sync_row(code, span.image_row, span.index))
                position = span.eav_offset + 4
        if position < len(data):
            self._emit_gap(rows, position, len(data), code_map, code_offsets, RAW_ROW_BYTES)

    # ------------------------------------------------------------------ #
    def _emit_data(
        self,
        rows: list[HexRow],
        data: bytes,
        start: int,
        end: int,
        image_row: int | None,
        line_index: int | None,
        group_bytes: int,
        groups_per_row: int,
        code_map: dict[int, TimingCode],
        code_offsets: list[int],
        row_start: int | None = None,
    ) -> None:
        """输出区间内的像素组；遇到定时码会先插一行 sync。"""
        if end <= start:
            return
        row_start = start if row_start is None else row_start
        position = start
        while position < end:
            code_offset = self._next_code(code_offsets, position, end)
            chunk_end = code_offset if code_offset is not None else end
            if chunk_end > position:
                if group_bytes <= 0:
                    for offset in range(position, chunk_end, RAW_ROW_BYTES):
                        rows.append(
                            HexRow(
                                "raw",
                                offset,
                                min(RAW_ROW_BYTES, chunk_end - offset),
                                line_index=line_index,
                                image_row=image_row,
                            )
                        )
                else:
                    self._emit_groups(
                        rows,
                        position,
                        chunk_end,
                        image_row,
                        line_index,
                        group_bytes,
                        groups_per_row,
                        row_start,
                    )
                position = chunk_end
            if code_offset is not None:
                code = code_map[code_offset]
                rows.append(self._sync_row(code, image_row, line_index))
                position = code_offset + 4

    def _emit_groups(
        self,
        rows: list[HexRow],
        start: int,
        end: int,
        image_row: int | None,
        line_index: int | None,
        group_bytes: int,
        groups_per_row: int,
        row_start: int,
    ) -> None:
        base_group = (start - row_start) // group_bytes
        total_groups = (end - start) // group_bytes
        remainder = (end - start) % group_bytes
        done = 0
        while done < total_groups:
            count = min(groups_per_row, total_groups - done)
            rows.append(
                HexRow(
                    "pixels",
                    start + done * group_bytes,
                    count * group_bytes,
                    groups=count,
                    group_start=base_group + done,
                    image_row=image_row,
                    line_index=line_index,
                )
            )
            done += count
        if remainder:
            rows.append(
                HexRow(
                    "raw",
                    start + total_groups * group_bytes,
                    remainder,
                    "不足一个像素组",
                    image_row=image_row,
                    line_index=line_index,
                )
            )

    def _emit_gap(
        self,
        rows: list[HexRow],
        start: int,
        end: int,
        code_map: dict[int, TimingCode],
        code_offsets: list[int],
        row_bytes: int,
    ) -> None:
        """行间/消隐间隙：按原始字节显示，遇到定时码单独标出。"""
        position = start
        while position < end:
            code_offset = self._next_code(code_offsets, position, end)
            chunk_end = code_offset if code_offset is not None else end
            if chunk_end > position:
                for offset in range(position, chunk_end, row_bytes):
                    rows.append(
                        HexRow("blank", offset, min(row_bytes, chunk_end - offset), "行间隙/消隐")
                    )
                position = chunk_end
            if code_offset is not None:
                rows.append(self._sync_row(code_map[code_offset], None, None))
                position = code_offset + 4

    @staticmethod
    def _next_code(code_offsets: list[int], start: int, end: int) -> int | None:
        index = bisect_left(code_offsets, start)
        if index < len(code_offsets) and code_offsets[index] < end:
            return code_offsets[index]
        return None

    @staticmethod
    def _sync_row(
        code: TimingCode, image_row: int | None, line_index: int | None
    ) -> HexRow:
        return HexRow(
            "sync",
            code.offset,
            4,
            f"{code.kind} 0x{code.xy:02X}  {xy_description(code.xy)}",
            line_index=line_index,
            image_row=image_row,
            xy=code.xy,
        )

    @staticmethod
    def _row_label(row, layout) -> str:
        return (
            f"ROW {row.y:03d}   offset=0x{row.data_start:08X}  "
            f"data={row.data_len} B  usable={row.usable_bytes} B"
        )


def format_frame(frame: Frame, groups_per_row: int = 8) -> StructuredData:
    if frame.protocol == "bt656":
        return BT656Formatter().format(frame, groups_per_row)
    raise KeyError(f"未实现的数据格式化器：{frame.protocol}")


def format_plain(frame: Frame, bytes_per_row: int = 16) -> StructuredData:
    """纯十六进制视图（不带协议结构），用于和结构化视图对照。"""
    data = frame.raw_data
    rows: list[HexRow] = [HexRow("info", 0, 0, "  ·  ".join(frame.summary_lines()))]
    for offset in range(0, len(data), bytes_per_row):
        rows.append(HexRow("raw", offset, min(bytes_per_row, len(data) - offset)))
    return StructuredData(
        rows=rows,
        groups_per_row=max(1, bytes_per_row // 4),
        total_bytes=len(data),
        pixel_format=frame.pixel_format,
        protocol=frame.protocol,
        image_width=frame.width,
        image_height=frame.height,
        legend=[],
        summary=frame.summary_lines(),
    )
