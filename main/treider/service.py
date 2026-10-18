"""Wires the trader's database queue to its opportunity processor."""

from __future__ import annotations

import logging
import time

from database import connect_db, initialize_database
from main.treider.account import initialize_trader_account
from main.treider.config import SIGNAL_POLL_INTERVAL_SECONDS
from main.treider.logging_setup import configure_logging
from main.treider.processor import process_opportunity
from main.treider.queue import claim_next, mark_failed, mark_processed
from main.treider.trade import process_due_training_trade

logger = logging.getLogger(__name__)


def _initialize() -> None:
    connection = connect_db()
    try:
        initialize_database(connection)
        balance, training_mode = initialize_trader_account(connection)
        logger.info(
            "Счёт трейдера готов: режим=%s, баланс=$%s",
            "тренировка" if training_mode else "обычный",
            balance,
        )
    finally:
        connection.close()


def _consume_one() -> bool:
    message = claim_next()
    if message is None:
        return False
    signal_id, payload = message
    try:
        process_opportunity(payload, signal_id)
    except Exception as error:
        mark_failed(signal_id, str(error))
        logger.exception("Ошибка обработки сообщения трейдера №%s", signal_id)
    else:
        mark_processed(signal_id)
    return True


def run() -> None:
    configure_logging()
    _initialize()
    logger.info("Трейдер ожидает JSON-сообщения от скринера")
    try:
        while True:
            try:
                did_settle = process_due_training_trade()
                did_consume = _consume_one()
                if not did_settle and not did_consume:
                    time.sleep(SIGNAL_POLL_INTERVAL_SECONDS)
            except Exception:
                logger.exception(
                    "Ошибка чтения очереди трейдера; повтор через %.1f сек.",
                    SIGNAL_POLL_INTERVAL_SECONDS,
                )
                time.sleep(SIGNAL_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Сервис трейдера остановлен")


if __name__ == "__main__":
    run()
