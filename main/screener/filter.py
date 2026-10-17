"""Volume filters for CoinMarketCap market-pair records."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from config import MAX_VOLUME_USD, MIN_VOLUME_USD


def filter_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep markets whose rolling 24-hour USD volume is within the inclusive range."""
    selected = []
    for market in markets:
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
