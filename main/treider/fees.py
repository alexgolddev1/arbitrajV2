"""Запрос персональных spot-комиссий аккаунта для конкретной пары."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import requests

from config import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    MEXC_API_KEY,
    MEXC_API_SECRET,
    OKX_API_KEY,
    OKX_API_PASSPHRASE,
    OKX_API_SECRET,
)
from main.treider.config import FEE_CACHE_TTL_SECONDS, MARKET_DATA_TIMEOUT_SECONDS
from main.treider.config import CONFIGURED_TAKER_FEES


@dataclass(frozen=True)
class TradeFeeRates:
    exchange: str
    symbol: str
    maker_rate: Decimal
    taker_rate: Decimal
    checked_at_utc: str
    details: str = ""
    source: str = "api"
    buy_taker_rate: Decimal | None = None
    sell_taker_rate: Decimal | None = None

    def taker_for(self, side: str) -> Decimal:
        selected = self.buy_taker_rate if side.lower() == "buy" else self.sell_taker_rate
        return self.taker_rate if selected is None else selected


_cache: dict[tuple[str, str], tuple[float, TradeFeeRates]] = {}
_cache_lock = threading.Lock()


def _symbol(pair: str, exchange: str) -> tuple[str, str, str]:
    normalized = pair.strip().upper()
    for separator in ("/", "_", "-"):
        if separator in normalized:
            base, quote = normalized.split(separator, 1)
            if base and quote:
                formatted = f"{base}-{quote}" if exchange == "okx" else f"{base}{quote}"
                return base, quote, formatted
    raise ValueError(f"Некорректная spot-пара для запроса комиссии: {pair!r}")


def _rate(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Биржа вернула некорректную ставку {name}: {value!r}") from error
    if not result.is_finite():
        raise ValueError(f"Биржа вернула неконечную ставку {name}: {value!r}")
    return result


def _api_error_details(error: Exception) -> str:
    """Describe an exchange failure without logging signed URLs or credentials."""
    response = getattr(error, "response", None)
    if response is not None:
        status = getattr(response, "status_code", "unknown")
        try:
            payload = response.json()
        except (ValueError, TypeError, AttributeError):
            payload = None
        if isinstance(payload, dict):
            code = next(
                (payload[key] for key in ("code", "retCode", "errorCode") if key in payload),
                None,
            )
            message = next(
                (payload[key] for key in ("msg", "retMsg", "message", "error") if key in payload),
                None,
            )
            if code is not None or message is not None:
                return f"HTTP {status}; код={code}; сообщение={message}"
        reason = getattr(response, "reason", None)
        return f"HTTP {status}" + (f"; {reason}" if reason else "")
    if isinstance(error, requests.RequestException):
        # RequestException.__str__ may contain a URL with a signed query string.
        return f"сетевая ошибка {type(error).__name__}"
    return f"{type(error).__name__}: {error}"


def _signed_query(
    url: str, params: dict[str, Any], secret: str, *, api_key: str, header_name: str
) -> Any:
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    response = requests.get(
        url,
        params={**params, "signature": signature},
        headers={header_name: api_key},
        timeout=MARKET_DATA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _binance(symbol: str) -> tuple[Decimal, Decimal, str, Decimal, Decimal]:
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise RuntimeError("Не заданы ключи Binance для чтения торговой комиссии")
    payload = _signed_query(
        "https://api.binance.com/api/v3/account/commission",
        {"symbol": symbol, "timestamp": int(time.time() * 1000)},
        BINANCE_API_SECRET,
        api_key=BINANCE_API_KEY,
        header_name="X-MBX-APIKEY",
    )
    standard = payload.get("standardCommission") or {}
    special = payload.get("specialCommission") or {}
    tax = payload.get("taxCommission") or {}
    discount = payload.get("discount") or {}
    standard_maker = _rate(standard.get("maker"), "standard maker")
    standard_taker = _rate(standard.get("taker"), "standard taker")
    maker_special = _rate(special.get("maker", "0"), "special maker")
    taker_special = _rate(special.get("taker", "0"), "special taker")
    maker_tax = _rate(tax.get("maker", "0"), "tax maker")
    taker_tax = _rate(tax.get("taker", "0"), "tax taker")
    maker = standard_maker + maker_special + maker_tax
    taker = standard_taker + taker_special + taker_tax
    buy_rate = taker + _rate(standard.get("buyer", "0"), "standard buyer")
    buy_rate += _rate(special.get("buyer", "0"), "special buyer")
    buy_rate += _rate(tax.get("buyer", "0"), "tax buyer")
    sell_rate = taker + _rate(standard.get("seller", "0"), "standard seller")
    sell_rate += _rate(special.get("seller", "0"), "special seller")
    sell_rate += _rate(tax.get("seller", "0"), "tax seller")
    discount_enabled = bool(discount.get("enabledForAccount") and discount.get("enabledForSymbol"))
    # API eligibility is not proof that the account holds enough BNB at execution time.
    # Deliberately do not apply the discount until BNB balance is checked as part of execution.
    return maker, taker, f"BNB discount eligible={discount_enabled}; not applied (BNB balance unverified)", buy_rate, sell_rate


def _bybit(symbol: str) -> tuple[Decimal, Decimal, str, Decimal, Decimal]:
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        raise RuntimeError("Не заданы ключи Bybit для чтения торговой комиссии")
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    params = {"category": "spot", "symbol": symbol}
    query = urlencode(params)
    signature = hmac.new(
        BYBIT_API_SECRET.encode(),
        (timestamp + BYBIT_API_KEY + recv_window + query).encode(),
        hashlib.sha256,
    ).hexdigest()
    response = requests.get(
        "https://api.bybit.com/v5/account/fee-rate",
        params=params,
        headers={
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        },
        timeout=MARKET_DATA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("retCode")) != "0":
        raise RuntimeError(f"Bybit вернул ошибку {payload.get('retCode')}: {payload.get('retMsg')}")
    rows = payload.get("result", {}).get("list", [])
    item = next((row for row in rows if row.get("symbol", "").upper() == symbol), None)
    if item is None:
        raise ValueError(f"Bybit не вернул торговую комиссию для {symbol}")
    maker = _rate(item.get("makerFeeRate"), "maker")
    taker = _rate(item.get("takerFeeRate"), "taker")
    return maker, taker, "", taker, taker


def _okx(symbol: str) -> tuple[Decimal, Decimal, str, Decimal, Decimal]:
    if not (OKX_API_KEY and OKX_API_SECRET and OKX_API_PASSPHRASE):
        raise RuntimeError("Не заданы ключи и passphrase OKX для чтения торговой комиссии")
    path = "/api/v5/account/trade-fee"
    params = {"instType": "SPOT", "instId": symbol}
    request_path = path + "?" + urlencode(params)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    message = timestamp + "GET" + request_path
    signature = base64.b64encode(
        hmac.new(OKX_API_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    response = requests.get(
        "https://www.okx.com" + path,
        params=params,
        headers={
            "OK-ACCESS-KEY": OKX_API_KEY,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        },
        timeout=MARKET_DATA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX вернул ошибку {payload.get('code')}: {payload.get('msg')}")
    items = payload.get("data", [])
    if not items:
        raise ValueError(f"OKX не вернул торговую комиссию для {symbol}")
    item = items[0]
    # OKX fee rates are negative for charges and positive for rebates; app convention is opposite.
    maker = -_rate(item.get("maker"), "maker")
    taker = -_rate(item.get("taker"), "taker")
    return maker, taker, "OKX rates normalized: positive is cost, negative is rebate", taker, taker


def _mexc(symbol: str) -> tuple[Decimal, Decimal, str, Decimal, Decimal]:
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        raise RuntimeError("Не заданы ключи MEXC для чтения торговой комиссии")
    payload = _signed_query(
        "https://api.mexc.com/api/v3/tradeFee",
        {"symbol": symbol, "timestamp": int(time.time() * 1000)},
        MEXC_API_SECRET,
        api_key=MEXC_API_KEY,
        header_name="X-MEXC-APIKEY",
    )
    if str(payload.get("code")) not in {"0", "200"}:
        raise RuntimeError(f"MEXC вернул ошибку {payload.get('code')}: {payload.get('msg')}")
    data = payload.get("data") or {}
    maker = _rate(data.get("makerCommission"), "maker")
    taker = _rate(data.get("takerCommission"), "taker")
    return maker, taker, "", taker, taker


_FETCHERS = {"binance": _binance, "bybit": _bybit, "okx": _okx, "mexc": _mexc}


def get_trade_fee_rates(exchange: str, market_pair: str) -> TradeFeeRates:
    """Получить maker/taker ставки аккаунта для спотовой торговой пары."""
    exchange_slug = exchange.strip().lower()
    if exchange_slug not in _FETCHERS:
        raise ValueError(f"Биржа {exchange!r} не поддерживает получение торговой комиссии")
    _, _, symbol = _symbol(market_pair, exchange_slug)
    cache_key = (exchange_slug, symbol)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] <= FEE_CACHE_TTL_SECONDS:
            return cached[1]

    try:
        maker, taker, details, buy_taker, sell_taker = _FETCHERS[exchange_slug](symbol)
        source = "api"
    except Exception as error:
        error_details = _api_error_details(error)
        configured = CONFIGURED_TAKER_FEES.get(exchange_slug)
        if configured is None:
            raise RuntimeError(
                f"Нет комиссии {exchange_slug} {symbol}: {error_details}; "
                "настроенная резервная ставка отсутствует"
            ) from error
        maker = taker = buy_taker = sell_taker = configured
        details = f"Использована настроенная ставка; ошибка API: {error_details}"
        source = "config"
    result = TradeFeeRates(
        exchange=exchange_slug,
        symbol=symbol,
        maker_rate=maker,
        taker_rate=taker,
        checked_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        details=details,
        source=source,
        buy_taker_rate=buy_taker,
        sell_taker_rate=sell_taker,
    )
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), result)
    return result
