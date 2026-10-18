"""Проверка доступного маршрута вывода и ввода по общей сети."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from main.treider.config import NETWORK_CACHE_TTL_SECONDS
from main.treider.exchange_networks import get_transfer_routes

logger = logging.getLogger(__name__)
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _currency_from_pair(pair: str) -> str:
    for separator in ("/", "_", "-"):
        if separator in pair:
            currency = pair.split(separator, 1)[0].strip().upper()
            if currency:
                return currency
    raise ValueError(f"Не удалось определить валюту для проверки сети из пары {pair!r}")


def _fetch_routes(currency: str, source: str, destination: str) -> dict[str, Any]:
    key = (currency, source.lower(), destination.lower())
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] <= NETWORK_CACHE_TTL_SECONDS:
            return cached[1]

    result = get_transfer_routes(currency, source, destination)
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return result


def check_transfer_network(signal: dict[str, Any]) -> dict[str, Any]:
    """Return the fastest shared, enabled network for buying then selling exchanges.

    Existing route logic permits an inferred network on a side where API data is
    absent, but never overrides an explicitly disabled withdrawal or deposit.
    """
    currency = _currency_from_pair(signal["source_pair"])
    source = str(signal["buy"]["exchange"]).strip().lower()
    destination = str(signal["sell"]["exchange"]).strip().lower()
    result = _fetch_routes(currency, source, destination)
    routes = result.get("routes") or []
    if not routes:
        return {
            "available": False,
            "currency": currency,
            "source_exchange": source,
            "destination_exchange": destination,
            "errors": result.get("errors") or {},
        }

    selected = min(routes, key=lambda route: float(route.get("estimated_seconds") or 0))
    return {
        "available": True,
        "currency": currency,
        "source_exchange": source,
        "destination_exchange": destination,
        "network": selected.get("network_name") or selected.get("network_profile"),
        "network_profile": selected.get("network_profile"),
        "estimated_seconds": float(selected.get("estimated_seconds") or 0),
        "assumed_source": bool(selected.get("source_assumed")),
        "assumed_destination": bool(selected.get("destination_assumed")),
        "errors": result.get("errors") or {},
    }
