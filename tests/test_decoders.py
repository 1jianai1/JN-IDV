"""图像解码器测试：YUV422 四种排列、RGB565/888、JPEG、异常数据。"""

from __future__ import annotations

import unittest

import numpy as np

from core.config import ImageConfig
from core.image.decoders import DecodeError, decode_image, get_decoder


def yuv_to_rgb_reference(y: int, u: int, v: int) -> tuple[int, int, int]:
    c = y - 16
    d = u - 128
    e = v - 128
    r = (298 * c + 409 * e + 128) >> 8
    g = (298 * c - 100 * d - 208 * e + 128) >> 8
    b = (298 * c + 516 * d + 128) >> 8
    return max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))


class YUV422Tests(unittest.TestCase):
    CASES = {
        # format: (group bytes, (Y,U,V) of pixel0, (Y,U,V) of pixel1)
        "UYVY": ((10, 20, 30, 40), (20, 10, 30), (40, 10, 30)),
        "YUYV": ((20, 10, 40, 30), (20, 10, 30), (40, 10, 30)),
        "YVYU": ((20, 30, 40, 10), (20, 10, 30), (40, 10, 30)),
        "VYUY": ((30, 20, 10, 40), (20, 10, 30), (40, 10, 30)),
    }

    def test_lane_order_and_color_conversion(self) -> None:
        for fmt, (group, pixel0, pixel1) in self.CASES.items():
            with self.subTest(fmt=fmt):
                data = bytes(group) * 2  # 2 个像素组 = 4 像素
                config = ImageConfig(width=4, height=1, pixel_format=fmt, stride=8)
                decoded = decode_image(data, 4, 1, config)
                self.assertEqual(decoded.width, 4)
                self.assertEqual(decoded.height, 1)
                rgb = decoded.rgb
                self.assertEqual(tuple(rgb[0, 0]), yuv_to_rgb_reference(*pixel0))
                self.assertEqual(tuple(rgb[0, 1]), yuv_to_rgb_reference(*pixel1))
                self.assertEqual(tuple(rgb[0, 2]), yuv_to_rgb_reference(*pixel0))
                self.assertEqual(tuple(rgb[0, 3]), yuv_to_rgb_reference(*pixel1))
                self.assertIsNotNone(decoded.yuv)
                self.assertEqual(tuple(decoded.yuv[0, 1]), (pixel1[0], pixel1[1], pixel1[2]))
                self.assertIsNotNone(decoded.luma)

    def test_odd_width_note(self) -> None:
        data = bytes((10, 20, 30, 40)) * 2
        config = ImageConfig(width=3, height=2, pixel_format="UYVY", stride=6)
        decoded = decode_image(data, 3, 2, config)
        self.assertEqual(decoded.width, 2)
        self.assertIn("偶数", decoded.note)

    def test_short_data_is_padded(self) -> None:
        config = ImageConfig(width=4, height=2, pixel_format="UYVY", stride=8)
        decoded = decode_image(bytes((10, 20, 30, 40)), 4, 2, config)
        self.assertEqual(decoded.rgb.shape, (2, 4, 3))
        self.assertIn("不足", decoded.note)

    def test_invalid_geometry(self) -> None:
        config = ImageConfig(width=0, height=0, pixel_format="UYVY", stride=0)
        with self.assertRaises(DecodeError):
            decode_image(b"", 0, 0, config)


class RGBTests(unittest.TestCase):
    def test_rgb565_little_endian(self) -> None:
        # 0xF800 红, 0x07E0 绿
        data = bytes((0x00, 0xF8, 0xE0, 0x07))
        config = ImageConfig(width=2, height=1, pixel_format="RGB565", stride=4)
        decoded = decode_image(data, 2, 1, config)
        np.testing.assert_array_equal(decoded.rgb[0, 0], [255, 0, 0])
        np.testing.assert_array_equal(decoded.rgb[0, 1], [0, 255, 0])

    def test_rgb565_big_endian(self) -> None:
        data = bytes((0xF8, 0x00, 0x07, 0xE0))
        config = ImageConfig(
            width=2, height=1, pixel_format="RGB565", stride=4, byte_order="big"
        )
        decoded = decode_image(data, 2, 1, config)
        np.testing.assert_array_equal(decoded.rgb[0, 0], [255, 0, 0])
        np.testing.assert_array_equal(decoded.rgb[0, 1], [0, 255, 0])

    def test_rgb888(self) -> None:
        data = bytes((255, 0, 0, 0, 255, 0))
        config = ImageConfig(width=2, height=1, pixel_format="RGB888", stride=6)
        decoded = decode_image(data, 2, 1, config)
        np.testing.assert_array_equal(decoded.rgb[0, 0], [255, 0, 0])
        np.testing.assert_array_equal(decoded.rgb[0, 1], [0, 255, 0])

    def test_registry(self) -> None:
        self.assertEqual(get_decoder("uyvy").key, "UYVY")
        with self.assertRaises(KeyError):
            get_decoder("NOPE")


class JPEGTests(unittest.TestCase):
    def test_jpeg_roundtrip_using_qt(self) -> None:
        try:
            from PySide6.QtCore import QBuffer, QByteArray
            from PySide6.QtGui import QImage
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 不可用")

        source = QImage(8, 6, QImage.Format.Format_RGB888)
        source.fill(0x336699)
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        source.save(buffer, "JPG", 95)
        data = bytes(buffer.data())
        buffer.close()

        config = ImageConfig(width=0, height=0, pixel_format="JPEG", stride=0)
        decoded = decode_image(data, 0, 0, config)
        self.assertEqual(decoded.width, 8)
        self.assertEqual(decoded.height, 6)
        self.assertEqual(decoded.rgb.shape, (6, 8, 3))


if __name__ == "__main__":
    unittest.main()
