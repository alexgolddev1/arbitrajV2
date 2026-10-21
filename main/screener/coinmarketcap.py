"""CoinMarketCap HTTP requests and response-to-column mapping."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests
from config import CMC_API_URL, CMC_REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
_MAX_REQUEST_ATTEMPTS = 3


def to_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def market_values(market: dict[str, Any]) -> tuple[Any, ...]:
    quotes = market.get("quotes") or []
    quote_metrics = quotes[0] if quotes else {}
    return (
        market.get("marketId"),
        market.get("rank"),
        market.get("exchangeId"),
        market.get("exchangeName"),
        market.get("exchangeSlug"),
        market.get("marketPair") or "",
        market.get("category"),
        market.get("marketUrl"),
        market.get("baseSymbol"),
        market.get("baseCurrencyId"),
        market.get("baseCurrencyName"),
        market.get("baseCurrencySlug"),
        market.get("quoteSymbol"),
        market.get("quoteCurrencyId"),
        to_decimal(market.get("price")),
        to_decimal(market.get("volumeUsd")),
        to_decimal(market.get("effectiveLiquidity")),
        to_datetime(market.get("lastUpdated")),
        to_decimal(market.get("quote")),
        to_decimal(market.get("volumeBase")),
        to_decimal(market.get("volumeQuote")),
        quote_metrics.get("id"),
        to_decimal(quote_metrics.get("volume24h")),
        to_decimal(quote_metrics.get("depthPositiveTwo")),
        to_decimal(quote_metrics.get("depthNegativeTwo")),
        market.get("feeType"),
        to_decimal(market.get("depthUsdNegativeTwo")),
        to_decimal(market.get("depthUsdPositiveTwo")),
        to_decimal(market.get("volumePercent")),
        to_decimal(market.get("marketScore")),
        market.get("marketReputation"),
        market.get("outlierDetected"),
        market.get("priceExcluded"),
        market.get("volumeExcluded"),
        market.get("outlierDisp"),
        market.get("priceExDisp"),
        market.get("volExDisp"),
        market.get("isVerified"),
        market.get("type"),
        market.get("hideStarsMarket"),
    )


def fetch_page(config: dict[str, str], start: int, limit: int) -> dict[str, Any]:
    params = {
        "slug": config["slug"],
        "category": config["category"],
        "start": start,
        "limit": limit,
        "includeUntracked": config["includeUntracked"],
    }
    response = None
    for attempt in range(1, _MAX_REQUEST_ATTEMPTS + 1):
        try:
            response = requests.get(
                CMC_API_URL,
                params=params,
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=CMC_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            if attempt >= _MAX_REQUEST_ATTEMPTS:
                logger.error(
                    "CoinMarketCap request failed after %s attempts: slug=%s start=%s limit=%s error=%s",
                    attempt, config["slug"], start, limit, error,
                )
                raise
            logger.warning(
                "CoinMarketCap request error, retrying (%s/%s): slug=%s start=%s limit=%s error=%s",
                attempt, _MAX_REQUEST_ATTEMPTS, config["slug"], start, limit, error,
            )
            time.sleep(attempt)
            continue

        if response.status_code in _TRANSIENT_HTTP_STATUSES:
            body_preview = response.text[:2000].replace("\n", " ").replace("\r", " ")
            if attempt >= _MAX_REQUEST_ATTEMPTS:
                logger.error(
                    "CoinMarketCap returned HTTP %s after %s attempts: slug=%s start=%s limit=%s body=%r",
                    response.status_code, attempt, config["slug"], start, limit, body_preview,
                )
                response.raise_for_status()
            logger.warning(
                "CoinMarketCap returned HTTP %s, retrying (%s/%s): slug=%s start=%s limit=%s body=%r",
                response.status_code, attempt, _MAX_REQUEST_ATTEMPTS,
                config["slug"], start, limit, body_preview,
            )
            time.sleep(attempt)
            continue
        break

    assert response is not None
    response.raise_for_status()
    # Parse in memory with Decimal to preserve API numeric precision; no JSON is stored.
    payload = json.loads(response.text, parse_float=Decimal)
    status = payload.get("status", {})
    if str(status.get("error_code", "0")) != "0":
        raise RuntimeError(f"CoinMarketCap API error: {status.get('error_message')}")
    if not isinstance(payload.get("data", {}).get("marketPairs"), list):
        raise ValueError("API response does not contain data.marketPairs array")
    return payload
