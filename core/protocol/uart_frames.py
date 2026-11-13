"""串口帧协议层。

帧格式（小端；与设备侧上传实现一致）：

    0  sync[4]  A5 5A A5 5A      4  ver=1       5  flags     6  hdr_len(u16)=64
    8  state0   12 state1       16 state2     20 state3   24 state4
   28  total    32 offset        36 length     40 ms       44 flags2
   48  crc_hdr(bytes[8..47])     52 crc_data   56 reserved[8]
   64  payload ...               tail[4] = 5A A5 5A 5A

``state0..state4`` 是设备侧可选的采集状态快照，仅供诊断展示，
上位机不解释其具体含义。本模块只做「字节流 -> 完整帧」的拆分与校验，
不做图像解析。
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass, field

SYNC = b"\xa5\x5a\xa5\x5a"
TAIL = b"\x5a\xa5\x5a\xa5"
HDR_LEN = 64
HDR_BODY_START = 8
HDR_BODY_LEN = 40
MAX_PAYLOAD = 16 * 1024 * 1024

FLAG_RAW = 0x01
FLAG_INTERLACED = 0x02
FLAG_PAL = 0x04
FLAG_SHORT = 0x08

FLAG_NAMES = {
    FLAG_RAW: "raw",
    FLAG_INTERLACED: "interlaced",
    FLAG_PAL: "pal",
    FLAG_SHORT: "short-frame",
}


def crc32(data: bytes, crc: int = 0) -> int:
    return zlib.crc32(data, crc) & 0xFFFFFFFF


@dataclass(frozen=True)
class FrameHeader:
    ver: int
    flags: int
    hdr_len: int
    state0: int
    state1: int
    state2: int
    state3: int
    state4: int
    total: int
    offset: int
    length: int
    ms: int
    crc_hdr: int
    crc_data: int
    flags2: int = 0

    # ------------------------------------------------------------------ #
    @property
    def flag_names(self) -> list[str]:
        return [name for bit, name in FLAG_NAMES.items() if self.flags & bit]

    @property
    def is_interlaced(self) -> bool:
        return bool(self.flags & FLAG_INTERLACED)

    @property
    def is_pal(self) -> bool:
        return bool(self.flags & FLAG_PAL)

    @property
    def short_frame(self) -> bool:
        return bool(self.flags & FLAG_SHORT)

    def to_dict(self) -> dict:
        data = {
            "ver": self.ver,
            "flags": self.flags,
            "flags_text": ",".join(self.flag_names) or "-",
            "hdr_len": self.hdr_len,
            "state0": self.state0,
            "state1": self.state1,
            "state2": self.state2,
            "state3": self.state3,
            "state4": self.state4,
            "total": self.total,
            "offset": self.offset,
            "length": self.length,
            "ms": self.ms,
            "crc_hdr": self.crc_hdr,
            "crc_data": self.crc_data,
            "flags2": self.flags2,
        }
        data["state0_hex"] = f"0x{self.state0:08X}"
        data["state1_hex"] = f"0x{self.state1:08X}"
        data["state2_hex"] = f"0x{self.state2:08X}"
        data["state3_hex"] = f"0x{self.state3:08X}"
        data["state4_hex"] = f"0x{self.state4:08X}"
        return data

    def summary(self) -> str:
        return (
            f"total={self.total} offset={self.offset} length={self.length} "
            f"flags=0x{self.flags:02X}({','.join(self.flag_names) or '-'}) "
            f"state0=0x{self.state0:08X} state1=0x{self.state1:08X} "
            f"state2=0x{self.state2:08X} state3=0x{self.state3:08X} "
            f"state4={self.state4}"
        )


@dataclass(frozen=True)
class FramePacket:
    header: FrameHeader
    payload: bytes
    received_at: float = field(default_factory=time.time)

    @property
    def total_size(self) -> int:
        return HDR_LEN + len(self.payload) + 4


def parse_header(data: bytes | bytearray) -> FrameHeader | None:
    """解析 64 字节头；sync / hdr_len / crc_hdr 不合法返回 None。"""
    if len(data) < HDR_LEN:
        return None
    if bytes(data[0:4]) != SYNC:
        return None
    hdr_len = struct.unpack_from("<H", data, 6)[0]
    if hdr_len != HDR_LEN:
        return None
    body = bytes(data[HDR_BODY_START : HDR_BODY_START + HDR_BODY_LEN])
    crc_hdr = struct.unpack_from("<I", data, 48)[0]
    if crc32(body) != crc_hdr:
        return None
    length = struct.unpack_from("<I", data, 36)[0]
    if length == 0 or length > MAX_PAYLOAD:
        return None
    return FrameHeader(
        ver=data[4],
        flags=data[5],
        hdr_len=hdr_len,
        state0=struct.unpack_from("<I", data, 8)[0],
        state1=struct.unpack_from("<I", data, 12)[0],
        state2=struct.unpack_from("<I", data, 16)[0],
        state3=struct.unpack_from("<I", data, 20)[0],
        state4=struct.unpack_from("<I", data, 24)[0],
        total=struct.unpack_from("<I", data, 28)[0],
        offset=struct.unpack_from("<I", data, 32)[0],
        length=length,
        ms=struct.unpack_from("<I", data, 40)[0],
        flags2=struct.unpack_from("<I", data, 44)[0],
        crc_hdr=crc_hdr,
        crc_data=struct.unpack_from("<I", data, 52)[0],
    )


def build_packet(payload: bytes, **fields) -> bytes:
    """构造一帧完整的上传数据（测试 / 模拟设备用）。"""
    total = int(fields.get("total", len(payload)))
    offset = int(fields.get("offset", 0))
    length = int(fields.get("length", len(payload)))
    hdr = bytearray(HDR_LEN)
    hdr[0:4] = SYNC
    hdr[4] = int(fields.get("ver", 1))
    hdr[5] = int(fields.get("flags", FLAG_RAW))
    struct.pack_into("<H", hdr, 6, HDR_LEN)

    def put_u32(off: int, value: int) -> None:
        struct.pack_into("<I", hdr, off, int(value) & 0xFFFFFFFF)

    put_u32(8, fields.get("state0", 0))
    put_u32(12, fields.get("state1", 0))
    put_u32(16, fields.get("state2", 0))
    put_u32(20, fields.get("state3", 0))
    put_u32(24, fields.get("state4", 0))
    put_u32(28, total)
    put_u32(32, offset)
    put_u32(36, length)
    put_u32(40, fields.get("ms", 0))
    put_u32(44, fields.get("flags2", 0))
    put_u32(48, crc32(bytes(hdr[HDR_BODY_START : HDR_BODY_START + HDR_BODY_LEN])))
    put_u32(52, crc32(payload))
    return bytes(hdr) + payload + TAIL


class FrameParser:
    """增量帧解析器：处理串口分段、噪声、伪 SYNC、坏 CRC。

    典型用法::

        parser = FrameParser()
        for chunk in serial_stream:
            for packet in parser.feed(chunk):
                ...
    """

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self._buf = bytearray()
        self.max_payload = max_payload
        self.discarded_bytes = 0
        self.bad_headers = 0
        self.bad_crc = 0
        self.bad_tail = 0
        self.frames = 0
        self._synced = False

    # ------------------------------------------------------------------ #
    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def has_sync(self) -> bool:
        return SYNC in self._buf

    @property
    def seen_sync(self) -> bool:
        """最近一次 feed 中是否已经看到过 SYNC（用于分离设备文字回显）。"""
        return self._synced

    def reset(self) -> None:
        self._buf.clear()
        self._synced = False
        self.discarded_bytes = 0
        self.bad_headers = 0
        self.bad_crc = 0
        self.bad_tail = 0
        self.frames = 0

    # ------------------------------------------------------------------ #
    def feed(self, chunk: bytes | bytearray) -> list[FramePacket]:
        packets: list[FramePacket] = []
        if not chunk:
            return packets
        self._buf += chunk
        buf = self._buf

        while True:
            start = buf.find(SYNC)
            if start < 0:
                # 没找到 SYNC：留下可能是半个 SYNC 的尾部
                keep = min(len(buf), len(SYNC) - 1)
                if len(buf) > keep:
                    self.discarded_bytes += len(buf) - keep
                    del buf[: len(buf) - keep]
                break
            if start > 0:
                self.discarded_bytes += start
                del buf[:start]
            self._synced = True

            if len(buf) < HDR_LEN:
                break
            header = parse_header(buf[:HDR_LEN])
            if header is None:
                self.bad_headers += 1
                del buf[0]
                continue

            end = HDR_LEN + header.length
            if len(buf) < end + 4:
                break
            if bytes(buf[end : end + 4]) != TAIL:
                self.bad_tail += 1
                del buf[0]
                continue
            payload = bytes(buf[HDR_LEN:end])
            if crc32(payload) != header.crc_data:
                self.bad_crc += 1
                del buf[0]
                continue

            del buf[: end + 4]
            self.frames += 1
            packets.append(FramePacket(header=header, payload=payload))

        return packets

    def stats(self) -> dict:
        return {
            "buffered": self.buffered,
            "discarded_bytes": self.discarded_bytes,
            "bad_headers": self.bad_headers,
            "bad_tail": self.bad_tail,
            "bad_crc": self.bad_crc,
            "frames": self.frames,
        }


def try_extract(data: bytes | bytearray) -> FramePacket | None:
    """一次性提取第一个完整帧。"""
    parser = FrameParser()
    packets = parser.feed(data)
    return packets[0] if packets else None


def diagnose(data: bytes) -> str:
    """解析失败时生成一份可读诊断文本。"""
    lines = [f"收到 {len(data)} 字节，SYNC 出现 {data.count(SYNC)} 次"]
    i = data.find(SYNC)
    if i < 0:
        lines.append("没有任何 SYNC：命令可能没执行 / 波特率不对 / 板子没在发数据")
        if data:
            lines.append("前 64 字节: " + " ".join(f"{b:02x}" for b in data[:64]))
        return "\n".join(lines)
    lines.append(f"第一个 SYNC 位于偏移 {i}")
    hdr = data[i : i + HDR_LEN]
    if len(hdr) < HDR_LEN:
        lines.append("头部不完整")
        return "\n".join(lines)
    lines.append("头 64 字节: " + " ".join(f"{b:02x}" for b in hdr))
    body = bytes(hdr[HDR_BODY_START : HDR_BODY_START + HDR_BODY_LEN])
    stored = struct.unpack_from("<I", hdr, 48)[0]
    lines.append(
        "crc_hdr 计算=0x%08x 头部=0x%08x %s"
        % (crc32(body), stored, "OK" if stored == crc32(body) else "不匹配")
    )
    header = parse_header(hdr)
    if header is None:
        lines.append("头部字段不合法（hdr_len 或 length 异常）")
        return "\n".join(lines)
    lines.append(
        "total=%d offset=%d length=%d flags=0x%02x"
        % (header.total, header.offset, header.length, header.flags)
    )
    available = len(data) - i - HDR_LEN
    if available < header.length:
        lines.append(f"数据不完整：需要 {header.length}，只有 {available}")
        return "\n".join(lines)
    payload = data[i + HDR_LEN : i + HDR_LEN + header.length]
    tail = data[i + HDR_LEN + header.length : i + HDR_LEN + header.length + 4]
    lines.append(
        "tail=%s %s"
        % (" ".join(f"{b:02x}" for b in tail), "OK" if bytes(tail) == TAIL else "不匹配")
    )
    lines.append(
        "crc_data 计算=0x%08x 头部=0x%08x %s"
        % (crc32(payload), header.crc_data, "OK" if crc32(payload) == header.crc_data else "不匹配")
    )
    return "\n".join(lines)
