"""Replay demo ledger executions against sanitized, time-ordered book snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

MAX_EQUITY_BOOK_AGE_SECONDS = 30


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def select_snapshot(event: dict[str, Any], snapshots: list[dict[str, Any]], side: str,
                    delay_seconds: Decimal) -> dict[str, Any] | None:
    """Zero delay uses the event's book; delays require a later recorded book."""
    if delay_seconds <= 0:
        embedded = _json(event.get("book_snapshot"))
        if isinstance(embedded, dict) and side in embedded:
            embedded = embedded[side]
        return embedded if isinstance(embedded, dict) and embedded.get("snapshot_type") else None
    target = _dt(event["event_at_utc"]) + timedelta(seconds=float(delay_seconds))
    candidates = []
    for row in snapshots:
        if (str(row.get("exchange_slug", "")).lower() != str(event.get("exchange_slug", "")).lower()
                or str(row.get("market_pair", "")).upper() != str(event.get("market_pair", "")).upper()):
            continue
        observed = _dt(row["observed_at_utc"])
        if observed >= target:
            candidates.append((observed, _json(row.get("book_snapshot"))))
    if not candidates:
        return None
    observed, selected = min(candidates, key=lambda item: item[0])
    if not isinstance(selected, dict):
        return None
    return {**selected, "_replay_observed_at_utc": observed.isoformat()}


def _fill(levels: Any, quantity: Decimal, depth_factor: Decimal) -> tuple[Decimal, Decimal]:
    remaining, total, filled = quantity, Decimal(0), Decimal(0)
    for level in levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price, size = Decimal(str(level[0])), Decimal(str(level[1])) * depth_factor
        if price <= 0 or size <= 0:
            continue
        amount = min(size, remaining)
        total += amount * price
        filled += amount
        remaining -= amount
        if remaining <= 0:
            break
    return filled, total


def _pair_for_asset(events: list[dict[str, Any]], exchange: str, asset: str) -> str | None:
    event = next((row for row in events if row.get("event_type") in {"buy", "sell"}
                  and str(row.get("exchange_slug", "")).lower() == exchange
                  and str(row.get("asset", "")).upper() == asset), None)
    return str(event["market_pair"]).upper() if event else None


def _equity(balances: dict[tuple[str, str], Decimal],
            books: dict[tuple[str, str], tuple[dict[str, Any], datetime]],
            exit_fees: dict[tuple[str, str], tuple[Decimal, str]], events: list[dict[str, Any]],
            at: datetime, depth_factor: Decimal, gaps: list[str]) -> Decimal:
    total = Decimal(0)
    for (exchange, asset), amount in balances.items():
        if asset in {"USDT", "USDC", "USD"}:
            total += amount
        elif amount:
            pair = _pair_for_asset(events, exchange, asset)
            market = (exchange, pair) if pair else None
            book_row = books.get(market) if market else None
            if not market or not book_row:
                gaps.append(f"no bid book for {asset} balance on {exchange}")
                continue
            snapshot, observed_at = book_row
            if (at - observed_at).total_seconds() < 0 or (at - observed_at).total_seconds() > MAX_EQUITY_BOOK_AGE_SECONDS:
                gaps.append(f"stale bid book for {asset} balance on {exchange}")
                continue
            bids = snapshot.get("bids") or []
            if not bids:
                gaps.append(f"no bids for {asset} balance on {exchange}")
                continue
            fee_rule = exit_fees.get(market)
            if fee_rule is None:
                gaps.append(f"unknown exit fee for {asset} on {exchange}")
                fee_rate, fee_asset = Decimal(0), ""
            else:
                fee_rate, fee_asset = fee_rule
            if fee_asset == asset:
                sell_quantity = amount / (Decimal(1) + fee_rate)
            else:
                sell_quantity = amount
            filled, proceeds = _fill(bids, sell_quantity, depth_factor)
            if filled < sell_quantity:
                gaps.append(f"insufficient bid depth for {asset} balance on {exchange}: {filled}/{sell_quantity}")
            if fee_asset in {"USDT", "USDC", "USD"}:
                proceeds *= Decimal(1) - fee_rate
            total += proceeds
    return total


def _historical_exit_fee(event: dict[str, Any], fee_event: dict[str, Any] | None) -> tuple[Decimal, str] | None:
    if not fee_event:
        return None
    amount = Decimal(str(fee_event.get("fee_amount") or 0))
    fee_asset = str(fee_event.get("fee_asset") or "USDT").upper()
    denominator = (Decimal(str(event.get("quantity") or 0)) if fee_asset == str(event.get("asset") or "").upper()
                   else Decimal(str(event.get("quote_amount") or 0)))
    if denominator <= 0:
        return None
    return amount / denominator, fee_asset


def replay_scenario(run: dict[str, Any], events: list[dict[str, Any]], snapshots: list[dict[str, Any]], *,
                    delay_seconds: Decimal = Decimal(0), buy_fee_percent: Decimal | None = None,
                    sell_fee_percent: Decimal | None = None,
                    depth_reduction_percent: Decimal = Decimal(0)) -> dict[str, Any]:
    if delay_seconds < 0 or not Decimal(0) <= depth_reduction_percent < Decimal(100):
        raise ValueError("Delay must be non-negative and depth reduction must be from 0 to 100%")
    if any(rate is not None and (not rate.is_finite() or rate < 0 or rate >= 100)
           for rate in (buy_fee_percent, sell_fee_percent)):
        raise ValueError("Commission must be from 0 to 100%")
    depth_factor = Decimal(1) - depth_reduction_percent / Decimal(100)
    starting = Decimal(str(run.get("starting_equity_usd") or 0))
    balances: dict[tuple[str, str], Decimal] = {}
    starting_rows = run.get("starting_balances") or []
    if isinstance(starting_rows, str):
        starting_rows = _json(starting_rows) or []
    for row in starting_rows:
        exchange = row.get("exchange")
        if exchange:
            key = (str(exchange).lower(), str(row.get("asset") or "USDT").upper())
            balances[key] = balances.get(key, Decimal(0)) + Decimal(str(row.get("balance") or 0))
    if not balances:
        balances[("legacy", "USDT")] = starting

    commission_events = {str(row.get("event_key")): row for row in events
                         if row.get("event_type") == "commission"}
    exit_fees: dict[tuple[str, str], tuple[Decimal, str]] = {}
    for event in events:
        if event.get("event_type") not in {"buy", "sell"}:
            continue
        market = (str(event.get("exchange_slug") or "").lower(),
                  str(event.get("market_pair") or "").upper())
        quote = str(event.get("quote_asset") or "USDT").upper()
        if sell_fee_percent is not None:
            exit_fees[market] = (sell_fee_percent / Decimal(100), quote)
            continue
        if event.get("event_type") != "sell":
            continue
        key = str(event.get("event_key") or "")
        settlement_id = str(event.get("settlement_id") or "")
        fee_key = key.replace(f"{settlement_id}:sell:", f"{settlement_id}:commission:sell:", 1)
        fee = _historical_exit_fee(event, commission_events.get(fee_key))
        if fee is not None:
            exit_fees[market] = fee
    ordered = []
    for event in events:
        kind = event.get("event_type")
        selected = None
        if kind in {"buy", "sell"}:
            side = "buy" if kind == "buy" else "sell"
            selected = select_snapshot(event, snapshots, side, delay_seconds)
            at = (_dt(selected["_replay_observed_at_utc"]) if selected and selected.get("_replay_observed_at_utc")
                  else _dt(event["event_at_utc"]) + timedelta(seconds=float(delay_seconds)))
        else:
            at = _dt(event["event_at_utc"]) + timedelta(seconds=float(delay_seconds))
        ordered.append((at, int(event.get("event_id") or 0), event, selected))
    ordered.sort(key=lambda item: (item[0], item[1]))

    gaps: list[str] = []
    if not run.get("baseline_complete", True):
        gaps.append("starting balances include unvalued or unallocated assets")
    if not any(row.get("event_type") in {"buy", "sell"} for row in events):
        gaps.append("journal has no executions for replay")
    points: list[tuple[datetime, Decimal]] = []
    books: dict[tuple[str, str], tuple[dict[str, Any], datetime]] = {}
    net_flows, fee_total, completed = Decimal(0), Decimal(0), 0
    for at, _, event, snapshot in ordered:
        kind = event.get("event_type")
        exchange = str(event.get("exchange_slug") or "").lower()
        asset = str(event.get("asset") or "").upper()
        if kind == "capital_movement":
            amount = Decimal(str(event.get("amount_usd") or 0))
            balances[(exchange or "legacy", "USDT")] = balances.get((exchange or "legacy", "USDT"), Decimal(0)) + amount
            net_flows += amount
        elif kind == "transfer_sent" or kind == "transfer_received":
            quantity = Decimal(str(event.get("quantity") or 0))
            key = (exchange, asset)
            if kind == "transfer_sent" and balances.get(key, Decimal(0)) < -quantity:
                gaps.append(f"{event.get('event_key')}: insufficient {asset} on {exchange}")
            balances[key] = balances.get(key, Decimal(0)) + quantity
            in_transit = ("in_transit", asset)
            if kind == "transfer_sent":
                balances[in_transit] = balances.get(in_transit, Decimal(0)) - quantity
            else:
                transit_amount = quantity
                if balances.get(in_transit, Decimal(0)) < transit_amount:
                    gaps.append(f"{event.get('event_key')}: received more {asset} than was sent")
                balances[in_transit] = max(Decimal(0), balances.get(in_transit, Decimal(0)) - transit_amount)
        elif kind == "commission" and ":commission:transfer" in str(event.get("event_key", "")):
            fee_asset = str(event.get("fee_asset") or asset or "USDT").upper()
            fee = Decimal(str(event.get("fee_amount") or 0))
            key = (exchange, fee_asset)
            if balances.get(key, Decimal(0)) < fee:
                gaps.append(f"{event.get('event_key')}: insufficient {fee_asset} for transfer fee on {exchange}")
            balances[key] = balances.get(key, Decimal(0)) - fee
            quote_fee = event.get("quote_amount")
            if quote_fee is None:
                gaps.append(f"{event.get('event_key')}: transfer fee has no USD valuation")
            else:
                fee_total += Decimal(str(quote_fee))
        elif kind in {"buy", "sell"}:
            side = "buy" if kind == "buy" else "sell"
            key = str(event.get("event_key") or "")
            fee_key = (f"{event.get('settlement_id')}:commission:buy" if side == "buy"
                       else key.replace(f"{event.get('settlement_id')}:sell:",
                                        f"{event.get('settlement_id')}:commission:sell:", 1))
            fee_event = commission_events.get(fee_key)
            if not snapshot:
                gaps.append(f"{key}: no book snapshot for delay {delay_seconds}s")
                points.append((at, _equity(balances, books, exit_fees, events, at, depth_factor, gaps)))
                continue
            pair = str(event.get("market_pair") or "").upper()
            levels = snapshot.get("asks" if side == "buy" else "bids", [])
            requested = Decimal(str(event.get("quantity") or 0))
            filled, notional = _fill(levels, requested, depth_factor)
            if filled <= 0:
                gaps.append(f"{key}: insufficient book depth")
                points.append((at, _equity(balances, books, exit_fees, events, at, depth_factor, gaps)))
                continue
            if filled < requested:
                gaps.append(f"{key}: partial execution {filled}/{requested}")
            bids = snapshot.get("bids") or []
            observed_value = snapshot.get("_replay_observed_at_utc") or snapshot.get("captured_at_utc") or event["event_at_utc"]
            observed_at = _dt(observed_value) if observed_value else None
            if bids and observed_at is not None:
                books[(exchange, pair)] = (snapshot, observed_at)
            else:
                gaps.append(f"{key}: book snapshot has no timestamp or bids for equity valuation")
            historical_fee = Decimal(str((fee_event or {}).get("fee_amount") or 0))
            fee_asset = str((fee_event or {}).get("fee_asset") or "USDT").upper()
            quote = str(event.get("quote_asset") or "USDT").upper()
            market_key = (exchange, pair)
            if side == "sell":
                if sell_fee_percent is not None:
                    exit_fees[market_key] = (sell_fee_percent / Decimal(100), quote)
                else:
                    recorded_fee = _historical_exit_fee(event, fee_event)
                    if recorded_fee is not None:
                        exit_fees[market_key] = recorded_fee
            fee_value_usd = Decimal(0)
            execution_price = notional / filled if filled else Decimal(0)
            if side == "buy":
                if fee_asset == asset:
                    fee = filled * (buy_fee_percent / 100 if buy_fee_percent is not None
                                    else historical_fee / requested if requested else 0)
                    net_coin = filled - fee
                    quote_cost = notional
                    fee_value_usd = fee * execution_price
                else:
                    fee = notional * (buy_fee_percent / 100 if buy_fee_percent is not None
                                      else historical_fee / Decimal(str(event.get("quote_amount") or notional))
                                      if event.get("quote_amount") or notional else 0)
                    net_coin, quote_cost = filled, notional + fee
                    fee_value_usd = fee
                cash_key = (exchange, quote)
                if balances.get(cash_key, Decimal(0)) < quote_cost:
                    gaps.append(f"{key}: insufficient {quote} on {exchange}")
                    points.append((at, _equity(balances, books, exit_fees, events, at, depth_factor, gaps)))
                    continue
                balances[cash_key] = balances.get(cash_key, Decimal(0)) - quote_cost
                balances[(exchange, asset)] = balances.get((exchange, asset), Decimal(0)) + net_coin
            else:
                coin_key = (exchange, asset)
                available = balances.get(coin_key, Decimal(0))
                if filled > available:
                    gaps.append(f"{key}: sell exceeds available {asset} on {exchange}")
                    filled, notional = _fill(levels, available, depth_factor)
                if fee_asset == asset:
                    fee = filled * (sell_fee_percent / 100 if sell_fee_percent is not None
                                    else historical_fee / requested if requested else 0)
                    fee_value_usd = fee * execution_price
                    cash_proceeds = notional
                else:
                    fee = notional * (sell_fee_percent / 100 if sell_fee_percent is not None
                                      else historical_fee / Decimal(str(event.get("quote_amount") or notional))
                                      if event.get("quote_amount") or notional else 0)
                    fee_value_usd = fee
                    cash_proceeds = notional - fee
                balances[coin_key] = max(Decimal(0), available - filled - (fee if fee_asset == asset else 0))
                balances[(exchange, quote)] = balances.get((exchange, quote), Decimal(0)) + cash_proceeds
                if balances[coin_key] == 0:
                    completed += 1
            fee_total += fee_value_usd
        elif kind == "commission":
            # Buy and sell commissions are included with their exact execution above.
            pass
        equity = _equity(balances, books, exit_fees, events, at, depth_factor, gaps)
        points.append((at, equity))

    peak, max_drawdown, final_equity = starting, Decimal(0), starting
    daily: dict[str, Decimal] = {}
    for at, equity in points:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        final_equity = equity
        daily[at.astimezone(timezone.utc).date().isoformat()] = equity
    return {
        "equity_usd": final_equity,
        "pnl_usd": final_equity - starting - net_flows,
        "max_drawdown_usd": max_drawdown,
        "fees_usd": fee_total,
        "closed_positions": completed,
        "gaps": gaps,
        "complete": not gaps and bool(points) and bool(run.get("baseline_complete", True)),
        "daily_equity": daily,
    }
