"""Read public spot quantity constraints and normalize exchange rule payloads."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import requests

from main.treider.config import MARKET_DATA_TIMEOUT_SECONDS
from main.treider.market_rules_normalizer import (
    UnknownMarketRules,
    normalize_market_rules,
)

@dataclass(frozen=True)
class SpotMarketRules:
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal | None
    source: str


def _json(url: str, params: dict[str, str]) -> Any:
    response = requests.get(url, params=params, timeout=MARKET_DATA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def parse_market_rules(exchange: str, payload: Any, market_pair: str, *, market_order: bool) -> SpotMarketRules:
    """Parse the documented spot instrument payload for one venue and symbol."""
    normalized = normalize_market_rules(exchange, payload, market_pair, market_order=market_order)
    return SpotMarketRules(
        normalized.quantity_step,
        normalized.min_quantity,
        normalized.min_notional,
        normalized.source,
    )


def fetch_spot_market_rules(exchange: str, market_pair: str, *, market_order: bool) -> SpotMarketRules:
    slug = exchange.strip().lower()
    symbol = "".join(part for part in market_pair.upper().replace("_", "/").replace("-", "/").split("/") if part)
    if slug == "binance":
        payload = _json("https://api.binance.com/api/v3/exchangeInfo", {"symbol": symbol})
    elif slug == "bybit":
        payload = _json("https://api.bybit.com/v5/market/instruments-info", {"category": "spot", "symbol": symbol})
    elif slug == "okx":
        inst_id = market_pair.upper().replace("/", "-").replace("_", "-")
        payload = _json("https://www.okx.com/api/v5/public/instruments", {"instType": "SPOT", "instId": inst_id})
    elif slug == "mexc":
        payload = _json("https://api.mexc.com/api/v3/exchangeInfo", {"symbol": symbol})
    else:
        raise UnknownMarketRules(f"Биржа {exchange!r} не поддерживается для правил количества")
    return parse_market_rules(slug, payload, market_pair, market_order=market_order)


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    if quantity < 0 or step <= 0:
        raise ValueError("Количество и шаг должны быть положительными")
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def withdraw_step(network: dict[str, Any]) -> Decimal:
    """Resolve exact withdrawal increment from documented network fields."""
    exchange = str(network.get("exchange") or "").lower()
    value = network.get("withdraw_step")
    if value not in (None, ""):
        try:
            step = Decimal(str(value))
        except Exception as error:
            raise UnknownMarketRules("Неизвестный шаг вывода") from error
        if not step.is_finite() or step <= 0:
            raise UnknownMarketRules(f"Некорректный шаг вывода: {value!r}")
        return step
    precision = network.get("withdraw_precision")
    if precision not in (None, ""):
        digits = int(precision)
        if digits < 0:
            raise UnknownMarketRules("Некорректная точность вывода")
        return Decimal(1).scaleb(-digits)
    raise UnknownMarketRules(f"{exchange or 'Биржа'} не вернула шаг/точность вывода")
