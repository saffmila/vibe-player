"""
Logging configuration and stdout/stderr redirection for Vibe Player.

Configures a rotating file handler, optional console output, and routes
``sys.stdout`` / ``sys.stderr`` through loggers. Reduces noise from common
HTTP/ML libraries.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = str(Path(__file__).resolve().parent / "app.log")


class StreamToLogger:
    """Send stream writes (e.g. stdout/stderr) into a logger at a fixed level."""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger = logger
        self.level = level
        self._in_logging = False

    def write(self, message: str) -> None:
        if not message.strip():
            return
        if self._in_logging:
            return
        try:
            self._in_logging = True
            self.logger.log(self.level, message.strip())
        finally:
            self._in_logging = False

    def flush(self) -> None:
        """No-op; present for file-like compatibility."""

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return -1


class SafeStreamHandler(logging.StreamHandler):
    """
    StreamHandler that ignores OSError when the stream is invalid.

    On Windows GUI apps, ``sys.__stderr__`` may be unusable from worker threads
    with no console attached (WinError 1).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except OSError:
            pass


def setup_logging(debug: bool = False) -> str:
    """
    Configure root logging: rotating file log, optional stderr mirror, and
    elevated log levels for noisy third-party libraries.
    """
    warnings.filterwarnings("ignore")

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
    )

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if debug:
        console_handler = SafeStreamHandler(sys.__stderr__)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    for noisy_lib in ("httpx", "httpcore", "huggingface_hub", "transformers", "urllib3"):
        logging.getLogger(noisy_lib).setLevel(logging.ERROR)

    sys.stdout = StreamToLogger(logging.getLogger("STDOUT"), logging.INFO)
    sys.stderr = StreamToLogger(logging.getLogger("STDERR"), logging.ERROR)

    logging.info(
        "%s Application started (debug=%s) %s",
        "=" * 20,
        debug,
        "=" * 20,
    )

    return LOG_PATH
