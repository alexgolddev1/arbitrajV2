"""Тренировочные сделки с ожиданием перевода и проверкой цены перед расчётом."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from database import connect_db
from main.treider.config import TRAINING_MODE
from main.treider.fees import TradeFeeRates, get_trade_fee_rates
from main.treider.market_data import (
    SpotBookTicker,
    fetch_market_sell_execution,
    fetch_spot_book,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeEstimate:
    quantity: Decimal
    buy_cost_usd: Decimal
    expected_sell_proceeds_usd: Decimal
    buy_fee_usd: Decimal
    expected_sell_fee_usd: Decimal
    expected_profit_usd: Decimal
    shortfall_usd: Decimal
    available_balance_usd: Decimal


def _quote_currency(pair: str) -> str:
    for separator in ("/", "_", "-"):
        if separator in pair:
            return pair.strip().upper().split(separator, 1)[1]
    raise ValueError(f"Не удалось определить котируемую валюту в паре {pair!r}")


def _estimate_trade(
    buy: SpotBookTicker,
    sell: SpotBookTicker,
    buy_fees: TradeFeeRates,
    sell_fees: TradeFeeRates,
    balance_usd: Decimal,
) -> TradeEstimate:
    if _quote_currency(buy.market_pair) not in {"USD", "USDT", "USDC", "BUSD", "FDUSD"}:
        raise ValueError(
            f"Расчёт баланса в USD для котируемой валюты {_quote_currency(buy.market_pair)} не поддержан"
        )
    if buy_fees.taker_rate <= -1 or sell_fees.taker_rate >= 1:
        raise ValueError("Получены комиссии, непригодные для расчёта размера сделки")

    buy_factor = Decimal(1) + buy_fees.taker_rate
    if buy_factor <= 0:
        raise ValueError("Комиссия покупки делает стоимость единицы неположительной")
    affordable_quantity = balance_usd / (buy.ask * buy_factor)
    # Объём продажи не ограничиваем размером первого bid: после задержки
    # продажа будет исполнена по нескольким уровням стакана.
    quantity = min(affordable_quantity, buy.ask_size)
    if quantity <= 0:
        raise ValueError("Недостаточный баланс или нулевой доступный объём стакана")

    buy_cost = quantity * buy.ask
    expected_sell_proceeds = quantity * sell.bid
    buy_fee = buy_cost * buy_fees.taker_rate
    expected_sell_fee = expected_sell_proceeds * sell_fees.taker_rate
    expected_profit = expected_sell_proceeds - expected_sell_fee - buy_cost - buy_fee
    return TradeEstimate(
        quantity=quantity,
        buy_cost_usd=buy_cost,
        expected_sell_proceeds_usd=expected_sell_proceeds,
        buy_fee_usd=buy_fee,
        expected_sell_fee_usd=expected_sell_fee,
        expected_profit_usd=expected_profit,
        shortfall_usd=max(Decimal(0), -expected_profit),
        available_balance_usd=balance_usd,
    )


def _read_balance() -> Decimal:
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT balance_usd FROM trader_account WHERE account_id = 1")
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("В БД не найден счёт трейдера")
        return Decimal(str(row["balance_usd"]))
    finally:
        connection.close()


def _prepare_training_trade(
    signal: dict[str, Any],
    signal_id: int,
    buy: SpotBookTicker,
    sell: SpotBookTicker,
    buy_fees: TradeFeeRates,
    sell_fees: TradeFeeRates,
    transfer_seconds: float,
) -> tuple[TradeEstimate | None, str]:
    """Записать отложенную симуляцию и зарезервировать объём без изменения баланса."""
    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT balance_usd FROM trader_account WHERE account_id = 1 FOR UPDATE"
            )
            account = cursor.fetchone()
            if account is None:
                raise RuntimeError("В БД не найден счёт трейдера")
            cursor.execute(
                "SELECT status FROM trader_training_settlements WHERE signal_id = %s",
                (signal_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                connection.commit()
                return None, f"сигнал уже запущен в тренировке (статус {existing['status']})"

            balance = Decimal(str(account["balance_usd"]))
            cursor.execute(
                "SELECT COALESCE(SUM(buy_cost_usd + buy_fee_usd), 0) AS reserved_usd "
                "FROM trader_training_settlements WHERE status IN ('pending', 'settling')"
            )
            reserved = Decimal(str(cursor.fetchone()["reserved_usd"]))
            available = max(Decimal(0), balance - reserved)
            estimate = _estimate_trade(buy, sell, buy_fees, sell_fees, available)
            if estimate.expected_profit_usd <= 0:
                connection.commit()
                return estimate, "ожидаемый результат после комиссий неположительный"

            settle_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=max(0.0, transfer_seconds)
            )
            cursor.execute(
                "INSERT INTO trader_training_settlements ("
                "signal_id, source_pair, buy_exchange, buy_market_pair, sell_exchange, sell_market_pair, "
                "quantity, buy_price, expected_sell_price, buy_taker_fee_rate, expected_sell_taker_fee_rate, "
                "buy_cost_usd, buy_fee_usd, expected_sell_fee_usd, expected_profit_usd, "
                "balance_before_usd, settle_after_utc, status"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
                (
                    signal_id, signal["source_pair"], buy.exchange, buy.market_pair,
                    sell.exchange, sell.market_pair, estimate.quantity, buy.ask, sell.bid,
                    buy_fees.taker_rate, sell_fees.taker_rate, estimate.buy_cost_usd,
                    estimate.buy_fee_usd, estimate.expected_sell_fee_usd,
                    estimate.expected_profit_usd, balance, settle_at,
                ),
            )
        connection.commit()
        return estimate, "ожидает окончания эмулируемого перевода"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _claim_due_trade() -> dict[str, Any] | None:
    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM trader_training_settlements WHERE "
                "(status = 'pending' AND settle_after_utc <= UTC_TIMESTAMP()) OR "
                "(status = 'settling' AND claimed_at < UTC_TIMESTAMP() - INTERVAL 2 MINUTE) "
                "ORDER BY settle_after_utc, settlement_id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = cursor.fetchone()
            if row is None:
                connection.rollback()
                return None
            cursor.execute(
                "UPDATE trader_training_settlements SET status = 'settling', "
                "claimed_at = UTC_TIMESTAMP(), settlement_attempts = settlement_attempts + 1 "
                "WHERE settlement_id = %s",
                (row["settlement_id"],),
            )
        connection.commit()
        return row
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _retry_settlement(trade_id: int, reason: str) -> None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trader_training_settlements SET status = 'pending', claimed_at = NULL, "
                "settle_after_utc = UTC_TIMESTAMP() + INTERVAL 15 SECOND, last_error = %s "
                "WHERE settlement_id = %s AND status = 'settling'",
                (reason[:4000], trade_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _settle_training_trade(trade: dict[str, Any]) -> None:
    trade_id = int(trade["settlement_id"])
    context = (
        f"сигнал={trade['signal_id']}, пара={trade['source_pair']}, "
        f"покупка={trade['buy_exchange']} {trade['buy_market_pair']} по {trade['buy_price']}, "
        f"продажа={trade['sell_exchange']} {trade['sell_market_pair']}"
    )
    try:
        quantity = Decimal(str(trade["quantity"]))
        execution = fetch_market_sell_execution(
            trade["sell_exchange"], trade["sell_market_pair"], quantity
        )
        sell_fees = get_trade_fee_rates(trade["sell_exchange"], trade["sell_market_pair"])
        buy_cost = Decimal(str(trade["buy_cost_usd"]))
        buy_fee = Decimal(str(trade["buy_fee_usd"]))
        proceeds = execution.proceeds
        sell_fee = proceeds * sell_fees.taker_rate
        realized_profit = proceeds - sell_fee - buy_cost - buy_fee
    except Exception as error:
        _retry_settlement(trade_id, str(error))
        logger.exception(
            "Не удалось повторно проверить цену продажи; повтор через 15 сек.; %s",
            context,
        )
        return

    connection = connect_db()
    try:
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT balance_usd FROM trader_account WHERE account_id = 1 FOR UPDATE"
            )
            account = cursor.fetchone()
            cursor.execute(
                "SELECT status FROM trader_training_settlements WHERE settlement_id = %s FOR UPDATE",
                (trade_id,),
            )
            state = cursor.fetchone()
            if account is None or state is None or state["status"] != "settling":
                raise RuntimeError("Тренировочный счёт или состояние отложенной сделки изменились")
            balance_before = Decimal(str(account["balance_usd"]))
            balance_after = (balance_before + realized_profit).quantize(Decimal("0.00000001"))
            cursor.execute(
                "UPDATE trader_account SET balance_usd = %s WHERE account_id = 1",
                (balance_after,),
            )
            cursor.execute(
                "UPDATE trader_training_settlements SET status = 'settled', claimed_at = NULL, "
                "final_sell_price = %s, actual_sell_taker_fee_rate = %s, "
                "realized_sell_proceeds_usd = %s, sell_fee_usd = %s, "
                "realized_profit_usd = %s, settlement_balance_before_usd = %s, "
                "balance_after_usd = %s, settled_at = UTC_TIMESTAMP(), "
                "last_error = NULL WHERE settlement_id = %s",
                (execution.average_price, sell_fees.taker_rate, proceeds, sell_fee,
                 realized_profit, balance_before, balance_after, trade_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    if realized_profit > 0:
        logger.info(
            "Тренировочная рыночная продажа после сети: лучший bid=%s, средняя цена исполнения=%s, "
            "уровней стакана=%s, прибыль после двух комиссий=%s USD, баланс=%s → %s USD; %s",
            execution.best_bid, execution.average_price, execution.levels_used,
            realized_profit, balance_before, balance_after, context,
        )
    else:
        logger.warning(
            "После ожидания сети возможность стала убыточной: лучший bid=%s, средняя цена исполнения=%s, "
            "уровней стакана=%s, результат=%s USD, не хватает до безубыточности=%s USD, баланс=%s → %s USD; %s",
            execution.best_bid, execution.average_price, execution.levels_used,
            realized_profit, -realized_profit,
            balance_before, balance_after, context,
        )


def process_due_training_trade() -> bool:
    """Settle one due training trade after refreshing the destination quote."""
    if not TRAINING_MODE:
        return False
    trade = _claim_due_trade()
    if trade is None:
        return False
    _settle_training_trade(trade)
    return True


def process_trade(
    signal: dict[str, Any],
    signal_id: int,
    buy_book: SpotBookTicker,
    sell_book: SpotBookTicker,
    buy_fees: TradeFeeRates | None,
    sell_fees: TradeFeeRates | None,
    transfer_seconds: float,
) -> None:
    """Estimate profitability, then schedule simulation or report live stub."""
    context = (
        f"сигнал={signal_id}, пара={signal['source_pair']}, "
        f"покупка={buy_book.exchange} ask={buy_book.ask}, "
        f"продажа={sell_book.exchange} bid={sell_book.bid}"
    )
    if buy_fees is None or sell_fees is None:
        logger.warning("Торговый расчёт пропущен: не получена комиссия одной или обеих бирж; %s", context)
        return

    try:
        if TRAINING_MODE:
            estimate, result = _prepare_training_trade(
                signal, signal_id, buy_book, sell_book,
                buy_fees, sell_fees, transfer_seconds,
            )
            if estimate is None:
                logger.info("Тренировочный сигнал не запущен: %s; %s", result, context)
                return
        else:
            estimate = _estimate_trade(
                buy_book, sell_book, buy_fees, sell_fees, _read_balance()
            )
            result = ""
    except Exception:
        logger.exception("Не удалось оценить тренировочную сделку; %s", context)
        return

    if estimate.expected_profit_usd <= 0:
        logger.info(
            "Связка не покрывает расходы двух сделок: ожидаемый результат=%s USD, "
            "не хватает до безубыточности=%s USD; объём=%s, комиссии покупки/продажи=%s/%s USD; %s",
            estimate.expected_profit_usd, estimate.shortfall_usd, estimate.quantity,
            estimate.buy_fee_usd, estimate.expected_sell_fee_usd, context,
        )
        return

    if not TRAINING_MODE:
        logger.info(
            "Расчёт после двух taker-комиссий положительный: ожидаемая прибыль=%s USD; %s",
            estimate.expected_profit_usd, context,
        )
        logger.info(
            "Функционал не реализован: рабочее исполнение ордеров; заявка не отправлена; %s",
            context,
        )
        return

    logger.info(
        "Тренировочная покупка смоделирована по ask=%s, объём=%s; ожидаемая прибыль=%s USD. "
        "Баланс пока не изменён: ожидание сети %.1f сек., затем будет повторно проверена цена продажи; %s",
        buy_book.ask, estimate.quantity, estimate.expected_profit_usd,
        max(0.0, transfer_seconds), context,
    )
