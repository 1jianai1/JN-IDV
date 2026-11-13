"""BT.656 布局与坐标映射测试。"""

from __future__ import annotations

import unittest

from core.config import ImageConfig
from core.formats.bt656 import build_bt656_layout, scan_timing_codes
from tests.synthetic import make_uyvy


def small_config(stride: int = 128, width: int = 64, height: int = 8) -> ImageConfig:
    return ImageConfig(width=width, height=height, pixel_format="UYVY", stride=stride)


class UniformLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = make_uyvy(64, 8)
        self.layout = build_bt656_layout(self.data, small_config())

    def test_geometry(self) -> None:
        self.assertEqual(self.layout.mode, "uniform")
        self.assertEqual(self.layout.width, 64)
        self.assertEqual(self.layout.height, 8)
        self.assertEqual(self.layout.stride, 128)
        self.assertEqual(self.layout.bytes_per_pixel, 2)
        self.assertEqual(self.layout.row(0).data_start, 0)
        self.assertEqual(self.layout.row(7).data_start, 128 * 7)
        self.assertFalse(self.layout.has_codes)

    def test_byte_to_pixel_mapping(self) -> None:
        self.assertEqual(self.layout.locate(0).x, 0)
        self.assertEqual(self.layout.locate(0).y, 0)
        self.assertEqual(self.layout.locate(0).component, "U")
        self.assertEqual(self.layout.locate(1).component, "Y0")
        self.assertEqual(self.layout.locate(2).component, "V")
        self.assertEqual(self.layout.locate(2).shared_pixels, 2)
        self.assertEqual(self.layout.locate(3).x, 1)
        self.assertEqual(self.layout.locate(3).component, "Y1")
        self.assertEqual(self.layout.locate(127).x, 63)
        self.assertEqual(self.layout.locate(128).y, 1)
        self.assertEqual(self.layout.locate(1023).x, 63)
        self.assertEqual(self.layout.locate(1023).y, 7)

    def test_pixel_to_byte_mapping(self) -> None:
        self.assertEqual(self.layout.offset_of(0, 0), 0)
        self.assertEqual(self.layout.offset_of(1, 0, "Y1"), 3)
        self.assertEqual(self.layout.offset_of(1, 0, "Y0"), 1)
        self.assertEqual(self.layout.offset_of(63, 7, "Y1"), 7 * 128 + 127)
        self.assertIsNone(self.layout.offset_of(64, 0))
        self.assertIsNone(self.layout.offset_of(0, 8))

    def test_round_trip(self) -> None:
        for y in (0, 1, 7):
            for x in (0, 1, 2, 31, 63):
                component = "Y0" if x % 2 == 0 else "Y1"
                offset = self.layout.offset_of(x, y, component)
                self.assertIsNotNone(offset)
                location = self.layout.locate(offset)
                self.assertEqual(location.x, x)
                self.assertEqual(location.y, y)
                self.assertEqual(location.component, component)

    def test_padding_is_reported(self) -> None:
        layout = build_bt656_layout(make_uyvy(64, 8), small_config(stride=160))
        self.assertEqual(layout.stride, 160)
        self.assertEqual(layout.row(0).usable_bytes, 128)
        location = layout.locate(140)
        self.assertTrue(location.in_padding)
        self.assertEqual(location.y, 0)
        self.assertTrue(any("填充" in warning for warning in layout.warnings))

    def test_truncated_buffer(self) -> None:
        layout = build_bt656_layout(self.data[:800], small_config())
        self.assertEqual(layout.height, 6)
        self.assertTrue(any("不足一行" in warning for warning in layout.warnings))

    def test_too_short_for_one_row(self) -> None:
        layout = build_bt656_layout(b"\x00" * 40, small_config())
        self.assertEqual(layout.height, 0)
        self.assertFalse(layout.mapped)
        self.assertTrue(layout.warnings)

    def test_packed_rows(self) -> None:
        packed = self.layout.packed_rows()
        self.assertEqual(len(packed), 64 * 2 * 8)
        self.assertEqual(packed[:128], self.data[:128])
        self.assertEqual(packed[-128:], self.data[-128:])


class CodeLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = make_uyvy(64, 8, with_codes=True)
        self.layout = build_bt656_layout(self.data, small_config())

    def test_mode_is_codes(self) -> None:
        self.assertEqual(self.layout.mode, "codes")
        self.assertEqual(self.layout.width, 64)
        self.assertEqual(self.layout.height, 8)
        self.assertEqual(self.layout.stride, 128)
        self.assertEqual(len(self.layout.codes), 16)
        self.assertEqual(len(self.layout.lines), 8)

    def test_row_offsets_exclude_sync_codes(self) -> None:
        for y in range(8):
            self.assertEqual(self.layout.row(y).data_start, y * 136 + 4)
        self.assertEqual(self.layout.locate(0).y, None)
        self.assertEqual(self.layout.locate(4).y, 0)
        self.assertEqual(self.layout.locate(4).x, 0)
        self.assertEqual(self.layout.locate(131).x, 63)
        self.assertEqual(self.layout.locate(136).y, None)
        self.assertEqual(self.layout.locate(140).y, 1)

    def test_active_rows_are_packed_without_codes(self) -> None:
        packed = self.layout.packed_rows()
        self.assertEqual(len(packed), 64 * 2 * 8)
        self.assertEqual(packed[:128], self.data[4 : 4 + 128])
        self.assertEqual(packed[128:256], self.data[140 : 140 + 128])
        self.assertNotIn(b"\xff\x00\x00\x80", packed)

    def test_summary_fields(self) -> None:
        summary = self.layout.summary()
        self.assertEqual(summary["active_rows"], 8)
        self.assertEqual(summary["fields"], {"0": 8})
        self.assertEqual(summary["code_count"], 16)

    def test_scan_timing_codes(self) -> None:
        codes = scan_timing_codes(self.data)
        self.assertEqual(codes[0].kind, "SAV")
        self.assertTrue(codes[0].is_active)
        self.assertEqual(codes[1].kind, "EAV")
        self.assertEqual(codes[0].description, "F0 V0 H0 场1 有效行 SAV")

    def test_irregular_codes_falls_back_to_stride(self) -> None:
        data = make_uyvy(64, 8, leak_every=3)
        layout = build_bt656_layout(data, small_config())
        self.assertEqual(layout.mode, "uniform")
        self.assertTrue(layout.has_codes)
        self.assertTrue(any("定时码" in warning for warning in layout.warnings))

    def test_blanking_lines_are_not_image_rows(self) -> None:
        # 第一对 SAV/EAV 的 V=1（消隐行），不应计入图像行；后面 7 行有效
        blank = b"\xff\x00\x00\xab" + b"\x80" * 128 + b"\xff\x00\x00\xb6"
        active = b"\xff\x00\x00\x80" + b"\x80" * 128 + b"\xff\x00\x00\x9d"
        data = blank + active * 7
        layout = build_bt656_layout(data, ImageConfig(width=64, height=8, stride=128))
        self.assertEqual(layout.mode, "codes")
        self.assertEqual(len(layout.rows), 7)
        self.assertEqual(layout.rows[0].y, 0)
        self.assertEqual(layout.rows[0].data_start, len(blank) + 4)
        self.assertEqual(len(layout.lines), 8)


if __name__ == "__main__":
    unittest.main()
