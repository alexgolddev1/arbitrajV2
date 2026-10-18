"""Fetch fresh spot best-bid/best-ask quotes for supported exchanges."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from main.treider.config import MARKET_DATA_TIMEOUT_SECONDS


class InsufficientLiquidity(ValueError):
    """The returned depth has no executable quantity for the requested sale."""


@dataclass(frozen=True)
class SpotBookTicker:
    exchange: str
    market_pair: str
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal


@dataclass(frozen=True)
class SpotMarketSell:
    exchange: str
    market_pair: str
    quantity: Decimal
    proceeds: Decimal
    average_price: Decimal
    best_bid: Decimal
    levels_used: int
    filled_quantity: Decimal
    remaining_quantity: Decimal


def _pair_parts(market_pair: str) -> tuple[str, str]:
    for separator in ("/", "_", "-"):
        if separator in market_pair:
            base, quote = market_pair.upper().split(separator, 1)
            if base and quote:
                return base, quote
    raise ValueError(f"Некорректная спотовая пара: {market_pair!r}")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Биржа вернула некорректное поле {field}: {value!r}") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"Биржа вернула недопустимое поле {field}: {value!r}")
    return result


def _get_json(url: str, params: dict[str, str]) -> Any:
    response = requests.get(url, params=params, timeout=MARKET_DATA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def fetch_spot_book(exchange: str, market_pair: str) -> SpotBookTicker:
    exchange_slug = exchange.strip().lower()
    base, quote = _pair_parts(market_pair)
    symbol = f"{base}{quote}"

    if exchange_slug == "binance":
        payload = _get_json(
            "https://data-api.binance.vision/api/v3/ticker/bookTicker",
            {"symbol": symbol},
        )
        values = (payload.get("bidPrice"), payload.get("bidQty"), payload.get("askPrice"), payload.get("askQty"))
    elif exchange_slug == "bybit":
        payload = _get_json(
            "https://api.bybit.com/v5/market/tickers",
            {"category": "spot", "symbol": symbol},
        )
        if str(payload.get("retCode")) != "0":
            raise RuntimeError(f"Bybit вернул ошибку {payload.get('retCode')}: {payload.get('retMsg')}")
        items = payload.get("result", {}).get("list", [])
        if not items:
            raise ValueError(f"Bybit не вернул spot-котировку для {symbol}")
        item = items[0]
        values = (item.get("bid1Price"), item.get("bid1Size"), item.get("ask1Price"), item.get("ask1Size"))
    elif exchange_slug == "okx":
        payload = _get_json(
            "https://www.okx.com/api/v5/market/ticker",
            {"instId": f"{base}-{quote}"},
        )
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX вернул ошибку {payload.get('code')}: {payload.get('msg')}")
        items = payload.get("data", [])
        if not items:
            raise ValueError(f"OKX не вернул spot-котировку для {base}-{quote}")
        item = items[0]
        values = (item.get("bidPx"), item.get("bidSz"), item.get("askPx"), item.get("askSz"))
    elif exchange_slug == "mexc":
        payload = _get_json(
            "https://api.mexc.com/api/v3/ticker/bookTicker",
            {"symbol": symbol},
        )
        values = (payload.get("bidPrice"), payload.get("bidQty"), payload.get("askPrice"), payload.get("askQty"))
    else:
        raise ValueError(f"Биржа {exchange!r} не поддерживается для проверки котировок")

    bid, bid_size, ask, ask_size = (
        _decimal(value, field)
        for value, field in zip(values, ("bid", "bid_size", "ask", "ask_size"))
    )
    return SpotBookTicker(exchange_slug, market_pair, bid, bid_size, ask, ask_size)


def fetch_market_sell_execution(
    exchange: str, market_pair: str, quantity: Decimal
) -> SpotMarketSell:
    """Simulate a market sell by consuming descending spot bid levels."""
    exchange_slug = exchange.strip().lower()
    base, quote = _pair_parts(market_pair)
    symbol = f"{base}{quote}"
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError(f"Объём рыночной продажи должен быть положительным: {quantity}")

    if exchange_slug == "binance":
        payload = _get_json(
            "https://data-api.binance.vision/api/v3/depth",
            {"symbol": symbol, "limit": "1000"},
        )
        bids = payload.get("bids", [])
    elif exchange_slug == "bybit":
        payload = _get_json(
            "https://api.bybit.com/v5/market/orderbook",
            {"category": "spot", "symbol": symbol, "limit": "1000"},
        )
        if str(payload.get("retCode")) != "0":
            raise RuntimeError(f"Bybit вернул ошибку {payload.get('retCode')}: {payload.get('retMsg')}")
        bids = payload.get("result", {}).get("b", [])
    elif exchange_slug == "okx":
        payload = _get_json(
            "https://www.okx.com/api/v5/market/books",
            {"instId": f"{base}-{quote}", "sz": "400"},
        )
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX вернул ошибку {payload.get('code')}: {payload.get('msg')}")
        items = payload.get("data", [])
        bids = items[0].get("bids", []) if items else []
    elif exchange_slug == "mexc":
        payload = _get_json(
            "https://api.mexc.com/api/v3/depth",
            {"symbol": symbol, "limit": "1000"},
        )
        bids = payload.get("bids", [])
    else:
        raise ValueError(f"Биржа {exchange!r} не поддерживается для рыночной продажи")

    return simulate_market_sell(exchange_slug, market_pair, quantity, bids)


def simulate_market_sell(
    exchange: str, market_pair: str, quantity: Decimal, bids: list[list[Any]]
) -> SpotMarketSell:
    """Consume bid depth once and preserve any unfilled remainder."""
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError(f"Объём рыночной продажи должен быть положительным: {quantity}")
    remaining = quantity
    proceeds = Decimal(0)
    best_bid = Decimal(0)
    levels_used = 0
    for level in bids:
        if len(level) < 2:
            continue
        price = _decimal(level[0], "bid_price")
        size = _decimal(level[1], "bid_size")
        if not best_bid:
            best_bid = price
        filled = min(remaining, size)
        proceeds += filled * price
        remaining -= filled
        levels_used += 1
        if remaining <= 0:
            break
    filled_quantity = quantity - remaining
    if filled_quantity <= 0 or best_bid <= 0:
        raise InsufficientLiquidity(f"В стакане нет bid-объёма для продажи {exchange} {market_pair}")
    return SpotMarketSell(
        exchange, market_pair, quantity, proceeds,
        proceeds / filled_quantity, best_bid, levels_used,
        filled_quantity, remaining,
    )


def fetch_current_spread(
    buy_exchange: str,
    buy_market_pair: str,
    sell_exchange: str,
    sell_market_pair: str,
) -> tuple[SpotBookTicker, SpotBookTicker, Decimal]:
    buy_base, buy_quote = _pair_parts(buy_market_pair)
    sell_base, sell_quote = _pair_parts(sell_market_pair)
    if (buy_base, buy_quote) != (sell_base, sell_quote):
        raise ValueError(
            "Нельзя сравнить пары с разными базовой или котируемой валютами: "
            f"{buy_market_pair} и {sell_market_pair}"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        buy_future = executor.submit(fetch_spot_book, buy_exchange, buy_market_pair)
        sell_future = executor.submit(fetch_spot_book, sell_exchange, sell_market_pair)
        buy_book = buy_future.result()
        sell_book = sell_future.result()

    spread = (sell_book.bid - buy_book.ask) / buy_book.ask * Decimal(100)
    return buy_book, sell_book, spread
