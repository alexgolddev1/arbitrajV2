"""Volume filters for CoinMarketCap market-pair records."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from config import MAX_VOLUME_USD, MIN_VOLUME_USD, SCREENER_QUOTE_SYMBOLS


def filter_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep markets with the configured quote asset and allowed 24-hour USD volume."""
    selected = []
    for market in markets:
        if (
            SCREENER_QUOTE_SYMBOLS
            and str(market.get("quoteSymbol") or "").strip().upper()
            not in SCREENER_QUOTE_SYMBOLS
        ):
            continue
        volume = market.get("volumeUsd")
        if volume is None:
            continue
        try:
            volume_usd = Decimal(str(volume))
        except InvalidOperation:
            continue
        if MIN_VOLUME_USD <= volume_usd <= MAX_VOLUME_USD:
            selected.append(market)
    return selected
