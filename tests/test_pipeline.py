"""核心流水线测试：Frame 元数据、原始 Buffer 保留、坐标映射与离线加载。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import ImageConfig
from core.pipeline import analyze_frame, build_frame, build_structured, load_raw_file
from tests.synthetic import make_uyvy


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.width, self.height = 720, 240
        self.data = make_uyvy(self.width, self.height)
        self.config = ImageConfig(
            width=self.width, height=self.height, pixel_format="UYVY", stride=1440
        )
        self.header = {"total": len(self.data), "offset": 0, "length": len(self.data)}
        self.frame = build_frame(
            self.data, self.config, self.header, frame_index=3, source="live"
        )

    def test_frame_keeps_raw_and_image(self) -> None:
        self.assertEqual(self.frame.raw_data, self.data)
        self.assertEqual(self.frame.width, 720)
        self.assertEqual(self.frame.height, 240)
        self.assertEqual(self.frame.image.shape, (240, 720, 3))
        self.assertEqual(self.frame.pixel_format, "UYVY")
        self.assertEqual(self.frame.protocol, "bt656")
        self.assertEqual(self.frame.frame_index, 3)
        self.assertEqual(self.frame.header, self.header)

    def test_mapping_and_pixel_access(self) -> None:
        offset = self.frame.location_to_offset(300, 100)
        self.assertIsNotNone(offset)
        location = self.frame.offset_to_location(offset)
        self.assertEqual((location.x, location.y), (300, 100))
        rgb = self.frame.pixel_rgb(300, 100)
        self.assertIsNotNone(rgb)
        self.assertEqual(len(rgb), 3)
        yuv = self.frame.pixel_yuv(300, 100)
        self.assertIsNotNone(yuv)

    def test_metadata_is_json_serializable(self) -> None:
        metadata = self.frame.to_metadata()
        text = json.dumps(metadata, ensure_ascii=False)
        self.assertIn("layout", text)
        self.assertEqual(metadata["stats"]["warnings"], [])
        self.assertIn("build_ms", metadata["stats"])

    def test_info_lines(self) -> None:
        lines = dict(self.frame.info_lines())
        self.assertEqual(lines["Resolution"], "720 x 240")
        self.assertEqual(lines["Buffer"], "345600 Bytes")
        self.assertEqual(lines["Stride"], "1440")
        self.assertEqual(lines["Format"], "YUV422 UYVY")

    def test_analysis_text(self) -> None:
        lines = analyze_frame(self.frame)
        text = "\n".join(lines)
        self.assertIn("Buffer: 345600", text)
        self.assertIn("Stride 均匀切行", text)
        self.assertIn("行长: min=1440 median=1440 max=1440", text)

    def test_structured(self) -> None:
        structured = build_structured(self.frame, 8)
        self.assertEqual(structured.total_bytes, len(self.data))
        self.assertEqual(structured.pixel_format, "UYVY")
        self.assertTrue(structured.rows)

    def test_load_raw_file(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="camera_viewer_pipeline_"))
        path = tmp / "frame.bin"
        path.write_bytes(self.data)
        frame = load_raw_file(path, self.config)
        self.assertEqual(frame.raw_data, self.data)
        self.assertEqual(frame.source, "file")
        self.assertEqual(frame.header["source_file"], str(path))

    def test_empty_payload(self) -> None:
        from core.image.decoders import DecodeError

        with self.assertRaises(DecodeError):
            build_frame(b"", self.config)


if __name__ == "__main__":
    unittest.main()
