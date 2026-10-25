"""Completed-trade quality metrics, grouped by an exact demo route and strategy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RealizedTradeQuality:
    realized_profit_usd: Decimal
    expected_profit_usd: Decimal | None
    forecast_error_usd: Decimal | None
    roi_percent: Decimal | None


@dataclass(frozen=True)
class RouteQualityStats:
    sample_count: int = 0
    profitable_count: int = 0
    win_rate_percent: Decimal = Decimal(0)
    expectancy_usd: Decimal = Decimal(0)
    average_roi_percent: Decimal = Decimal(0)
    max_drawdown_usd: Decimal = Decimal(0)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _route_assumptions(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _measure_realized_quality(settlement: dict[str, Any]) -> RealizedTradeQuality:
    """Compare the closed trade's actual result with the saved entry estimate."""
    realized = _decimal_or_none(settlement.get("realized_profit_usd"))
    if realized is None:
        raise ValueError("Нет достоверного фактического PnL закрытой сделки")
    expected = _decimal_or_none(settlement.get("expected_profit_after_reserve_usd"))
    if expected is None:
        expected = _decimal_or_none(settlement.get("expected_profit_usd"))
    debit = _decimal_or_none(settlement.get("buy_debit_usd"))
    roi = realized / debit * Decimal(100) if debit is not None and debit > 0 else None
    return RealizedTradeQuality(
        realized_profit_usd=realized,
        expected_profit_usd=expected,
        forecast_error_usd=realized - expected if expected is not None else None,
        roi_percent=roi,
    )


def _get_route_quality_stats(
    cursor: Any,
    *,
    source_pair: str,
    buy_exchange: str,
    sell_exchange: str,
    network_name: str | None,
    route_status: str,
    strategy: str,
    run_id: str,
    signal_threshold_percent: Any = None,
) -> RouteQualityStats:
    """Aggregate closed results only for the exact pair, route, network, status and strategy."""
    if signal_threshold_percent is None:
        # Do not combine historical threshold tiers when the current signal lacks its tier.
        return RouteQualityStats()
    where_network = "network_name IS NULL" if network_name is None else "LOWER(network_name) = LOWER(%s)"
    parameters: list[Any] = [source_pair, buy_exchange.lower(), sell_exchange.lower()]
    if network_name is not None:
        parameters.append(network_name)
    parameters.append(route_status)
    cursor.execute(
        "SELECT route_assumptions, realized_profit_usd, buy_debit_usd, "
        "expected_profit_after_reserve_usd, expected_profit_usd "
        "FROM trader_training_settlements WHERE status = 'settled' AND position_status = 'closed' "
        "AND source_pair = %s AND LOWER(buy_exchange) = %s AND LOWER(sell_exchange) = %s "
        f"AND {where_network} AND route_status = %s "
        "ORDER BY settled_at, settlement_id",
        tuple(parameters),
    )

    outcomes: list[RealizedTradeQuality] = []
    for row in cursor.fetchall():
        assumptions = _route_assumptions(row.get("route_assumptions"))
        if str(assumptions.get("demo_run_id") or "") != str(run_id):
            continue
        if str(assumptions.get("demo_strategy") or "") != str(strategy):
            continue
        if signal_threshold_percent is not None:
            saved_threshold = _decimal_or_none(assumptions.get("signal_threshold_percent"))
            requested_threshold = _decimal_or_none(signal_threshold_percent)
            if saved_threshold is None or requested_threshold is None or saved_threshold != requested_threshold:
                continue
        try:
            outcomes.append(_measure_realized_quality(row))
        except ValueError:
            # Incomplete/legacy rows cannot establish the profitability of this route.
            continue

    count = len(outcomes)
    if not count:
        return RouteQualityStats()

    profits = [item.realized_profit_usd for item in outcomes]
    rois = [item.roi_percent for item in outcomes if item.roi_percent is not None]
    profitable = sum(value > 0 for value in profits)
    running_total = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    for profit in profits:
        running_total += profit
        peak = max(peak, running_total)
        max_drawdown = max(max_drawdown, peak - running_total)
    return RouteQualityStats(
        sample_count=count,
        profitable_count=profitable,
        win_rate_percent=Decimal(profitable) / Decimal(count) * Decimal(100),
        expectancy_usd=sum(profits, Decimal(0)) / Decimal(count),
        average_roi_percent=(sum(rois, Decimal(0)) / Decimal(len(rois)) if rois else Decimal(0)),
        max_drawdown_usd=max_drawdown,
    )
