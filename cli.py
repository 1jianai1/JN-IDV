#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JN-IDV (Camera Image Debug Viewer) — 命令行入口（Agent / 自动化调试用）。

GUI 与 CLI 共用 ``core`` 下的全部能力：
  * 串口连接 / 下发命令
  * 请求一帧并分块接收、CRC 校验、拼接
  * BT.656 结构分析、Hex 结构化导出
  * PNG / JPG / Raw / Metadata 导出
  * 生成下位机上传参考 C 代码

示例：
    python cli.py list-ports
    python cli.py info --port COM12
    python cli.py capture --port COM12 --out cap.bin --png cap.png
    python cli.py decode --input cap.bin --out cap.png
    python cli.py analyze --input cap.bin
    python cli.py hex --input cap.bin --out cap_hex.txt
    python cli.py device-code --out camera_upload.c
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import numpy as np  # noqa: E402

from core import APP_NAME, __version__  # noqa: E402
from core.acquisition.capture import (  # noqa: E402
    CaptureCancelled,
    CaptureEngine,
    DeviceError,
    TransportError,
    send_commands,
)
from core.config import (  # noqa: E402
    CaptureConfig,
    ImageConfig,
    SerialConfig,
)
from core.device_template import build_device_example_code, device_code_filename  # noqa: E402
from core.export.exporter import (  # noqa: E402
    build_hex_text,
    write_hex_text,
    write_jpeg,
    write_metadata,
    write_png,
)
from core.formats.pixel_formats import pixel_format_keys  # noqa: E402
from core.log import get_logger, setup_logging, shutdown_logging  # noqa: E402
from core.pipeline import analyze_frame, build_frame, build_structured, load_raw_file  # noqa: E402
from core.serial.transport import SerialTransport, list_serial_ports  # noqa: E402

log = get_logger("cli")


# ---------------------------------------------------------------------- #
# 参数
# ---------------------------------------------------------------------- #
def add_image_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("图像参数")
    group.add_argument("--width", type=int, default=720, help="图像宽度（默认 720）")
    group.add_argument("--height", type=int, default=240, help="图像高度（默认 240）")
    group.add_argument(
        "--format",
        dest="pixel_format",
        default="UYVY",
        choices=pixel_format_keys(),
        help="像素格式（默认 UYVY）",
    )
    group.add_argument(
        "--stride", type=int, default=1440, help="每行字节数；0=按 width×bpp 自动（默认 1440）"
    )
    group.add_argument(
        "--byte-order",
        choices=["little", "big"],
        default="little",
        help="RGB565 字节序（默认 little）",
    )
    group.add_argument(
        "--groups-per-row", type=int, default=8, help="Hex 每行像素组数量（默认 8）"
    )


def add_serial_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("串口参数")
    group.add_argument("--port", default="COM12", help="串口号，例如 COM12")
    group.add_argument("--baud", type=int, default=1_000_000, help="波特率（默认 1000000）")
    group.add_argument("--data-bits", type=int, default=8, choices=[5, 6, 7, 8])
    group.add_argument("--stop-bits", type=float, default=1.0, choices=[1.0, 1.5, 2.0])
    group.add_argument(
        "--parity", default="none", choices=["none", "even", "odd", "mark", "space"]
    )
    group.add_argument("--flow", default="none", choices=["none", "rtscts", "xonxoff"])


def image_from_args(args) -> ImageConfig:
    return ImageConfig(
        width=args.width,
        height=args.height,
        pixel_format=args.pixel_format,
        stride=args.stride,
        byte_order=args.byte_order,
        groups_per_row=args.groups_per_row,
    )


def serial_from_args(args) -> SerialConfig:
    return SerialConfig(
        port=args.port,
        baudrate=args.baud,
        bytesize=args.data_bits,
        stopbits=args.stop_bits,
        parity=args.parity,
        flow=args.flow,
    )


# ---------------------------------------------------------------------- #
# 子命令
# ---------------------------------------------------------------------- #
def cmd_list_ports(args) -> int:
    ports = list_serial_ports()
    if not ports:
        print("未发现可用串口。")
        return 1
    for info in ports:
        print(f"{info.device:<10} {info.description}")
    return 0


def cmd_info(args) -> int:
    serial_cfg = serial_from_args(args)
    transport = SerialTransport(serial_cfg)
    try:
        transport.open()
        lines = send_commands(transport, ["camera info"], wait=args.wait, on_line=print)
        if not lines:
            print("没有收到回显（检查端口/波特率/板子是否在线）。")
            return 1
        return 0
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        transport.close()


def cmd_send(args) -> int:
    if not args.cmd:
        print("至少需要一个 -c/--cmd 命令", file=sys.stderr)
        return 2
    serial_cfg = serial_from_args(args)
    transport = SerialTransport(serial_cfg)
    try:
        transport.open()
        send_commands(transport, args.cmd, wait=args.wait, on_line=print)
        return 0
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        transport.close()


def cmd_capture(args) -> int:
    image_cfg = image_from_args(args)
    serial_cfg = serial_from_args(args)
    capture_cfg = CaptureConfig(
        mode=args.mode,
        prepare_command=args.prepare if args.prepare is not None else "",
        prepare_wait=args.prepare_wait,
        chunk_size=args.chunk,
        chunk_timeout=args.timeout,
        whole_command=args.whole_command,
        whole_timeout=max(args.timeout, 30.0),
    )
    for warning in capture_cfg.validate() + image_cfg.validate():
        print(f"[warn] {warning}", file=sys.stderr)

    transport = SerialTransport(serial_cfg)
    cancel = threading.Event()

    def on_progress(progress):
        if progress.total:
            sys.stdout.write(
                f"\r  {progress.phase:<7} {progress.received}/{progress.total} "
                f"({progress.percent}%)"
            )
        else:
            sys.stdout.write(f"\r  {progress.phase:<7} {progress.received} bytes")
        sys.stdout.flush()

    try:
        transport.open()
        engine = CaptureEngine(transport, image_cfg, capture_cfg)
        print(f"[host] 采集模式：{capture_cfg.mode}  {serial_cfg.display()}")
        result = engine.run(cancel=cancel, on_progress=on_progress, on_line=print)
    except CaptureCancelled:
        print("\n[host] 已停止")
        return 130
    except (DeviceError, TransportError, ConnectionError, RuntimeError) as exc:
        print(f"\n[host] 采集失败：{exc}", file=sys.stderr)
        return 1
    finally:
        transport.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result.payload)
    print(f"\n[host] 已保存 {out}（{len(result.payload)} 字节）")
    print("[hdr ] " + result.header.summary())

    frame = build_frame(
        result.payload,
        image_cfg,
        header=result.header.to_dict(),
        frame_index=0,
        source="live",
        extra_metadata={"capture": result.stats},
    )
    print(f"[frame] {frame.width}x{frame.height}  {frame.pixel_format}  layout={frame.layout.mode}")
    if args.hex_out:
        write_hex_text(args.hex_out, frame, build_structured(frame, image_cfg.groups_per_row))
    if args.metadata:
        write_metadata(args.metadata, frame)
    if args.png:
        write_png(args.png, frame.image)
    if args.jpg:
        write_jpeg(args.jpg, frame.image)
    return 0


def cmd_decode(args) -> int:
    image_cfg = image_from_args(args)
    frame = load_raw_file(args.input, image_cfg)
    out = Path(args.out)
    suffix = out.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        write_jpeg(out, frame.image)
    else:
        write_png(out, frame.image)
    if args.metadata:
        write_metadata(args.metadata, frame)
    print(f"[frame] {frame.width}x{frame.height}  {frame.pixel_format}  layout={frame.layout.mode}")
    for line in analyze_frame(frame):
        print(line)
    return 0


def cmd_analyze(args) -> int:
    image_cfg = image_from_args(args)
    frame = load_raw_file(args.input, image_cfg)
    for line in analyze_frame(frame):
        print(line)
    if args.hex_out:
        write_hex_text(args.hex_out, frame, build_structured(frame, image_cfg.groups_per_row))
    if args.metadata:
        write_metadata(args.metadata, frame)
    return 0


def cmd_hex(args) -> int:
    image_cfg = image_from_args(args)
    frame = load_raw_file(args.input, image_cfg)
    if args.out:
        write_hex_text(args.out, frame, build_structured(frame, image_cfg.groups_per_row))
    else:
        sys.stdout.write(build_hex_text(frame, build_structured(frame, image_cfg.groups_per_row)))
    return 0


def cmd_device_code(args) -> int:
    image_cfg = image_from_args(args)
    serial_cfg = serial_from_args(args)
    capture_cfg = CaptureConfig(chunk_size=args.chunk)
    kind = args.kind
    if args.out:
        out = Path(args.out)
        if out.suffix.lower() == ".h":
            kind = "h"
    else:
        out = Path(device_code_filename(image_cfg, kind))
    code = build_device_example_code(image_cfg, serial_cfg, capture_cfg, kind=kind)
    if args.out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")
        print(f"已生成 {out}（{len(code)} 字符）")
    else:
        sys.stdout.write(code)
    return 0


def cmd_gen(args) -> int:
    """生成合成测试 Buffer（彩色条纹 + 渐变），仅用于离线验证解析/GUI。"""
    width, height = args.width, args.height
    stride = width * 2
    payload = bytearray()
    for y in range(height):
        if args.codes:
            payload += b"\xff\x00\x00\x80"          # SAV F0V0H0
        for x in range(0, width, 2):
            bar = (x * 8) // max(1, width)
            y0 = 30 + bar * 24 + int(20 * np.sin(2 * np.pi * y / height))
            y1 = min(255, y0 + 12)
            u = (90, 54, 128, 128, 160, 128, 200, 128)[bar]
            v = (128, 128, 90, 160, 128, 200, 128, 54)[bar]
            payload += bytes((u, max(0, min(255, y0)), v, max(0, min(255, y1))))
        if args.codes or (args.leak and y % args.leak == 0):
            payload += b"\xff\x00\x00\x9d"          # EAV F0V0H1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(payload))
    print(f"已生成合成测试 Buffer {out}（{len(payload)} 字节，{width}x{height}）")
    print("提示：这是合成数据，只用于离线验证解析与 GUI，不代表设备采集结果。")
    return 0


# ---------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jn-idv",
        description=f"{APP_NAME} CLI v{__version__}（GUI: python main.py）",
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别 DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--log-file", action="store_true", help="同时写入 logs/ 目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-ports", help="列出可用串口")
    p.set_defaults(func=cmd_list_ports)

    p = sub.add_parser("info", help="发送 camera info 并打印回显")
    add_serial_args(p)
    p.add_argument("--wait", type=float, default=1.2, help="等待回显秒数")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("send", help="发送一条或多条 MSH 命令")
    add_serial_args(p)
    p.add_argument("-c", "--cmd", action="append", default=[], help="要发送的命令，可重复")
    p.add_argument("--wait", type=float, default=0.8, help="每条命令等待回显秒数")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("capture", help="采集一帧并导出（GUI 相同流程）")
    add_serial_args(p)
    add_image_args(p)
    p.add_argument("--out", default="capture.bin", help="原始 Buffer 保存路径")
    p.add_argument("--mode", choices=["chunked", "whole"], default="chunked")
    p.add_argument("--prepare", default="camera capture 0 1", help="采集前置命令；空字符串=不执行")
    p.add_argument("--prepare-wait", type=float, default=1.5)
    p.add_argument("--chunk", type=int, default=57600, help="分块大小（默认 57600）")
    p.add_argument("--timeout", type=float, default=20.0, help="每块超时秒数")
    p.add_argument("--whole-command", default="camera raw 800", help="整场模式命令")
    p.add_argument("--png", help="同时导出 PNG")
    p.add_argument("--jpg", help="同时导出 JPG")
    p.add_argument("--hex-out", help="同时导出结构化 Hex 文本")
    p.add_argument("--metadata", help="同时导出 JSON 元数据")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("decode", help="把已有 .bin 解码为 PNG/JPG")
    add_image_args(p)
    p.add_argument("--input", required=True, help="原始 Buffer 文件（payload，不含协议头）")
    p.add_argument("--out", required=True, help="输出图片路径（.png/.jpg）")
    p.add_argument("--metadata", help="同时导出 JSON 元数据")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("analyze", help="分析已有 .bin 的 BT.656 结构")
    add_image_args(p)
    p.add_argument("--input", required=True)
    p.add_argument("--hex-out", help="同时导出结构化 Hex 文本")
    p.add_argument("--metadata", help="同时导出 JSON 元数据")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("hex", help="导出结构化 Hex 文本")
    add_image_args(p)
    p.add_argument("--input", required=True)
    p.add_argument("--out", help="输出文件；不传则打印到 stdout")
    p.set_defaults(func=cmd_hex)

    p = sub.add_parser("device-code", help="生成下位机上传参考 C 代码")
    add_serial_args(p)
    add_image_args(p)
    p.add_argument("--chunk", type=int, default=57600)
    p.add_argument("--kind", choices=["c", "h"], default="c")
    p.add_argument("--out", help="输出路径；不传则打印到 stdout")
    p.set_defaults(func=cmd_device_code)

    p = sub.add_parser("gen", help="生成合成测试 Buffer（仅离线验证）")
    add_image_args(p)
    p.add_argument("--out", default="synthetic.bin")
    p.add_argument("--codes", action="store_true", help="每行带 BT.656 定时码（测试 codes 模式）")
    p.add_argument("--leak", type=int, default=0, help="每 N 行多写 4 字节定时码")
    p.set_defaults(func=cmd_gen)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    setup_logging(
        log_dir=PROJECT_DIR / "logs" if args.log_file else None,
        level=level,
        console=True,
        file=bool(args.log_file),
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        log.exception("命令执行失败")
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    finally:
        shutdown_logging()


if __name__ == "__main__":
    sys.exit(main())
