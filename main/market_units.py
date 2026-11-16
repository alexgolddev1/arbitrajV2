"""Ручные соответствия биржевых лотов базовой валюте скринера."""

from __future__ import annotations

import json
import os
import re
import threading
from functools import lru_cache
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "market_unit_mappings.json"
_WRITE_LOCK = threading.Lock()
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,40}$")


def _mapping_key(
    exchange: str, source_slug: str, source_symbol: str, market_symbol: str,
) -> tuple[str, str, str, str]:
    return (
        exchange.strip().lower(), source_slug.strip().lower(),
        source_symbol.strip().upper(), market_symbol.strip().upper(),
    )


@lru_cache(maxsize=16)
def _read_market_unit_mappings(path_text: str, mtime_ns: int, file_size: int) -> tuple[dict[str, str], ...]:
    path = Path(path_text)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("mappings"), list):
        raise ValueError("Некорректный формат config/market_unit_mappings.json")
    result: list[dict[str, str]] = []
    for item in data["mappings"]:
        if not isinstance(item, dict):
            continue
        try:
            exchange, slug, source, market = _mapping_key(
                str(item["exchange"]), str(item.get("source_slug", "")),
                str(item["source_symbol"]), str(item["market_symbol"]),
            )
            scale = Decimal(str(item["units_per_market_unit"]))
        except (KeyError, InvalidOperation, ValueError):
            continue
        if not exchange or not _SYMBOL_RE.fullmatch(source) or not _SYMBOL_RE.fullmatch(market):
            continue
        if not scale.is_finite() or scale <= 0:
            continue
        result.append({
            "exchange": exchange,
            "source_slug": slug,
            "source_symbol": source,
            "market_symbol": market,
            "units_per_market_unit": str(scale),
        })
    return tuple(result)


def load_market_unit_mappings(path: Path = CONFIG_PATH) -> list[dict[str, str]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return []
    return [dict(item) for item in _read_market_unit_mappings(str(path), stat.st_mtime_ns, stat.st_size)]


def get_market_unit_scale(
    exchange: str, source_slug: str, source_symbol: str, market_symbol: str,
    path: Path = CONFIG_PATH,
) -> Decimal | None:
    source = source_symbol.strip().upper()
    market = market_symbol.strip().upper()
    if source and source == market:
        return Decimal(1)
    key = _mapping_key(exchange, source_slug, source, market)
    for mapping in load_market_unit_mappings(path):
        if _mapping_key(
            mapping["exchange"], mapping["source_slug"],
            mapping["source_symbol"], mapping["market_symbol"],
        ) == key:
            return Decimal(mapping["units_per_market_unit"])
    return None


def suggest_market_unit_scale(source_symbol: str, market_symbol: str) -> Decimal | None:
    """Suggest a likely lot factor for the confirmation form; never applies it."""
    source = source_symbol.strip().upper()
    market = market_symbol.strip().upper()
    if source and market and source != market and source.endswith(market):
        inverse = suggest_market_unit_scale(market, source)
        return Decimal(1) / inverse if inverse else None
    if not source or not market.endswith(source) or market == source:
        return None
    prefix = market[:-len(source)]
    match = re.fullmatch(r"(\d+)([KMB]?)", prefix)
    if not match:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    scale = Decimal(match.group(1)) * multiplier
    return scale if scale > 0 else None


def save_market_unit_mapping(
    *, exchange: str, source_slug: str, source_symbol: str, market_symbol: str,
    units_per_market_unit: str, path: Path = CONFIG_PATH,
) -> None:
    exchange, slug, source, market = _mapping_key(exchange, source_slug, source_symbol, market_symbol)
    if not exchange or not _SYMBOL_RE.fullmatch(source) or not _SYMBOL_RE.fullmatch(market):
        raise ValueError("Некорректная биржа или тикер для соответствия")
    try:
        scale = Decimal(units_per_market_unit.strip())
    except (InvalidOperation, AttributeError) as error:
        raise ValueError("Коэффициент должен быть положительным числом") from error
    if not scale.is_finite() or scale <= 0 or not -18 <= scale.adjusted() <= 18:
        raise ValueError("Коэффициент должен быть конечным числом в диапазоне от 1e-18 до 1e18")

    with _WRITE_LOCK:
        mappings = load_market_unit_mappings(path)
        key = (exchange, slug, source, market)
        updated = {
            "exchange": exchange,
            "source_slug": slug,
            "source_symbol": source,
            "market_symbol": market,
            "units_per_market_unit": str(scale),
        }
        existing = [item for item in mappings if _mapping_key(
            item["exchange"], item["source_slug"], item["source_symbol"], item["market_symbol"]
        ) != key]
        existing.append(updated)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "mappings": existing}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        _read_market_unit_mappings.cache_clear()


def remove_market_unit_mapping(
    *, exchange: str, source_slug: str, source_symbol: str, market_symbol: str,
    path: Path = CONFIG_PATH,
) -> None:
    key = _mapping_key(exchange, source_slug, source_symbol, market_symbol)
    with _WRITE_LOCK:
        mappings = [item for item in load_market_unit_mappings(path) if _mapping_key(
            item["exchange"], item["source_slug"], item["source_symbol"], item["market_symbol"]
        ) != key]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "mappings": mappings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        _read_market_unit_mappings.cache_clear()
