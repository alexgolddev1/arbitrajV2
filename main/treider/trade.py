"""Тренировочные сделки с ожиданием перевода и проверкой цены перед расчётом."""

from __future__ import annotations

import logging
import json
import uuid
import requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from database import connect_db
from main.treider.config import (
    BUY_FEE_CURRENCY,
    DEMO_STRATEGY,
    MIN_NET_PROFIT_USDT,
    PRICE_MOVE_RESERVE_PERCENT,
    SETTLEMENT_MAX_WAIT_SECONDS,
    SETTLEMENT_RETRY_SECONDS,
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
    simulate_market_sell,
)
from main.treider.config import TRANSFER_FEE_AMOUNT
from main.treider.return_transfer import record_pending_return_transfer
from main.treider.market_rules import fetch_spot_market_rules, floor_to_step
from main.treider.trade_checks import (
    TradeEstimate, _base_currency, _estimate_trade, _prepare_training_trade,
    _select_trade_estimate,
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
                "cost_basis_sold_usd, cost_basis_remaining_usd, return_transfer_fees_accumulated_usd, "
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
            return_transfer_fees = Decimal(str(state.get("return_transfer_fees_accumulated_usd") or 0))
            realized_profit = (
                accumulated_proceeds - accumulated_sell_fees - accumulated_cost_sold
                - return_transfer_fees
            )
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
            sale_event_key = f"{trade_id}:sell:{sale_key}"
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), sale_event_key, "sell", location, trade["sell_market_pair"], _base_currency(trade["source_pair"]), filled, execution.average_price, proceeds, book_snapshot=snapshot, run_id=event_run_id)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:commission:sell:{sale_key}", "commission", location, trade["sell_market_pair"], "USDT", fee_amount=sell_fee, fee_asset="USDT", quote_amount=sell_fee, book_snapshot=snapshot, run_id=event_run_id)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:balance:sell:coin:{sale_key}", "balance_change", location, asset=_base_currency(trade["source_pair"]), quantity=-filled, run_id=event_run_id)
            _log_demo_event(cursor, trade_id, int(trade["signal_id"]), f"{trade_id}:balance:sell:usdt:{sale_key}", "balance_change", location, asset="USDT", quantity=proceeds - sell_fee, balance_before=balance_before, balance_after=balance_after, run_id=event_run_id)
            if per_exchange and location != buy_exchange:
                record_pending_return_transfer(
                    cursor, settlement_id=trade_id, signal_id=int(trade["signal_id"]),
                    run_id=str(event_run_id or DEMO_RUN_ID), sale_event_key=sale_event_key,
                    source_exchange=location, destination_exchange=buy_exchange,
                    amount_usdt=proceeds - sell_fee,
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
    try:
        buy_unit_count = Decimal(str(signal.get("buy", {}).get("units_per_market_unit", "1")))
        sell_unit_count = Decimal(str(signal.get("sell", {}).get("units_per_market_unit", "1")))
    except Exception:
        logger.warning("Торговая симуляция отклонена: неверный коэффициент единиц рынка; %s", context)
        return "rejected", "неверный коэффициент единиц рынка"
    source_base = _base_currency(signal["source_pair"])
    buy_base = _base_currency(str(signal.get("buy", {}).get("market_pair", "")))
    sell_base = _base_currency(str(signal.get("sell", {}).get("market_pair", "")))
    if (
        buy_unit_count != 1 or sell_unit_count != 1
        or buy_base != source_base or sell_base != source_base
    ):
        reason = (
            "тикер или размер лота биржи отличается от валюты скринера; деморасчёт пока не переводит "
            "шаги количества, комиссии и минимум вывода между единицами рынка"
        )
        logger.info("Торговая симуляция отклонена до покупки: %s; %s", reason, context)
        return "rejected", reason
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
