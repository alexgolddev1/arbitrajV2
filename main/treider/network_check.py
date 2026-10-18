"""Проверка доступного маршрута вывода и ввода по общей сети."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from main.treider.config import NETWORK_CACHE_TTL_SECONDS
from main.treider.config import (
    ALLOW_DISABLED_ROUTE_DEMO,
    ALLOW_UNCONFIRMED_ROUTE_DEMO,
    TRANSFER_NETWORK,
)
from config import TRANSFER_NETWORK_MAP
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
    """Return an explicitly selected route, preferring verified routes over assumptions."""
    currency = _currency_from_pair(signal["source_pair"])
    source = str(signal["buy"]["exchange"]).strip().lower()
    destination = str(signal["sell"]["exchange"]).strip().lower()
    result = _fetch_routes(currency, source, destination)
    routes = result.get("routes") or []
    if not routes:
        return {
            "available": False,
            "route_status": "unavailable",
            "currency": currency,
            "source_exchange": source,
            "destination_exchange": destination,
            "errors": result.get("errors") or {},
        }

    requested_profile = TRANSFER_NETWORK_MAP.get(currency) or TRANSFER_NETWORK
    selected = sorted(
        routes,
        key=lambda route: (
            0 if requested_profile and route.get("network_profile") == requested_profile else 1,
            {"confirmed": 0, "unconfirmed": 1, "disabled": 2}.get(route.get("route_status"), 3),
            route.get("network_profile", ""),
        ),
    )[0]
    if selected.get("route_status") == "disabled" and not ALLOW_DISABLED_ROUTE_DEMO:
        return {"available": False, "route_status": "unavailable", "currency": currency,
                "source_exchange": source, "destination_exchange": destination,
                "errors": result.get("errors") or {}}
    if selected.get("route_status") != "confirmed" and not ALLOW_UNCONFIRMED_ROUTE_DEMO:
        return {"available": False, "route_status": "unconfirmed", "currency": currency,
                "source_exchange": source, "destination_exchange": destination,
                "errors": result.get("errors") or {}}
    return {
        "available": True,
        "route_status": selected.get("route_status", "unconfirmed"),
        "currency": currency,
        "source_exchange": source,
        "destination_exchange": destination,
        "network": selected.get("network_name") or selected.get("network_profile"),
        "network_profile": selected.get("network_profile"),
        "estimated_seconds": float(selected.get("estimated_seconds") or 0),
        "withdraw_fee": selected.get("withdraw_fee"),
        "withdraw_fee_currency": selected.get("withdraw_fee_currency"),
        "identity_verified": bool(selected.get("identity_verified")),
        "fee_required": selected.get("fee_required", True),
        "time_model": selected.get("time_model"),
        "random_seed": selected.get("random_seed"),
        "fixed_delay_seconds": selected.get("fixed_delay_seconds"),
        "source_network": selected.get("source_network"),
        "destination_network": selected.get("destination_network"),
        "assumed_source": bool(selected.get("source_assumed")),
        "assumed_destination": bool(selected.get("destination_assumed")),
        "errors": result.get("errors") or {},
    }
