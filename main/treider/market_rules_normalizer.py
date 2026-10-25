"""Normalize public spot market-rule payloads from supported exchanges."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class UnknownMarketRules(ValueError):
    """A required market constraint was absent or could not be verified."""


@dataclass(frozen=True)
class NormalizedMarketRules:
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal | None
    source: str


def _decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise UnknownMarketRules(f"Неизвестное ограничение рынка: {field}") from error
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise UnknownMarketRules(f"Некорректное ограничение рынка {field}={value!r}")
    return number


def _rule(step: Any, minimum: Any, notional: Any, source: str) -> NormalizedMarketRules:
    quantity_step = _decimal(step, "quantity_step")
    min_quantity = _decimal(minimum, "min_quantity", allow_zero=True)
    min_notional = None if notional in (None, "") else _decimal(
        notional, "min_notional", allow_zero=True
    )
    return NormalizedMarketRules(quantity_step, min_quantity, min_notional, source)


def _symbol(market_pair: str) -> str:
    return "".join(
        part for part in market_pair.upper().replace("_", "/").replace("-", "/").split("/") if part
    )


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _mexc_step_from_precision(value: Any) -> Decimal:
    """Convert MEXC baseAssetPrecision decimal places to a quantity increment."""
    precision = _decimal(value, "baseAssetPrecision", allow_zero=True)
    if precision != precision.to_integral_value():
        raise UnknownMarketRules(f"Некорректная точность количества MEXC: {value!r}")
    return Decimal(1).scaleb(-int(precision))


def normalize_market_rules(
    exchange: str, payload: Any, market_pair: str, *, market_order: bool
) -> NormalizedMarketRules:
    """Map exchange-specific fields to step, minimum quantity and minimum notional."""
    slug = exchange.strip().lower()
    symbol = _symbol(market_pair)

    if slug == "binance":
        symbols = payload.get("symbols", [])
        row = next((item for item in symbols if item.get("symbol") == symbol), None)
        if row is None:
            raise UnknownMarketRules(f"Binance не вернул правила {symbol}")
        filters = {item.get("filterType"): item for item in row.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE" if market_order else "LOT_SIZE")
        if not lot:
            raise UnknownMarketRules(f"Binance не вернул применимое ограничение количества для {symbol}")
        notionals = []
        for name in ("NOTIONAL", "MIN_NOTIONAL"):
            item = filters.get(name)
            if item:
                if market_order:
                    applies = item.get("applyMinToMarket", item.get("applyToMarket"))
                    if applies is None:
                        raise UnknownMarketRules(f"Binance не вернул флаг применения {name} для market order")
                    if str(applies).lower() not in {"true", "1"}:
                        continue
                notionals.append(item.get("minNotional"))
        notional = max((_decimal(value, "minNotional", allow_zero=True) for value in notionals), default=None)
        return _rule(lot.get("stepSize"), lot.get("minQty"), notional, "binance exchangeInfo")

    if slug == "bybit":
        if str(payload.get("retCode")) != "0":
            raise UnknownMarketRules(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        rows = payload.get("result", {}).get("list", [])
        row = next((item for item in rows if item.get("symbol") == symbol), None)
        if row is None:
            raise UnknownMarketRules(f"Bybit не вернул правила {symbol}")
        lot = row.get("lotSizeFilter") or {}
        step = lot.get("basePrecision")
        notional = lot.get("minOrderAmt")
        if not _present(notional):
            raise UnknownMarketRules(f"Bybit не вернул minOrderAmt для {symbol}")
        return _rule(step, "0", notional, "Bybit instruments-info")

    if slug == "okx":
        if str(payload.get("code")) != "0":
            raise UnknownMarketRules(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        inst_id = market_pair.upper().replace("/", "-").replace("_", "-")
        row = next((item for item in payload.get("data", []) if item.get("instId") == inst_id), None)
        if row is None:
            raise UnknownMarketRules(f"OKX не вернул правила {inst_id}")
        return _rule(row.get("lotSz"), row.get("minSz"), None, "OKX public instruments")

    if slug == "mexc":
        rows = payload.get("symbols", []) if isinstance(payload, dict) else []
        row = next((item for item in rows if item.get("symbol") == symbol), None)
        if row is None:
            raise UnknownMarketRules(f"MEXC не вернул правила {symbol}")
        filters = {item.get("filterType"): item for item in row.get("filters", [])}
        lot = filters.get("LOT_SIZE") or {}

        # MEXC documents baseSizePrecision as minimum quantity, not quantity step.
        # If no explicit step is present, baseAssetPrecision specifies the decimal precision.
        step = lot.get("stepSize")
        if not _present(step) or _decimal(step, "quantity_step", allow_zero=True) == 0:
            step = _mexc_step_from_precision(row.get("baseAssetPrecision"))

        minimum = lot.get("minQty") if _present(lot.get("minQty")) else row.get("baseSizePrecision")
        notional = filters.get("MIN_NOTIONAL", {}).get("minNotional")
        if not _present(notional):
            notional = row.get("quoteAmountPrecisionMarket" if market_order else "quoteAmountPrecision")
        if not _present(minimum) or not _present(notional):
            raise UnknownMarketRules(f"MEXC не вернул полный набор правил количества для {symbol}")
        return _rule(step, minimum, notional, "MEXC exchangeInfo")

    raise UnknownMarketRules(f"Биржа {exchange!r} не поддерживается для правил количества")
