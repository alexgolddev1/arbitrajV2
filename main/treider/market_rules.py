"""Read public spot quantity constraints and normalize exchange rule payloads."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

import requests

from main.treider.config import MARKET_DATA_TIMEOUT_SECONDS


class UnknownMarketRules(ValueError):
    """A required market constraint was absent or could not be verified."""


@dataclass(frozen=True)
class SpotMarketRules:
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal | None
    source: str


def _required_decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise UnknownMarketRules(f"Неизвестное ограничение рынка: {field}") from error
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise UnknownMarketRules(f"Некорректное ограничение рынка {field}={value!r}")
    return number


def _json(url: str, params: dict[str, str]) -> Any:
    response = requests.get(url, params=params, timeout=MARKET_DATA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _rule(step: Any, minimum: Any, notional: Any, source: str) -> SpotMarketRules:
    quantity_step = _required_decimal(step, "quantity_step")
    min_quantity = _required_decimal(minimum, "min_quantity", allow_zero=True)
    min_notional = None if notional in (None, "") else _required_decimal(notional, "min_notional", allow_zero=True)
    return SpotMarketRules(quantity_step, min_quantity, min_notional, source)


def parse_market_rules(exchange: str, payload: Any, market_pair: str, *, market_order: bool) -> SpotMarketRules:
    """Parse the documented spot instrument payload for one venue and symbol."""
    slug = exchange.lower()
    symbol = "".join(part for part in market_pair.upper().replace("_", "/").replace("-", "/").split("/") if part)
    if slug == "binance":
        symbols = payload.get("symbols", [])
        row = next((item for item in symbols if item.get("symbol") == symbol), None)
        if row is None:
            raise UnknownMarketRules(f"Binance не вернул правила {symbol}")
        filters = {item.get("filterType"): item for item in row.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE" if market_order else "LOT_SIZE")
        if not lot:
            raise UnknownMarketRules(f"Binance не вернул применимое ограничение количества для {symbol}")
        min_notional = None
        notionals = []
        for name in ("NOTIONAL", "MIN_NOTIONAL"):
            item = filters.get(name)
            if item:
                if market_order:
                    applies_to_market = item.get("applyMinToMarket", item.get("applyToMarket"))
                    if applies_to_market is None:
                        raise UnknownMarketRules(f"Binance не вернул флаг применения {name} для market order")
                    if str(applies_to_market).lower() not in {"true", "1"}:
                        continue
                notionals.append(item.get("minNotional"))
        if notionals:
            min_notional = max(_required_decimal(item, "minNotional", allow_zero=True) for item in notionals)
        return _rule(lot.get("stepSize"), lot.get("minQty"), min_notional, "binance exchangeInfo")
    if slug == "bybit":
        if str(payload.get("retCode")) != "0":
            raise UnknownMarketRules(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        rows = payload.get("result", {}).get("list", [])
        row = next((item for item in rows if item.get("symbol") == symbol), None)
        if row is None:
            raise UnknownMarketRules(f"Bybit не вернул правила {symbol}")
        lot = row.get("lotSizeFilter") or {}
        # For spot the documented basePrecision gives quantity precision;
        # minOrderQty is deprecated and minOrderAmt is the active minimum.
        step = lot.get("basePrecision")
        min_notional = lot.get("minOrderAmt")
        if min_notional in (None, ""):
            raise UnknownMarketRules(f"Bybit не вернул minOrderAmt для {symbol}")
        return _rule(step, "0", min_notional, "Bybit instruments-info")
    if slug == "okx":
        if str(payload.get("code")) != "0":
            raise UnknownMarketRules(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        inst_id = market_pair.upper().replace("/", "-").replace("_", "-")
        row = next((item for item in payload.get("data", []) if item.get("instId") == inst_id), None)
        if row is None:
            raise UnknownMarketRules(f"OKX не вернул правила {inst_id}")
        # The public SPOT instrument schema publishes lotSz/minSz, but no notional filter.
        return _rule(row.get("lotSz"), row.get("minSz"), None, "OKX public instruments")
    if slug == "mexc":
        rows = payload.get("symbols", []) if isinstance(payload, dict) else []
        row = next((item for item in rows if item.get("symbol") == symbol), None)
        if row is None:
            raise UnknownMarketRules(f"MEXC не вернул правила {symbol}")
        filters = {item.get("filterType"): item for item in row.get("filters", [])}
        lot = filters.get("LOT_SIZE") or {}
        # MEXC documents baseSizePrecision as minimum order quantity and precision.
        step = lot.get("stepSize") or row.get("baseSizePrecision")
        minimum = lot.get("minQty") or row.get("baseSizePrecision")
        notional = filters.get("MIN_NOTIONAL", {}).get("minNotional") or row.get(
            "quoteAmountPrecisionMarket" if market_order else "quoteAmountPrecision"
        )
        if step in (None, "") or minimum in (None, "") or notional in (None, ""):
            raise UnknownMarketRules(f"MEXC не вернул полный набор правил количества для {symbol}")
        return _rule(step, minimum, notional, "MEXC exchangeInfo")
    raise UnknownMarketRules(f"Биржа {exchange!r} не поддерживается для правил количества")


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
        return _required_decimal(value, "withdraw_step")
    precision = network.get("withdraw_precision")
    if precision not in (None, ""):
        digits = int(precision)
        if digits < 0:
            raise UnknownMarketRules("Некорректная точность вывода")
        return Decimal(1).scaleb(-digits)
    raise UnknownMarketRules(f"{exchange or 'Биржа'} не вернула шаг/точность вывода")
