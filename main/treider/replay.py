"""Replay demo ledger executions against sanitized, time-ordered book snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any


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


def select_snapshot(
    event: dict[str, Any], snapshots: list[dict[str, Any]], side: str, delay_seconds: Decimal,
) -> dict[str, Any] | None:
    """Zero delay uses the event's contemporaneous book; delays require a later recorded book."""
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


def _fill(levels: Any, quantity: Decimal, depth_factor: Decimal, *, buying: bool) -> tuple[Decimal, Decimal]:
    remaining = quantity
    total = Decimal(0)
    filled = Decimal(0)
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


def replay_scenario(
    run: dict[str, Any], events: list[dict[str, Any]], snapshots: list[dict[str, Any]], *,
    delay_seconds: Decimal = Decimal(0), buy_fee_percent: Decimal | None = None,
    sell_fee_percent: Decimal | None = None, depth_reduction_percent: Decimal = Decimal(0),
) -> dict[str, Any]:
    if delay_seconds < 0 or not Decimal(0) <= depth_reduction_percent < Decimal(100):
        raise ValueError("Задержка должна быть неотрицательной, уменьшение глубины — от 0 до 100%")
    if any(rate is not None and (not rate.is_finite() or rate < 0 or rate >= 100)
           for rate in (buy_fee_percent, sell_fee_percent)):
        raise ValueError("Комиссия должна быть от 0 до 100%")
    depth_factor = Decimal(1) - depth_reduction_percent / Decimal(100)
    starting = Decimal(str(run.get("starting_equity_usd") or 0))
    cash = starting
    positions: dict[tuple[int, str], Decimal] = {}
    marks: dict[tuple[str, str], Decimal] = {}
    fee_by_settlement: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") == "commission":
            fee_by_settlement.setdefault(int(event["settlement_id"]), []).append(event)
    ordered = []
    for event in events:
        kind = event.get("event_type")
        selected = None
        if kind in {"buy", "sell"}:
            side = "buy" if kind == "buy" else "sell"
            selected = select_snapshot(event, snapshots, side, delay_seconds)
            event_time = (_dt(selected.get("_replay_observed_at_utc", event["event_at_utc"]))
                          if selected else _dt(event["event_at_utc"]) + timedelta(seconds=float(delay_seconds)))
        else:
            event_time = _dt(event["event_at_utc"]) + timedelta(seconds=float(delay_seconds))
        ordered.append((event_time, int(event.get("event_id") or 0), event, selected))
    ordered.sort(key=lambda item: (item[0], item[1]))
    points: list[tuple[datetime, Decimal]] = []
    gaps: list[str] = []
    if not run.get("baseline_complete", True):
        gaps.append("Стартовые остатки содержат неоценённые или неоднозначные активы")
    if not any(row.get("event_type") in {"buy", "sell"} for row in events):
        gaps.append("В журнале нет исполнений для replay")
    fee_total = Decimal(0)
    net_capital_flows = Decimal(0)
    completed = 0
    for event_time, _, event, snapshot in ordered:
        event_type = event.get("event_type")
        if event_type == "capital_movement":
            amount = Decimal(str(event.get("amount_usd") or 0))
            cash += amount
            net_capital_flows += amount
            points.append((event_time, cash))
            continue
        if event_type == "commission" and ":commission:transfer" in str(event.get("event_key", "")):
            transfer_fee = Decimal(str(event.get("quote_amount") or 0))
            cash -= transfer_fee
            fee_total += transfer_fee
            points.append((event_time, cash))
            continue
        if event_type not in {"buy", "sell"}:
            continue
        side = "buy" if event_type == "buy" else "sell"
        if not snapshot:
            gaps.append(f"{event.get('event_key')}: нет снимка для задержки {delay_seconds}s")
            continue
        levels = snapshot.get("asks" if side == "buy" else "bids", [])
        quantity = Decimal(str(event.get("quantity") or 0))
        filled, notional = _fill(levels, quantity, depth_factor, buying=side == "buy")
        if filled <= 0:
            gaps.append(f"{event.get('event_key')}: недостаточно глубины")
            continue
        if filled < quantity:
            gaps.append(f"{event.get('event_key')}: частичное исполнение {filled}/{quantity}")
        settlement_id = int(event["settlement_id"])
        asset = str(event.get("asset") or "")
        position_key = (settlement_id, asset)
        exchange_pair = (str(event.get("exchange_slug", "")).lower(), str(event.get("market_pair", "")).upper())
        bid_levels = snapshot.get("bids") or []
        ask_levels = snapshot.get("asks") or []
        if bid_levels:
            marks[exchange_pair] = Decimal(str(bid_levels[0][0]))
        elif ask_levels:
            marks[exchange_pair] = Decimal(str(ask_levels[0][0]))
        commissions = fee_by_settlement.get(settlement_id, [])
        fee_event = next((row for row in commissions if ("buy" if "buy" in str(row.get("event_key")) else "sell") == side
                          and (side == "buy" or str(row.get("event_key", "")).startswith(f"{settlement_id}:commission:sell:"))), None)
        historical_fee = Decimal(0)
        fee_asset = "USDT"
        if fee_event:
            historical_fee = Decimal(str(fee_event.get("fee_amount") or 0))
            fee_asset = str(fee_event.get("fee_asset") or "USDT")
        if side == "buy":
            if fee_asset == asset:
                fee_rate = (buy_fee_percent / Decimal(100) if buy_fee_percent is not None
                            else historical_fee / quantity if quantity else Decimal(0))
                fee = filled * fee_rate
                net_quantity = filled - fee
                cash -= notional
            else:
                fee_rate = (buy_fee_percent / Decimal(100) if buy_fee_percent is not None
                            else historical_fee / Decimal(str(event.get("quote_amount") or notional)) if event.get("quote_amount") or notional else Decimal(0))
                fee = notional * fee_rate
                net_quantity = filled
                cash -= notional + fee
            positions[position_key] = positions.get(position_key, Decimal(0)) + net_quantity
        else:
            available = positions.get(position_key, Decimal(0))
            if filled > available:
                gaps.append(f"{event.get('event_key')}: продажа ограничена остатком replay {available}/{filled}")
                filled, notional = _fill(levels, available, depth_factor, buying=False)
                if filled <= 0:
                    continue
            fee_rate = (sell_fee_percent / Decimal(100) if sell_fee_percent is not None
                        else historical_fee / Decimal(str(event.get("quote_amount") or notional)) if event.get("quote_amount") or notional else Decimal(0))
            fee = notional * fee_rate
            cash += notional - fee
            positions[position_key] = max(Decimal(0), positions.get(position_key, Decimal(0)) - filled)
        fee_total += fee
        valuation = cash
        for (position_trade, coin), amount in positions.items():
            if amount <= 0:
                continue
            trade = next((row for row in events if int(row.get("settlement_id") or 0) == position_trade and row.get("event_type") == "buy"), None)
            if trade:
                key = (str(trade.get("exchange_slug", "")).lower(), str(trade.get("market_pair", "")).upper())
                if key in marks:
                    valuation += amount * marks[key]
                else:
                    gaps.append(f"{position_trade}: нет цены для оценки {coin}")
        points.append((event_time, valuation))
        if side == "sell" and positions.get(position_key, Decimal(0)) == 0:
            completed += 1
    peak = starting
    max_drawdown = Decimal(0)
    final_equity = starting
    daily: dict[str, Decimal] = {}
    for at, equity in points:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        final_equity = equity
        daily[at.astimezone(timezone.utc).date().isoformat()] = equity
    return {
        "equity_usd": final_equity,
        "pnl_usd": final_equity - starting - net_capital_flows,
        "max_drawdown_usd": max_drawdown,
        "fees_usd": fee_total,
        "closed_positions": completed,
        "gaps": gaps,
        "complete": not gaps and bool(points) and bool(run.get("baseline_complete", True)),
        "daily_equity": daily,
    }
