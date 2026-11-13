"""结构化 Hex 组织测试：覆盖完整、按行/像素组切分、Offset 定位。"""

from __future__ import annotations

import unittest

from core.config import ImageConfig
from core.hex.formatter import BT656Formatter, format_frame, format_plain
from core.image.frame import Frame
from core.pipeline import build_frame
from tests.synthetic import make_uyvy


def assert_full_coverage(test: unittest.TestCase, structured, total: int) -> None:
    cursor = 0
    for row in structured.rows:
        if row.length == 0:
            continue
        test.assertEqual(row.offset, cursor, f"row {row.kind} 不连续：{row.offset} != {cursor}")
        cursor += row.length
    test.assertEqual(cursor, total)


class UniformFormatterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = make_uyvy(128, 16)
        cls.config = ImageConfig(width=128, height=16, pixel_format="UYVY", stride=256)
        cls.frame = build_frame(cls.data, cls.config, {"total": len(cls.data)})
        cls.structured = format_frame(cls.frame, groups_per_row=8)

    def test_rows_cover_entire_buffer(self) -> None:
        assert_full_coverage(self, self.structured, len(self.data))

    def test_pixel_rows_shape(self) -> None:
        pixel_rows = [row for row in self.structured.rows if row.kind == "pixels"]
        self.assertTrue(pixel_rows)
        for row in pixel_rows:
            self.assertEqual(row.length % 4, 0)
            self.assertEqual(row.length, row.groups * 4)
            self.assertLessEqual(row.groups, 8)
        # 16 行 × (256/32=8 行每组) = 128 个像素行
        self.assertEqual(len(pixel_rows), 16 * 8)

    def test_line_rows(self) -> None:
        line_rows = [row for row in self.structured.rows if row.kind == "line"]
        self.assertEqual(len(line_rows), 16)
        self.assertEqual(line_rows[0].image_row, 0)
        self.assertEqual(line_rows[-1].image_row, 15)

    def test_row_lookup(self) -> None:
        for offset in (0, 31, 32, 200, 255, 256, len(self.data) - 1):
            index = self.structured.row_index_for_offset(offset)
            self.assertIsNotNone(index)
            row = self.structured.rows[index]
            self.assertLessEqual(row.offset, offset)
            self.assertLess(offset, row.offset + row.length)

    def test_groups_per_row_setting(self) -> None:
        structured = format_frame(self.frame, groups_per_row=4)
        pixel_rows = [row for row in structured.rows if row.kind == "pixels"]
        self.assertTrue(all(row.groups <= 4 for row in pixel_rows))
        self.assertEqual(pixel_rows[0].groups, 4)
        assert_full_coverage(self, structured, len(self.data))


class CodeFormatterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = make_uyvy(64, 8, with_codes=True)
        cls.config = ImageConfig(width=64, height=8, pixel_format="UYVY", stride=128)
        cls.frame = build_frame(cls.data, cls.config)
        cls.structured = format_frame(cls.frame, groups_per_row=4)

    def test_mode(self) -> None:
        self.assertEqual(self.frame.layout.mode, "codes")
        assert_full_coverage(self, self.structured, len(self.data))

    def test_sync_rows(self) -> None:
        sync_rows = [row for row in self.structured.rows if row.kind == "sync"]
        self.assertEqual(len(sync_rows), 16)
        self.assertEqual(sync_rows[0].offset, 0)
        self.assertEqual(sync_rows[0].length, 4)
        self.assertEqual(sync_rows[0].xy, 0x80)
        self.assertIn("SAV", sync_rows[0].label)
        self.assertEqual(sync_rows[1].xy, 0x9D)
        self.assertEqual((sync_rows[0].image_row, sync_rows[0].line_index), (0, 0))

    def test_sync_rows_are_not_pixel_groups(self) -> None:
        pixel_rows = [row for row in self.structured.rows if row.kind == "pixels"]
        for row in pixel_rows:
            self.assertNotIn("SAV", row.label)
            self.assertNotIn("EAV", row.label)

    def test_lookup_inside_sync(self) -> None:
        index = self.structured.row_index_for_offset(0)
        self.assertEqual(self.structured.rows[index].kind, "sync")
        index = self.structured.row_index_for_offset(4)
        self.assertEqual(self.structured.rows[index].kind, "pixels")


class PlainFormatterTests(unittest.TestCase):
    def test_plain_rows(self) -> None:
        data = make_uyvy(32, 4)
        frame = build_frame(data, ImageConfig(width=32, height=4, stride=64))
        structured = format_plain(frame, 16)
        assert_full_coverage(self, structured, len(data))
        rows = [row for row in structured.rows if row.length]
        self.assertEqual(len(rows), len(data) // 16)
        self.assertEqual(rows[0].offset, 0)
        self.assertEqual(rows[0].length, 16)


if __name__ == "__main__":
    unittest.main()
