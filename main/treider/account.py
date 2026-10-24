"""Хранение режима и баланса единого счёта трейдера в MySQL."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from mysql.connector import MySQLConnection

from main.treider.config import (
    BUY_FEE_CURRENCY, DEMO_STRATEGY, INITIAL_BALANCE_USD, INITIAL_EXCHANGE_SLUG,
    MAX_LOSS_BALANCE_PERCENT, MAX_LOSS_PROFIT_RATIO, MIN_NET_PROFIT_USDT,
    PRICE_MOVE_RESERVE_PERCENT, CONFIGURED_TAKER_FEES, ALLOW_DISABLED_ROUTE_DEMO,
    ALLOW_UNCONFIRMED_ROUTE_DEMO, FIXED_TRANSFER_SECONDS, RANDOM_TRANSFER_TIME,
    SAME_ROUTE_REENTRY_COOLDOWN_SECONDS, SETTLEMENT_MAX_WAIT_SECONDS, SETTLEMENT_RETRY_SECONDS,
    SIGNAL_MAX_AGE_SECONDS, SIGNAL_MAX_ATTEMPTS, STOP_LOSS_PERCENT, TRADE_SIZE_CANDIDATES_USDT,
    TRAINING_MODE, TRANSFER_FEE_AMOUNT, TRANSFER_FEE_CURRENCY, TRANSFER_NETWORK, TRANSFER_RANDOM_SEED,
)


def _demo_config_snapshot() -> dict[str, Any]:
    """Keep only result-affecting settings; API credentials are intentionally excluded."""
    return {
        "training_mode": TRAINING_MODE,
        "strategy": DEMO_STRATEGY,
        "trade_size_candidates_usdt": [str(value) for value in TRADE_SIZE_CANDIDATES_USDT],
        "initial_balance_usd": str(INITIAL_BALANCE_USD),
        "initial_exchange": INITIAL_EXCHANGE_SLUG,
        "buy_fee_currency": BUY_FEE_CURRENCY,
        "configured_taker_fees": {
            exchange: str(rate) if rate is not None else None
            for exchange, rate in sorted(CONFIGURED_TAKER_FEES.items())
        },
        "transfer_fee_amount": str(TRANSFER_FEE_AMOUNT) if TRANSFER_FEE_AMOUNT is not None else None,
        "transfer_fee_currency": TRANSFER_FEE_CURRENCY,
        "transfer_network": TRANSFER_NETWORK,
        "fixed_transfer_seconds": str(FIXED_TRANSFER_SECONDS),
        "random_transfer_time": RANDOM_TRANSFER_TIME,
        "transfer_random_seed_sha256": hashlib.sha256(TRANSFER_RANDOM_SEED.encode("utf-8")).hexdigest()
        if TRANSFER_RANDOM_SEED else None,
        "allow_unconfirmed_route": ALLOW_UNCONFIRMED_ROUTE_DEMO,
        "allow_disabled_route": ALLOW_DISABLED_ROUTE_DEMO,
        "min_net_profit_usdt": str(MIN_NET_PROFIT_USDT),
        "price_move_reserve_percent": str(PRICE_MOVE_RESERVE_PERCENT),
        "stop_loss_percent": str(STOP_LOSS_PERCENT),
        "max_loss_balance_percent": str(MAX_LOSS_BALANCE_PERCENT),
        "max_loss_profit_ratio": str(MAX_LOSS_PROFIT_RATIO),
        "settlement_retry_seconds": SETTLEMENT_RETRY_SECONDS,
        "settlement_max_wait_seconds": SETTLEMENT_MAX_WAIT_SECONDS,
        "signal_max_age_seconds": SIGNAL_MAX_AGE_SECONDS,
        "signal_max_attempts": SIGNAL_MAX_ATTEMPTS,
        "same_route_reentry_cooldown_seconds": SAME_ROUTE_REENTRY_COOLDOWN_SECONDS,
    }


def _git_commit() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
            text=True, timeout=2, check=False,
        )
        value = result.stdout.strip()
        return value[:40] if result.returncode == 0 and value else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _ensure_demo_run(connection: MySQLConnection) -> str | None:
    if not TRAINING_MODE:
        return None
    config = _demo_config_snapshot()
    config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
    config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    commit = _git_commit()
    with connection.cursor(dictionary=True) as cursor:
        cursor.execute(
            "SELECT run.run_id, run.config_hash, run.git_commit FROM trader_demo_run_state AS state "
            "JOIN trader_demo_runs AS run ON run.run_id = state.active_run_id WHERE state.state_id = 1 FOR UPDATE"
        )
        active = cursor.fetchone()
        if active and active["config_hash"] == config_hash and active["git_commit"] == commit:
            return str(active["run_id"])

        cursor.execute(
            "SELECT run_id FROM trader_demo_operation_log WHERE run_id NOT IN "
            "(SELECT run_id FROM trader_demo_runs) GROUP BY run_id"
        )
        for orphan in cursor.fetchall():
            orphan_id = str(orphan["run_id"])
            cursor.execute(
                "INSERT IGNORE INTO trader_demo_runs (run_id, git_commit, config_hash, config, starting_balances, "
                "starting_equity_usd, baseline_complete, status) VALUES (%s, 'legacy', %s, %s, %s, 0, 0, 'legacy')",
                (orphan_id, "0" * 64, json.dumps({"legacy": True}), json.dumps([])),
            )
        legacy_run_id = "00000000-0000-0000-0000-000000000000"
        cursor.execute(
            "INSERT IGNORE INTO trader_demo_runs (run_id, git_commit, config_hash, config, starting_balances, "
            "starting_equity_usd, baseline_complete, status) VALUES (%s, 'legacy', %s, %s, %s, 0, 0, 'legacy')",
            (legacy_run_id, "0" * 64, json.dumps({"legacy": True}), json.dumps([])),
        )
        cursor.execute(
            "SELECT settlement_id FROM trader_training_settlements "
            "WHERE JSON_EXTRACT(route_assumptions, '$.demo_run_id') IS NULL"
        )
        for settlement in cursor.fetchall():
            settlement_id = int(settlement["settlement_id"])
            cursor.execute(
                "SELECT run_id FROM trader_demo_operation_log WHERE settlement_id = %s ORDER BY event_id LIMIT 1",
                (settlement_id,),
            )
            first_event = cursor.fetchone()
            settlement_run = str(first_event["run_id"]) if first_event else legacy_run_id
            cursor.execute(
                "UPDATE trader_training_settlements SET route_assumptions = JSON_SET("
                "COALESCE(route_assumptions, JSON_OBJECT()), '$.demo_run_id', %s) WHERE settlement_id = %s",
                (settlement_run, settlement_id),
            )

        cursor.execute("SELECT exchange_slug, asset, balance FROM trader_balances ORDER BY exchange_slug, asset")
        balances = cursor.fetchall()
        starting_balances = [
            {"exchange": row["exchange_slug"], "asset": row["asset"], "balance": str(row["balance"])}
            for row in balances
        ]
        starting_equity = sum(
            (Decimal(str(row["balance"])) for row in balances if row["asset"] == "USDT"), Decimal(0)
        )
        cursor.execute("SELECT balance_usd FROM trader_account WHERE account_id = 1")
        legacy_account = cursor.fetchone() or {}
        legacy_balance = Decimal(str(legacy_account.get("balance_usd") or 0))
        if legacy_balance:
            starting_balances.append({"exchange": None, "asset": "USDT", "balance": str(legacy_balance), "location": "legacy_account"})
            starting_equity += legacy_balance
        has_coin_balances = any(row["asset"] != "USDT" and Decimal(str(row["balance"])) != 0 for row in balances)
        cursor.execute(
            "SELECT COUNT(*) AS active_count FROM trader_training_settlements "
            "WHERE remaining_quantity > 0 AND status IN ('pending','settling','partial','open')"
        )
        has_active_positions = int((cursor.fetchone() or {}).get("active_count") or 0) > 0
        run_id = str(uuid.uuid4())
        if active:
            cursor.execute(
                "UPDATE trader_demo_runs SET status = 'superseded', ended_at_utc = UTC_TIMESTAMP(6) WHERE run_id = %s",
                (active["run_id"],),
            )
        cursor.execute(
            "INSERT INTO trader_demo_runs (run_id, git_commit, config_hash, config, starting_balances, "
            "starting_equity_usd, baseline_complete) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (run_id, commit, config_hash, config_json, json.dumps(starting_balances), starting_equity,
            int(not has_coin_balances and not has_active_positions)),
        )
        cursor.execute(
            "INSERT INTO trader_demo_run_state (state_id, active_run_id) VALUES (1, %s) "
            "ON DUPLICATE KEY UPDATE active_run_id = VALUES(active_run_id)", (run_id,),
        )
    return run_id


def initialize_trader_account(connection: MySQLConnection) -> tuple[Decimal, bool]:
    """Создать счёт с начальным балансом; на перезапуске баланс не сбрасывать."""
    with connection.cursor(dictionary=True) as cursor:
        cursor.execute(
            "INSERT INTO trader_account (account_id, balance_usd, initial_capital_usd, "
            "initial_capital_funded, initial_exchange_slug, training_mode) "
            "VALUES (1, 0, %s, 0, %s, %s) "
            "ON DUPLICATE KEY UPDATE training_mode = VALUES(training_mode)",
            (INITIAL_BALANCE_USD, INITIAL_EXCHANGE_SLUG, int(TRAINING_MODE)),
        )
        cursor.execute(
            "UPDATE trader_account SET initial_capital_usd = %s "
            "WHERE account_id = 1 AND initial_capital_usd IS NULL",
            (INITIAL_BALANCE_USD,),
        )
        cursor.execute(
            "SELECT balance_usd, training_mode, initial_capital_funded "
            "FROM trader_account WHERE account_id = 1 FOR UPDATE"
        )
        row = cursor.fetchone()
        if row is not None and not row["initial_capital_funded"]:
            cursor.execute(
                "INSERT INTO trader_balances (exchange_slug, asset, balance) VALUES (%s, 'USDT', %s) "
                "ON DUPLICATE KEY UPDATE balance = balance",
                (INITIAL_EXCHANGE_SLUG, INITIAL_BALANCE_USD),
            )
            cursor.execute(
                "UPDATE trader_account SET initial_capital_funded = 1, initial_exchange_slug = %s "
                "WHERE account_id = 1 AND initial_capital_funded = 0",
                (INITIAL_EXCHANGE_SLUG,),
            )
    _ensure_demo_run(connection)
    connection.commit()
    if row is None:
        raise RuntimeError("Не удалось создать или прочитать счёт трейдера")
    return Decimal(str(row["balance_usd"])), bool(row["training_mode"])


def active_demo_run_id(connection: MySQLConnection) -> str | None:
    with connection.cursor(dictionary=True) as cursor:
        cursor.execute("SELECT active_run_id FROM trader_demo_run_state WHERE state_id = 1")
        row = cursor.fetchone()
    return str(row["active_run_id"]) if row and row.get("active_run_id") else None
