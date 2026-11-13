"""测试/演示用合成图像数据（明确标记为合成，绝不冒充设备采集）。

生成 UYVY 4:2:2 的彩色条纹 + 渐变 + 标记块，可选 BT.656 SAV/EAV 定时码。
"""

from __future__ import annotations

import numpy as np


def make_uyvy(
    width: int = 720,
    height: int = 240,
    *,
    with_codes: bool = False,
    leak_every: int = 0,
    seed: int = 7128,
) -> bytes:
    """返回 width x height 的 UYVY 原始 Buffer。"""
    rng = np.random.default_rng(seed)
    xx = np.arange(width)
    yy = np.arange(height)
    grid_x, grid_y = np.meshgrid(xx, yy)

    # 8 条竖直彩条 + 垂直渐变
    bar = (grid_x * 8) // max(1, width)
    luma = 26 + bar * 26 + (grid_y * 30) // max(1, height - 1)
    luma = np.clip(luma, 16, 235).astype(np.int16)

    # 色度：让每条彩条颜色不同
    u_table = np.array([90, 54, 128, 128, 160, 128, 200, 128], dtype=np.int16)
    v_table = np.array([128, 128, 90, 160, 128, 200, 128, 54], dtype=np.int16)
    chroma_u = u_table[bar]
    chroma_v = v_table[bar]

    # 标记块：方便目视检查坐标/联动
    block = (slice(100, 141), slice(300, 341))
    luma[block] = 220
    chroma_u[block] = 90
    chroma_v[block] = 180

    # 细网格，检查最近邻缩放
    luma[((grid_x % 16) == 0) | ((grid_y % 16) == 0)] = 255

    # 少量噪声（不改变结构）
    luma = np.clip(luma + rng.integers(-3, 4, size=luma.shape), 16, 235).astype(np.uint8)
    chroma_u = np.clip(chroma_u, 0, 255).astype(np.uint8)
    chroma_v = np.clip(chroma_v, 0, 255).astype(np.uint8)

    # UYVY: U0 Y0 V0 Y1（每 2 像素一组）
    u = chroma_u[:, 0::2]
    v = chroma_v[:, 0::2]
    y0 = luma[:, 0::2]
    y1 = luma[:, 1::2]
    groups = np.stack([u, y0, v, y1], axis=-1)  # (h, w/2, 4)
    rows = groups.reshape(height, -1).tobytes()

    if not with_codes and not leak_every:
        return rows

    stride = width * 2
    out = bytearray()
    for y in range(height):
        if with_codes:
            out += b"\xff\x00\x00\x80"
        out += rows[y * stride : (y + 1) * stride]
        if with_codes or (leak_every and y % leak_every == 0):
            out += b"\xff\x00\x00\x9d"
    return bytes(out)


def make_uyvy_bad(width: int = 720, height: int = 2) -> bytes:
    """不完整/异常数据，用于测试错误处理。"""
    return make_uyvy(width, height)[:-7]
