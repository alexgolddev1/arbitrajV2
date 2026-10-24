"""Wires the trader's database queue to its opportunity processor."""

from __future__ import annotations

import logging
import time

from database import connect_db, finalize_stale_opportunity_lifetimes, initialize_database
from main.treider.account import active_demo_run_id, initialize_trader_account
from main.treider.config import (
    ACTIVE_OPPORTUNITY_FALLBACK_SECONDS,
    SIGNAL_MAX_ATTEMPTS,
    SIGNAL_POLL_INTERVAL_SECONDS,
)
from main.treider.logging_setup import configure_logging
from main.treider.processor import TemporaryProcessingError, process_opportunity
from main.treider.queue import (
    claim_next,
    enqueue_active_opportunity_fallbacks,
    mark_processed,
    mark_rejected,
    mark_retry,
)
from main.treider.trade import process_due_training_trade, set_demo_run_id

logger = logging.getLogger(__name__)


def _initialize() -> None:
    connection = connect_db()
    try:
        initialize_database(connection)
        balance, training_mode = initialize_trader_account(connection)
        set_demo_run_id(active_demo_run_id(connection))
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
    signal_id, payload = message.signal_id, message.payload
    try:
        disposition, reason = process_opportunity(payload, signal_id)
    except TemporaryProcessingError as error:
        changed = mark_retry(signal_id, message.lease_token, str(error), message.attempt_count)
        logger.warning(
            "Временная ошибка сигнала №%s; попытка %s/%s; отложен=%s; причина=%s",
            signal_id, message.attempt_count, SIGNAL_MAX_ATTEMPTS, changed, error,
        )
    except (ValueError, KeyError) as error:
        changed = mark_rejected(signal_id, message.lease_token, str(error))
        logger.warning("Сигнал №%s отклонён при проверке структуры=%s; причина=%s", signal_id, changed, error)
    except Exception as error:
        changed = mark_retry(signal_id, message.lease_token, str(error), message.attempt_count)
        logger.exception("Сбой обработки сигнала №%s; задача отправлена на повтор=%s", signal_id, changed)
    else:
        if disposition == "temporary_error":
            changed = mark_retry(signal_id, message.lease_token, reason, message.attempt_count)
            logger.warning("Временная ошибка сигнала №%s; отложен=%s; причина=%s", signal_id, changed, reason)
        elif disposition == "rejected":
            mark_rejected(signal_id, message.lease_token, reason)
        else:
            mark_processed(signal_id, message.lease_token, disposition)
    return True


def run() -> None:
    configure_logging()
    _initialize()
    logger.info("Трейдер ожидает JSON-сообщения от скринера")
    last_signal_at = time.monotonic()
    last_lifetime_cleanup_at = 0.0
    try:
        while True:
            try:
                did_settle = process_due_training_trade()
                did_consume = _consume_one()
                now = time.monotonic()
                if now - last_lifetime_cleanup_at >= 30:
                    closed = finalize_stale_opportunity_lifetimes(180)
                    if closed:
                        logger.info("Закрыто устаревших WebSocket-ситуаций без проверки более 180 секунд: %s", closed)
                    last_lifetime_cleanup_at = now
                if did_consume:
                    last_signal_at = now
                elif now - last_signal_at >= ACTIVE_OPPORTUNITY_FALLBACK_SECONDS:
                    enqueue_active_opportunity_fallbacks()
                    last_signal_at = now
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
