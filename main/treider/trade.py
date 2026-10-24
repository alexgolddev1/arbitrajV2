"""Тренировочные сделки с ожиданием перевода и проверкой цены перед расчётом."""

from __future__ import annotations

import logging
import json
import uuid
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from database import connect_db
from main.treider.config import (
    BUY_FEE_CURRENCY,
    DEMO_STRATEGY,
    TRADE_SIZE_CANDIDATES_USDT,
    MIN_NET_PROFIT_USDT,
    MAX_LOSS_BALANCE_PERCENT,
    MAX_LOSS_PROFIT_RATIO,
    PRICE_MOVE_RESERVE_PERCENT,
    SETTLEMENT_MAX_WAIT_SECONDS,
    SETTLEMENT_RETRY_SECONDS,
    SAME_ROUTE_REENTRY_COOLDOWN_SECONDS,
    SIGNAL_MAX_AGE_SECONDS,
    STOP_LOSS_PERCENT,
    TRAINING_MODE,
)
from main.treider.fees import TradeFeeRates, get_trade_fee_rates
from main.treider.market_data import (
    InsufficientLiquidity,
    SpotBookTicker,
    fetch_market_sell_execution,
    fetch_spot_order_book,
    fetch_spot_book,
    fetch_limit_sell_execution,
    simulate_market_sell,
)
from main.treider.config import TRANSFER_FEE_AMOUNT
from main.treider.market_rules import (
    fetch_spot_market_rules, floor_to_step, withdraw_step,
)

logger = logging.getLogger(__name__)
DEMO_RUN_ID = str(uuid.uuid4())


def set_demo_run_id(run_id: str | None) -> None:
    global DEMO_RUN_ID
    if run_id:
        DEMO_RUN_ID = str(run_id)


def _log_demo_event(
    cursor: Any, trade_id: int, signal_id: int, event_key: str, event_type: str,
    exchange: str, market_pair: str | None = None, asset: str | None = None,
    quantity: Decimal | None = None, price: Decimal | None = None,
    quote_amount: Decimal | None = None, fee_amount: Decimal | None = None,
    fee_asset: str | None = None, balance_before: Decimal | None = None,
    balance_after: Decimal | None = None, book_snapshot: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> None:
    """Append an idempotent demo ledger event; only explicit market fields enter snapshots."""
    cursor.execute(
        "INSERT INTO trader_demo_operation_log (event_key, run_id, settlement_id, signal_id, event_type, "
        "exchange_slug, market_pair, asset, quantity, price, quote_amount, fee_amount, fee_asset, "
        "balance_before, balance_after, book_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE event_key = VALUES(event_key)",
        (event_key, run_id or DEMO_RUN_ID, trade_id, signal_id, event_type, exchange.lower(), market_pair, asset,
         quantity, price, quote_amount, fee_amount, fee_asset,
         balance_before, balance_after,
         json.dumps(book_snapshot, separators=(",", ":")) if book_snapshot is not None else None),
    )


def _ticker_snapshot(book: SpotBookTicker) -> dict[str, Any]:
    timestamp = book.book_timestamp_utc or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "snapshot_type": "best_quote", "captured_at_utc": timestamp.astimezone(timezone.utc).isoformat(),
        "bids": [[str(book.bid), str(book.bid_size)]], "asks": [[str(book.ask), str(book.ask_size)]],
    }


def _record_replay_books(cursor: Any, signal_id: int, buy_book: Any, sell_book: Any) -> None:
    """Persist sanitized depth observations used while evaluating this signal."""
    for side, book in (("buy", buy_book), ("sell", sell_book)):
        snapshot = book.snapshot()
        cursor.execute(
            "INSERT INTO trader_replay_book_snapshots "
            "(run_id, signal_id, side, exchange_slug, market_pair, book_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (DEMO_RUN_ID, signal_id, side, book.exchange.lower(), book.market_pair,
             json.dumps(snapshot, separators=(",", ":"))),
        )


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


def _strategy_trigger(
    strategy: str, best_bid: Decimal, take_profit_price: Decimal,
    stop_loss_price: Decimal, stop_loss_triggered: bool = False,
) -> str | None:
    if strategy == "immediate_after_transfer":
        return "immediate_after_transfer"
    return "stop_loss" if stop_loss_triggered else _exit_trigger(best_bid, take_profit_price, stop_loss_price)


@dataclass(frozen=True)
class TradeEstimate:
    quantity: Decimal
    quantity_after_fees: Decimal
    transfer_dust_quantity: Decimal
    buy_average_price: Decimal
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
    buy_book_snapshot: dict[str, Any] | None = None
    sell_book_snapshot: dict[str, Any] | None = None


def _quote_currency(pair: str) -> str:
    for separator in ("/", "_", "-"):
        if separator in pair:
            return pair.strip().upper().split(separator, 1)[1]
    raise ValueError(f"Не удалось определить котируемую валюту в паре {pair!r}")


def _required_rule_decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"Неизвестное ограничение {name}") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"Некорректное ограничение {name}: {value!r}")
    return result


def _max_loss_budget(cursor: Any, available_balance: Decimal) -> tuple[Decimal, str]:
    """Limit a new loss to one quarter of the latest four winning trades."""
    cursor.execute(
        "SELECT realized_profit_usd FROM trader_training_settlements "
        "WHERE status = 'settled' AND position_status = 'closed' "
        "AND realized_profit_usd > 0 ORDER BY settled_at DESC, settlement_id DESC LIMIT 4"
    )
    profits = [Decimal(str(row["realized_profit_usd"])) for row in cursor.fetchall()]
    if profits:
        return sum(profits, Decimal(0)) * MAX_LOSS_PROFIT_RATIO, f"последние {len(profits)} прибыльных сделок"
    return available_balance * MAX_LOSS_BALANCE_PERCENT / Decimal(100), "процент доступного баланса"


def _risk_limited_stop_price(
    estimate: TradeEstimate,
    buy_price: Decimal,
    sell_fee_rate: Decimal,
    loss_budget: Decimal,
) -> Decimal:
    """Return a stop limit that prevents a completed trade exceeding its loss budget."""
    sellable_quantity = estimate.quantity_after_fees
    net_sell_factor = Decimal(1) - sell_fee_rate
    if sellable_quantity <= 0 or net_sell_factor <= 0:
        raise ValueError("Нельзя рассчитать ограничение убытка без объёма продажи и ставки комиссии")
    buy_debit = estimate.buy_cost_usd + estimate.buy_fee_usd + estimate.transfer_fee_usd
    risk_floor = (buy_debit - loss_budget) / (sellable_quantity * net_sell_factor)
    return max(calculate_stop_loss_price(buy_price), risk_floor, Decimal("0.000000000000000001"))


def _estimate_trade(
    buy: SpotBookTicker,
    sell: SpotBookTicker,
    buy_fees: TradeFeeRates,
    sell_fees: TradeFeeRates,
    balance_usd: Decimal,
    transfer: dict[str, Any] | None = None,
    *, buy_order_book: Any = None, sell_order_book: Any = None,
    market_rules: tuple[Any, Any] | None = None,
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
    transfer_fee_raw = (transfer or {}).get("withdraw_fee")
    if transfer_fee_raw is None:
        transfer_fee_raw = TRANSFER_FEE_AMOUNT
    transfer_fee = Decimal(str(transfer_fee_raw if transfer_fee_raw is not None else 0))
    transfer_fee_currency = str((transfer or {}).get("withdraw_fee_currency") or "").upper()
    if transfer_fee > 0 and not transfer_fee_currency:
        raise ValueError("Недостаточно данных: не указана валюта комиссии перевода")
    quote_transfer_fee = transfer_fee if transfer_fee_currency == quote else Decimal(0)
    affordable_balance = max(Decimal(0), balance_usd - quote_transfer_fee)
    buy_rules, sell_rules = market_rules or (
        fetch_spot_market_rules(buy.exchange, buy.market_pair, market_order=False),
        fetch_spot_market_rules(sell.exchange, sell.market_pair, market_order=True),
    )
    asks = buy_order_book.asks if buy_order_book is not None else [[buy.ask, buy.ask_size]]
    remaining_quote = affordable_balance
    affordable_quantity = Decimal(0)
    for level in asks:
        price, size = Decimal(str(level[0])), Decimal(str(level[1]))
        spend = min(remaining_quote, price * size * buy_factor)
        affordable_quantity += spend / (price * buy_factor)
        remaining_quote -= spend
        if remaining_quote <= 0:
            break
    quantity = floor_to_step(affordable_quantity, buy_rules.quantity_step)
    if quantity <= 0:
        raise ValueError("Недостаточный баланс или нулевой доступный объём стакана")
    buy_cost = Decimal(0)
    buy_remaining = quantity
    for level in asks:
        price, size = Decimal(str(level[0])), Decimal(str(level[1]))
        filled = min(buy_remaining, size)
        buy_cost += filled * price
        buy_remaining -= filled
        if buy_remaining <= 0:
            break
    if buy_remaining > 0:
        raise ValueError("Недостаточно ожидаемой глубины asks для покупки")
    buy_average_price = buy_cost / quantity
    if quantity < buy_rules.min_quantity or buy_cost < (buy_rules.min_notional or Decimal(0)):
        raise ValueError("После шага рынка покупка ниже min quantity/min notional")

    buy_fee_currency = BUY_FEE_CURRENCY[buy.exchange]
    buy_fee_base = quantity * buy_rate if buy_fee_currency == "base" else Decimal(0)
    buy_fee = buy_cost * buy_rate if buy_fee_currency == "quote" else Decimal(0)
    buy_net_quantity = quantity - buy_fee_base
    if buy_net_quantity <= 0:
        raise ValueError("Комиссия покупки оставляет нулевое количество актива")
    if transfer_fee_currency and transfer_fee_currency not in {quote, _base_currency(buy.market_pair)}:
        raise ValueError(f"Комиссия перевода в {transfer_fee_currency} не поддерживается для USDT-симуляции")
    if transfer_fee_currency == _base_currency(buy.market_pair):
        buy_net_quantity -= transfer_fee
    if buy_net_quantity <= 0:
        raise ValueError("Комиссия перевода съедает весь купленный актив")
    transfer_dust = Decimal(0)
    if buy.exchange.lower() != sell.exchange.lower():
        if not transfer:
            raise ValueError("Неизвестные ограничения вывода: нет выбранного маршрута перевода")
        source_network = transfer.get("source_network") or transfer
        step = withdraw_step(source_network)
        min_withdraw_value = transfer.get("min_withdraw")
        if min_withdraw_value in (None, ""):
            min_withdraw_value = (transfer.get("source_network") or {}).get("min_withdraw")
        if min_withdraw_value in (None, ""):
            raise ValueError("Неизвестно минимальное количество вывода для маршрута")
        withdrawal_quantity = floor_to_step(buy_net_quantity, step)
        if withdrawal_quantity < _required_rule_decimal(min_withdraw_value, "min_withdraw"):
            raise ValueError("Остаток ниже минимума вывода; вход не открывается")
        quantity_after_fees = floor_to_step(withdrawal_quantity, sell_rules.quantity_step)
        transfer_dust = buy_net_quantity - quantity_after_fees
    else:
        quantity_after_fees = floor_to_step(buy_net_quantity, sell_rules.quantity_step)
        transfer_dust = buy_net_quantity - quantity_after_fees
    if quantity_after_fees <= 0 or quantity_after_fees < sell_rules.min_quantity:
        raise ValueError("После округления количества продажи ниже min quantity")
    execution = (
        simulate_market_sell(sell.exchange, sell.market_pair, quantity_after_fees, sell_order_book.bids)
        if sell_order_book is not None else
        fetch_market_sell_execution(sell.exchange, sell.market_pair, quantity_after_fees)
    )
    if execution.remaining_quantity > 0:
        raise ValueError(
            f"Недостаточно ожидаемой глубины bids: исполнится {execution.filled_quantity} из {quantity_after_fees}"
        )
    if sell_rules.min_notional is not None and execution.proceeds < sell_rules.min_notional:
        raise ValueError("После округления количества ожидаемая продажа ниже min notional")
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
        transfer_dust_quantity=transfer_dust,
        buy_average_price=buy_average_price,
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
        buy_book_snapshot=buy_order_book.snapshot() if buy_order_book is not None else None,
        sell_book_snapshot=sell_order_book.snapshot() if sell_order_book is not None else getattr(execution, "book_snapshot", None),
    )


def _base_currency(pair: str) -> str:
    for separator in ("/", "_", "-"):
        if separator in pair:
            return pair.strip().upper().split(separator, 1)[0]
    raise ValueError(f"Не удалось определить базовую валюту в паре {pair!r}")


def _read_balance(exchange: str | None = None) -> Decimal:
    connection = connect_db()
    try:
        with connection.cursor(dictionary=True) as cursor:
            if exchange is None:
                cursor.execute("SELECT balance_usd FROM trader_account WHERE account_id = 1")
            else:
                cursor.execute("SELECT balance FROM trader_balances WHERE exchange_slug = %s AND asset = 'USDT'", (exchange.lower(),))
            row = cursor.fetchone()
        if row is None and exchange is not None:
            return Decimal(0)
        if row is None:
            raise RuntimeError("В БД не найден счёт трейдера")
        return Decimal(str(row.get("balance", row.get("balance_usd"))))
    finally:
        connection.close()


def _select_trade_estimate(
    cursor: Any, signal_id: int, buy: SpotBookTicker, sell: SpotBookTicker,
    buy_fees: TradeFeeRates, sell_fees: TradeFeeRates,
    available_balance: Decimal, transfer: dict[str, Any] | None,
    take_profit_price: Decimal,
) -> tuple[TradeEstimate | None, Decimal | None, Decimal, str, str | None]:
    """Evaluate configured quote sizes against one shared depth snapshot per market."""
    buy_book = fetch_spot_order_book(buy.exchange, buy.market_pair)
    sell_book = (
        buy_book if (buy.exchange.lower(), buy.market_pair.upper()) == (sell.exchange.lower(), sell.market_pair.upper())
        else fetch_spot_order_book(sell.exchange, sell.market_pair)
    )
    _record_replay_books(cursor, signal_id, buy_book, sell_book)
    rules = (
        fetch_spot_market_rules(buy.exchange, buy.market_pair, market_order=False),
        fetch_spot_market_rules(sell.exchange, sell.market_pair, market_order=True),
    )
    loss_budget, loss_source = _max_loss_budget(cursor, available_balance)
    estimates: list[tuple[TradeEstimate, Decimal]] = []
    errors: list[str] = []
    for candidate_size in TRADE_SIZE_CANDIDATES_USDT:
        if candidate_size > available_balance:
            continue
        try:
            estimate = _estimate_trade(
                buy, sell, buy_fees, sell_fees, candidate_size, transfer,
                buy_order_book=buy_book, sell_order_book=sell_book, market_rules=rules,
            )
            stop_price = _risk_limited_stop_price(
                estimate, estimate.buy_average_price, sell_fees.taker_for("sell"), loss_budget,
            )
            estimates.append((estimate, stop_price))
        except (ValueError, ArithmeticError) as error:
            errors.append(str(error))
    if not estimates:
        reason = errors[-1] if errors else "нет настроенного объёма, не превышающего доступный баланс"
        return None, None, loss_budget, loss_source, reason
    risk_valid = [(estimate, stop) for estimate, stop in estimates if stop < take_profit_price]
    candidates = risk_valid or estimates
    profit_valid = [item for item in candidates if item[0].expected_profit_after_reserve_usd >= MIN_NET_PROFIT_USDT]
    selected = max(profit_valid or candidates, key=lambda item: item[0].expected_profit_after_reserve_usd)
    return selected[0], selected[1], loss_budget, loss_source, None


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

            if SAME_ROUTE_REENTRY_COOLDOWN_SECONDS > 0:
                cursor.execute(
                    "SELECT settled_at FROM trader_training_settlements "
                    "WHERE source_pair = %s AND status = 'settled' AND position_status = 'closed' "
                    "AND settled_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND "
                    "AND ((LOWER(buy_exchange) = LOWER(%s) AND LOWER(sell_exchange) = LOWER(%s)) "
                    "OR (LOWER(buy_exchange) = LOWER(%s) AND LOWER(sell_exchange) = LOWER(%s))) "
                    "ORDER BY settled_at DESC LIMIT 1",
                    (signal["source_pair"], SAME_ROUTE_REENTRY_COOLDOWN_SECONDS,
                     buy.exchange, sell.exchange, sell.exchange, buy.exchange),
                )
                recent = cursor.fetchone()
                if recent is not None:
                    connection.commit()
                    settled_at = recent["settled_at"]
                    if settled_at.tzinfo is None:
                        settled_at = settled_at.replace(tzinfo=timezone.utc)
                    elapsed = max(0.0, (datetime.now(timezone.utc) - settled_at).total_seconds())
                    remaining = max(0.0, SAME_ROUTE_REENTRY_COOLDOWN_SECONDS - elapsed)
                    return None, (
                        f"повторный вход по {signal['source_pair']} на биржах "
                        f"{buy.exchange} и {sell.exchange} запрещён таймером повторного входа; "
                        f"осталось {remaining:.0f} сек."
                    )

            cursor.execute(
                "SELECT balance FROM trader_balances WHERE exchange_slug = %s AND asset = 'USDT' FOR UPDATE",
                (buy.exchange.lower(),),
            )
            exchange_balance = cursor.fetchone()
            balance = Decimal(str(exchange_balance["balance"])) if exchange_balance else Decimal(0)
            available = balance
            take_profit_price = Decimal(str(signal["sell"]["price"]))
            estimate, stop_loss_price, loss_budget, loss_budget_source, sizing_error = _select_trade_estimate(
                cursor, signal_id, buy, sell, buy_fees, sell_fees, available, transfer, take_profit_price,
            )
            if estimate is None:
                connection.commit()
                return None, f"ни один настроенный объём не прошёл ограничения: {sizing_error}"
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
            if stop_loss_price >= take_profit_price:
                connection.commit()
                return estimate, (
                    f"стоп с ограничением риска ({stop_loss_price}) не ниже TP ({take_profit_price}); "
                    f"допустимый убыток {loss_budget} USDT ({loss_budget_source})"
                )
            logger.info(
                "Ограничение риска сделки: максимум убытка=%s USDT (%s), защитная цена продажи=%s; %s, сигнал=%s",
                loss_budget, loss_budget_source, stop_loss_price,
                signal["source_pair"], signal_id,
            )
            assumptions = dict(transfer or {})
            assumptions["buy_fee_source"] = buy_fees.source
            assumptions["sell_fee_source"] = sell_fees.source
            assumptions["buy_fee_currency_model"] = BUY_FEE_CURRENCY[buy.exchange]
            assumptions["purchase_model"] = "marketable-limit-at-best-ask; capped-by-visible-best-ask-size"
            assumptions["demo_strategy"] = DEMO_STRATEGY
            assumptions["demo_run_id"] = DEMO_RUN_ID
            assumptions["take_profit_source"] = "signal.sell.price"
            assumptions["stop_loss_percent"] = str(STOP_LOSS_PERCENT)
            assumptions["max_loss_usd"] = str(loss_budget)
            assumptions["max_loss_source"] = loss_budget_source
            assumptions["max_loss_profit_ratio"] = str(MAX_LOSS_PROFIT_RATIO)
            cursor.execute(
                "INSERT INTO trader_training_settlements ("
                "signal_id, source_pair, buy_exchange, buy_market_pair, sell_exchange, sell_market_pair, "
                "quantity, buy_price, expected_sell_price, stop_loss_price, buy_taker_fee_rate, expected_sell_taker_fee_rate, "
                "buy_cost_usd, buy_fee_usd, expected_sell_fee_usd, expected_profit_usd, "
                "balance_before_usd, settle_after_utc, status, route_status, route_assumptions, network_name, "
                "transfer_seconds, buy_fee_source, sell_fee_source, buy_fee_currency, buy_fee_base_quantity, "
                "transfer_fee_amount, transfer_fee_currency, transfer_fee_usd, bought_quantity, quantity_after_fees, transfer_dust_quantity, remaining_quantity, "
                "expected_sell_proceeds_usd, expected_profit_before_reserve_usd, price_reserve_usd, "
                "expected_profit_after_reserve_usd, buy_debit_usd, purchase_model, position_status, asset_exchange, "
                "planned_sell_at, retry_started_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'marketable-limit-at-best-ask', 'pending_transfer', %s, %s, NULL)",
                (
                    signal_id, signal["source_pair"], buy.exchange, buy.market_pair,
                    sell.exchange, sell.market_pair, estimate.quantity, estimate.buy_average_price, take_profit_price, stop_loss_price,
                    buy_fees.taker_for("buy"), sell_fees.taker_for("sell"), estimate.buy_cost_usd,
                    estimate.buy_fee_usd, estimate.expected_sell_fee_usd,
                    estimate.expected_profit_before_reserve_usd, balance, settle_at,
                    route_status, json.dumps(assumptions, ensure_ascii=False, default=str),
                    (transfer or {}).get("network"), transfer_seconds,
                    buy_fees.source, sell_fees.source,
                    BUY_FEE_CURRENCY[buy.exchange], estimate.buy_fee_base_quantity,
                    estimate.transfer_fee_amount, estimate.transfer_fee_currency, estimate.transfer_fee_usd,
                    estimate.quantity, estimate.quantity_after_fees, estimate.transfer_dust_quantity, estimate.quantity_after_fees,
                    estimate.expected_sell_proceeds_usd, estimate.expected_profit_before_reserve_usd,
                    estimate.price_reserve_usd, estimate.expected_profit_after_reserve_usd,
                    buy_debit, buy.exchange.lower(), settle_at,
                ),
            )
            settlement_id = int(cursor.lastrowid or 0)
            if settlement_id <= 0:
                cursor.execute("SELECT settlement_id FROM trader_training_settlements WHERE signal_id = %s", (signal_id,))
                settlement_id = int(cursor.fetchone()["settlement_id"])
            coin = _base_currency(signal["source_pair"])
            cursor.execute(
                "SELECT balance FROM trader_balances WHERE exchange_slug = %s AND asset = %s FOR UPDATE",
                (buy.exchange.lower(), coin),
            )
            prior_coin_row = cursor.fetchone()
            prior_coin_balance = Decimal(str(prior_coin_row["balance"])) if prior_coin_row else Decimal(0)
            cursor.execute(
                "UPDATE trader_balances SET balance = balance - %s "
                "WHERE exchange_slug = %s AND asset = 'USDT' AND balance >= %s",
                (buy_debit, buy.exchange.lower(), buy_debit),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Недостаточно доступного демобаланса для атомарного резервирования покупки")
            cursor.execute(
                "INSERT INTO trader_balances (exchange_slug, asset, balance) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE balance = balance + VALUES(balance)",
                (buy.exchange.lower(), _base_currency(signal["source_pair"]), estimate.quantity_after_fees + estimate.transfer_dust_quantity),
            )
            coin_credit = estimate.quantity_after_fees + estimate.transfer_dust_quantity
            sizing_book_snapshot = {
                "buy": estimate.buy_book_snapshot or _ticker_snapshot(buy),
                "sell": estimate.sell_book_snapshot,
            }
            _log_demo_event(cursor, settlement_id, signal_id, f"{settlement_id}:buy", "buy",
                buy.exchange, buy.market_pair, coin, estimate.quantity, estimate.buy_average_price, estimate.buy_cost_usd,
                book_snapshot=sizing_book_snapshot)
            _log_demo_event(cursor, settlement_id, signal_id, f"{settlement_id}:commission:buy", "commission",
                buy.exchange, buy.market_pair,
                "USDT" if BUY_FEE_CURRENCY[buy.exchange] == "quote" else coin,
                fee_amount=estimate.buy_fee_usd if BUY_FEE_CURRENCY[buy.exchange] == "quote" else estimate.buy_fee_base_quantity,
                fee_asset="USDT" if BUY_FEE_CURRENCY[buy.exchange] == "quote" else coin,
                quote_amount=estimate.buy_fee_usd,
                book_snapshot=sizing_book_snapshot)
            _log_demo_event(cursor, settlement_id, signal_id, f"{settlement_id}:balance:buy:usdt", "balance_change",
                buy.exchange, asset="USDT", quantity=-buy_debit, balance_before=balance,
                balance_after=balance - buy_debit, book_snapshot=sizing_book_snapshot)
            _log_demo_event(cursor, settlement_id, signal_id, f"{settlement_id}:balance:buy:{coin}", "balance_change",
                buy.exchange, asset=coin, quantity=coin_credit, balance_before=prior_coin_balance,
                balance_after=prior_coin_balance + coin_credit, book_snapshot=sizing_book_snapshot)
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
                "(status IN ('pending','partial','open') AND settle_after_utc <= UTC_TIMESTAMP()) OR "
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
                "WHERE settlement_id = %s AND status = %s",
                (token, row["settlement_id"], row["status"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Задача продажи изменилась до захвата")
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
                "status = IF(position_status = 'stop_loss', 'partial', IF(position_status <> 'legacy' AND planned_sell_at < "
                "UTC_TIMESTAMP() - INTERVAL %s SECOND, 'open', 'pending')), "
                "position_status = IF(position_status = 'stop_loss', 'stop_loss', IF(position_status <> 'legacy' AND planned_sell_at < "
                "UTC_TIMESTAMP() - INTERVAL %s SECOND, 'open', position_status)), "
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


def _persist_sale_dust(trade_id: int, quantity: Decimal, claim_token: str | None) -> None:
    """Retain a non-tradable sell remainder as an explicitly tracked coin balance."""
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE trader_training_settlements SET sale_dust_quantity = sale_dust_quantity + %s, "
                "remaining_quantity = 0, status = 'settled', position_status = 'closed', "
                "settled_at = UTC_TIMESTAMP(), claimed_at = NULL, claim_token = NULL "
                "WHERE settlement_id = %s AND status = 'settling' AND claim_token = %s",
                (quantity, trade_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Sale dust update lost settlement lease")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _settle_training_trade(trade: dict[str, Any]) -> None:
    trade_id = int(trade["settlement_id"])
    # Rows from before per-exchange accounting have no reliable coin location.
    if "asset_exchange" in trade and not trade.get("asset_exchange"):
        _retry_settlement(trade_id, "Старый остаток не распределён по биржам автоматически", trade.get("claim_token"))
        return
    asset_exchange = str(trade.get("asset_exchange") or trade["sell_exchange"]).lower()
    buy_exchange = str(trade["buy_exchange"]).lower()
    sell_exchange = str(trade["sell_exchange"]).lower()
    assumptions = trade.get("route_assumptions") or {}
    if isinstance(assumptions, str):
        try:
            assumptions = json.loads(assumptions)
        except (TypeError, ValueError):
            assumptions = {}
    strategy = assumptions.get("demo_strategy", "tp_sl") if isinstance(assumptions, dict) else "tp_sl"
    event_run_id = assumptions.get("demo_run_id") if isinstance(assumptions, dict) else None
    if asset_exchange != sell_exchange:
        connection = connect_db()
        try:
            connection.start_transaction()
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(
                    "SELECT status, claim_token, remaining_quantity FROM trader_training_settlements "
                    "WHERE settlement_id = %s FOR UPDATE", (trade_id,),
                )
                transfer_state = cursor.fetchone()
                if (not transfer_state or transfer_state["status"] != "settling"
                        or transfer_state["claim_token"] != trade.get("claim_token")):
                    raise RuntimeError("Потеряно владение задачей перевода")
                qty = Decimal(str(transfer_state["remaining_quantity"]))
                asset = _base_currency(trade["source_pair"])
                cursor.execute(
                    "UPDATE trader_balances SET balance = balance - %s WHERE exchange_slug = %s "
                    "AND asset = %s AND balance >= %s", (qty, asset_exchange, asset, qty),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Не найден демобаланс монет на бирже отправления")
                cursor.execute(
                    "INSERT INTO trader_balances (exchange_slug, asset, balance) VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE balance = balance + VALUES(balance)", (sell_exchange, asset, qty),
                )
                _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:transfer_sent", "transfer_sent", asset_exchange, asset=asset, quantity=-qty, run_id=event_run_id)
                _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:transfer_received", "transfer_received", sell_exchange, asset=asset, quantity=qty, run_id=event_run_id)
                _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:balance:transfer:sent", "balance_change", asset_exchange, asset=asset, quantity=-qty, run_id=event_run_id)
                _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:balance:transfer:received", "balance_change", sell_exchange, asset=asset, quantity=qty, run_id=event_run_id)
                transfer_fee = Decimal(str(trade.get("transfer_fee_amount") or 0))
                if transfer_fee > 0:
                    transfer_fee_asset = str(trade.get("transfer_fee_currency") or asset).upper()
                    _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:commission:transfer", "commission", asset_exchange, asset=transfer_fee_asset, fee_amount=transfer_fee, fee_asset=transfer_fee_asset, quote_amount=Decimal(str(trade.get("transfer_fee_usd") or 0)), run_id=event_run_id)
                cursor.execute(
                    "INSERT INTO trader_asset_transfers (settlement_id, asset, amount, source_exchange, destination_exchange) "
                    "VALUES (%s, %s, %s, %s, %s)", (trade_id, asset, qty, asset_exchange, sell_exchange),
                )
                cursor.execute(
                    "UPDATE trader_training_settlements SET asset_exchange = %s, transfer_completed_at = UTC_TIMESTAMP() "
                    "WHERE settlement_id = %s AND status = 'settling' AND claim_token = %s",
                    (sell_exchange, trade_id, trade.get("claim_token")),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Не удалось зафиксировать перевод монет")
            connection.commit()
            trade["asset_exchange"] = sell_exchange
        except Exception as error:
            connection.rollback()
            _retry_settlement(trade_id, str(error), trade.get("claim_token"))
            return
        finally:
            connection.close()
    context = (
        f"сигнал={trade['signal_id']}, пара={trade['source_pair']}, "
        f"покупка={trade['buy_exchange']} {trade['buy_market_pair']} по {trade['buy_price']}, "
        f"продажа={trade['sell_exchange']} {trade['sell_market_pair']}"
    )
    try:
        remaining_quantity = trade.get("remaining_quantity")
        quantity = Decimal(str(
            trade["quantity"] if remaining_quantity is None else remaining_quantity
        ))
        sell_fees = get_trade_fee_rates(trade["sell_exchange"], trade["sell_market_pair"])
        current_sell_book = fetch_spot_book(trade["sell_exchange"], trade["sell_market_pair"])
        take_profit_price = Decimal(str(trade["expected_sell_price"]))
        stop_loss_price = Decimal(str(
            trade.get("stop_loss_price")
            or calculate_stop_loss_price(Decimal(str(trade["buy_price"])))
        ))
        stop_loss_triggered = trade.get("position_status") == "stop_loss"
        trigger = _strategy_trigger(
            strategy, current_sell_book.bid, take_profit_price, stop_loss_price, stop_loss_triggered,
        )
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
        if trigger == "stop_loss" and not stop_loss_triggered:
            connection = connect_db()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE trader_training_settlements SET position_status = 'stop_loss' "
                        "WHERE settlement_id = %s AND status = 'settling' AND claim_token = %s",
                        (trade_id, trade.get("claim_token")),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Стоп-лосс больше не принадлежит текущей задаче")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        sell_rules = fetch_spot_market_rules(trade["sell_exchange"], trade["sell_market_pair"], market_order=True)
        executable_quantity = floor_to_step(quantity, sell_rules.quantity_step)
        if executable_quantity <= 0 or executable_quantity < sell_rules.min_quantity:
            _persist_sale_dust(trade_id, quantity, trade.get("claim_token"))
            return
        if current_sell_book.bid <= 0 or current_sell_book.bid_size <= 0:
            raise InsufficientLiquidity("No executable bid liquidity; keep position for retry")
        # Exchange filters apply to the submitted order size, not to how much
        # the current book happens to fill. A thin book is a retryable partial.
        if (sell_rules.min_notional is not None
                and executable_quantity * current_sell_book.bid < sell_rules.min_notional):
            _persist_sale_dust(trade_id, quantity, trade.get("claim_token"))
            return
        execution = (
            fetch_limit_sell_execution(
                trade["sell_exchange"], trade["sell_market_pair"], executable_quantity, take_profit_price
            )
            if trigger == "take_profit"
            else fetch_market_sell_execution(
                trade["sell_exchange"], trade["sell_market_pair"], executable_quantity,
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
                "sell_proceeds_accumulated_usd, sell_fees_accumulated_usd, buy_debit_usd, bought_quantity, "
                "cost_basis_sold_usd, cost_basis_remaining_usd, "
                "settlement_attempts, planned_sell_at, asset_exchange FROM trader_training_settlements "
                "WHERE settlement_id = %s FOR UPDATE",
                (trade_id,),
            )
            state = cursor.fetchone()
            if (account is None or state is None or state["status"] != "settling"
                    or state["claim_token"] != trade.get("claim_token")):
                raise RuntimeError("Тренировочный счёт или состояние отложенной сделки изменились")
            per_exchange = "asset_exchange" in trade
            location = str(state.get("asset_exchange") or asset_exchange).lower()
            if per_exchange:
                cursor.execute(
                    "SELECT balance FROM trader_balances WHERE exchange_slug = %s AND asset = 'USDT' FOR UPDATE",
                    (location,),
                )
                cash_row = cursor.fetchone()
                balance_before = Decimal(str(cash_row["balance"])) if cash_row else Decimal(0)
            else:
                balance_before = Decimal(str(account["balance_usd"]))
            old_remaining = Decimal(str(state["remaining_quantity"]))
            filled = min(old_remaining, execution.filled_quantity)
            remaining = old_remaining - filled
            accumulated_proceeds = Decimal(str(state["sell_proceeds_accumulated_usd"])) + proceeds
            accumulated_sell_fees = Decimal(str(state["sell_fees_accumulated_usd"])) + sell_fee
            buy_debit = Decimal(str(state["buy_debit_usd"]))
            bought_quantity = Decimal(str(state.get("bought_quantity") or trade.get("quantity") or 0))
            sold_before = Decimal(str(state.get("sold_quantity") or 0))
            remaining_cost = Decimal(str(state.get("cost_basis_remaining_usd") or 0))
            if remaining_cost <= 0 and bought_quantity > 0:
                remaining_cost = buy_debit * old_remaining / (old_remaining + sold_before)
            cost_sold = remaining_cost if remaining <= 0 else (
                remaining_cost * filled / old_remaining if old_remaining > 0 else Decimal(0)
            )
            remaining_cost -= cost_sold
            accumulated_cost_sold = Decimal(str(state.get("cost_basis_sold_usd") or 0)) + cost_sold
            realized_profit = accumulated_proceeds - accumulated_sell_fees - accumulated_cost_sold
            balance_after = (balance_before + proceeds - sell_fee).quantize(Decimal("0.00000001"))
            if per_exchange:
                asset = _base_currency(trade["source_pair"])
                cursor.execute(
                    "UPDATE trader_balances SET balance = balance - %s WHERE exchange_slug = %s "
                    "AND asset = %s AND balance >= %s", (filled, location, asset, filled),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Недостаточно монет на демобалансе биржи продажи")
                cursor.execute(
                    "INSERT INTO trader_balances (exchange_slug, asset, balance) VALUES (%s, 'USDT', %s) "
                    "ON DUPLICATE KEY UPDATE balance = balance + VALUES(balance)",
                    (location, proceeds - sell_fee),
                )
            else:
                cursor.execute("UPDATE trader_account SET balance_usd = %s WHERE account_id = 1", (balance_after,))
            sold_total = Decimal(str(state.get("sold_quantity") or 0)) + filled
            sale_key = format(sold_total, "f")
            snapshot = getattr(execution, "book_snapshot", None)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:sell:{sale_key}", "sell", location, trade["sell_market_pair"], _base_currency(trade["source_pair"]), filled, execution.average_price, proceeds, book_snapshot=snapshot, run_id=event_run_id)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:commission:sell:{sale_key}", "commission", location, trade["sell_market_pair"], "USDT", fee_amount=sell_fee, fee_asset="USDT", quote_amount=sell_fee, book_snapshot=snapshot, run_id=event_run_id)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:balance:sell:coin:{sale_key}", "balance_change", location, asset=_base_currency(trade["source_pair"]), quantity=-filled, run_id=event_run_id)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:balance:sell:usdt:{sale_key}", "balance_change", location, asset="USDT", quantity=proceeds - sell_fee, balance_before=balance_before, balance_after=balance_after, run_id=event_run_id)
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
            next_status = (
                "settled" if remaining <= 0 else "partial"
                if trigger == "stop_loss" or not exceeded_wait else "open"
            )
            next_position_status = (
                "closed" if remaining <= 0 else "stop_loss" if trigger == "stop_loss"
                else "open" if exceeded_wait else "partial"
            )
            next_retry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=SETTLEMENT_RETRY_SECONDS
            )
            cursor.execute(
                "UPDATE trader_training_settlements SET status = %s, position_status = %s, sell_fee_source = %s, "
                "claimed_at = NULL, claim_token = NULL, final_sell_price = %s, actual_sell_taker_fee_rate = %s, "
                "realized_sell_proceeds_usd = %s, sell_fee_usd = %s, realized_profit_usd = %s, "
                "settlement_balance_before_usd = %s, balance_after_usd = %s, settled_at = %s, "
                "sell_proceeds_accumulated_usd = %s, sell_fees_accumulated_usd = %s, "
                "cost_basis_sold_usd = %s, cost_basis_remaining_usd = %s, "
                "sold_quantity = sold_quantity + %s, remaining_quantity = %s, actual_sell_at = UTC_TIMESTAMP(), "
                "last_valuation_price = %s, valuation_usd = %s, valued_at = UTC_TIMESTAMP(), "
                "settle_after_utc = %s, last_error = %s "
                "WHERE settlement_id = %s AND status = 'settling' AND claim_token = %s",
                (next_status, next_position_status, sell_fees.source, execution.average_price, sell_rate,
                 accumulated_proceeds, accumulated_sell_fees, realized_profit, balance_before,
                 balance_after, datetime.now(timezone.utc).replace(tzinfo=None) if next_status == "settled" else None,
                 accumulated_proceeds, accumulated_sell_fees, accumulated_cost_sold, remaining_cost, filled, remaining,
                 execution.best_bid, remaining * execution.best_bid if remaining > 0 else Decimal(0),
                 next_retry if remaining > 0 else trade["settle_after_utc"],
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
                buy_book, sell_book, buy_fees, sell_fees, _read_balance(buy_book.exchange), transfer
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
