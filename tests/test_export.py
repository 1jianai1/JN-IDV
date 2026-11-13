"""导出测试：PNG/JPG/Raw/Hex/元数据/调试会话，以及原始 Buffer 一致性。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.config import ImageConfig
from core.export.exporter import (
    ExportError,
    build_hex_text,
    export_session,
    write_hex_text,
    write_jpeg,
    write_metadata,
    write_png,
    write_raw_buffer,
)
from core.pipeline import build_frame, build_structured
from tests.synthetic import make_uyvy


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="camera_viewer_test_"))
        cls.data = make_uyvy(64, 16)
        cls.config = ImageConfig(width=64, height=16, pixel_format="UYVY", stride=128)
        cls.frame = build_frame(
            cls.data,
            cls.config,
            {"total": len(cls.data), "offset": 0, "length": len(cls.data)},
        )
        cls.structured = build_structured(cls.frame, 8)

    def test_png_roundtrip(self) -> None:
        path = write_png(self.tmp / "frame.png", self.frame.image)
        self.assertTrue(path.exists())
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
        try:
            from PySide6.QtGui import QImage

            image = QImage(str(path))
            self.assertEqual((image.width(), image.height()), (64, 16))
            color = image.pixelColor(0, 0)
            expected = self.frame.image[0, 0]
            self.assertLessEqual(abs(color.red() - int(expected[0])), 1)
        except ImportError:  # pragma: no cover
            pass

    def test_jpeg_export(self) -> None:
        path = write_jpeg(self.tmp / "frame.jpg", self.frame.image)
        self.assertTrue(path.exists())
        from PySide6.QtGui import QImage

        image = QImage(str(path))
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (64, 16))

    def test_raw_export_is_exact(self) -> None:
        path = write_raw_buffer(self.tmp / "frame.bin", self.frame)
        self.assertEqual(path.read_bytes(), self.data)

    def test_hex_text(self) -> None:
        path = write_hex_text(self.tmp / "frame_hex.txt", self.frame, self.structured)
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("structured hex dump", text)
        self.assertIn("00000000", text)
        self.assertIn("pixel-group", text)
        self.assertIn("ROW 000", text)

    def test_hex_text_contains_sync_codes(self) -> None:
        data = make_uyvy(32, 4, with_codes=True)
        frame = build_frame(data, ImageConfig(width=32, height=4, stride=64))
        structured = build_structured(frame, 4)
        text = build_hex_text(frame, structured)
        self.assertIn("SAV", text)
        self.assertIn("EAV", text)
        self.assertTrue(text.endswith("\r\n"))

    def test_metadata_json(self) -> None:
        path = write_metadata(self.tmp / "frame.json", self.frame)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["width"], 64)
        self.assertEqual(payload["height"], 16)
        self.assertEqual(payload["raw_bytes"], len(self.data))
        self.assertEqual(payload["layout"]["mode"], "uniform")
        self.assertEqual(payload["header"]["total"], len(self.data))

    def test_export_session(self) -> None:
        session = self.tmp / "session"
        session.mkdir(exist_ok=True)
        paths = export_session(
            session, self.frame, self.structured, "log line", "/* device code */"
        )
        names = {path.name for path in paths}
        self.assertTrue(any(name.endswith(".bin") for name in names))
        self.assertTrue(any(name.endswith(".png") for name in names))
        self.assertTrue(any(name.endswith("_hex.txt") for name in names))
        self.assertTrue(any(name.endswith(".json") for name in names))
        self.assertIn("session_", " ".join(names))

    def test_invalid_image_raises(self) -> None:
        with self.assertRaises(ExportError):
            write_png(self.tmp / "bad.png", np.zeros((4, 4), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
