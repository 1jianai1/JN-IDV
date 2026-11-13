"""采集协议抽象：只负责「怎么让设备把数据发上来」与「怎么判断一帧完整」。

新增 DVP / MIPI 等协议时实现本接口并注册到 ``PROTOCOL_REGISTRY``，
采集引擎与 GUI 都不需要修改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.config import CaptureConfig
from core.protocol.uart_frames import FramePacket, FrameParser

PROTOCOL_REGISTRY: dict[str, type["CaptureProtocol"]] = {}


def register_protocol(cls: type["CaptureProtocol"]) -> type["CaptureProtocol"]:
    PROTOCOL_REGISTRY[cls.key] = cls
    return cls


class CaptureProtocol(ABC):
    key: str = ""
    name: str = ""

    @abstractmethod
    def prepare_commands(self, capture: CaptureConfig) -> list[str]:
        """采集前需要执行的命令（例如让设备先采集一帧）。"""

    @abstractmethod
    def dump_command(self, capture: CaptureConfig, offset: int, length: int) -> str:
        """分块导出命令。"""

    @abstractmethod
    def whole_command(self, capture: CaptureConfig) -> str:
        """一次导出整场的命令。"""

    @abstractmethod
    def new_parser(self) -> FrameParser:
        """创建一个增量帧解析器。"""

    @abstractmethod
    def validate_packet(
        self,
        packet: FramePacket,
        expected_offset: int | None,
        expected_length: int | None,
        total: int | None,
    ) -> None:
        """校验包与请求是否一致，不一致抛 ``ProtocolError``。"""


class ProtocolError(RuntimeError):
    pass


@register_protocol
class UartFrameProtocol(CaptureProtocol):
    """通用 UART 分块上传协议（64B 头 + payload + 4B 尾）。"""

    key = "bt656"
    name = "BT.656"

    def prepare_commands(self, capture: CaptureConfig) -> list[str]:
        cmd = (capture.prepare_command or "").strip()
        return [cmd] if cmd else []

    def dump_command(self, capture: CaptureConfig, offset: int, length: int) -> str:
        template = capture.dump_command_template or "camera dump {off} {len}"
        return template.format(off=offset, len=length)

    def whole_command(self, capture: CaptureConfig) -> str:
        return (capture.whole_command or "camera raw 800").strip()

    def new_parser(self) -> FrameParser:
        return FrameParser()

    def validate_packet(
        self,
        packet: FramePacket,
        expected_offset: int | None,
        expected_length: int | None,
        total: int | None,
    ) -> None:
        hdr = packet.header
        if expected_offset is not None and hdr.offset != expected_offset:
            raise ProtocolError(
                f"分块偏移不匹配：期望 {expected_offset}，设备返回 {hdr.offset}"
            )
        if expected_length is not None and len(packet.payload) != expected_length:
            raise ProtocolError(
                f"分块长度不匹配：期望 {expected_length}，设备返回 {len(packet.payload)}"
            )
        if total is not None and hdr.total != total:
            raise ProtocolError(
                f"帧总长变化：上一块 total={total}，本块 total={hdr.total}"
            )
        if hdr.offset + len(packet.payload) > hdr.total:
            raise ProtocolError(
                f"分块越界：offset={hdr.offset} length={len(packet.payload)} total={hdr.total}"
            )


def get_protocol(key: str) -> CaptureProtocol:
    try:
        cls = PROTOCOL_REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"未实现的接收协议：{key}") from exc
    return cls()
