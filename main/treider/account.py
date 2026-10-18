"""Хранение режима и баланса единого счёта трейдера в MySQL."""

from __future__ import annotations

from decimal import Decimal

from mysql.connector import MySQLConnection

from main.treider.config import INITIAL_BALANCE_USD, TRAINING_MODE


def initialize_trader_account(connection: MySQLConnection) -> tuple[Decimal, bool]:
    """Создать счёт с начальным балансом; на перезапуске баланс не сбрасывать."""
    with connection.cursor(dictionary=True) as cursor:
        cursor.execute(
            "INSERT INTO trader_account (account_id, balance_usd, training_mode) "
            "VALUES (1, %s, %s) "
            "ON DUPLICATE KEY UPDATE training_mode = VALUES(training_mode)",
            (INITIAL_BALANCE_USD, int(TRAINING_MODE)),
        )
        cursor.execute(
            "SELECT balance_usd, training_mode FROM trader_account WHERE account_id = 1"
        )
        row = cursor.fetchone()
    connection.commit()
    if row is None:
        raise RuntimeError("Не удалось создать или прочитать счёт трейдера")
    return Decimal(str(row["balance_usd"])), bool(row["training_mode"])
