"""JSON-схема и формирование сообщения о зрелой арбитражной возможности."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TypedDict


class TradeSide(TypedDict):
    exchange: str
    market_pair: str
    price: str
    available_base_quantity: str
    available_quote_amount: str


class OpportunitySignal(TypedDict):
    event: str
    lifetime_id: int
    source_market_id: int
    source_pair: str
    threshold_percent: str
    spread_percent: str
    max_spread_percent: str
    buy: TradeSide
    sell: TradeSide
    network: str
    estimated_transfer_seconds: float
    opportunity_started_at_utc: str
    lifetime_seconds: float


def _side(
    exchange: str,
    market_pair: str,
    price: Decimal,
    available_base_quantity: Decimal,
) -> TradeSide:
    return {
        "exchange": exchange,
        "market_pair": market_pair,
        "price": str(price),
        "available_base_quantity": str(available_base_quantity),
        "available_quote_amount": str(price * available_base_quantity),
    }


def build_opportunity_signal(
    *,
    lifetime_id: int,
    source_market_id: int,
    source_pair: str,
    threshold_percent: Decimal,
    spread_percent: Decimal,
    max_spread_percent: Decimal,
    buy_exchange: str,
    buy_market_pair: str,
    buy_price: Decimal,
    buy_size: Decimal,
    sell_exchange: str,
    sell_market_pair: str,
    sell_price: Decimal,
    sell_size: Decimal,
    network: str,
    estimated_transfer_seconds: float,
    started_at_utc: str,
    lifetime_seconds: float,
) -> tuple[str, OpportunitySignal]:
    """Return an idempotency key and the complete JSON-ready opportunity."""
    payload: OpportunitySignal = {
        "event": "opportunity_matured",
        "lifetime_id": lifetime_id,
        "source_market_id": source_market_id,
        "source_pair": source_pair,
        "threshold_percent": str(threshold_percent),
        "spread_percent": str(spread_percent),
        "max_spread_percent": str(max_spread_percent),
        "buy": _side(buy_exchange, buy_market_pair, buy_price, buy_size),
        "sell": _side(sell_exchange, sell_market_pair, sell_price, sell_size),
        "network": network,
        "estimated_transfer_seconds": estimated_transfer_seconds,
        "opportunity_started_at_utc": started_at_utc,
        "lifetime_seconds": round(lifetime_seconds, 3),
    }
    return f"{lifetime_id}:{threshold_percent}", payload


def validate_opportunity_signal(payload: dict[str, Any]) -> OpportunitySignal:
    """Reject malformed queue messages before the trader handles them."""
    required = {
        "event", "lifetime_id", "source_market_id", "source_pair", "threshold_percent",
        "spread_percent", "max_spread_percent", "buy", "sell", "network",
        "estimated_transfer_seconds", "opportunity_started_at_utc", "lifetime_seconds",
    }
    missing = required.difference(payload)
    if missing:
        raise ValueError(f"В JSON возможности нет обязательных полей: {', '.join(sorted(missing))}")
    if payload["event"] != "opportunity_matured":
        raise ValueError(f"Неизвестный тип сообщения трейдера: {payload['event']}")
    for side_name in ("buy", "sell"):
        side = payload[side_name]
        if not isinstance(side, dict) or not {"exchange", "market_pair", "price"}.issubset(side):
            raise ValueError(f"В JSON возможности неверно заполнено поле {side_name}")
    return payload  # type: ignore[return-value]
