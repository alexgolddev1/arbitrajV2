"""Надёжная очередь JSON-сообщений между скринером и трейдером на MySQL."""

from __future__ import annotations

import json
from typing import Any, Mapping

from database import connect_db


def enqueue(opportunity_key: str, payload: Mapping[str, Any]) -> bool:
    """Save one opportunity once; the unique key prevents duplicate events."""
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO trader_signals (opportunity_key, payload) VALUES (%s, %s)",
                (opportunity_key, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )
            inserted = cursor.rowcount == 1
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next() -> tuple[int, dict[str, Any]] | None:
    """Claim the next message, recovering claims abandoned for two minutes."""
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT signal_id, payload FROM trader_signals "
                "WHERE status = 'pending' OR "
                "(status = 'processing' AND claimed_at < UTC_TIMESTAMP() - INTERVAL 2 MINUTE) "
                "ORDER BY signal_id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = cursor.fetchone()
            if row is None:
                connection.rollback()
                return None
            cursor.execute(
                "UPDATE trader_signals SET status = 'processing', claimed_at = UTC_TIMESTAMP() "
                "WHERE signal_id = %s",
                (row["signal_id"],),
            )
        connection.commit()
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("Сообщение очереди трейдера должно быть JSON-объектом")
        return int(row["signal_id"]), payload
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_processed(signal_id: int) -> None:
    _finish(signal_id, None)


def mark_failed(signal_id: int, error_message: str) -> None:
    _finish(signal_id, error_message)


def _finish(signal_id: int, error_message: str | None) -> None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            if error_message:
                cursor.execute(
                    "UPDATE trader_signals SET status = 'failed', processed_at = UTC_TIMESTAMP(), "
                    "error_message = %s WHERE signal_id = %s AND status = 'processing'",
                    (error_message[:4000], signal_id),
                )
            else:
                cursor.execute(
                    "UPDATE trader_signals SET status = 'processed', processed_at = UTC_TIMESTAMP(), "
                    "error_message = NULL WHERE signal_id = %s AND status = 'processing'",
                    (signal_id,),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
