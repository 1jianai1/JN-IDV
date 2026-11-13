"""协议层测试：帧头、增量解析、CRC、噪声恢复。"""

from __future__ import annotations

import unittest

from core.protocol.uart_frames import (
    FLAG_INTERLACED,
    FLAG_PAL,
    FLAG_RAW,
    HDR_LEN,
    SYNC,
    TAIL,
    FrameParser,
    build_packet,
    crc32,
    diagnose,
    parse_header,
    try_extract,
)


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = bytes(range(256)) * 4
        self.packet = build_packet(
            self.payload,
            total=1000,
            offset=0,
            length=len(self.payload),
            flags=FLAG_RAW | FLAG_INTERLACED | FLAG_PAL,
            state0=0xB69D8080,
            state1=0xFFFFFFFF,
            state2=0x0001080A,
            state3=0x00010800,
            state4=0,
            ms=800,
        )

    def test_build_and_parse_header(self) -> None:
        header = parse_header(self.packet[:HDR_LEN])
        self.assertIsNotNone(header)
        assert header is not None
        self.assertEqual(header.total, 1000)
        self.assertEqual(header.offset, 0)
        self.assertEqual(header.length, len(self.payload))
        self.assertEqual(header.state0, 0xB69D8080)
        self.assertEqual(header.state1, 0xFFFFFFFF)
        self.assertTrue(header.is_interlaced)
        self.assertTrue(header.is_pal)
        self.assertIn("raw", header.flag_names)
        self.assertEqual(header.crc_data, crc32(self.payload))
        self.assertIn("total=1000", header.summary())

    def test_tail_and_crc(self) -> None:
        self.assertTrue(self.packet.startswith(SYNC))
        self.assertTrue(self.packet.endswith(TAIL))
        self.assertGreater(len(self.packet), HDR_LEN + len(self.payload))

    def test_parse_byte_by_byte(self) -> None:
        parser = FrameParser()
        packets = []
        for byte in self.packet:
            packets += parser.feed(bytes([byte]))
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].payload, self.payload)
        self.assertEqual(packets[0].header.total, 1000)
        self.assertEqual(parser.frames, 1)

    def test_noise_before_frame(self) -> None:
        parser = FrameParser()
        packets = parser.feed(b"\x00\x11hello\r\n" + self.packet)
        self.assertEqual(len(packets), 1)
        self.assertGreater(parser.discarded_bytes, 0)

    def test_false_sync_then_recover(self) -> None:
        bad = b"\xa5\x5a\xa5\x5a" + b"\x00" * 60
        parser = FrameParser()
        packets = parser.feed(bad + self.packet)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].payload, self.payload)
        self.assertGreaterEqual(parser.bad_headers, 1)

    def test_bad_crc_then_recover(self) -> None:
        corrupted = bytearray(self.packet)
        corrupted[HDR_LEN + 5] ^= 0xFF
        parser = FrameParser()
        packets = parser.feed(bytes(corrupted) + self.packet)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].payload, self.payload)
        self.assertGreaterEqual(parser.bad_crc, 1)

    def test_two_frames_in_one_feed(self) -> None:
        second = build_packet(b"abcdef", total=6, offset=0, length=6)
        parser = FrameParser()
        packets = parser.feed(self.packet + second)
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[1].payload, b"abcdef")

    def test_try_extract(self) -> None:
        packet = try_extract(b"junk" + self.packet)
        self.assertIsNotNone(packet)
        assert packet is not None
        self.assertEqual(packet.payload, self.payload)

    def test_diagnose_reports_no_sync(self) -> None:
        text = diagnose(b"no sync here")
        self.assertIn("没有任何 SYNC", text)

    def test_diagnose_truncated(self) -> None:
        text = diagnose(self.packet[:-20])
        self.assertIn("数据不完整", text)


if __name__ == "__main__":
    unittest.main()
