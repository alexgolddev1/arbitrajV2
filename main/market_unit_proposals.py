"""Предложения единиц рынка: справочные источники и проверка метаданных API."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from main.market_units import _mapping_key, suggest_market_unit_scale

REFERENCE_PATH = Path(__file__).resolve().parents[1] / "config" / "market_unit_references.json"
CACHE_PATH = REFERENCE_PATH.with_name("market_unit_proposals.json")
MAX_AGE = timedelta(hours=6)


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return _mapping_key(item["exchange"], item["source_slug"], item["source_symbol"], item["market_symbol"])


def _fresh(timestamp: str | None, now: datetime) -> bool:
    try:
        age = now - datetime.fromisoformat(timestamp or "").astimezone(timezone.utc)
        return timedelta(0) <= age < MAX_AGE
    except (ValueError, TypeError):
        return False


def load_proposals(
    reference_path: Path = REFERENCE_PATH, cache_path: Path = CACHE_PATH,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Возвращает подсказки; никогда не записывает подтверждённые соответствия."""
    now = datetime.now(timezone.utc)
    cached = {_key(row): row for row in _read(cache_path).get("proposals", [])}
    result = {}
    for reference in _read(reference_path).get("references", []):
        row = dict(reference)
        observation = cached.get(_key(row))
        if observation:
            row.update({name: observation.get(name) for name in (
                "api_status", "api_checked_at_utc", "networks",
            )})
            if not _fresh(row.get("api_checked_at_utc"), now):
                row["api_status"] = "stale"
        result[_key(row)] = row
    return result


def propose_market_unit(
    exchange: str, source_slug: str, source_symbol: str, market_symbol: str,
    references: dict[tuple[str, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    row = references.get(_mapping_key(exchange, source_slug, source_symbol, market_symbol))
    if row:
        return dict(row)
    scale = suggest_market_unit_scale(source_symbol, market_symbol)
    return {
        "units_per_market_unit": str(scale) if scale else "",
        "reason": "Числовой префикс: требуется проверка актива" if scale else "Недостаточно данных для предложения",
        "sources": [], "api_status": "unverified",
    }


def refresh_market_unit_proposals(
    reference_path: Path = REFERENCE_PATH, cache_path: Path = CACHE_PATH,
    *, now: datetime | None = None,
) -> None:
    """Периодически читает только метаданные кошельков, сохраняя безопасные поля."""
    from main.treider.exchange_networks import get_exchange_networks, _network_profile

    now = now or datetime.now(timezone.utc)
    old = {_key(row): row for row in _read(cache_path).get("proposals", [])}
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    output = []
    for reference in _read(reference_path).get("references", []):
        prior = old.get(_key(reference))
        if prior and _fresh(prior.get("api_checked_at_utc"), now):
            output.append(prior)
            continue
        exchange = reference["exchange"]
        symbol = reference["market_symbol"]
        api_key = (exchange, symbol)
        if api_key not in observations:
            try:
                observations[api_key] = get_exchange_networks(symbol, (exchange,))
            except Exception:
                observations[api_key] = {"errors": {exchange: "API недоступен"}}
        payload = observations[api_key]
        networks = [
            {"network": _network_profile(item), "contract": str(item.get("contract_address") or "")}
            for item in payload.get("networks", {}).get(exchange, [])
        ]
        status = "metadata_only" if networks else "unavailable"
        expected_contract = reference.get("contract")
        expected_network = reference.get("network")
        comparable = [item for item in networks if item["network"] == expected_network and item["contract"]]
        if expected_contract and comparable:
            def address(value: str) -> str:
                return value.lower() if value.startswith("0x") else value
            status = "matched" if any(address(item["contract"]) == address(expected_contract) for item in comparable) else "conflict"
        if payload.get("errors", {}).get(exchange):
            status = "unavailable"
        output.append({
            **{key: reference[key] for key in ("exchange", "source_slug", "source_symbol", "market_symbol")},
            "api_status": status, "api_checked_at_utc": now.isoformat(timespec="seconds"),
            "networks": networks,
        })
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"version": 1, "proposals": output}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, cache_path)


if __name__ == "__main__":
    refresh_market_unit_proposals()
