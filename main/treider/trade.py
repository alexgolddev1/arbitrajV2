"""Тренировочные сделки с ожиданием перевода и проверкой цены перед расчётом."""

from __future__ import annotations

import logging
import json
import uuid
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from database import connect_db
from main.treider.config import (
    BUY_FEE_CURRENCY,
    FRACTIONAL_QUANTITY_DECIMAL_PLACES,
    INTEGER_QUANTITY_PRICE_THRESHOLD_USD,
    MIN_NET_PROFIT_USDT,
    PRICE_MOVE_RESERVE_PERCENT,
    SETTLEMENT_MAX_WAIT_SECONDS,
    SETTLEMENT_RETRY_SECONDS,
    SIGNAL_MAX_AGE_SECONDS,
    STOP_LOSS_PERCENT,
    TRAINING_MODE,
)
from main.treider.fees import TradeFeeRates, get_trade_fee_rates
from main.treider.market_data import (
    InsufficientLiquidity,
    SpotBookTicker,
    fetch_market_sell_execution,
    fetch_spot_book,
    fetch_limit_sell_execution,
)
from main.treider.config import TRANSFER_FEE_AMOUNT

logger = logging.getLogger(__name__)


def calculate_stop_loss_price(entry_price: Decimal, stop_loss_percent: Decimal = STOP_LOSS_PERCENT) -> Decimal:
    if entry_price <= 0 or stop_loss_percent < 0 or stop_loss_percent >= 100:
        raise ValueError("Некорректная цена входа или процент стоп-лосса")
    return entry_price * (Decimal(1) - stop_loss_percent / Decimal(100))


def _exit_trigger(best_bid: Decimal, take_profit_price: Decimal, stop_loss_price: Decimal) -> str | None:
    if best_bid >= take_profit_price:
        return "take_profit"
    if best_bid <= stop_loss_price:
        return "stop_loss"
    return None


@dataclass(frozen=True)
class TradeEstimate:
    quantity: Decimal
    quantity_after_fees: Decimal
    buy_cost_usd: Decimal
    expected_sell_average_price: Decimal
    expected_sell_proceeds_usd: Decimal
    buy_fee_usd: Decimal
    buy_fee_base_quantity: Decimal
    transfer_fee_amount: Decimal
    transfer_fee_currency: str
    transfer_fee_usd: Decimal
    expected_sell_fee_usd: Decimal
    expected_profit_before_reserve_usd: Decimal
    price_reserve_usd: Decimal
    expected_profit_after_reserve_usd: Decimal
    shortfall_usd: Decimal
    available_balance_usd: Decimal


def _quote_currency(pair: str) -> str:
    for separator in ("/", "_", "-"):
        if separator in pair:
            return pair.strip().upper().split(separator, 1)[1]
    raise ValueError(f"Не удалось определить котируемую валюту в паре {pair!r}")


def _round_buy_quantity(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """Round simulated buy size down: whole cheap coins, 8 decimals for pricier coins."""
    decimals = (
        0 if unit_price < INTEGER_QUANTITY_PRICE_THRESHOLD_USD
        else FRACTIONAL_QUANTITY_DECIMAL_PLACES
    )
    step = Decimal(1).scaleb(-decimals)
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def _estimate_trade(
    buy: SpotBookTicker,
    sell: SpotBookTicker,
    buy_fees: TradeFeeRates,
    sell_fees: TradeFeeRates,
    balance_usd: Decimal,
    transfer: dict[str, Any] | None = None,
) -> TradeEstimate:
    quote = _quote_currency(buy.market_pair)
    if quote != "USDT":
        raise ValueError(
            f"Поддерживаются только пары с USDT; {quote} автоматически к USDT не приравнивается"
        )
    buy_rate = buy_fees.taker_for("buy")
    sell_rate = sell_fees.taker_for("sell")
    if buy_rate <= -1 or sell_rate >= 1:
        raise ValueError("Получены комиссии, непригодные для расчёта размера сделки")

    buy_factor = Decimal(1) + (buy_rate if BUY_FEE_CURRENCY[buy.exchange] == "quote" else Decimal(0))
    if buy_factor <= 0:
        raise ValueError("Комиссия покупки делает стоимость единицы неположительной")
    transfer_fee = Decimal(str((transfer or {}).get("withdraw_fee") or TRANSFER_FEE_AMOUNT or 0))
    transfer_fee_currency = str((transfer or {}).get("withdraw_fee_currency") or "").upper()
    if transfer_fee > 0 and not transfer_fee_currency:
        raise ValueError("Недостаточно данных: не указана валюта комиссии перевода")
    quote_transfer_fee = transfer_fee if transfer_fee_currency == quote else Decimal(0)
    affordable_balance = max(Decimal(0), balance_usd - quote_transfer_fee)
    affordable_quantity = affordable_balance / (buy.ask * buy_factor)
    quantity = _round_buy_quantity(min(affordable_quantity, buy.ask_size), buy.ask)
    if quantity <= 0:
        raise ValueError("Недостаточный баланс или нулевой доступный объём стакана")

    buy_cost = quantity * buy.ask
    buy_fee_currency = BUY_FEE_CURRENCY[buy.exchange]
    buy_fee_base = quantity * buy_rate if buy_fee_currency == "base" else Decimal(0)
    buy_fee = buy_cost * buy_rate if buy_fee_currency == "quote" else Decimal(0)
    quantity_after_fees = quantity - buy_fee_base
    if quantity_after_fees <= 0:
        raise ValueError("Комиссия покупки оставляет нулевое количество актива")
    if transfer_fee_currency and transfer_fee_currency not in {quote, _base_currency(buy.market_pair)}:
        raise ValueError(f"Комиссия перевода в {transfer_fee_currency} не поддерживается для USDT-симуляции")
    if transfer_fee_currency == _base_currency(buy.market_pair):
        quantity_after_fees -= transfer_fee
    if quantity_after_fees <= 0:
        raise ValueError("Комиссия перевода съедает весь купленный актив")
    execution = fetch_market_sell_execution(sell.exchange, sell.market_pair, quantity_after_fees)
    if execution.remaining_quantity > 0:
        raise ValueError(
            f"Недостаточно ожидаемой глубины bids: исполнится {execution.filled_quantity} из {quantity_after_fees}"
        )
    expected_sell_proceeds = execution.proceeds
    expected_sell_fee = expected_sell_proceeds * sell_rate
    transfer_fee_usd = quote_transfer_fee
    expected_profit_before_reserve = (
        expected_sell_proceeds - expected_sell_fee - buy_cost - buy_fee - transfer_fee_usd
    )
    reserve = buy_cost * PRICE_MOVE_RESERVE_PERCENT / Decimal(100)
    expected_profit_after_reserve = expected_profit_before_reserve - reserve
    return TradeEstimate(
        quantity=quantity,
        quantity_after_fees=quantity_after_fees,
        buy_cost_usd=buy_cost,
        expected_sell_average_price=execution.average_price,
        expected_sell_proceeds_usd=expected_sell_proceeds,
        buy_fee_usd=buy_fee,
        buy_fee_base_quantity=buy_fee_base,
        transfer_fee_amount=transfer_fee,
        transfer_fee_currency=transfer_fee_currency or "USDT",
        transfer_fee_usd=transfer_fee_usd,
        expected_sell_fee_usd=expected_sell_fee,
        expected_profit_before_reserve_usd=expected_profit_before_reserve,
        price_reserve_usd=reserve,
        expected_profit_after_reserve_usd=expected_profit_after_reserve,
        shortfall_usd=max(Decimal(0), MIN_NET_PROFIT_USDT - expected_profit_after_reserve),
        available_balance_usd=balance_usd,
    )


def _base_currency(pair: str) -> str:
    for separator in ("/", "_", "-"):
        if separator in pair:
            return pair.strip().upper().split(separator, 1)[0]
    raise ValueError(f"Не удалось определить базовую валюту в паре {pair!r}")


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
    transfer: dict[str, Any] | None = None,
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
                "FROM trader_training_settlements WHERE status IN ('pending', 'settling') AND route_status = 'legacy'"
            )
            legacy_reserved = Decimal(str(cursor.fetchone()["reserved_usd"]))
            available = max(Decimal(0), balance - legacy_reserved)
            estimate = _estimate_trade(buy, sell, buy_fees, sell_fees, available, transfer)
            if estimate.expected_profit_after_reserve_usd < MIN_NET_PROFIT_USDT:
                logger.info(
                    "Тренировочная сделка невыгодна после полного расчёта: прибыль до резерва=%s USDT, "
                    "резерв цены=%s USDT, прибыль после резерва=%s USDT, минимум=%s USDT, "
                    "не хватает=%s USDT; объём покупки=%s по ask=%s, выручка продажи=%s "
                    "по средней цене=%s, комиссия покупки=%s (ставка=%s, валюта=%s), "
                    "комиссия продажи=%s (ставка=%s), комиссия перевода=%s %s (в USDT учтено %s); сигнал=%s, пара=%s",
                    estimate.expected_profit_before_reserve_usd,
                    estimate.price_reserve_usd,
                    estimate.expected_profit_after_reserve_usd,
                    MIN_NET_PROFIT_USDT,
                    estimate.shortfall_usd,
                    estimate.quantity,
                    buy.ask,
                    estimate.expected_sell_proceeds_usd,
                    estimate.expected_sell_average_price,
                    estimate.buy_fee_usd,
                    buy_fees.taker_for("buy"),
                    BUY_FEE_CURRENCY[buy.exchange],
                    estimate.expected_sell_fee_usd,
                    sell_fees.taker_for("sell"),
                    estimate.transfer_fee_amount,
                    estimate.transfer_fee_currency,
                    estimate.transfer_fee_usd,
                    signal_id,
                    signal["source_pair"],
                )
                connection.commit()
                return estimate, (
                    "ожидаемая прибыль после резерва ниже настроенного минимума: "
                    f"{estimate.expected_profit_after_reserve_usd} USDT; минимум {MIN_NET_PROFIT_USDT} USDT"
                )
            started_raw = signal.get("opportunity_started_at_utc")
            if started_raw:
                started_at = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)).total_seconds()
                if age > SIGNAL_MAX_AGE_SECONDS:
                    connection.commit()
                    return estimate, f"сигнал устарел для нового входа: {age:.1f} сек."

            settle_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=max(0.0, transfer_seconds)
            )
            route_status = str((transfer or {}).get("route_status") or "unconfirmed")
            if (transfer or {}).get("withdraw_fee") is None and transfer and transfer.get("fee_required", True):
                connection.commit()
                return estimate, "недостаточно данных: неизвестна комиссия перевода"
            buy_debit = estimate.buy_cost_usd + estimate.buy_fee_usd + estimate.transfer_fee_usd
            take_profit_price = Decimal(str(signal["sell"]["price"]))
            stop_loss_price = calculate_stop_loss_price(buy.ask)
            assumptions = dict(transfer or {})
            assumptions["buy_fee_source"] = buy_fees.source
            assumptions["sell_fee_source"] = sell_fees.source
            assumptions["buy_fee_currency_model"] = BUY_FEE_CURRENCY[buy.exchange]
            assumptions["purchase_model"] = "marketable-limit-at-best-ask; capped-by-visible-best-ask-size"
            assumptions["take_profit_source"] = "signal.sell.price"
            assumptions["stop_loss_percent"] = str(STOP_LOSS_PERCENT)
            cursor.execute(
                "INSERT INTO trader_training_settlements ("
                "signal_id, source_pair, buy_exchange, buy_market_pair, sell_exchange, sell_market_pair, "
                "quantity, buy_price, expected_sell_price, stop_loss_price, buy_taker_fee_rate, expected_sell_taker_fee_rate, "
                "buy_cost_usd, buy_fee_usd, expected_sell_fee_usd, expected_profit_usd, "
                "balance_before_usd, settle_after_utc, status, route_status, route_assumptions, network_name, "
                "transfer_seconds, buy_fee_source, sell_fee_source, buy_fee_currency, buy_fee_base_quantity, "
                "transfer_fee_amount, transfer_fee_currency, transfer_fee_usd, bought_quantity, quantity_after_fees, remaining_quantity, "
                "expected_sell_proceeds_usd, expected_profit_before_reserve_usd, price_reserve_usd, "
                "expected_profit_after_reserve_usd, buy_debit_usd, purchase_model, position_status, "
                "planned_sell_at, retry_started_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'marketable-limit-at-best-ask', 'pending_transfer', %s, NULL)",
                (
                    signal_id, signal["source_pair"], buy.exchange, buy.market_pair,
                    sell.exchange, sell.market_pair, estimate.quantity, buy.ask, take_profit_price, stop_loss_price,
                    buy_fees.taker_for("buy"), sell_fees.taker_for("sell"), estimate.buy_cost_usd,
                    estimate.buy_fee_usd, estimate.expected_sell_fee_usd,
                    estimate.expected_profit_before_reserve_usd, balance, settle_at,
                    route_status, json.dumps(assumptions, ensure_ascii=False, default=str),
                    (transfer or {}).get("network"), transfer_seconds,
                    buy_fees.source, sell_fees.source,
                    BUY_FEE_CURRENCY[buy.exchange], estimate.buy_fee_base_quantity,
                    estimate.transfer_fee_amount, estimate.transfer_fee_currency, estimate.transfer_fee_usd,
                    estimate.quantity, estimate.quantity_after_fees, estimate.quantity_after_fees,
                    estimate.expected_sell_proceeds_usd, estimate.expected_profit_before_reserve_usd,
                    estimate.price_reserve_usd, estimate.expected_profit_after_reserve_usd,
                    buy_debit, settle_at,
                ),
            )
            cursor.execute(
                "UPDATE trader_account SET balance_usd = balance_usd - %s WHERE account_id = 1 AND balance_usd >= %s",
                (buy_debit, buy_debit),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Недостаточно доступного демобаланса для атомарного резервирования покупки")
        connection.commit()
        return estimate, "виртуальная покупка записана; ожидание эмулируемого перевода"
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
                "(status IN ('pending','partial') AND settle_after_utc <= UTC_TIMESTAMP()) OR "
                "(status = 'settling' AND claimed_at < UTC_TIMESTAMP() - INTERVAL 2 MINUTE) "
                "ORDER BY settle_after_utc, settlement_id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = cursor.fetchone()
            if row is None:
                connection.rollback()
                return None
            token = str(uuid.uuid4())
            cursor.execute(
                "UPDATE trader_training_settlements SET status = 'settling', "
                "claimed_at = UTC_TIMESTAMP(), claim_token = %s, settlement_attempts = settlement_attempts + 1 "
                "WHERE settlement_id = %s",
                (token, row["settlement_id"]),
            )
        connection.commit()
        row["claim_token"] = token
        return row
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _retry_settlement(trade_id: int, reason: str, claim_token: str | None = None) -> None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trader_training_settlements SET "
                "status = IF(position_status <> 'legacy' AND planned_sell_at < "
                "UTC_TIMESTAMP() - INTERVAL %s SECOND, 'open', 'pending'), "
                "position_status = IF(position_status <> 'legacy' AND planned_sell_at < "
                "UTC_TIMESTAMP() - INTERVAL %s SECOND, 'open', position_status), "
                "claimed_at = NULL, claim_token = NULL, retry_started_at = COALESCE(retry_started_at, UTC_TIMESTAMP()), "
                "settle_after_utc = UTC_TIMESTAMP() + INTERVAL %s SECOND, last_error = %s "
                "WHERE settlement_id = %s AND status = 'settling' AND claim_token = %s",
                (SETTLEMENT_MAX_WAIT_SECONDS, SETTLEMENT_MAX_WAIT_SECONDS,
                 SETTLEMENT_RETRY_SECONDS, reason[:4000], trade_id, claim_token),
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
        quantity = Decimal(str(trade.get("remaining_quantity") or trade["quantity"]))
        sell_fees = get_trade_fee_rates(trade["sell_exchange"], trade["sell_market_pair"])
        current_sell_book = fetch_spot_book(trade["sell_exchange"], trade["sell_market_pair"])
        take_profit_price = Decimal(str(trade["expected_sell_price"]))
        stop_loss_price = Decimal(str(
            trade.get("stop_loss_price")
            or calculate_stop_loss_price(Decimal(str(trade["buy_price"])))
        ))
        trigger = _exit_trigger(current_sell_book.bid, take_profit_price, stop_loss_price)
        if trigger is None:
            _retry_settlement(
                trade_id,
                f"тейк-профит не достигнут и стоп-лосс не сработал: bid={current_sell_book.bid}, "
                f"тейк-профит={take_profit_price}, стоп-лосс={stop_loss_price}",
                trade.get("claim_token"),
            )
            logger.info(
                "Тренировочная позиция остаётся открытой: лучшая цена продажи (bid)=%s; "
                "последняя цена сделки=%s; TP=%s; SL=%s; "
                "повторная проверка через %s сек.; %s",
                current_sell_book.bid, current_sell_book.last_price or "н/д",
                take_profit_price, stop_loss_price, SETTLEMENT_RETRY_SECONDS, context,
            )
            return
        execution = (
            fetch_limit_sell_execution(
                trade["sell_exchange"], trade["sell_market_pair"], quantity, take_profit_price
            )
            if trigger == "take_profit"
            else fetch_market_sell_execution(
                trade["sell_exchange"], trade["sell_market_pair"], quantity
            )
        )
        if execution.filled_quantity <= 0:
            raise InsufficientLiquidity("Недостаточно bid-ликвидности: стакан не исполнил ни одной части объёма")
        proceeds = execution.proceeds
        sell_rate = sell_fees.taker_for("sell")
        sell_fee = proceeds * sell_rate
    except InsufficientLiquidity as error:
        _retry_settlement(trade_id, str(error), trade.get("claim_token"))
        logger.warning(
            "В стакане недостаточно ликвидности даже для частичной продажи; повтор через %s сек.; %s",
            SETTLEMENT_RETRY_SECONDS, context,
        )
        return
    except Exception as error:
        _retry_settlement(trade_id, str(error), trade.get("claim_token"))
        logger.exception(
            "Ошибка API/расчёта продажи; это не считается нехваткой ликвидности; повтор через %s сек.; %s",
            SETTLEMENT_RETRY_SECONDS,
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
                "SELECT status, claim_token, position_status, remaining_quantity, sold_quantity, "
                "sell_proceeds_accumulated_usd, sell_fees_accumulated_usd, buy_debit_usd, "
                "settlement_attempts, planned_sell_at FROM trader_training_settlements "
                "WHERE settlement_id = %s FOR UPDATE",
                (trade_id,),
            )
            state = cursor.fetchone()
            if (account is None or state is None or state["status"] != "settling"
                    or state["claim_token"] != trade.get("claim_token")):
                raise RuntimeError("Тренировочный счёт или состояние отложенной сделки изменились")
            balance_before = Decimal(str(account["balance_usd"]))
            old_remaining = Decimal(str(state["remaining_quantity"]))
            filled = min(old_remaining, execution.filled_quantity)
            remaining = old_remaining - filled
            accumulated_proceeds = Decimal(str(state["sell_proceeds_accumulated_usd"])) + proceeds
            accumulated_sell_fees = Decimal(str(state["sell_fees_accumulated_usd"])) + sell_fee
            buy_debit = Decimal(str(state["buy_debit_usd"]))
            realized_profit = accumulated_proceeds - accumulated_sell_fees - buy_debit
            balance_after = (balance_before + proceeds - sell_fee).quantize(Decimal("0.00000001"))
            cursor.execute(
                "UPDATE trader_account SET balance_usd = %s WHERE account_id = 1",
                (balance_after,),
            )
            exceeded_wait = False
            if remaining > 0:
                cursor.execute(
                    "SELECT TIMESTAMPDIFF(SECOND, planned_sell_at, UTC_TIMESTAMP()) AS elapsed_seconds "
                    "FROM trader_training_settlements WHERE settlement_id = %s",
                    (trade_id,),
                )
                elapsed_row = cursor.fetchone()
                elapsed_since_sell = int((elapsed_row or {}).get("elapsed_seconds") or 0)
                exceeded_wait = elapsed_since_sell >= SETTLEMENT_MAX_WAIT_SECONDS
            next_status = "settled" if remaining <= 0 else "open" if exceeded_wait else "partial"
            next_position_status = "closed" if remaining <= 0 else "open" if exceeded_wait else "partial"
            next_retry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=SETTLEMENT_RETRY_SECONDS
            )
            cursor.execute(
                "UPDATE trader_training_settlements SET status = %s, position_status = %s, sell_fee_source = %s, "
                "claimed_at = NULL, claim_token = NULL, final_sell_price = %s, actual_sell_taker_fee_rate = %s, "
                "realized_sell_proceeds_usd = %s, sell_fee_usd = %s, realized_profit_usd = %s, "
                "settlement_balance_before_usd = %s, balance_after_usd = %s, settled_at = %s, "
                "sell_proceeds_accumulated_usd = %s, sell_fees_accumulated_usd = %s, "
                "sold_quantity = sold_quantity + %s, remaining_quantity = %s, actual_sell_at = UTC_TIMESTAMP(), "
                "last_valuation_price = %s, valuation_usd = %s, valued_at = UTC_TIMESTAMP(), "
                "settle_after_utc = %s, last_error = %s "
                "WHERE settlement_id = %s AND status = 'settling' AND claim_token = %s",
                (next_status, next_position_status, sell_fees.source, execution.average_price, sell_rate,
                 accumulated_proceeds, accumulated_sell_fees, realized_profit, balance_before,
                 balance_after, datetime.now(timezone.utc).replace(tzinfo=None) if next_status == "settled" else None,
                 accumulated_proceeds, accumulated_sell_fees, filled, remaining,
                 execution.best_bid, remaining * execution.best_bid if remaining > 0 else Decimal(0),
                 next_retry if next_status == "partial" else trade["settle_after_utc"],
                 None if remaining <= 0 else f"частично исполнено; остаток {remaining}",
                 trade_id, trade.get("claim_token")),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Устаревшее владение задачей: продажу зафиксировать нельзя")
        connection.commit()
    except Exception as error:
        connection.rollback()
        try:
            _retry_settlement(trade_id, str(error), trade.get("claim_token"))
        except Exception:
            logger.exception(
                "Не удалось вернуть тренировочную продажу в очередь повторов; settlement=%s",
                trade_id,
            )
            raise
        logger.exception(
            "Ошибка фиксации тренировочной продажи; баланс не зафиксирован, "
            "сделка поставлена на повтор через %s сек.; settlement=%s",
            SETTLEMENT_RETRY_SECONDS,
            trade_id,
        )
        return
    finally:
        connection.close()

    if realized_profit > 0:
        logger.info(
            "Тренировочная продажа (%s) после сети: лучший bid=%s, средняя цена исполнения=%s, "
            "уровней стакана=%s, исполнено=%s, остаток=%s, накопленный результат=%s USD, баланс=%s → %s USDT; %s",
            trigger, execution.best_bid, execution.average_price, execution.levels_used,
            execution.filled_quantity, execution.remaining_quantity,
            realized_profit, balance_before, balance_after, context,
        )
    else:
        logger.warning(
            "После ожидания сети сработал выход (%s), продажа убыточна или частична: лучший bid=%s, средняя цена исполнения=%s, "
            "уровней стакана=%s, исполнено=%s, остаток=%s, результат=%s USDT, баланс=%s → %s USDT; %s",
            trigger, execution.best_bid, execution.average_price, execution.levels_used,
            execution.filled_quantity, execution.remaining_quantity, realized_profit,
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
    transfer_seconds: float | dict[str, Any],
) -> tuple[str, str]:
    """Estimate profitability, then schedule simulation or report live stub."""
    context = (
        f"сигнал={signal_id}, пара={signal['source_pair']}, "
        f"покупка={buy_book.exchange} ask={buy_book.ask}, "
        f"продажа={sell_book.exchange} bid={sell_book.bid}"
    )
    if buy_fees is None or sell_fees is None:
        logger.warning("Торговый расчёт пропущен: не получена комиссия одной или обеих бирж; %s", context)
        return "rejected", "не получены торговые комиссии"
    transfer = transfer_seconds if isinstance(transfer_seconds, dict) else {
        "estimated_seconds": transfer_seconds,
        "route_status": "unconfirmed",
        "withdraw_fee": TRANSFER_FEE_AMOUNT,
        "withdraw_fee_currency": "USDT",
        "fee_required": TRANSFER_FEE_AMOUNT is not None,
    }
    delay_seconds = float(transfer.get("estimated_seconds", 0))

    try:
        if TRAINING_MODE:
            estimate, result = _prepare_training_trade(
                signal, signal_id, buy_book, sell_book,
                buy_fees, sell_fees, delay_seconds, transfer,
            )
            if estimate is None:
                logger.info("Тренировочный сигнал не запущен: %s; %s", result, context)
                return ("simulation_started" if "уже запущен" in result else "rejected"), result
            if result != "виртуальная покупка записана; ожидание эмулируемого перевода":
                logger.info("Тренировочный вход отклонён до покупки: %s; %s", result, context)
                return "rejected", result
        else:
            estimate = _estimate_trade(
                buy_book, sell_book, buy_fees, sell_fees, _read_balance(), transfer
            )
            result = ""
    except ValueError as error:
        logger.info("Торговая симуляция отклонена по ограничениям расчёта: %s; %s", error, context)
        return "rejected", str(error)
    except Exception as error:
        logger.exception("Не удалось оценить тренировочную сделку; %s", context)
        return "temporary_error", str(error)

    if estimate.expected_profit_after_reserve_usd < MIN_NET_PROFIT_USDT:
        logger.info(
            "Сделка отклонена по порогу: прибыль до резерва=%s USDT, резерв=%s, "
            "прибыль после резерва=%s, минимум=%s; не хватает=%s USDT; объём=%s; %s",
            estimate.expected_profit_before_reserve_usd, estimate.price_reserve_usd,
            estimate.expected_profit_after_reserve_usd, MIN_NET_PROFIT_USDT,
            estimate.shortfall_usd, estimate.quantity, context,
        )
        return "rejected", "прибыль после резерва ниже минимума"

    if not TRAINING_MODE:
        logger.info(
            "Расчёт положительный: до резерва=%s USDT, резерв=%s, после резерва=%s USDT; %s",
            estimate.expected_profit_before_reserve_usd, estimate.price_reserve_usd,
            estimate.expected_profit_after_reserve_usd, context,
        )
        logger.info(
            "Функционал не реализован: рабочее исполнение ордеров; заявка не отправлена; %s",
            context,
        )
        return "rejected", "рабочий режим не реализован"

    logger.info(
        "Виртуальная покупка зафиксирована по ask=%s, объём=%s; TP=%s из JSON sell.price, SL=%s; прибыль до резерва=%s USDT. "
        "Стоимость покупки зарезервирована из доступного баланса; ожидаем сеть %.1f сек., затем повторно проверим стакан продажи; %s",
        buy_book.ask, estimate.quantity,
        signal["sell"]["price"], calculate_stop_loss_price(buy_book.ask),
        estimate.expected_profit_before_reserve_usd,
        max(0.0, delay_seconds), context,
    )
    return "simulation_started", result
