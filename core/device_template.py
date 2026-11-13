"""生成「下位机上传」参考 C 源码（上位机右上角菜单一键复制/导出）。

生成的代码使用通用 UART 分块上传帧格式，
并把图像参数、串口参数、默认分块大小写成显式宏，方便粘贴到任何板级工程里。
"""

from __future__ import annotations

import time
from string import Template

from core import APP_NAME, __version__
from core.config import CaptureConfig, ImageConfig, SerialConfig
from core.formats.pixel_formats import get_pixel_format

_C_TEMPLATE = Template(
    r'''/**
 * @file    $file_name
 * @brief   Camera raw frame upload over UART — 由 $app_name 生成
 *
 * 配套上位机参数（可直接照抄到 $app_name 的左侧配置栏）：
 *   Protocol      : $protocol
 *   Resolution    : $width x $height
 *   Pixel format  : $pixel_format
 *   Stride        : $stride Bytes/Line
 *   Buffer size   : $buffer_size Bytes
 *   Byte order    : $byte_order
 *   Serial        : $baud 8N1（上位机：$port）
 *   Chunk size    : $chunk Bytes（上位机分块导出，实测安全值）
 *
 * 传输帧格式（小端）：
 *
 *   offset  size  field
 *   0       4     sync = A5 5A A5 5A
 *   4       1     ver  = 1
 *   5       1     flags（bit0 raw / bit1 interlaced / bit2 PAL / bit3 短帧）
 *   6       2     hdr_len = 64
 *   8       4     state0  —— 采集状态快照 0（可选，可填 0）
 *   12      4     state1  —— 采集状态快照 1
 *   16      4     state2  —— 采集状态快照 2
 *   20      4     state3  —— 采集状态快照 3
 *   24      4     state4  —— 采集状态快照 4
 *   28      4     total   —— 整块 Buffer 总字节数
 *   32      4     offset  —— 本包 payload 在整块 Buffer 中的偏移
 *   36      4     length  —— 本包 payload 字节数
 *   40      4     ms      —— 采集耗时/请求毫秒数，可填 0
 *   44      4     flags2  —— 预留
 *   48      4     crc_hdr —— CRC32(bytes[8..47])
 *   52      4     crc_data—— CRC32(payload)
 *   56      8     reserved
 *   64      N     payload
 *   64+N    4     tail = 5A A5 5A A5
 *
 * 上位机处理方式：
 *   1. 用户在 GUI 点击 Capture Frame；
 *   2. 上位机先发送 prepare 命令（例如 camera capture 0 1）让板子采集一帧；
 *   3. 再发送 `cam_up <off> <len>` 分块导出，按 64B 头里的 total/offset 拼接；
 *   4. 用 CRC32 校验每个分块，再交给 BT.656 解析器 / Hex Viewer。
 *
 * 本文件可独立编译（依赖 RT-Thread），下方三个 RT_WEAK 钩子需要按板级实现。
 *
 * @note    由 $app_name v$app_version 于 $generated_at 生成
 */

#include <rtthread.h>
#include <rtdevice.h>
#include <string.h>
#include <stdlib.h>

/* ==================================================================== */
/* 上位机侧参数（与 Image Debug Viewer 配置一致）                         */
/* ==================================================================== */
#define CAM_UP_WIDTH                $width
#define CAM_UP_HEIGHT               $height
#define CAM_UP_STRIDE               $stride
#define CAM_UP_PAYLOAD_BYTES        $buffer_size
#define CAM_UP_DEFAULT_CHUNK        $chunk
#define CAM_UP_BAUDRATE             $baud

/* ==================================================================== */
/* 协议常量                                                              */
/* ==================================================================== */
#define CAM_UP_SYNC0                0xA5U
#define CAM_UP_SYNC1                0x5AU
#define CAM_UP_TAIL0                0x5AU
#define CAM_UP_TAIL1                0xA5U
#define CAM_UP_VER                  1U
#define CAM_UP_HDR_LEN              64U
#define CAM_UP_HDR_BODY             40U     /* bytes[8..47] */
#define CAM_UP_MAX_PAYLOAD          (16U * 1024U * 1024U)

/* flags */
#define CAM_UP_F_RAW                0x01U
#define CAM_UP_F_ILV                0x02U
#define CAM_UP_F_PAL                0x04U
#define CAM_UP_F_SHORT              0x08U

#ifndef RT_WEAK
#if defined(__GNUC__)
#define RT_WEAK __attribute__((weak))
#else
#define RT_WEAK
#endif
#endif

static void cam_up_put_u32(rt_uint8_t *p, rt_uint32_t v)
{
    p[0] = (rt_uint8_t)(v & 0xFFU);
    p[1] = (rt_uint8_t)((v >> 8) & 0xFFU);
    p[2] = (rt_uint8_t)((v >> 16) & 0xFFU);
    p[3] = (rt_uint8_t)((v >> 24) & 0xFFU);
}

/* 标准 CRC-32（poly 0xEDB88320，与 zlib.crc32 等价，初值传 0） */
rt_uint32_t cam_up_crc32(const rt_uint8_t *p, rt_uint32_t n, rt_uint32_t crc)
{
    rt_uint32_t i;
    rt_uint32_t k;

    crc = ~crc;
    for (i = 0U; i < n; i++)
    {
        crc ^= (rt_uint32_t)p[i];
        for (k = 0U; k < 8U; k++)
        {
            crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return ~crc;
}

/* TX 环形缓冲满时 rt_device_write 可能只写一部分，必须循环写满 */
static int cam_up_write_all(rt_device_t dev, const rt_uint8_t *p, rt_uint32_t n)
{
    while (n > 0U)
    {
        rt_size_t w = rt_device_write(dev, 0, p, (rt_size_t)n);

        if (w == 0U)
        {
            rt_thread_mdelay(1);
            continue;
        }
        if (w > (rt_size_t)n)
        {
            w = (rt_size_t)n;
        }
        p += w;
        n -= (rt_uint32_t)w;
    }
    return RT_EOK;
}

/* ==================================================================== */
/* 板级钩子（RT_WEAK 默认实现，实际工程里覆盖掉）                          */
/* ==================================================================== */

/**
 * @brief 触发一次摄像头采集（例如等待一帧 DMA 完成的信号量）
 * @param ms 采集超时毫秒数
 * @return 0 成功，负值失败
 */
RT_WEAK int cam_up_capture(rt_uint32_t ms)
{
    (void)ms;
    rt_kprintf("[cam_up] cam_up_capture() 未实现：请在此触发一次采集\n");
    return -RT_ENOSYS;
}

/**
 * @brief 返回待上传的原始 Buffer 起始地址与总字节数
 * @param total 输出：Buffer 总字节数（= CAM_UP_PAYLOAD_BYTES 时最理想）
 * @return 常量指针；返回 RT_NULL 表示当前没有可用数据
 */
RT_WEAK const rt_uint8_t *cam_up_get_buffer(rt_uint32_t *total)
{
    if (total != RT_NULL)
    {
        *total = 0U;
    }
    rt_kprintf("[cam_up] cam_up_get_buffer() 未实现：请返回 frame_buf 与长度\n");
    return RT_NULL;
}

/**
 * @brief 填写采集时刻的状态快照（上位机只做展示/诊断，可全填 0）
 */
RT_WEAK void cam_up_get_state(rt_uint32_t *state0, rt_uint32_t *state1,
                              rt_uint32_t *state2, rt_uint32_t *state3,
                              rt_uint32_t *state4)
{
    if (state0 != RT_NULL) { *state0 = 0U; }
    if (state1 != RT_NULL) { *state1 = 0U; }
    if (state2 != RT_NULL) { *state2 = 0U; }
    if (state3 != RT_NULL) { *state3 = 0U; }
    if (state4 != RT_NULL) { *state4 = 0U; }
}

/* ==================================================================== */
/* 组帧与发送                                                            */
/* ==================================================================== */
/**
 * @brief 发送一包（64B 头 + payload + 4B 尾）
 * @param buf   整块 Buffer 起始地址
 * @param total 整块 Buffer 总字节数
 * @param off   本包偏移
 * @param len   本包 payload 长度
 * @param flags CAM_UP_F_xxx 组合
 * @param ms    采集耗时/请求毫秒数，可填 0
 */
int cam_up_send(const rt_uint8_t *buf, rt_uint32_t total, rt_uint32_t off,
                rt_uint32_t len, rt_uint8_t flags, rt_uint32_t ms)
{
    rt_uint8_t hdr[CAM_UP_HDR_LEN];
    rt_uint8_t tail[4];
    rt_uint32_t state0 = 0U, state1 = 0U, state2 = 0U, state3 = 0U, state4 = 0U;
    rt_device_t dev;
    int ret;

    if ((buf == RT_NULL) || (total == 0U) || (len == 0U) ||
            (off >= total) || ((off + len) > total) ||
            (len > CAM_UP_MAX_PAYLOAD))
    {
        return -RT_EINVAL;
    }

    dev = rt_console_get_device();
    if (dev == RT_NULL)
    {
        return -RT_EIO;
    }

    cam_up_get_state(&state0, &state1, &state2, &state3, &state4);

    memset(hdr, 0, sizeof(hdr));
    hdr[0] = CAM_UP_SYNC0;
    hdr[1] = CAM_UP_SYNC1;
    hdr[2] = CAM_UP_SYNC0;
    hdr[3] = CAM_UP_SYNC1;
    hdr[4] = CAM_UP_VER;
    hdr[5] = flags;
    hdr[6] = (rt_uint8_t)(CAM_UP_HDR_LEN & 0xFFU);
    hdr[7] = (rt_uint8_t)((CAM_UP_HDR_LEN >> 8) & 0xFFU);

    cam_up_put_u32(&hdr[8], state0);
    cam_up_put_u32(&hdr[12], state1);
    cam_up_put_u32(&hdr[16], state2);
    cam_up_put_u32(&hdr[20], state3);
    cam_up_put_u32(&hdr[24], state4);
    cam_up_put_u32(&hdr[28], total);
    cam_up_put_u32(&hdr[32], off);
    cam_up_put_u32(&hdr[36], len);
    cam_up_put_u32(&hdr[40], ms);
    cam_up_put_u32(&hdr[44], 0U);
    cam_up_put_u32(&hdr[48], cam_up_crc32(&hdr[8], CAM_UP_HDR_BODY, 0U));
    cam_up_put_u32(&hdr[52], cam_up_crc32(buf + off, len, 0U));

    tail[0] = CAM_UP_TAIL0;
    tail[1] = CAM_UP_TAIL1;
    tail[2] = CAM_UP_TAIL0;
    tail[3] = CAM_UP_TAIL1;

    ret = cam_up_write_all(dev, hdr, CAM_UP_HDR_LEN);
    if (ret == RT_EOK)
    {
        ret = cam_up_write_all(dev, buf + off, len);
    }
    if (ret == RT_EOK)
    {
        ret = cam_up_write_all(dev, tail, sizeof(tail));
    }
    return ret;
}

/**
 * @brief 导出当前 Buffer 的一段；off/len 由上位机分块请求决定
 */
int cam_up_dump(rt_uint32_t off, rt_uint32_t len)
{
    const rt_uint8_t *buf;
    rt_uint32_t total = 0U;
    int ret;

    buf = cam_up_get_buffer(&total);
    if ((buf == RT_NULL) || (total == 0U))
    {
        rt_kprintf("[cam_up] no buffer，请先采集一帧\n");
        return -RT_EIO;
    }
    if (off >= total)
    {
        rt_kprintf("[cam_up] bad offset off=%u total=%u\n",
                   (unsigned int)off, (unsigned int)total);
        return -RT_EINVAL;
    }
    if ((len == 0U) || ((off + len) > total))
    {
        len = total - off;
    }

    rt_kprintf("[cam_up] begin off=%u len=%u total=%u\n",
               (unsigned int)off, (unsigned int)len, (unsigned int)total);

    ret = cam_up_send(buf, total, off, len, CAM_UP_F_RAW, 0U);

    rt_kprintf("\n[cam_up] end off=%u len=%u\n",
               (unsigned int)off, (unsigned int)(off + len));
    return ret;
}

/* ==================================================================== */
/* MSH 命令                                                              */
/*   cam_up                导出整块 Buffer                                 */
/*   cam_up <off> <len>    分块导出（上位机自动使用）                       */
/*   cam_up cap [ms]       先采集一帧再导出                                */
/* ==================================================================== */
static int cam_up(int argc, char **argv)
{
    if ((argc >= 2) && (strcmp(argv[1], "cap") == 0))
    {
        rt_uint32_t ms = (argc > 2) ? (rt_uint32_t)strtoul(argv[2], RT_NULL, 0) : 800U;
        int ret = cam_up_capture(ms);

        if (ret != RT_EOK)
        {
            rt_kprintf("[cam_up] capture failed ret=%d\n", ret);
            return ret;
        }
        return cam_up_dump(0U, 0U);
    }

    if (argc == 1)
    {
        return cam_up_dump(0U, 0U);
    }

    {
        rt_uint32_t off = (rt_uint32_t)strtoul(argv[1], RT_NULL, 0);
        rt_uint32_t len = (argc > 2) ? (rt_uint32_t)strtoul(argv[2], RT_NULL, 0) : 0U;

        return cam_up_dump(off, len);
    }
}
MSH_CMD_EXPORT(cam_up, camera raw buffer upload: cam_up [off] [len] | cam_up cap [ms]);

/* ==================================================================== */
/* 使用说明                                                              */
/* ==================================================================== */
/*
 * 1) 在板级工程里实现 cam_up_capture / cam_up_get_buffer：
 *
 *        RT_WEAK const rt_uint8_t *cam_up_get_buffer(rt_uint32_t *total)
 *        {
 *            *total = CAM_UP_PAYLOAD_BYTES;
 *            return (const rt_uint8_t *)frame_buf;
 *        }
 *
 * 2) 上位机左侧配置栏保持：
 *        Resolution = $width x $height
 *        Pixel format = $pixel_format
 *        Stride = $stride
 *        Buffer size = $buffer_size
 *
 * 3) 串口波特率 $baud；一帧 $buffer_size 字节约需
 *        $transfer_seconds 秒（理论值，8N1，不含协议开销）。
 *
 * 4) 若单次发送整场会卡住 shell，优先使用分块模式（默认 $chunk 字节/块），
 *    上位机已经处理分段接收、CRC 校验与拼帧。
 */
''',
)

_H_TEMPLATE = Template(
    r'''/**
 * @file    $file_name
 * @brief   Camera raw frame upload — 对外接口（由 $app_name 生成）
 *
 * 与 $file_base.c 配套使用；上位机参数见 .c 文件头注释。
 * 传输帧格式：64 字节头（A5 5A A5 5A 同步、total/offset/length、双 CRC32）
 *            + payload + 4 字节尾（5A A5 5A A5）。
 *
 * @note    由 $app_name v$app_version 于 $generated_at 生成
 */

#ifndef CAM_UP_H
#define CAM_UP_H

#include <rtthread.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 上位机侧参数 */
#define CAM_UP_WIDTH                $width
#define CAM_UP_HEIGHT               $height
#define CAM_UP_STRIDE               $stride
#define CAM_UP_PAYLOAD_BYTES        $buffer_size
#define CAM_UP_DEFAULT_CHUNK        $chunk
#define CAM_UP_BAUDRATE             $baud

/* 协议常量 */
#define CAM_UP_SYNC0                0xA5U
#define CAM_UP_SYNC1                0x5AU
#define CAM_UP_TAIL0                0x5AU
#define CAM_UP_TAIL1                0xA5U
#define CAM_UP_VER                  1U
#define CAM_UP_HDR_LEN              64U

#define CAM_UP_F_RAW                0x01U
#define CAM_UP_F_ILV                0x02U
#define CAM_UP_F_PAL                0x04U
#define CAM_UP_F_SHORT              0x08U

/* 板级钩子 */
int cam_up_capture(rt_uint32_t ms);
const rt_uint8_t *cam_up_get_buffer(rt_uint32_t *total);
void cam_up_get_state(rt_uint32_t *state0, rt_uint32_t *state1,
                      rt_uint32_t *state2, rt_uint32_t *state3,
                      rt_uint32_t *state4);

/* 组帧与发送 */
rt_uint32_t cam_up_crc32(const rt_uint8_t *p, rt_uint32_t n, rt_uint32_t crc);
int cam_up_send(const rt_uint8_t *buf, rt_uint32_t total, rt_uint32_t off,
                rt_uint32_t len, rt_uint8_t flags, rt_uint32_t ms);
int cam_up_dump(rt_uint32_t off, rt_uint32_t len);

#ifdef __cplusplus
}
#endif

#endif /* CAM_UP_H */
''',
)


def _context(
    image: ImageConfig,
    serial: SerialConfig,
    capture: CaptureConfig,
    kind: str,
    generated_at: str,
) -> dict:
    fmt = get_pixel_format(image.pixel_format)
    stride = image.effective_stride()
    buffer_size = stride * int(image.height) if stride > 0 else 0
    baud = max(1, int(serial.baudrate))
    transfer_bits = buffer_size * 10.0  # 8N1，每字节 10 bit
    transfer_seconds = transfer_bits / baud
    file_base = "camera_upload"
    return {
        "file_name": f"{file_base}.{kind}",
        "file_base": file_base,
        "app_name": APP_NAME,
        "app_version": __version__,
        "generated_at": generated_at,
        "width": int(image.width),
        "height": int(image.height),
        "stride": stride,
        "buffer_size": buffer_size,
        "pixel_format": fmt.label,
        "pixel_format_key": fmt.key,
        "byte_order": image.byte_order,
        "baud": baud,
        "port": serial.port,
        "chunk": int(capture.chunk_size),
        "transfer_seconds": f"{transfer_seconds:.2f}",
        "protocol": "BT.656",
    }


def build_device_c_code(
    image: ImageConfig,
    serial: SerialConfig,
    capture: CaptureConfig,
    generated_at: str | None = None,
) -> str:
    stamp = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    return _C_TEMPLATE.substitute(**_context(image, serial, capture, "c", stamp))


def build_device_header(
    image: ImageConfig,
    serial: SerialConfig,
    capture: CaptureConfig,
    generated_at: str | None = None,
) -> str:
    stamp = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    return _H_TEMPLATE.substitute(**_context(image, serial, capture, "h", stamp))


def build_device_example_code(
    image: ImageConfig,
    serial: SerialConfig,
    capture: CaptureConfig,
    kind: str = "c",
    generated_at: str | None = None,
) -> str:
    if kind.lower() in ("h", "hpp", "header"):
        return build_device_header(image, serial, capture, generated_at)
    return build_device_c_code(image, serial, capture, generated_at)


def device_code_filename(image: ImageConfig, kind: str = "c") -> str:
    return f"camera_upload.{kind}"
