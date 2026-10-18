"""Idempotent signal queue with bounded retries and fencing lease tokens."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from database import connect_db
from main.treider.config import SIGNAL_MAX_AGE_SECONDS, SIGNAL_MAX_ATTEMPTS


@dataclass(frozen=True)
class ClaimedSignal:
    signal_id: int
    payload: dict[str, Any]
    lease_token: str
    attempt_count: int


def enqueue(opportunity_key: str, payload: Mapping[str, Any]) -> bool:
    """Save one signal exactly once and persist its new-entry expiration."""
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO trader_signals "
                "(opportunity_key, payload, expires_at) VALUES (%s, %s, "
                "DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND))",
                (opportunity_key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                 SIGNAL_MAX_AGE_SECONDS),
            )
            inserted = cursor.rowcount == 1
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next() -> ClaimedSignal | None:
    """Claim a message once; the token fences out a worker whose lease expired."""
    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "UPDATE trader_signals SET status = 'failed', decision = 'expired', "
                "processed_at = UTC_TIMESTAMP(), error_message = 'Максимальный возраст сигнала истёк' "
                "WHERE status IN ('pending','processing') AND created_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                (SIGNAL_MAX_AGE_SECONDS,),
            )
            cursor.execute(
                "SELECT signal_id, payload, attempt_count FROM trader_signals WHERE "
                "(status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= UTC_TIMESTAMP())) OR "
                "(status = 'processing' AND claimed_at < UTC_TIMESTAMP() - INTERVAL 2 MINUTE) "
                "ORDER BY signal_id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = cursor.fetchone()
            if row is None:
                connection.commit()
                return None
            if int(row["attempt_count"]) >= SIGNAL_MAX_ATTEMPTS:
                cursor.execute(
                    "UPDATE trader_signals SET status='failed', decision='max_attempts', "
                    "processed_at=UTC_TIMESTAMP(), error_message='Достигнут предел повторов' WHERE signal_id=%s",
                    (row["signal_id"],),
                )
                connection.commit()
                return None
            token = str(uuid.uuid4())
            cursor.execute(
                "UPDATE trader_signals SET status='processing', claimed_at=UTC_TIMESTAMP(), "
                "attempt_count=attempt_count+1, lease_token=%s, lease_version=lease_version+1 "
                "WHERE signal_id=%s",
                (token, row["signal_id"]),
            )
            connection.commit()
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("Сообщение очереди трейдера должно быть JSON-объектом")
        return ClaimedSignal(int(row["signal_id"]), payload, token, int(row["attempt_count"]) + 1)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_processed(signal_id: int, token: str, decision: str) -> bool:
    return _finish(signal_id, token, "processed", decision, None)


def mark_rejected(signal_id: int, token: str, reason: str) -> bool:
    return _finish(signal_id, token, "processed", "rejected", reason)


def mark_retry(signal_id: int, token: str, reason: str, attempt_count: int) -> bool:
    connection = connect_db()
    try:
        delay = min(300, 2 ** max(0, attempt_count - 1))
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trader_signals SET status=IF(attempt_count >= %s OR "
                "created_at < UTC_TIMESTAMP() - INTERVAL %s SECOND, 'failed', 'pending'), "
                "decision=IF(attempt_count >= %s OR "
                "created_at < UTC_TIMESTAMP() - INTERVAL %s SECOND, 'retry_exhausted', 'temporary_error'), "
                "next_attempt_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND), claimed_at=NULL, "
                "lease_token=NULL, error_message=%s, processed_at=IF(attempt_count >= %s OR "
                "created_at < UTC_TIMESTAMP() - INTERVAL %s SECOND, UTC_TIMESTAMP(), NULL) "
                "WHERE signal_id=%s AND status='processing' AND lease_token=%s",
                (SIGNAL_MAX_ATTEMPTS, SIGNAL_MAX_AGE_SECONDS, SIGNAL_MAX_ATTEMPTS,
                 SIGNAL_MAX_AGE_SECONDS, delay, reason[:4000], SIGNAL_MAX_ATTEMPTS,
                 SIGNAL_MAX_AGE_SECONDS, signal_id, token),
            )
            changed = cursor.rowcount == 1
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish(signal_id: int, token: str, status: str, decision: str, reason: str | None) -> bool:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trader_signals SET status=%s, decision=%s, processed_at=UTC_TIMESTAMP(), "
                "claimed_at=NULL, lease_token=NULL, error_message=%s "
                "WHERE signal_id=%s AND status='processing' AND lease_token=%s",
                (status, decision, reason[:4000] if reason else None, signal_id, token),
            )
            changed = cursor.rowcount == 1
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
