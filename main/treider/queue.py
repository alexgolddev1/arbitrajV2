"""Idempotent signal queue with bounded retries and fencing lease tokens."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from database import connect_db
from main.treider.config import (
    ACTIVE_OPPORTUNITY_FALLBACK_SECONDS,
    SIGNAL_MAX_AGE_SECONDS,
    SIGNAL_MAX_ATTEMPTS,
)
from main.treider.signals import build_opportunity_signal

logger = logging.getLogger(__name__)


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


def enqueue_active_opportunity_fallbacks() -> int:
    """Queue fresh active opportunities when their normal JSON signal is absent/idle."""
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT life.lifetime_id, life.source_market_id, life.source_pair, "
                "life.threshold_percent, life.spread_percent, life.max_spread_percent, "
                "life.buy_exchange, life.buy_market_pair, life.buy_price, "
                "life.sell_exchange, life.sell_market_pair, life.sell_price, "
                "life.started_at, life.last_confirmed_at, life.transfer_seconds "
                "FROM opportunity_lifetimes AS life "
                "WHERE life.is_active = 1 "
                "AND life.last_confirmed_at >= UTC_TIMESTAMP() - INTERVAL 20 SECOND "
                "AND life.threshold_percent = (SELECT MAX(active.threshold_percent) "
                "FROM opportunity_lifetimes AS active "
                "WHERE active.source_market_id = life.source_market_id AND active.is_active = 1) "
                "AND NOT EXISTS (SELECT 1 FROM trader_signals AS pending "
                "WHERE CAST(JSON_UNQUOTE(JSON_EXTRACT(pending.payload, '$.lifetime_id')) AS UNSIGNED) = life.lifetime_id "
                "AND pending.status IN ('pending', 'processing')) "
                "AND NOT EXISTS (SELECT 1 FROM trader_training_settlements AS position "
                "WHERE position.source_pair = life.source_pair "
                "AND position.status IN ('pending', 'settling', 'partial', 'open')) "
                "ORDER BY life.last_confirmed_at DESC, life.spread_percent DESC LIMIT 50"
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    now = datetime.now(timezone.utc)
    bucket = int(now.timestamp() // ACTIVE_OPPORTUNITY_FALLBACK_SECONDS)
    queued = 0
    for row in rows:
        threshold = Decimal(str(row["threshold_percent"])).normalize()
        started_at = row["started_at"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        lifetime_seconds = max(0.0, (now - started_at).total_seconds())
        signal_key, payload = build_opportunity_signal(
            lifetime_id=int(row["lifetime_id"]),
            source_market_id=int(row["source_market_id"]),
            source_pair=str(row["source_pair"]),
            threshold_percent=threshold,
            spread_percent=Decimal(str(row["spread_percent"])),
            max_spread_percent=Decimal(str(row["max_spread_percent"])),
            buy_exchange=str(row["buy_exchange"]),
            buy_market_pair=str(row["buy_market_pair"]),
            buy_price=Decimal(str(row["buy_price"])),
            buy_size=Decimal(0),
            sell_exchange=str(row["sell_exchange"]),
            sell_market_pair=str(row["sell_market_pair"]),
            sell_price=Decimal(str(row["sell_price"])),
            sell_size=Decimal(0),
            network="UNKNOWN",
            estimated_transfer_seconds=float(row["transfer_seconds"] or 300),
            started_at_utc=started_at.isoformat(sep=" "),
            lifetime_seconds=lifetime_seconds,
        )
        fallback_key = f"{signal_key}:fallback:{bucket}"
        if enqueue(fallback_key, payload):
            queued += 1
            logger.info(
                "Резервный сигнал из активных WebSocket-ситуаций: %s, порог %.4f%%, жизнь %.1f сек.",
                row["source_pair"], threshold, lifetime_seconds,
            )
    return queued


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
