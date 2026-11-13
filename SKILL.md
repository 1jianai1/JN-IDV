---
name: camera-image-debug-viewer
description: Use when working on JN-IDV (Camera Image Debug Viewer, GUI/CLI), capturing raw camera buffers over UART, decoding BT.656/YUV422/RGB/JPEG, structured hex analysis, or extending capture protocols and pixel formats.
---

# Skill: JN-IDV (Camera Image Debug Viewer)

仓库名与程序名均为 **JN-IDV**；完整描述性名称是 Camera Image Debug Viewer。

## When to use

- 需要抓取摄像头原始 Buffer，或把 `.bin` 离线解析成 PNG / Hex / 元数据。
- 需要修改或扩展本工程的 GUI / CLI / 核心模块。
- 需要解释「原始字节 ↔ BT.656 行 ↔ 像素组 ↔ 图像坐标」的映射关系。

## Hard rules

1. **不要改本工程目录之外的文件**；本工程可独立拷贝、重命名和分发。
2. **不要改下位机协议**。当前协议固定为 `A5 5A A5 5A + 64B header + payload + 5A A5 5A 5A`。
3. **不要用一次串口 `read()` 当完整图像**；必须走 `FrameParser` + `total/offset/length/CRC` 校验。
4. **不要丢弃 `Frame.raw_data`**；导出 Raw 必须与设备 payload 逐字节一致。
5. GUI 不实现协议/解码；CLI 不依赖 GUI。新增能力放在 `core/`，GUI 只消费 `Frame` / `StructuredData`。
6. 代码、注释、文档中不要写入具体芯片型号、厂商、私有寄存器名或私有命令名；用通用占位命令。
7. 修改后至少跑：单元测试 + GUI 离屏冒烟（见下）。

## Environment

```powershell
conda activate kbhost
# 依赖：numpy, pyserial, PySide6-Essentials
```

若需要重装：`pip install -r requirements.txt`。

## Fast commands

```powershell
cd JN-IDV        # 或本地实际的检出目录名

# GUI（人工调试）
python main.py

# 82 个核心单元测试
python -m unittest discover -s tests -p "test_*.py"

# GUI 冒烟（无显示器可跑；截图写到 out/）
python tests/gui_smoke.py
python tests/gui_smoke.py --native   # 使用系统平台插件，能渲染中文字体

# 合成测试帧（明确是合成数据，不冒充设备采集）
python cli.py gen --out out/synthetic.bin --codes
python cli.py decode --input out/synthetic.bin --out out/synthetic.png
python cli.py analyze --input out/synthetic.bin
```

串口采集（无硬件时会失败并给出原因）：

```powershell
python cli.py list-ports
python cli.py info --port COM12 --baud 1000000
python cli.py capture --port COM12 --out cap.bin --png cap.png --prepare "camera capture 0 1"
```

> 默认命令是通用占位示例。实际设备命令在界面左侧或 CLI 参数中配置。

## Core API map

```python
from core.config import ImageConfig, SerialConfig, CaptureConfig
from core.serial.transport import SerialTransport, list_serial_ports
from core.acquisition.capture import CaptureEngine, send_commands, CaptureCancelled
from core.pipeline import build_frame, build_structured, analyze_frame, load_raw_file
from core.hex.formatter import format_frame, format_plain
from core.export.exporter import write_png, write_jpeg, write_raw_buffer, write_hex_text, write_metadata, export_session
from core.device_template import build_device_example_code
```

关键调用链：

```python
image = ImageConfig(width=720, height=240, pixel_format="UYVY", stride=0)  # stride=0 -> 1440
capture = CaptureConfig(mode="chunked", prepare_command="camera capture 0 1", chunk_size=57600)

transport = SerialTransport(SerialConfig(port="COM12", baudrate=1_000_000))
transport.open()
result = CaptureEngine(transport, image, capture).run()   # result.payload / result.header / result.stats
frame = build_frame(result.payload, image, header=result.header.to_dict(), frame_index=0)
structured = build_structured(frame, groups_per_row=8)

# 坐标映射
loc = frame.offset_to_location(123456)          # PixelLocation
off = frame.location_to_offset(320, 120, "Y0")  # int | None
print(loc.describe(), hex(off))

write_png("cap.png", frame.image)
write_raw_buffer("cap.bin", frame)
```

`Frame` 字段：`raw_data`、`image`(HxWx3 RGB)、`luma`、`yuv`、`width/height`、`pixel_format`、`protocol`、`frame_index`、`timestamp`、`layout`、`config`、`header`、`metadata`、`source`。

`FrameHeader` 字段：`ver`、`flags`、`state0..state4`、`total`、`offset`、`length`、`ms`、`crc_hdr`、`crc_data`。

`BT656Layout`：

- `mode`: `codes`（检测到规则 SAV/EAV） / `uniform`（按 Stride） / `raw`（无映射，如 JPEG）
- `rows`: `ActiveRow(y, data_start, data_len, usable_bytes, line_index, field_id, ...)`
- `locate(offset)` / `offset_of(x, y, component)` / `packed_rows()` / `summary()`

`StructuredData`：

- `rows`: `HexRow(kind, offset, length, label, line_index, image_row, group_start, groups, xy, field_id)`
- `kind`: `info` / `line` / `sync` / `pixels` / `blank` / `raw` / `hash` / `warn`
- `row_index_for_offset(offset)`：搜索/跳转定位
- 显示行按 offset 单调递增且**完整覆盖**原始 Buffer

## Common tasks

### 离线验证一张图

```powershell
python cli.py gen --out out/t.bin --codes
python cli.py analyze --input out/t.bin
python cli.py decode --input out/t.bin --out out/t.png
```

### 把真实采集的 payload 变成 PNG + Hex

```powershell
python cli.py decode --input cap.bin --out cap.png --width 720 --height 240 --format UYVY --stride 1440
python cli.py hex --input cap.bin --out cap_hex.txt
```

### 生成下位机上传示例代码

```powershell
python cli.py device-code --out camera_upload.c
python cli.py device-code --out camera_upload.h
```

生成内容使用通用 UART 分块上传帧格式；板级需要实现三个 `RT_WEAK` 钩子：
`cam_up_capture` / `cam_up_get_buffer` / `cam_up_get_state`。

### 新增像素格式

1. `core/formats/pixel_formats.py` 添加 `PixelFormat(..., lane_components=(...), group_bytes=...)`。
2. `core/image/decoders.py` 实现 `ImageDecoder.decode()` 并注册 `DECODERS`。
3. 跑 `tests/test_decoders.py` 风格测试；GUI 图例/Hex 会自动使用新 lane 定义。

### 新增采集协议

1. 继承 `core/protocol/base.py: CaptureProtocol`，实现 `prepare_commands / dump_command / whole_command / new_parser / validate_packet`。
2. 用 `@register_protocol` 注册，并把 key 加到 `core.config.PROTOCOLS`（GUI 自动出现）。
3. 为它实现 `BT656Layout` 风格的布局与 `DataFormatter`；`StructuredData` 行结构是 GUI 与协议的唯一契约。

## Pitfalls

- **不要按 `width*bpp*height` 硬算 offset**：BT.656 可能带 SAV/EAV、消隐、行尾填充。用 `layout.locate/offset_of`。
- **YUV422 是宏像素组**：`UYVY = U0 Y0 V0 Y1`，U/V 由两个像素共享；`group_span` 应为 2，Hex 不能把两个像素当成两套 U/V。
- **分块默认 57600 B**：单次整场导出可能卡住设备的 shell；不要随意改成整场单命令。
- **`total` 来自帧头**：配置的 `expected_buffer_size` 只用于提示；拼接必须按 `header.total/offset`。
- **实际行数可能少于配置值**：不要强制补零再显示；`Frame.height` 用实际值，Log 会有告警。
- **定时码不规律时**：布局会回退 `uniform`，不能为了展示 SAV/EAV 强行用错误行结构。
- **JPEG 没有逐像素映射**：`layout.mode == "raw"`，Hex 只能显示字节；不要伪造坐标。
- **GUI 线程**：串口 I/O、解码、结构组织都在 QThread；不要在 GUI 线程调用 `CaptureEngine.run()`。
- **无条件字体**：`QT_QPA_PLATFORM=offscreen` 下可能没有字体，截图文字是方框；这是环境问题，不影响真实桌面运行。用 `tests/gui_smoke.py --native` 看带字体的截图。
- **日志**：GUI 的日志 handler 只是追加一个 `logging.Handler`；不要移除文件日志，否则违反「日志不能因为没打开 Log 页而停止」。

## Verification checklist before finishing

- [ ] `python -m unittest discover -s tests -p "test_*.py"` → OK
- [ ] `python tests/gui_smoke.py` → `GUI smoke OK`，上方 Image Preview 与下方 Hex/Log 渲染无异常
- [ ] `python cli.py gen ... && python cli.py decode ...` → PNG 生成成功
- [ ] 没有改动本工程目录之外的文件
- [ ] 代码、注释、文档中没有具体芯片型号 / 厂商 / 私有寄存器名 / 私有命令名
- [ ] 新增导出/协议时，`Frame.raw_data` 仍与设备 payload 逐字节一致
