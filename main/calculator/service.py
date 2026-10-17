"""Связать исходные пары скринера с ценами выбранных бирж."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
from typing import Any

from config import (
    CALCULATOR_CATEGORY,
    CALCULATOR_EXCHANGES,
    CALCULATOR_MIN_SPREAD_PERCENT,
    CALCULATOR_REQUEST_DELAY_SECONDS,
    CALCULATOR_WORKERS,
)
from database import (
    calculate_market_opportunities,
    connect_db,
    get_calculator_sources,
    initialize_database,
    replace_calculator_market_pairs,
)

from .coinmarketcap import fetch_currency_markets

logger = logging.getLogger(__name__)


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _row(source_market_id: int, market: dict[str, Any]) -> tuple[Any, ...]:
    return (
        source_market_id,
        market["marketId"],
        market.get("rank"),
        market.get("exchangeId"),
        market.get("exchangeName"),
        str(market.get("exchangeSlug", "")).lower(),
        market.get("marketPair") or "",
        market.get("category"),
        market.get("marketUrl"),
        market.get("baseSymbol"),
        market.get("quoteSymbol"),
        _decimal(market.get("price")),
        _decimal(market.get("volumeUsd")),
        _decimal(market.get("openInterestUsd")),
        _decimal(market.get("fundingRate")),
        _decimal(market.get("indexPrice")),
        _decimal(market.get("indexBasis")),
        _datetime(market.get("lastUpdated")),
    )


def update_calculator_prices() -> int:
    """Обновить цены только для бирж из CALCULATOR_EXCHANGES."""
    exchanges = {exchange.strip().lower() for exchange in CALCULATOR_EXCHANGES if exchange.strip()}
    if not exchanges:
        raise ValueError("В CALCULATOR_EXCHANGES не указаны биржи")

    connection = connect_db()
    try:
        initialize_database(connection)
        sources = get_calculator_sources(connection)
        rows_by_source: dict[int, list[tuple[Any, ...]]] = {}

        sources_by_slug: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            slug = str(source["base_currency_slug"]).strip().lower()
            if slug:
                sources_by_slug.setdefault(slug, []).append(source)

        api_rows = 0
        matched_rows = 0
        failed_slugs: list[str] = []
        total_slugs = len(sources_by_slug)
        if not 1 <= CALCULATOR_WORKERS <= 8:
            raise ValueError("CALCULATOR_WORKERS должен быть от 1 до 8")
        if CALCULATOR_REQUEST_DELAY_SECONDS < 0:
            raise ValueError("CALCULATOR_REQUEST_DELAY_SECONDS не может быть отрицательным")

        slug_items = list(sources_by_slug.items())
        logger.debug(
            "Запуск запросов для %s валют, одновременно: %s",
            total_slugs, CALCULATOR_WORKERS,
        )
        completed = 0
        with ThreadPoolExecutor(max_workers=CALCULATOR_WORKERS) as executor:
            futures = {}
            for slug, slug_sources in slug_items:
                future = executor.submit(fetch_currency_markets, slug, exchanges)
                futures[future] = (slug, slug_sources)

            for future in as_completed(futures):
                completed += 1
                slug, slug_sources = futures[future]
                try:
                    markets = future.result()
                except Exception:
                    failed_slugs.append(slug)
                    logger.exception(
                        "Не удалось получить рынки для %s (%s/%s); "
                        "сохраняю прежние данные и продолжаю",
                        slug, completed, total_slugs,
                    )
                    continue

                api_rows += len(markets)
                selected = [
                    market for market in markets
                    if str(market.get("exchangeSlug", "")).lower() in exchanges
                    and str(market.get("category", "")).strip().lower()
                    == CALCULATOR_CATEGORY.strip().lower()
                ]
                logger.debug(
                    "Валюта %s: получено рынков %s, на выбранных биржах %s "
                    "(завершено %s/%s)",
                    slug, len(markets), len(selected), completed, total_slugs,
                )

                for source in slug_sources:
                    source_id = int(source["market_id"])
                    quote_symbol = str(source["quote_symbol"]).strip().upper()
                    matches = [
                        market for market in selected
                        if str(market.get("quoteSymbol", "")).strip().upper() == quote_symbol
                        and market.get("marketId") is not None
                    ]
                    rows = [_row(source_id, market) for market in matches]
                    rows_by_source[source_id] = rows
                    matched_rows += len(rows)

        saved = replace_calculator_market_pairs(connection, rows_by_source)
        flagged = calculate_market_opportunities(
            connection,
            CALCULATOR_MIN_SPREAD_PERCENT,
            list(rows_by_source),
        )
        logger.info(
            "Калькулятор завершён: исходных записей %s, валют %s, "
            "получено рынков API %s, совпадений по биржам и котируемой валюте %s, "
            "записано %s, отмечено для показа (разница > %s%%): %s, "
            "ошибок по валютам: %s",
            len(sources), len(sources_by_slug), api_rows, matched_rows, saved,
            CALCULATOR_MIN_SPREAD_PERCENT, flagged, len(failed_slugs),
        )
        return saved
    finally:
        connection.close()
