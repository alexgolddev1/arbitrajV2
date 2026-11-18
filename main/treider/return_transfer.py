"""Shared USDT return-transfer workflow and simulated execution for demo mode."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from database import connect_db
from main.treider.config import SETTLEMENT_RETRY_SECONDS, TRAINING_MODE
from main.treider.network_check import check_transfer_network_for_currency

logger = logging.getLogger(__name__)
RETURN_RETRY_SECONDS = max(30, SETTLEMENT_RETRY_SECONDS)
RETURN_CLAIM_SECONDS = max(60, SETTLEMENT_RETRY_SECONDS * 4)


def check_return_route(source_exchange: str, destination_exchange: str) -> dict[str, Any]:
    """Find the reverse USDT route and preserve unknown fees or limits as unknown."""
    if source_exchange.strip().lower() == destination_exchange.strip().lower():
        return {
            "available": True, "same_exchange": True, "currency": "USDT",
            "source_exchange": source_exchange.strip().lower(),
            "destination_exchange": destination_exchange.strip().lower(),
            "withdraw_fee": "0", "withdraw_fee_currency": "USDT", "min_withdraw": "0",
            "estimated_seconds": 0, "network": None, "network_profile": None,
            "route_status": "same_exchange",
        }
    return check_transfer_network_for_currency(
        "USDT", source_exchange, destination_exchange, require_withdrawal_limits=True,
    )


def validate_return_route(route: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if not route or not route.get("available"):
        return None, "обратный маршрут USDT недоступен"
    if route.get("same_exchange"):
        return route, None
    if route.get("withdraw_fee") in (None, ""):
        return None, "неизвестна комиссия обратного перевода USDT"
    if str(route.get("withdraw_fee_currency") or "").upper() != "USDT":
        return None, "комиссия обратного перевода выражена не в USDT"
    if route.get("min_withdraw") in (None, ""):
        return None, "неизвестен минимум обратного вывода USDT"
    try:
        fee = Decimal(str(route["withdraw_fee"]))
        minimum = Decimal(str(route["min_withdraw"]))
        seconds = Decimal(str(route.get("estimated_seconds") or 0))
    except (InvalidOperation, TypeError, ValueError):
        return None, "некорректная комиссия, минимум или задержка обратного перевода"
    if not fee.is_finite() or fee < 0 or not minimum.is_finite() or minimum < 0:
        return None, "некорректная комиссия или минимум обратного вывода USDT"
    if not seconds.is_finite() or seconds < 0:
        return None, "некорректная задержка обратного перевода"
    return {
        **route,
        "withdraw_fee": str(fee),
        "withdraw_fee_currency": "USDT",
        "min_withdraw": str(minimum),
        "estimated_seconds": float(seconds),
    }, None


def record_pending_return_transfer(
    cursor: Any, *, settlement_id: int, signal_id: int, run_id: str,
    sale_event_key: str, source_exchange: str, destination_exchange: str,
    amount_usdt: Decimal,
) -> None:
    """Keep one cumulative return per settlement, released after the position closes."""
    if source_exchange.strip().lower() == destination_exchange.strip().lower() or amount_usdt <= 0:
        return
    combined_key = f"{settlement_id}:return:combined"
    cursor.execute(
        "SELECT COALESCE(SUM(amount_usdt), 0) AS queued_amount FROM trader_demo_return_transfers "
        "WHERE settlement_id = %s AND sale_event_key <> %s",
        (settlement_id, combined_key),
    )
    queued = cursor.fetchone() or {}
    amount_usdt -= Decimal(str(queued.get("queued_amount") or 0))
    if amount_usdt <= 0:
        return
    cursor.execute(
        "INSERT INTO trader_demo_return_transfers "
        "(settlement_id, signal_id, run_id, sale_event_key, source_exchange, destination_exchange, "
        "amount_usdt, status, next_attempt_at_utc) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', UTC_TIMESTAMP(6)) "
        "ON DUPLICATE KEY UPDATE amount_usdt = IF(status = 'pending', VALUES(amount_usdt), amount_usdt)",
        (settlement_id, signal_id, run_id, combined_key, source_exchange.strip().lower(),
         destination_exchange.strip().lower(), amount_usdt),
    )


def _claim_due_return() -> dict[str, Any] | None:
    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM trader_demo_return_transfers "
                "WHERE ((status = 'pending' AND next_attempt_at_utc <= UTC_TIMESTAMP(6) "
                "AND EXISTS (SELECT 1 FROM trader_training_settlements AS settlement "
                "WHERE settlement.settlement_id = trader_demo_return_transfers.settlement_id "
                "AND settlement.remaining_quantity = 0 AND settlement.status = 'settled')) "
                "OR (status = 'in_transit' AND due_at_utc <= UTC_TIMESTAMP(6))) "
                "AND (claim_token IS NULL OR claimed_at_utc < UTC_TIMESTAMP(6) - INTERVAL %s SECOND) "
                "ORDER BY COALESCE(due_at_utc, next_attempt_at_utc), return_transfer_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                (RETURN_CLAIM_SECONDS,),
            )
            row = cursor.fetchone()
            if row is None:
                connection.commit()
                return None
            token = str(uuid.uuid4())
            cursor.execute(
                "UPDATE trader_demo_return_transfers SET claim_token = %s, claimed_at_utc = UTC_TIMESTAMP(6) "
                "WHERE return_transfer_id = %s",
                (token, row["return_transfer_id"]),
            )
            connection.commit()
            return {**row, "claim_token": token}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _release_return_claim(row: dict[str, Any], reason: str, *, blocked: bool = False) -> None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trader_demo_return_transfers SET status = %s, last_error = %s, "
                "attempt_count = attempt_count + 1, next_attempt_at_utc = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), "
                "due_at_utc = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND), "
                "claimed_at_utc = NULL, claim_token = NULL "
                "WHERE return_transfer_id = %s AND claim_token = %s",
                ("blocked" if blocked else row["status"], reason,
                 RETURN_RETRY_SECONDS, RETURN_RETRY_SECONDS,
                 row["return_transfer_id"], row["claim_token"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _start_return_transfer(row: dict[str, Any]) -> None:
    route = check_return_route(row["source_exchange"], row["destination_exchange"])
    route, reason = validate_return_route(route)
    if reason:
        _release_return_claim(row, reason)
        return
    if route.get("same_exchange"):
        _release_return_claim(row, "биржи отправления и получения совпадают", blocked=True)
        return

    amount = Decimal(str(row["amount_usdt"]))
    fee = Decimal(str(route["withdraw_fee"]))
    minimum = Decimal(str(route["min_withdraw"]))
    if amount <= 0:
        _release_return_claim(row, "сумма возврата USDT не положительна", blocked=True)
        return
    send_amount = amount - fee
    if send_amount <= 0 or send_amount < minimum:
        _release_return_claim(row, "выручка после комиссии ниже минимума обратного вывода")
        return

    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM trader_demo_return_transfers WHERE return_transfer_id = %s FOR UPDATE",
                (row["return_transfer_id"],),
            )
            current = cursor.fetchone()
            if (not current or current["status"] != "pending"
                    or current.get("claim_token") != row["claim_token"]):
                raise RuntimeError("Устаревшее владение демопереводом USDT")
            cursor.execute(
                "SELECT balance FROM trader_balances WHERE exchange_slug = %s AND asset = 'USDT' FOR UPDATE",
                (row["source_exchange"],),
            )
            balance_row = cursor.fetchone()
            balance_before = Decimal(str(balance_row["balance"])) if balance_row else Decimal(0)
            debit = send_amount + fee
            if balance_before < debit:
                connection.rollback()
                _release_return_claim(
                    row, f"для обратного вывода нужно {debit} USDT, "
                    f"на {row['source_exchange']} доступно {balance_before} USDT",
                )
                return
            cursor.execute(
                "UPDATE trader_balances SET balance = balance - %s WHERE exchange_slug = %s "
                "AND asset = 'USDT' AND balance >= %s",
                (debit, row["source_exchange"], debit),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Не удалось списать сумму и комиссию обратного демоперевода")
            balance_after = balance_before - debit
            from main.treider.trade import _log_demo_event
            sale_event_key = str(row["sale_event_key"])
            _log_demo_event(
                cursor, int(row["settlement_id"]), int(row["signal_id"]),
                f"{sale_event_key}:return_transfer:sent", "transfer_sent",
                row["source_exchange"], asset="USDT", quantity=-send_amount,
                quote_amount=send_amount, run_id=str(row["run_id"]),
            )
            _log_demo_event(
                cursor, int(row["settlement_id"]), int(row["signal_id"]),
                f"{sale_event_key}:commission:return_transfer", "commission",
                row["source_exchange"], asset="USDT", fee_amount=fee, fee_asset="USDT",
                quote_amount=fee, run_id=str(row["run_id"]),
            )
            _log_demo_event(
                cursor, int(row["settlement_id"]), int(row["signal_id"]),
                f"{sale_event_key}:balance:return_transfer:sent", "balance_change",
                row["source_exchange"], asset="USDT", quantity=-debit,
                balance_before=balance_before, balance_after=balance_after,
                run_id=str(row["run_id"]),
            )
            seconds = Decimal(str(route["estimated_seconds"]))
            due_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=float(seconds))
            cursor.execute(
                "UPDATE trader_demo_return_transfers SET status = 'in_transit', amount_usdt = %s, fee_usdt = %s, "
                "network_name = %s, network_profile = %s, route_status = %s, transfer_seconds = %s, "
                "due_at_utc = %s, sent_at_utc = UTC_TIMESTAMP(6), last_error = NULL, "
                "claimed_at_utc = NULL, claim_token = NULL "
                "WHERE return_transfer_id = %s AND status = 'pending' AND claim_token = %s",
                (send_amount, fee, route.get("network"), route.get("network_profile"), route.get("route_status"),
                 seconds, due_at, row["return_transfer_id"], row["claim_token"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Не удалось зафиксировать отправку обратного демоперевода")
            cursor.execute(
                "UPDATE trader_training_settlements SET return_transfer_fees_accumulated_usd = "
                "return_transfer_fees_accumulated_usd + %s, realized_profit_usd = "
                "COALESCE(realized_profit_usd, 0) - %s WHERE settlement_id = %s",
                (fee, fee, row["settlement_id"]),
            )
        connection.commit()
        logger.info(
            "Демо-возврат USDT отправлен: %s → %s, сумма=%s USDT, комиссия=%s USDT, сеть=%s, ожидание=%.1f сек.; settlement=%s",
            row["source_exchange"], row["destination_exchange"], send_amount, fee,
            route.get("network") or "?", float(seconds), row["settlement_id"],
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _complete_return_transfer(row: dict[str, Any]) -> None:
    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM trader_demo_return_transfers WHERE return_transfer_id = %s FOR UPDATE",
                (row["return_transfer_id"],),
            )
            current = cursor.fetchone()
            if (not current or current["status"] != "in_transit"
                    or current.get("claim_token") != row["claim_token"]):
                raise RuntimeError("Устаревшее владение получением обратного демоперевода")
            amount = Decimal(str(current["amount_usdt"]))
            destination = str(current["destination_exchange"]).lower()
            cursor.execute(
                "SELECT balance FROM trader_balances WHERE exchange_slug = %s AND asset = 'USDT' FOR UPDATE",
                (destination,),
            )
            balance_row = cursor.fetchone()
            balance_before = Decimal(str(balance_row["balance"])) if balance_row else Decimal(0)
            cursor.execute(
                "INSERT INTO trader_balances (exchange_slug, asset, balance) VALUES (%s, 'USDT', %s) "
                "ON DUPLICATE KEY UPDATE balance = balance + VALUES(balance)",
                (destination, amount),
            )
            balance_after = balance_before + amount
            from main.treider.trade import _log_demo_event
            sale_event_key = str(current["sale_event_key"])
            _log_demo_event(
                cursor, int(current["settlement_id"]), int(current["signal_id"]),
                f"{sale_event_key}:return_transfer:received", "transfer_received",
                destination, asset="USDT", quantity=amount, quote_amount=amount,
                run_id=str(current["run_id"]),
            )
            _log_demo_event(
                cursor, int(current["settlement_id"]), int(current["signal_id"]),
                f"{sale_event_key}:balance:return_transfer:received", "balance_change",
                destination, asset="USDT", quantity=amount,
                balance_before=balance_before, balance_after=balance_after,
                run_id=str(current["run_id"]),
            )
            cursor.execute(
                "UPDATE trader_demo_return_transfers SET status = 'completed', completed_at_utc = UTC_TIMESTAMP(6), "
                "claimed_at_utc = NULL, claim_token = NULL, last_error = NULL "
                "WHERE return_transfer_id = %s AND status = 'in_transit' AND claim_token = %s",
                (row["return_transfer_id"], row["claim_token"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Не удалось зафиксировать зачисление обратного демоперевода")
        connection.commit()
        logger.info(
            "Демо-возврат USDT зачислен: %s → %s, сумма=%s USDT; settlement=%s",
            row["source_exchange"], destination, amount, row["settlement_id"],
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def process_due_return_transfer() -> bool:
    """Process a return through one entry point; demo mode simulates it without withdrawals."""
    if not TRAINING_MODE:
        return False
    row = _claim_due_return()
    if row is None:
        return False
    try:
        if row["status"] == "pending":
            _start_return_transfer(row)
        elif row["status"] == "in_transit":
            _complete_return_transfer(row)
        else:
            _release_return_claim(row, f"неожиданный статус обратного демоперевода: {row['status']}", blocked=True)
    except Exception as error:
        logger.exception(
            "Ошибка демовозврата USDT; settlement=%s; повторная проверка через %s сек.",
            row.get("settlement_id"), RETURN_RETRY_SECONDS,
        )
        _release_return_claim(row, str(error))
    return True
