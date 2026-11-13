"""配置对象测试：自动 Stride、期望 Buffer、参数校验。"""

from __future__ import annotations

import unittest

from core.config import CaptureConfig, ImageConfig, SerialConfig


class ImageConfigTests(unittest.TestCase):
    def test_auto_stride_and_buffer(self) -> None:
        config = ImageConfig(width=720, height=240, pixel_format="UYVY", stride=0)
        self.assertEqual(config.effective_stride(), 1440)
        self.assertEqual(config.expected_buffer_size, 345600)
        self.assertEqual(config.auto_stride, 1440)

    def test_explicit_stride(self) -> None:
        config = ImageConfig(width=720, height=240, pixel_format="UYVY", stride=1444)
        self.assertEqual(config.effective_stride(), 1444)
        self.assertEqual(config.expected_buffer_size, 1444 * 240)
        self.assertTrue(any("填充" in warning for warning in config.validate()))

    def test_short_stride_warning(self) -> None:
        config = ImageConfig(width=720, height=240, pixel_format="UYVY", stride=1000)
        self.assertTrue(any("截断" in warning for warning in config.validate()))

    def test_formats(self) -> None:
        self.assertEqual(ImageConfig(width=100, pixel_format="RGB565").auto_stride, 200)
        self.assertEqual(ImageConfig(width=100, pixel_format="RGB888").auto_stride, 300)
        self.assertEqual(ImageConfig(width=100, pixel_format="JPEG").auto_stride, 0)
        self.assertEqual(ImageConfig(width=100, pixel_format="YUYV").auto_stride, 200)

    def test_as_dict(self) -> None:
        payload = ImageConfig(width=720, height=240).as_dict()
        self.assertEqual(payload["effective_stride"], 1440)
        self.assertEqual(payload["expected_buffer_size"], 345600)


class SerialConfigTests(unittest.TestCase):
    def test_display(self) -> None:
        config = SerialConfig(port="COM12", baudrate=1_000_000)
        text = config.display()
        self.assertIn("COM12", text)
        self.assertIn("1000000", text)
        self.assertIn("8N1", text)


class CaptureConfigTests(unittest.TestCase):
    def test_defaults_valid(self) -> None:
        self.assertEqual(CaptureConfig().validate(), [])

    def test_bad_template(self) -> None:
        config = CaptureConfig(dump_command_template="camera dump")
        self.assertTrue(any("{off}" in warning for warning in config.validate()))

    def test_bad_mode(self) -> None:
        config = CaptureConfig(mode="nope")
        self.assertTrue(config.validate())


if __name__ == "__main__":
    unittest.main()
