"""Shared MySQL access, schema initialization, config and market persistence."""

from __future__ import annotations

import time
import re
import logging
from datetime import datetime
from pathlib import Path
from decimal import Decimal
from typing import Any

import mysql.connector
from mysql.connector import MySQLConnection
from config import (
    DB_CONNECT_RETRIES,
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_RETRY_DELAY_SECONDS,
    DB_USER,
    DEFAULT_CONFIG,
)

logger = logging.getLogger(__name__)

MARKET_COLUMNS = (
    "market_id, rank_no, exchange_id, exchange_name, exchange_slug, market_pair, "
    "category, market_url, base_symbol, base_currency_id, base_currency_name, "
    "base_currency_slug, quote_symbol, quote_currency_id, price, volume_usd, "
    "effective_liquidity, last_updated, quote_price, volume_base, volume_quote, "
    "quote_metric_id, quote_volume_24h, quote_depth_positive_two, quote_depth_negative_two, "
    "fee_type, depth_usd_negative_two, depth_usd_positive_two, volume_percent, "
    "market_score, market_reputation, outlier_detected, price_excluded, "
    "volume_excluded, outlier_disp, price_ex_disp, vol_ex_disp, is_verified, "
    "market_type, hide_stars_market"
)

MARKET_INSERT = f"""
INSERT INTO market_pairs ({MARKET_COLUMNS})
VALUES ({', '.join(['%s'] * 40)})
ON DUPLICATE KEY UPDATE
    rank_no=VALUES(rank_no), exchange_id=VALUES(exchange_id),
    exchange_name=VALUES(exchange_name), exchange_slug=VALUES(exchange_slug),
    market_pair=VALUES(market_pair), category=VALUES(category), market_url=VALUES(market_url),
    base_symbol=VALUES(base_symbol), base_currency_id=VALUES(base_currency_id),
    base_currency_name=VALUES(base_currency_name), base_currency_slug=VALUES(base_currency_slug),
    quote_symbol=VALUES(quote_symbol), quote_currency_id=VALUES(quote_currency_id),
    price=VALUES(price), volume_usd=VALUES(volume_usd),
    effective_liquidity=VALUES(effective_liquidity), last_updated=VALUES(last_updated),
    quote_price=VALUES(quote_price), volume_base=VALUES(volume_base),
    volume_quote=VALUES(volume_quote), quote_metric_id=VALUES(quote_metric_id),
    quote_volume_24h=VALUES(quote_volume_24h),
    quote_depth_positive_two=VALUES(quote_depth_positive_two),
    quote_depth_negative_two=VALUES(quote_depth_negative_two), fee_type=VALUES(fee_type),
    depth_usd_negative_two=VALUES(depth_usd_negative_two),
    depth_usd_positive_two=VALUES(depth_usd_positive_two), volume_percent=VALUES(volume_percent),
    market_score=VALUES(market_score), market_reputation=VALUES(market_reputation),
    outlier_detected=VALUES(outlier_detected), price_excluded=VALUES(price_excluded),
    volume_excluded=VALUES(volume_excluded), outlier_disp=VALUES(outlier_disp),
    price_ex_disp=VALUES(price_ex_disp), vol_ex_disp=VALUES(vol_ex_disp),
    is_verified=VALUES(is_verified), market_type=VALUES(market_type),
    hide_stars_market=VALUES(hide_stars_market), fetched_at=CURRENT_TIMESTAMP
"""

CALCULATOR_MARKET_INSERT = """
INSERT INTO calculator_market_pairs (
    source_market_id, market_id, rank_no, exchange_id, exchange_name, exchange_slug,
    market_pair, category, market_url, base_symbol, quote_symbol, price,
    volume_usd, open_interest_usd, funding_rate, index_price, index_basis,
    last_updated
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    rank_no=VALUES(rank_no), exchange_id=VALUES(exchange_id),
    exchange_name=VALUES(exchange_name),
    exchange_slug=VALUES(exchange_slug), market_pair=VALUES(market_pair),
    category=VALUES(category), market_url=VALUES(market_url),
    base_symbol=VALUES(base_symbol), quote_symbol=VALUES(quote_symbol),
    price=VALUES(price), volume_usd=VALUES(volume_usd),
    open_interest_usd=VALUES(open_interest_usd), funding_rate=VALUES(funding_rate),
    index_price=VALUES(index_price), index_basis=VALUES(index_basis),
    last_updated=VALUES(last_updated), fetched_at=CURRENT_TIMESTAMP
"""


def connect_db() -> MySQLConnection:
    settings = {
        "host": DB_HOST,
        "port": DB_PORT,
        "database": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "connection_timeout": DB_CONNECT_TIMEOUT_SECONDS,
    }
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            return mysql.connector.connect(**settings)
        except mysql.connector.Error:
            if attempt == DB_CONNECT_RETRIES:
                raise
            print(
                f"MySQL connection attempt {attempt}/{DB_CONNECT_RETRIES} failed; "
                f"retrying in {DB_RETRY_DELAY_SECONDS} seconds",
                flush=True,
            )
            time.sleep(DB_RETRY_DELAY_SECONDS)
    raise RuntimeError("Could not connect to MySQL")


def initialize_database(connection: MySQLConnection) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    statements = schema_path.read_text(encoding="utf-8").split(";")
    with connection.cursor() as cursor:
        for statement in statements:
            if statement.strip():
                cursor.execute(statement)
        # CREATE TABLE IF NOT EXISTS не обновляет схему уже существующей БД.
        cursor.execute("SHOW COLUMNS FROM market_pairs LIKE 'show_flag'")
        if cursor.fetchone() is None:
            cursor.execute(
                "ALTER TABLE market_pairs ADD COLUMN show_flag TINYINT(1) NOT NULL DEFAULT 0"
            )
        cursor.execute("SHOW COLUMNS FROM market_pairs LIKE 'price_difference_percent'")
        if cursor.fetchone() is None:
            cursor.execute(
                "ALTER TABLE market_pairs "
                "ADD COLUMN price_difference_percent DECIMAL(20, 12) NULL"
            )
        cursor.execute("SHOW COLUMNS FROM calculator_market_pairs LIKE 'rank_no'")
        if cursor.fetchone() is None:
            cursor.execute(
                "ALTER TABLE calculator_market_pairs ADD COLUMN rank_no INT NULL AFTER market_id"
            )
        cursor.execute("SHOW COLUMNS FROM calculator_market_pairs LIKE 'market_id'")
        market_id_column = cursor.fetchone()
        if market_id_column is not None and not str(market_id_column[1]).lower().startswith("decimal"):
            cursor.execute(
                "ALTER TABLE calculator_market_pairs "
                "MODIFY COLUMN market_id DECIMAL(65, 0) NOT NULL"
            )
        cursor.executemany(
            "INSERT IGNORE INTO config (config_key, config_value) VALUES (%s, %s)",
            list(DEFAULT_CONFIG.items()),
        )
    connection.commit()


def reset_market_opportunities(connection: MySQLConnection) -> int:
    """Сбросить отметки показа перед новым запуском скринера."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE market_pairs SET show_flag = 0, price_difference_percent = NULL "
            "WHERE show_flag <> 0 OR price_difference_percent IS NOT NULL"
        )
        reset_count = cursor.rowcount
    connection.commit()
    return reset_count


def read_config(connection: MySQLConnection) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT config_key, config_value FROM config")
        config = dict(cursor.fetchall())
    for key, default in DEFAULT_CONFIG.items():
        config.setdefault(key, default)
    for key in ("start", "stop", "limit", "interval_minutes"):
        config[key] = str(int(config[key]))
    if int(config["start"]) < 1 or int(config["stop"]) < int(config["start"]):
        raise ValueError("config.start must be >= 1 and config.stop must be >= config.start")
    if not 1 <= int(config["limit"]) <= 500:
        raise ValueError("config.limit must be between 1 and 500")
    if int(config["interval_minutes"]) < 1:
        raise ValueError("config.interval_minutes must be >= 1")
    config["includeUntracked"] = str(config["includeUntracked"]).lower()
    return config


def save_markets(connection: MySQLConnection, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(MARKET_INSERT, rows)
    connection.commit()
    return len(rows)


def delete_markets_outside_volume(
    connection: MySQLConnection,
    minimum: Any,
    maximum: Any,
) -> int:
    """Keep only rows whose stored 24-hour USD volume is within the given range."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM market_pairs "
            "WHERE volume_usd IS NULL OR volume_usd < %s OR volume_usd > %s",
            (minimum, maximum),
        )
        deleted = cursor.rowcount
    connection.commit()
    return deleted


def get_calculator_sources(connection: MySQLConnection) -> list[dict[str, Any]]:
    """Return screener rows that have enough data to query CMC by currency slug."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT market_id, base_currency_slug, base_symbol, quote_symbol "
            "FROM market_pairs "
            "WHERE base_currency_slug IS NOT NULL AND base_currency_slug <> '' "
            "AND quote_symbol IS NOT NULL AND quote_symbol <> ''"
        )
        rows = cursor.fetchall()
    return [
        {
            "market_id": row[0],
            "base_currency_slug": row[1],
            "base_symbol": row[2],
            "quote_symbol": row[3],
        }
        for row in rows
    ]


def get_websocket_markets(connection: MySQLConnection) -> list[dict[str, Any]]:
    """Return one spot market per source and exchange for live book monitoring."""
    with connection.cursor(dictionary=True) as cursor:
        cursor.execute(
            "SELECT venue.source_market_id, source.market_pair AS source_pair, "
            "source.base_symbol AS source_base_symbol, source.quote_symbol AS source_quote_symbol, "
            "venue.exchange_slug, venue.exchange_name, venue.market_pair, venue.base_symbol, "
            "venue.quote_symbol, venue.rank_no, venue.category "
            "FROM calculator_market_pairs AS venue "
            "JOIN market_pairs AS source ON source.market_id = venue.source_market_id "
            "WHERE LOWER(venue.category) = 'spot' AND venue.price IS NOT NULL AND venue.price > 0 "
            "ORDER BY venue.source_market_id, venue.exchange_slug, venue.rank_no"
        )
        rows = cursor.fetchall()
    # One instrument per exchange per source pair avoids duplicate comparisons.
    selected: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        key = (int(row["source_market_id"]), str(row["exchange_slug"]).lower())
        selected.setdefault(key, row)
    return list(selected.values())


def record_opportunity_lifetime(
    *,
    lifetime_id: int | None = None,
    source_market_id: int,
    source_pair: str,
    threshold_percent: Decimal,
    spread_percent: Decimal,
    max_spread_percent: Decimal,
    buy_exchange: str,
    buy_market_pair: str,
    buy_price: Decimal,
    sell_exchange: str,
    sell_market_pair: str,
    sell_price: Decimal,
    started_at: datetime,
    last_confirmed_at: datetime,
    ended_at: datetime | None = None,
) -> int | None:
    """Insert a confirmed episode or close it with its observed lifetime."""
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            if ended_at is None:
                cursor.execute(
                    "INSERT INTO opportunity_lifetimes (source_market_id, source_pair, threshold_percent, "
                    "spread_percent, max_spread_percent, buy_exchange, buy_market_pair, buy_price, "
                    "sell_exchange, sell_market_pair, sell_price, started_at, last_confirmed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (source_market_id, source_pair, threshold_percent, spread_percent, max_spread_percent,
                     buy_exchange, buy_market_pair, buy_price, sell_exchange, sell_market_pair, sell_price,
                     started_at, last_confirmed_at),
                )
                lifetime_id = int(cursor.lastrowid)
            else:
                cursor.execute(
                    "UPDATE opportunity_lifetimes SET spread_percent = %s, max_spread_percent = %s, "
                    "buy_exchange = %s, buy_market_pair = %s, buy_price = %s, sell_exchange = %s, "
                    "sell_market_pair = %s, sell_price = %s, last_confirmed_at = %s, ended_at = %s, "
                    "duration_seconds = GREATEST(0, TIMESTAMPDIFF(SECOND, started_at, %s)), is_active = 0 "
                    "WHERE lifetime_id = %s AND is_active = 1",
                    (spread_percent, max_spread_percent, buy_exchange, buy_market_pair, buy_price,
                     sell_exchange, sell_market_pair, sell_price, last_confirmed_at, ended_at,
                     ended_at, lifetime_id),
                )
        connection.commit()
        return lifetime_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def touch_opportunity_lifetimes(rows: list[tuple[Any, ...]]) -> None:
    """Batch checkpoint active episodes so restarts cannot inflate their duration."""
    if not rows:
        return
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                "UPDATE opportunity_lifetimes SET last_confirmed_at = %s, spread_percent = %s, "
                "max_spread_percent = %s, buy_exchange = %s, buy_market_pair = %s, buy_price = %s, "
                "sell_exchange = %s, sell_market_pair = %s, sell_price = %s "
                "WHERE lifetime_id = %s AND is_active = 1",
                rows,
            )
        connection.commit()
    finally:
        connection.close()


def finalize_interrupted_lifetimes() -> int:
    """Close active episodes at their last confirmed quote after a process restart."""
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE opportunity_lifetimes SET ended_at = last_confirmed_at, "
                "duration_seconds = GREATEST(0, TIMESTAMPDIFF(SECOND, started_at, last_confirmed_at)), "
                "is_active = 0 WHERE is_active = 1"
            )
            count = cursor.rowcount
        connection.commit()
        return count
    finally:
        connection.close()


def finalize_lifetime_at_checkpoint(
    lifetime_id: int, last_confirmed_at: datetime | None = None
) -> None:
    """Close one episode at its last heartbeat when its instruments disappear."""
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE opportunity_lifetimes SET last_confirmed_at = COALESCE(%s, last_confirmed_at), "
                "ended_at = COALESCE(%s, last_confirmed_at), "
                "duration_seconds = GREATEST(0, TIMESTAMPDIFF(SECOND, started_at, COALESCE(%s, last_confirmed_at))), "
                "is_active = 0 WHERE lifetime_id = %s AND is_active = 1",
                (last_confirmed_at, last_confirmed_at, last_confirmed_at, lifetime_id),
            )
        connection.commit()
    finally:
        connection.close()


def replace_calculator_market_pairs(
    connection: MySQLConnection,
    rows_by_source: dict[int, list[tuple[Any, ...]]],
) -> int:
    """Atomically replace exchange quotes for every successfully queried source market."""
    affected_sources = list(rows_by_source)
    try:
        with connection.cursor() as cursor:
            for source_market_id in affected_sources:
                cursor.execute(
                    "DELETE FROM calculator_market_pairs WHERE source_market_id = %s",
                    (source_market_id,),
                )
                rows = rows_by_source[source_market_id]
                if rows:
                    cursor.executemany(CALCULATOR_MARKET_INSERT, rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return sum(len(rows) for rows in rows_by_source.values())


def calculate_market_opportunities(
    connection: MySQLConnection,
    threshold_percent: Decimal,
    source_market_ids: list[int],
) -> int:
    """Сравнить лучшие цены разных бирж и отметить расхождения выше порога."""
    if not source_market_ids:
        return 0

    placeholders = ", ".join(["%s"] * len(source_market_ids))
    best_prices: dict[int, dict[str, tuple[int, Decimal, str, str, Decimal]]] = {}
    source_pairs: dict[int, str] = {}
    with connection.cursor() as cursor:
        query = (
            "SELECT venue.source_market_id, venue.exchange_slug, venue.price, venue.rank_no, "
            "venue.base_symbol, venue.market_pair, venue.exchange_name, source.market_pair "
            "FROM calculator_market_pairs AS venue "
            "JOIN market_pairs AS source ON source.market_id = venue.source_market_id "
            f"WHERE venue.source_market_id IN ({placeholders}) "
            "AND venue.price IS NOT NULL AND venue.price > 0 "
            "ORDER BY venue.source_market_id, venue.exchange_slug, venue.rank_no"
        )
        cursor.execute(query, tuple(source_market_ids))
        for (source_market_id, exchange_slug, price, rank_no, base_symbol, market_pair,
             exchange_name, source_pair) in cursor.fetchall():
            if not exchange_slug:
                continue
            source_market_id = int(source_market_id)
            source_pairs[source_market_id] = str(source_pair or "")
            source_prices = best_prices.setdefault(source_market_id, {})
            # Биржи могут котировать лот из N токенов (например, 1000CHEEMS),
            # тогда как другие указывают цену одного токена (CHEEMS).
            pair_base = str(market_pair or "").split("/", 1)[0]
            symbol_for_units = pair_base or str(base_symbol or "")
            match = re.match(r"^(\d+)(?=[A-Z])", symbol_for_units.upper())
            unit_count = Decimal(match.group(1)) if match else Decimal("1")
            if unit_count <= 0:
                unit_count = Decimal("1")
            normalized_price = Decimal(str(price)) / unit_count
            rank = int(rank_no) if rank_no is not None else 2**31
            current = source_prices.get(exchange_slug)
            if current is None or rank < current[0]:
                source_prices[exchange_slug] = (
                    rank, normalized_price, str(exchange_name or exchange_slug),
                    str(market_pair or ""), Decimal(str(price)),
                )

        cursor.execute(
            f"UPDATE market_pairs SET show_flag = 0, price_difference_percent = NULL "
            f"WHERE market_id IN ({placeholders})",
            tuple(source_market_ids),
        )
        updates: list[tuple[Decimal, int, int]] = []
        history_rows_by_pair: dict[str, tuple[Any, ...]] = {}
        flagged = 0
        compared = 0
        max_spread = Decimal("0")
        for source_market_id, venue_prices in best_prices.items():
            venue_quotes = list(venue_prices.values())
            if len(venue_quotes) < 2:
                continue
            compared += 1
            low_quote = min(venue_quotes, key=lambda item: item[1])
            high_quote = max(venue_quotes, key=lambda item: item[1])
            lowest, highest = low_quote[1], high_quote[1]
            difference_percent = (highest - lowest) / lowest * Decimal("100")
            max_spread = max(max_spread, difference_percent)
            show_flag = int(difference_percent > threshold_percent)
            flagged += show_flag
            updates.append((difference_percent, show_flag, source_market_id))
            if show_flag:
                source_pair = source_pairs.get(source_market_id, "")
                history_rows_by_pair[source_pair] = (
                    source_market_id, source_pair, difference_percent,
                    low_quote[2], low_quote[3], low_quote[4],
                    high_quote[2], high_quote[3], high_quote[4],
                )
        if updates:
            cursor.executemany(
                "UPDATE market_pairs SET price_difference_percent = %s, show_flag = %s "
                "WHERE market_id = %s",
                updates,
            )
        if history_rows_by_pair:
            cursor.executemany(
                "INSERT INTO opportunity_history (source_market_id, source_pair, spread_percent, "
                "low_exchange, low_market_pair, low_price, high_exchange, high_market_pair, high_price) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                list(history_rows_by_pair.values()),
            )
        cursor.execute(
            "DELETE FROM opportunity_history WHERE observed_at < UTC_TIMESTAMP() - INTERVAL 30 DAY"
        )
    logger.info(
        "Проверка спреда: валют с ценами минимум на двух биржах %s; "
        "максимальный спред %.6f%%; порог %.6f%%; выше порога %s",
        compared, max_spread, threshold_percent, flagged,
    )
    connection.commit()
    return flagged
