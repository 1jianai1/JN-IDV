"""JN-IDV (Camera Image Debug Viewer) — GUI/CLI 共用的核心业务模块。

设计原则：
  * 不依赖 PySide6（GUI 才依赖 Qt），保证核心可以被 CLI / 测试 / Agent 直接使用。
  * 串口协议、帧解析、BT.656 结构、图像解码、Hex 组织、导出各自独立，
    新增协议/格式时只需要注册新的实现，不需要改 GUI。
"""

__version__ = "1.0.0"
APP_NAME = "JN-IDV"
