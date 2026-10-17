"""Периодический запуск обновления рынка и биржевых цен."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import DEFAULT_INTERVAL_MINUTES
from database import connect_db, initialize_database, read_config
from main.calculator.service import update_calculator_prices
from main.calculator.websocket_monitor import initialize_monitor, monitor_forever
from main.screener.service import update_market_data

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    log_dir = Path("/app/log")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "updates.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )


def _set_status(status: str, phase: str, next_run: datetime | None = None) -> None:
    """Сохраняет состояние worker, чтобы веб-панель видела его из другого контейнера."""
    connection = connect_db()
    try:
        values = {
            "worker_status": status,
            "worker_phase": phase,
            "worker_next_run_at": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "",
            "worker_updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO config (config_key, config_value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE config_value = VALUES(config_value)",
                list(values.items()),
            )
        connection.commit()
    finally:
        connection.close()


def _interval_minutes() -> int:
    connection = connect_db()
    try:
        return int(read_config(connection)["interval_minutes"])
    finally:
        connection.close()


def _refresh_cycle() -> int:
    """Run the REST cycle and return the delay before its next scheduled run."""
    cycle_started = time.perf_counter()
    cycle_error = False
    try:
        _set_status("running", "Обновляется список монет")
        update_market_data()
        _set_status("running", "Пересчитываются цены и разница")
        update_calculator_prices()
    except Exception:
        cycle_error = True
        logger.exception("Цикл обновления завершился с ошибкой")

    logger.info(
        "Итог цикла: %s, длительность %.1f сек.",
        "ошибка" if cycle_error else "успешно",
        time.perf_counter() - cycle_started,
    )

    try:
        interval_minutes = _interval_minutes()
    except Exception:
        logger.exception(
            "Не удалось прочитать интервал обновления; использую %s минут",
            DEFAULT_INTERVAL_MINUTES,
        )
        interval_minutes = DEFAULT_INTERVAL_MINUTES

    next_run = datetime.utcnow() + timedelta(minutes=interval_minutes)
    try:
        _set_status("error" if cycle_error else "waiting", "Ошибка цикла" if cycle_error else "Ожидание", next_run)
    except Exception:
        logger.exception("Не удалось сохранить состояние worker")
    return interval_minutes * 60


def run() -> None:
    """Refresh REST data on schedule while WebSocket monitoring stays connected."""
    _configure_logging()
    connection = connect_db()
    try:
        initialize_database(connection)
    finally:
        connection.close()
    initialize_monitor()
    try:
        monitor_forever(_refresh_cycle)
    except KeyboardInterrupt:
        logger.info("Worker остановлен пользователем")
