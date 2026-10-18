"""Настройка отдельного файла и консольного вывода логов трейдера."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging() -> None:
    log_directory = Path(os.getenv("TRADER_LOG_DIR", "/app/log"))
    log_directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_directory / "trader.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=os.getenv("TRADER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
        force=True,
    )
