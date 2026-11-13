"""把 Python logging 接到 Qt（线程安全：从任意线程 emit 信号，Qt 自动排队）。"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from core.log import DEFAULT_DATEFMT


class LogBridge(QObject):
    record = Signal(object)  # (levelno: int, formatted_text: str)


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: LogBridge, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self.bridge = bridge
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s  %(message)s",
                datefmt=DEFAULT_DATEFMT,
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        try:
            self.bridge.record.emit((record.levelno, text))
        except Exception:
            self.handleError(record)
