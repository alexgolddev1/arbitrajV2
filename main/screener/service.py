"""Reusable market refresh function and periodic worker."""

from __future__ import annotations

import logging

from database import (
    connect_db,
    delete_markets_outside_volume,
    initialize_database,
    read_config,
    reset_market_opportunities,
    save_markets,
)
from config import MAX_VOLUME_USD, MIN_VOLUME_USD

from .coinmarketcap import fetch_page, market_values
from .filter import filter_markets

logger = logging.getLogger(__name__)


def update_market_data() -> int:
    """Fetch the configured CoinMarketCap range and upsert it into MySQL."""
    connection = connect_db()
    try:
        initialize_database(connection)
        reset_count = reset_market_opportunities(connection)
        if reset_count:
            logger.debug("Сброшены флаги показа возможностей: %s", reset_count)
        config = read_config(connection)
        start = int(config["start"])
        stop = int(config["stop"])
        page_size = int(config["limit"])
        found = 0
        selected = 0
        stored = 0
        filtered_out = 0

        while start <= stop:
            limit = min(page_size, stop - start + 1)
            logger.debug(
                "Запрос %s %s: start=%s, limit=%s",
                config["slug"], config["category"], start, limit,
            )
            payload = fetch_page(config, start, limit)
            markets = payload["data"]["marketPairs"]
            found += len(markets)
            selected_markets = filter_markets(markets)
            selected += len(selected_markets)
            filtered_out += len(markets) - len(selected_markets)
            rows = [
                market_values(market)
                for market in selected_markets
                if market.get("marketId") is not None
            ]
            stored += save_markets(connection, rows)
            logger.debug("Сохранено за страницу: %s; всего обновлено: %s", len(rows), stored)
            if len(markets) < limit:
                break
            start += limit

        deleted = delete_markets_outside_volume(
            connection,
            MIN_VOLUME_USD,
            MAX_VOLUME_USD,
        )
        logger.info(
            "Обновление завершено: получено %s, прошло фильтр объёма %s, "
            "отфильтровано %s, записано в БД %s, удалено устаревших %s",
            found, selected, filtered_out, stored, deleted,
        )
        return stored
    finally:
        connection.close()
