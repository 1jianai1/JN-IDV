"""日志：CLI 控制台 + 文件，GUI 通过 logging.Handler 接入同一套 log 源。

约定：
  * 所有模块使用 ``get_logger("模块名")``，根 logger 名为 ``cameraviewer``。
  * setup_logging() 可重复调用，会替换自己加的 handler，不会影响第三方 logger。
  * GUI 只是额外挂一个转发 handler，日志本身照常写文件/控制台。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT_LOGGER = "cameraviewer"
DEFAULT_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-28s %(message)s"
DEFAULT_DATEFMT = "%H:%M:%S"

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_installed: list[logging.Handler] = []


class _DropNoise(logging.Filter):
    """过滤 pylink/Qt 等库偶尔打出的重复告警。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        return True


def default_log_dir() -> Path:
    return _PROJECT_DIR / "logs"


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(ROOT_LOGGER)
    if name.startswith(ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def setup_logging(
    log_dir: str | Path | None = None,
    level: int = logging.INFO,
    console: bool = True,
    file: bool = True,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> Path | None:
    """配置根 logger，返回日志文件路径（file=False 时返回 None）。"""
    global _installed
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level)
    logger.propagate = False
    logger.disabled = False

    for handler in list(_installed):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    _installed = []

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(level)
        stream.setFormatter(formatter)
        stream.addFilter(_DropNoise())
        logger.addHandler(stream)
        _installed.append(stream)

    log_path: Path | None = None
    if file:
        directory = Path(log_dir) if log_dir else default_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"camera_viewer_{_timestamp()}.log"
        fh = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        _installed.append(fh)

    return log_path


def add_handler(handler: logging.Handler, level: int | None = None) -> None:
    logger = logging.getLogger(ROOT_LOGGER)
    if level is not None:
        handler.setLevel(level)
    if handler.formatter is None:
        handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT))
    logger.addHandler(handler)


def remove_handler(handler: logging.Handler) -> None:
    try:
        logging.getLogger(ROOT_LOGGER).removeHandler(handler)
    except Exception:
        pass


def shutdown_logging() -> None:
    global _installed
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(_installed):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    _installed = []


def _timestamp() -> str:
    import datetime

    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
