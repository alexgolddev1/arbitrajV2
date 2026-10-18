"""Read deposit/withdraw network availability from exchange wallet APIs.

Run with: python -m main.treider.exchange_networks USDT
API credentials are read from environment variables and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
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
    TRANSFER_NETWORK_MAP,
)
from main.treider.config import (
    ALLOW_DISABLED_ROUTE_DEMO,
    ALLOW_UNCONFIRMED_ROUTE_DEMO,
    FIXED_TRANSFER_SECONDS,
    TRANSFER_FEE_AMOUNT,
    TRANSFER_FEE_CURRENCY,
    TRANSFER_NETWORK,
)
from main.treider.tran_emulation import TransferTimeEmulator

TIMEOUT_SECONDS = 20
EXCHANGES = ("binance", "bybit", "okx", "mexc")
NETWORK_ALIASES = {
    "TRON": ("TRON", "TRC20", "TRC-20", "TRX"),
    "BSC": ("BSC", "BEP20", "BEP-20", "BNB SMART", "BNB CHAIN", "BEP20BSC"),
    "SOLANA": ("SOLANA", "SOL", "SPL"),
    "TON": ("TON", "TONCOIN", "THE OPEN NETWORK"),
    "ETHEREUM": ("ETHEREUM", "ERC20", "ERC-20", "ETH"),
    "ARBITRUM": ("ARBITRUM", "ARBITRUM ONE", "ARBITRUMONE", "ARB", "ARBITRUM-ONE"),
    "BITCOIN": ("BITCOIN", "BTC"),
    "ONTOLOGY": ("ONTOLOGY", "ONT"),
}


def _hmac_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _request_json(url: str, *, headers: dict[str, str], params: dict[str, Any]) -> Any:
    response = requests.get(
        url, headers=headers, params=params, timeout=TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json()


def _network_record(
    exchange: str,
    currency: str,
    network_code: Any,
    network_name: Any,
    deposit_enabled: Any,
    withdraw_enabled: Any,
    *,
    fee: Any = None,
    min_deposit: Any = None,
    min_withdraw: Any = None,
    contract_address: Any = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def enabled_flag(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "enabled", "enable"}:
            return True
        if normalized in {"0", "false", "no", "disabled", "disable"}:
            return False
        return None

    return {
        "exchange": exchange,
        "currency": currency.upper(),
        "network_code": str(network_code or ""),
        "network_name": str(network_name or network_code or ""),
        "deposit_enabled": enabled_flag(deposit_enabled),
        "withdraw_enabled": enabled_flag(withdraw_enabled),
        "withdraw_fee": fee,
        "min_deposit": min_deposit,
        "min_withdraw": min_withdraw,
        "contract_address": contract_address,
        "raw": raw or {},
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _binance(currency: str) -> list[dict[str, Any]]:
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise RuntimeError("Не заданы BINANCE_API_KEY и BINANCE_API_SECRET")
    params: dict[str, Any] = {"timestamp": int(time.time() * 1000)}
    query = urlencode(params)
    params["signature"] = _hmac_hex(BINANCE_API_SECRET, query)
    payload = _request_json(
        "https://api.binance.com/sapi/v1/capital/config/getall",
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        params=params,
    )
    if not isinstance(payload, list):
        message = payload.get("msg", payload) if isinstance(payload, dict) else payload
        raise RuntimeError(f"Binance вернул ошибку: {message}")
    coin = next((item for item in payload if item.get("coin", "").upper() == currency), None)
    if coin is None:
        return []
    return [
        _network_record(
            "binance", currency, net.get("network"), net.get("name"),
            net.get("depositEnable"), net.get("withdrawEnable"),
            fee=net.get("withdrawFee"), min_deposit=net.get("depositDust"),
            min_withdraw=net.get("withdrawMin"), contract_address=net.get("contract"),
            raw=net,
        )
        for net in coin.get("networkList", [])
    ]


def _bybit(currency: str) -> list[dict[str, Any]]:
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        raise RuntimeError("Не заданы BYBIT_API_KEY и BYBIT_API_SECRET")
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    query = urlencode({"coin": currency})
    signature = _hmac_hex(
        BYBIT_API_SECRET, timestamp + BYBIT_API_KEY + recv_window + query
    )
    payload = _request_json(
        "https://api.bybit.com/v5/asset/coin/query-info",
        headers={
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        },
        params={"coin": currency},
    )
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit вернул ошибку {payload.get('retCode')}: {payload.get('retMsg')}")
    rows = payload.get("result", {}).get("rows", [])
    coin = next((item for item in rows if item.get("coin", "").upper() == currency), None)
    if coin is None:
        return []
    return [
        _network_record(
            "bybit", currency, net.get("chain"), net.get("chainType"),
            str(net.get("chainDeposit", "0")) == "1",
            str(net.get("chainWithdraw", "0")) == "1",
            fee=net.get("withdrawFee"), min_deposit=net.get("depositMin"),
            min_withdraw=net.get("withdrawMin"), contract_address=net.get("contractAddress"),
            raw=net,
        )
        for net in coin.get("chains", [])
    ]


def _okx(currency: str) -> list[dict[str, Any]]:
    if not (OKX_API_KEY and OKX_API_SECRET and OKX_API_PASSPHRASE):
        raise RuntimeError(
            "Для OKX задайте OKX_API_KEY, OKX_API_SECRET и OKX_API_PASSPHRASE"
        )
    path = "/api/v5/asset/currencies"
    params = {"ccy": currency}
    request_path = path + "?" + urlencode(params)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    message = timestamp + "GET" + request_path
    signature = base64.b64encode(
        hmac.new(OKX_API_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    payload = _request_json(
        "https://www.okx.com" + path,
        headers={
            "OK-ACCESS-KEY": OKX_API_KEY,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        },
        params=params,
    )
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX вернул ошибку {payload.get('code')}: {payload.get('msg')}")
    return [
        _network_record(
            "okx", currency, net.get("chain"), net.get("chain"),
            net.get("canDep"), net.get("canWd"), fee=net.get("fee"),
            min_deposit=net.get("minDep"), min_withdraw=net.get("minWd"),
            contract_address=net.get("ctAddr"), raw=net,
        )
        for net in payload.get("data", [])
        if net.get("ccy", "").upper() == currency
    ]


def _mexc(currency: str) -> list[dict[str, Any]]:
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        raise RuntimeError("Не заданы MEXC_API_KEY и MEXC_API_SECRET")
    params: dict[str, Any] = {"timestamp": int(time.time() * 1000)}
    query = urlencode(params)
    params["signature"] = _hmac_hex(MEXC_API_SECRET, query)
    payload = _request_json(
        "https://api.mexc.com/api/v3/capital/config/getall",
        headers={"X-MEXC-APIKEY": MEXC_API_KEY},
        params=params,
    )
    if not isinstance(payload, list):
        message = payload.get("msg", payload) if isinstance(payload, dict) else payload
        raise RuntimeError(f"MEXC вернул ошибку: {message}")
    coin = next((item for item in payload if item.get("coin", "").upper() == currency), None)
    if coin is None:
        return []
    return [
        _network_record(
            "mexc", currency, net.get("netWork") or net.get("network"),
            net.get("name"), net.get("depositEnable"), net.get("withdrawEnable"),
            fee=net.get("withdrawFee"), min_deposit=net.get("depositMin"),
            min_withdraw=net.get("withdrawMin"), contract_address=net.get("contract"),
            raw=net,
        )
        for net in coin.get("networkList", [])
    ]


_FETCHERS = {
    "binance": _binance,
    "bybit": _bybit,
    "okx": _okx,
    "mexc": _mexc,
}


def get_exchange_networks(
    currency: str, exchanges: tuple[str, ...] = EXCHANGES
) -> dict[str, Any]:
    """Fetch transfer network details for one currency from selected exchanges.

    Each exchange is independent: missing keys or API errors are included under
    ``errors`` while successful exchange results remain available.
    """
    symbol = currency.strip().upper()
    if not symbol:
        raise ValueError("Укажите тикер валюты, например USDT")
    result: dict[str, Any] = {"currency": symbol, "networks": {}, "errors": {}}
    for exchange in exchanges:
        normalized_exchange = exchange.strip().lower()
        fetcher = _FETCHERS.get(normalized_exchange)
        if fetcher is None:
            result["errors"][normalized_exchange] = "Биржа не поддерживается"
            continue
        try:
            result["networks"][normalized_exchange] = fetcher(symbol)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            result["errors"][normalized_exchange] = str(exc)
    return result


def _network_profile(record: dict[str, Any]) -> str:
    """Map labels and compound API names without confusing different chains.

    Exchange APIs commonly return labels such as ``Ethereum (ERC20)`` or
    ``MEME-ERC20``. Match known aliases as complete alphanumeric tokens/phrases,
    then reject labels that contain aliases from more than one chain (for
    example ``ETH-ARB``).
    """
    def profile_for(value: Any) -> str | None:
        label_tokens = re.findall(r"[A-Z0-9]+", str(value or "").upper())
        if not label_tokens:
            return None
        label = f" {' '.join(label_tokens)} "
        matches = {
            profile
            for profile, aliases in NETWORK_ALIASES.items()
            if any(
                f" {' '.join(re.findall(r'[A-Z0-9]+', alias.upper()))} " in label
                for alias in aliases
            )
        }
        return next(iter(matches)) if len(matches) == 1 else None

    code_profile = profile_for(record.get("network_code"))
    name_profile = profile_for(record.get("network_name"))
    if code_profile and name_profile and code_profile != name_profile:
        identity = "".join(
            character for character in f"{record.get('network_code')}:{record.get('network_name')}".upper()
            if character.isalnum()
        )
        return f"UNKNOWN:{identity}"
    if code_profile or name_profile:
        return code_profile or name_profile  # Prefer the exchange's network code.
    identity = "".join(
        character for character in str(record.get("network_code") or record.get("network_name") or "UNKNOWN").upper()
        if character.isalnum()
    )
    return f"UNKNOWN:{identity}"


def _enabled_record(records: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    """Prefer an explicitly enabled chain when an exchange reports aliases twice."""
    return next((item for item in records if item.get(field) is True), records[0] if records else None)


def build_transfer_routes(
    currency: str,
    source_exchange: str,
    destination_exchange: str,
    exchange_data: dict[str, Any],
    *,
    emulator: TransferTimeEmulator | None = None,
    configured_profile: str | None = None,
) -> list[dict[str, Any]]:
    """Build explicit shared routes; only use configured assumptions when allowed."""
    source = source_exchange.strip().lower()
    destination = destination_exchange.strip().lower()
    all_networks = exchange_data.get("networks", {})
    source_rows = all_networks.get(source, [])
    destination_rows = all_networks.get(destination, [])
    source_by_profile: dict[str, list[dict[str, Any]]] = {}
    destination_by_profile: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        profile = _network_profile(row)
        if not profile.startswith("UNKNOWN:"):
            source_by_profile.setdefault(profile, []).append(row)
    for row in destination_rows:
        profile = _network_profile(row)
        if not profile.startswith("UNKNOWN:"):
            destination_by_profile.setdefault(profile, []).append(row)

    time_emulator = emulator or TransferTimeEmulator()
    routes = []
    profiles = sorted(set(source_by_profile) & set(destination_by_profile))
    for profile in profiles:
        source_record = _enabled_record(source_by_profile.get(profile, []), "withdraw_enabled")
        destination_record = _enabled_record(destination_by_profile.get(profile, []), "deposit_enabled")
        unavailable = bool(
            source_record and source_record.get("withdraw_enabled") is False
            or destination_record and destination_record.get("deposit_enabled") is False
        )
        if unavailable and not ALLOW_DISABLED_ROUTE_DEMO:
            continue
        source_assumed = destination_assumed = False
        exemplar = source_record or destination_record or {}
        source_contract = str((source_record or {}).get("contract_address") or "").strip().lower()
        destination_contract = str((destination_record or {}).get("contract_address") or "").strip().lower()
        identity_verified = bool(source_contract and destination_contract and source_contract == destination_contract)
        route_status = "disabled" if unavailable else "confirmed" if identity_verified else "unconfirmed"
        network_name = profile if not profile.startswith("UNKNOWN:") else (
            exemplar.get("network_name") or exemplar.get("network_code") or "UNKNOWN"
        )
        if route_status == "unconfirmed" and not ALLOW_UNCONFIRMED_ROUTE_DEMO:
            continue
        routes.append({
            "currency": currency.strip().upper(),
            "source_exchange": source,
            "destination_exchange": destination,
            "network_profile": profile,
            "network_name": network_name,
            "withdraw_enabled": source_record.get("withdraw_enabled"),
            "deposit_enabled": destination_record.get("deposit_enabled"),
            "source_assumed": source_assumed,
            "destination_assumed": destination_assumed,
            "assumed": route_status != "confirmed",
            "route_status": route_status,
            "identity_verified": identity_verified,
            "withdraw_fee": (source_record or {}).get("withdraw_fee"),
            "withdraw_fee_currency": currency.strip().upper(),
            "fee_required": (source_record or {}).get("withdraw_fee") is not None,
            "estimated_seconds": time_emulator.get_emulated_duration(profile),
            "time_model": "seeded_random" if time_emulator.random_mode else "fixed",
            "random_seed": time_emulator.seed,
            "fixed_delay_seconds": str(FIXED_TRANSFER_SECONDS),
            "source_network": source_record,
            "destination_network": destination_record,
        })
    requested_profile = (configured_profile or "").strip().upper()
    if requested_profile and requested_profile not in {route["network_profile"] for route in routes}:
        explicit_disabled = any(
            _network_profile(row) == requested_profile
            and (row.get("withdraw_enabled") is False or row.get("deposit_enabled") is False)
            for row in source_rows + destination_rows
        )
        if explicit_disabled and not ALLOW_DISABLED_ROUTE_DEMO:
            # A configured network is a constraint, not merely a preference.
            # Never silently route through a different chain when it is disabled.
            return []
        if (
            explicit_disabled and ALLOW_DISABLED_ROUTE_DEMO
            or not explicit_disabled and ALLOW_UNCONFIRMED_ROUTE_DEMO
        ):
            source_record = _enabled_record(source_by_profile.get(requested_profile, []), "withdraw_enabled")
            destination_record = _enabled_record(destination_by_profile.get(requested_profile, []), "deposit_enabled")
            source_contract = str((source_record or {}).get("contract_address") or "").strip().lower()
            destination_contract = str((destination_record or {}).get("contract_address") or "").strip().lower()
            identity_verified = bool(source_contract and destination_contract and source_contract == destination_contract)
            actual_fee = (source_record or {}).get("withdraw_fee")
            configured_fee = str(TRANSFER_FEE_AMOUNT) if TRANSFER_FEE_AMOUNT is not None else None
            routes.append({
                "currency": currency.strip().upper(), "source_exchange": source,
                "destination_exchange": destination, "network_profile": requested_profile,
                "network_name": requested_profile,
                "withdraw_enabled": (source_record or {}).get("withdraw_enabled"),
                "deposit_enabled": (destination_record or {}).get("deposit_enabled"),
                "source_assumed": source_record is None,
                "destination_assumed": destination_record is None,
                "assumed": not identity_verified,
                "route_status": "disabled" if explicit_disabled else "confirmed" if identity_verified else "unconfirmed",
                "identity_verified": identity_verified,
                "withdraw_fee": actual_fee if actual_fee is not None else configured_fee,
                "withdraw_fee_currency": (
                    currency.strip().upper() if actual_fee is not None
                    else TRANSFER_FEE_CURRENCY or currency.strip().upper()
                ),
                "fee_required": actual_fee is not None or configured_fee is not None,
                "estimated_seconds": time_emulator.get_emulated_duration(requested_profile),
                "time_model": "seeded_random" if time_emulator.random_mode else "fixed",
                "random_seed": time_emulator.seed,
                "fixed_delay_seconds": str(FIXED_TRANSFER_SECONDS),
                "source_network": source_record, "destination_network": destination_record,
            })
    return routes


def get_transfer_routes(
    currency: str,
    source_exchange: str,
    destination_exchange: str,
) -> dict[str, Any]:
    """Fetch both exchanges and estimate routes, assuming a missing side exists."""
    source = source_exchange.strip().lower()
    destination = destination_exchange.strip().lower()
    exchange_data = get_exchange_networks(currency, (source, destination))
    profile = TRANSFER_NETWORK_MAP.get(currency.strip().upper()) or TRANSFER_NETWORK or None
    return {
        "currency": currency.strip().upper(),
        "source_exchange": source,
        "destination_exchange": destination,
        "networks": exchange_data["networks"],
        "routes": build_transfer_routes(
            currency, source, destination, exchange_data, configured_profile=profile
        ),
        "errors": exchange_data["errors"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Получить сети и статусы ввода/вывода валюты")
    parser.add_argument("currency", help="Тикер валюты, например USDT")
    parser.add_argument(
        "--exchanges", default=",".join(EXCHANGES),
        help="Список бирж через запятую: binance,bybit,okx,mexc",
    )
    parser.add_argument("--route-from", choices=EXCHANGES, help="Биржа отправления")
    parser.add_argument("--route-to", choices=EXCHANGES, help="Биржа получения")
    args = parser.parse_args()
    if args.route_from or args.route_to:
        if not (args.route_from and args.route_to):
            parser.error("Для расчёта маршрута задайте одновременно --route-from и --route-to")
        result = get_transfer_routes(args.currency, args.route_from, args.route_to)
    else:
        result = get_exchange_networks(
            args.currency,
            tuple(item.strip() for item in args.exchanges.split(",") if item.strip()),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
