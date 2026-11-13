"""下位机上传示例代码生成测试。"""

from __future__ import annotations

import unittest

from core.config import CaptureConfig, ImageConfig, SerialConfig
from core.device_template import (
    build_device_c_code,
    build_device_example_code,
    build_device_header,
    device_code_filename,
)


class DeviceTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image = ImageConfig(width=720, height=240, pixel_format="UYVY", stride=1440)
        self.serial = SerialConfig(port="COM12", baudrate=1_000_000)
        self.capture = CaptureConfig(chunk_size=57600)

    def test_c_code_contains_protocol_and_config(self) -> None:
        code = build_device_c_code(self.image, self.serial, self.capture)
        self.assertIn("#define CAM_UP_WIDTH                720", code)
        self.assertIn("#define CAM_UP_HEIGHT               240", code)
        self.assertIn("#define CAM_UP_STRIDE               1440", code)
        self.assertIn("#define CAM_UP_PAYLOAD_BYTES        345600", code)
        self.assertIn("#define CAM_UP_DEFAULT_CHUNK        57600", code)
        self.assertIn("#define CAM_UP_BAUDRATE             1000000", code)
        self.assertIn("0xA5U", code)
        self.assertIn("0x5AU", code)
        self.assertIn("cam_up_send", code)
        self.assertIn("cam_up_crc32", code)
        self.assertIn("MSH_CMD_EXPORT", code)
        self.assertIn("YUV422 UYVY", code)
        self.assertNotIn("$", code)

    def test_header(self) -> None:
        header = build_device_header(self.image, self.serial, self.capture)
        self.assertIn("#ifndef CAM_UP_H", header)
        self.assertIn("#define CAM_UP_PAYLOAD_BYTES        345600", header)
        self.assertIn("cam_up_dump", header)
        self.assertNotIn("$", header)

    def test_kind_switch(self) -> None:
        self.assertIn("#ifndef CAM_UP_H", build_device_example_code(
            self.image, self.serial, self.capture, kind="h"
        ))
        self.assertIn("int cam_up_send", build_device_example_code(
            self.image, self.serial, self.capture, kind="c"
        ))

    def test_filename(self) -> None:
        self.assertEqual(device_code_filename(self.image, "c"), "camera_upload.c")
        self.assertEqual(device_code_filename(self.image, "h"), "camera_upload.h")

    def test_different_geometry(self) -> None:
        image = ImageConfig(width=640, height=480, pixel_format="RGB888", stride=1920)
        code = build_device_c_code(image, self.serial, self.capture)
        self.assertIn("#define CAM_UP_STRIDE               1920", code)
        self.assertIn("#define CAM_UP_PAYLOAD_BYTES        921600", code)
        self.assertIn("RGB888", code)


if __name__ == "__main__":
    unittest.main()
