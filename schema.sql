CREATE TABLE IF NOT EXISTS config (
    config_key VARCHAR(64) PRIMARY KEY,
    config_value VARCHAR(255) NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_signals (
    signal_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    opportunity_key VARCHAR(191) NOT NULL,
    payload JSON NOT NULL,
    status ENUM('pending', 'processing', 'processed', 'failed') NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at DATETIME NULL,
    processed_at DATETIME NULL,
    error_message TEXT NULL,
    decision VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt_at DATETIME NULL,
    expires_at DATETIME NULL,
    lease_token CHAR(36) NULL,
    lease_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
    UNIQUE KEY uq_trader_signal_opportunity (opportunity_key),
    INDEX idx_trader_signal_queue (status, signal_id),
    INDEX idx_trader_signal_claimed (status, claimed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_account (
    account_id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    balance_usd DECIMAL(18, 8) NOT NULL,
    initial_capital_usd DECIMAL(18, 8) NULL,
    initial_capital_funded TINYINT(1) NOT NULL DEFAULT 0,
    initial_exchange_slug VARCHAR(32) NULL,
    training_mode TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_balances (
    exchange_slug VARCHAR(32) NOT NULL,
    asset VARCHAR(24) NOT NULL,
    balance DECIMAL(38, 18) NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (exchange_slug, asset)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_capital_movements (
    movement_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    movement_type ENUM('deposit', 'withdrawal', 'allocation') NOT NULL,
    exchange_slug VARCHAR(32) NULL,
    amount_usd DECIMAL(18, 8) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_trader_capital_movement_created (created_at, movement_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_training_settlements (
    settlement_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    signal_id BIGINT UNSIGNED NOT NULL,
    source_pair VARCHAR(128) NOT NULL,
    buy_exchange VARCHAR(64) NOT NULL,
    buy_market_pair VARCHAR(128) NOT NULL,
    sell_exchange VARCHAR(64) NOT NULL,
    sell_market_pair VARCHAR(128) NOT NULL,
    quantity DECIMAL(38, 18) NOT NULL,
    buy_price DECIMAL(38, 18) NOT NULL,
    expected_sell_price DECIMAL(38, 18) NOT NULL,
    stop_loss_price DECIMAL(38, 18) NULL,
    final_sell_price DECIMAL(38, 18) NULL,
    buy_taker_fee_rate DECIMAL(20, 12) NOT NULL,
    expected_sell_taker_fee_rate DECIMAL(20, 12) NOT NULL,
    actual_sell_taker_fee_rate DECIMAL(20, 12) NULL,
    buy_cost_usd DECIMAL(38, 12) NOT NULL,
    realized_sell_proceeds_usd DECIMAL(38, 12) NULL,
    buy_fee_usd DECIMAL(38, 12) NOT NULL,
    expected_sell_fee_usd DECIMAL(38, 12) NOT NULL,
    sell_fee_usd DECIMAL(38, 12) NULL,
    expected_profit_usd DECIMAL(38, 12) NOT NULL,
    route_status VARCHAR(24) NOT NULL DEFAULT 'legacy',
    route_assumptions JSON NULL,
    network_name VARCHAR(64) NULL,
    transfer_seconds DECIMAL(12, 3) NULL,
    buy_fee_source VARCHAR(16) NOT NULL DEFAULT 'legacy',
    sell_fee_source VARCHAR(16) NOT NULL DEFAULT 'legacy',
    buy_fee_currency VARCHAR(24) NOT NULL DEFAULT 'USDT',
    buy_fee_base_quantity DECIMAL(38, 18) NOT NULL DEFAULT 0,
    transfer_dust_quantity DECIMAL(38, 18) NOT NULL DEFAULT 0,
    transfer_fee_amount DECIMAL(38, 18) NOT NULL DEFAULT 0,
    transfer_fee_currency VARCHAR(24) NOT NULL DEFAULT 'USDT',
    transfer_fee_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    bought_quantity DECIMAL(38, 18) NOT NULL DEFAULT 0,
    quantity_after_fees DECIMAL(38, 18) NOT NULL DEFAULT 0,
    sold_quantity DECIMAL(38, 18) NOT NULL DEFAULT 0,
    remaining_quantity DECIMAL(38, 18) NOT NULL DEFAULT 0,
    expected_sell_proceeds_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    expected_profit_before_reserve_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    price_reserve_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    expected_profit_after_reserve_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    sell_proceeds_accumulated_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    sell_fees_accumulated_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    buy_debit_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    purchase_model VARCHAR(64) NOT NULL DEFAULT 'legacy',
    position_status ENUM('legacy', 'pending_transfer', 'ready_to_sell', 'partial', 'stop_loss', 'closed', 'open') NOT NULL DEFAULT 'legacy',
    asset_exchange VARCHAR(32) NULL,
    transfer_completed_at DATETIME NULL,
    planned_sell_at DATETIME NULL,
    actual_sell_at DATETIME NULL,
    retry_started_at DATETIME NULL,
    last_valuation_price DECIMAL(38, 18) NULL,
    valuation_usd DECIMAL(38, 12) NULL,
    valued_at DATETIME NULL,
    realized_profit_usd DECIMAL(38, 12) NULL,
    balance_before_usd DECIMAL(18, 8) NOT NULL,
    settlement_balance_before_usd DECIMAL(18, 8) NULL,
    balance_after_usd DECIMAL(18, 8) NULL,
    status ENUM('pending', 'settling', 'settled', 'partial', 'open', 'expired') NOT NULL DEFAULT 'pending',
    settle_after_utc DATETIME NOT NULL,
    claimed_at DATETIME NULL,
    settlement_attempts INT UNSIGNED NOT NULL DEFAULT 0,
    claim_token CHAR(36) NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at DATETIME NULL,
    UNIQUE KEY uq_trader_training_signal (signal_id),
    INDEX idx_trader_training_due (status, settle_after_utc),
    CONSTRAINT fk_trader_training_settlement_signal
        FOREIGN KEY (signal_id) REFERENCES trader_signals (signal_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_asset_transfers (
    transfer_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    settlement_id BIGINT UNSIGNED NOT NULL,
    asset VARCHAR(24) NOT NULL,
    amount DECIMAL(38, 18) NOT NULL,
    source_exchange VARCHAR(32) NOT NULL,
    destination_exchange VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_trader_asset_transfer_settlement (settlement_id),
    CONSTRAINT fk_trader_asset_transfer_settlement
        FOREIGN KEY (settlement_id) REFERENCES trader_training_settlements (settlement_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_demo_operation_log (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_key VARCHAR(191) NOT NULL,
    run_id CHAR(36) NOT NULL,
    settlement_id BIGINT UNSIGNED NOT NULL,
    signal_id BIGINT UNSIGNED NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    event_at_utc DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
    exchange_slug VARCHAR(64) NOT NULL,
    market_pair VARCHAR(128) NULL,
    asset VARCHAR(24) NULL,
    quantity DECIMAL(38, 18) NULL,
    price DECIMAL(38, 18) NULL,
    quote_amount DECIMAL(38, 12) NULL,
    fee_amount DECIMAL(38, 18) NULL,
    fee_asset VARCHAR(24) NULL,
    balance_before DECIMAL(38, 18) NULL,
    balance_after DECIMAL(38, 18) NULL,
    book_snapshot JSON NULL,
    UNIQUE KEY uq_trader_demo_event_key (event_key),
    INDEX idx_trader_demo_log_time (event_at_utc, event_id),
    INDEX idx_trader_demo_log_run (run_id, event_id),
    INDEX idx_trader_demo_log_settlement (settlement_id, event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_demo_runs (
    run_id CHAR(36) NOT NULL PRIMARY KEY,
    git_commit VARCHAR(40) NOT NULL,
    config_hash CHAR(64) NOT NULL,
    config JSON NOT NULL,
    starting_balances JSON NOT NULL,
    starting_equity_usd DECIMAL(38, 12) NOT NULL,
    baseline_complete TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    started_at_utc DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
    ended_at_utc DATETIME(6) NULL,
    INDEX idx_trader_demo_runs_started (started_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_demo_run_state (
    state_id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
    active_run_id CHAR(36) NULL,
    updated_at_utc DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6) ON UPDATE UTC_TIMESTAMP(6),
    CONSTRAINT fk_trader_demo_run_state_active
        FOREIGN KEY (active_run_id) REFERENCES trader_demo_runs (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_replay_book_snapshots (
    snapshot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    signal_id BIGINT UNSIGNED NOT NULL,
    side VARCHAR(8) NOT NULL,
    exchange_slug VARCHAR(64) NOT NULL,
    market_pair VARCHAR(128) NOT NULL,
    observed_at_utc DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
    book_snapshot JSON NOT NULL,
    INDEX idx_trader_replay_book (run_id, exchange_slug, market_pair, observed_at_utc),
    INDEX idx_trader_replay_signal (run_id, signal_id),
    CONSTRAINT fk_trader_replay_book_run
        FOREIGN KEY (run_id) REFERENCES trader_demo_runs (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_demo_daily_reports (
    run_id CHAR(36) NOT NULL,
    report_day_kyiv DATE NOT NULL,
    equity_usd DECIMAL(38, 12) NULL,
    pnl_usd DECIMAL(38, 12) NULL,
    daily_pnl_usd DECIMAL(38, 12) NULL,
    cumulative_pnl_usd DECIMAL(38, 12) NULL,
    fees_usd DECIMAL(38, 12) NOT NULL DEFAULT 0,
    trade_count INT UNSIGNED NOT NULL DEFAULT 0,
    closed_trade_count INT UNSIGNED NOT NULL DEFAULT 0,
    drawdown_usd DECIMAL(38, 12) NULL,
    drawdown_percent DECIMAL(20, 12) NULL,
    position_value_usd DECIMAL(38, 12) NULL,
    transfer_value_usd DECIMAL(38, 12) NULL,
    valuation_complete TINYINT(1) NOT NULL DEFAULT 0,
    valuation_stale TINYINT(1) NOT NULL DEFAULT 0,
    captured_at_utc DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
    PRIMARY KEY (run_id, report_day_kyiv),
    CONSTRAINT fk_trader_demo_daily_run
        FOREIGN KEY (run_id) REFERENCES trader_demo_runs (run_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trader_demo_equity_snapshots (
    snapshot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    captured_at_utc DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
    equity_usd DECIMAL(38, 12) NULL,
    valuation_complete TINYINT(1) NOT NULL DEFAULT 0,
    valuation_stale TINYINT(1) NOT NULL DEFAULT 0,
    INDEX idx_trader_equity_run_time (run_id, captured_at_utc),
    CONSTRAINT fk_trader_equity_run FOREIGN KEY (run_id) REFERENCES trader_demo_runs (run_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS market_pairs (
    market_id BIGINT UNSIGNED PRIMARY KEY,
    rank_no INT NULL,
    exchange_id BIGINT NULL,
    exchange_name VARCHAR(128) NULL,
    exchange_slug VARCHAR(128) NULL,
    market_pair VARCHAR(128) NOT NULL,
    category VARCHAR(32) NULL,
    market_url TEXT NULL,
    base_symbol VARCHAR(64) NULL,
    base_currency_id BIGINT NULL,
    base_currency_name VARCHAR(255) NULL,
    base_currency_slug VARCHAR(255) NULL,
    quote_symbol VARCHAR(64) NULL,
    quote_currency_id BIGINT NULL,
    price DECIMAL(38, 18) NULL,
    volume_usd DECIMAL(38, 12) NULL,
    effective_liquidity DECIMAL(38, 18) NULL,
    last_updated DATETIME NULL,
    quote_price DECIMAL(38, 18) NULL,
    volume_base DECIMAL(38, 18) NULL,
    volume_quote DECIMAL(38, 18) NULL,
    quote_metric_id VARCHAR(64) NULL,
    quote_volume_24h DECIMAL(38, 12) NULL,
    quote_depth_positive_two DECIMAL(38, 12) NULL,
    quote_depth_negative_two DECIMAL(38, 12) NULL,
    fee_type VARCHAR(64) NULL,
    depth_usd_negative_two DECIMAL(38, 12) NULL,
    depth_usd_positive_two DECIMAL(38, 12) NULL,
    volume_percent DECIMAL(20, 12) NULL,
    market_score DECIMAL(20, 12) NULL,
    market_reputation INT NULL,
    outlier_detected TINYINT NULL,
    price_excluded TINYINT NULL,
    volume_excluded TINYINT NULL,
    outlier_disp TINYINT NULL,
    price_ex_disp TINYINT NULL,
    vol_ex_disp TINYINT NULL,
    is_verified TINYINT NULL,
    market_type VARCHAR(64) NULL,
    hide_stars_market TINYINT NULL,
    show_flag TINYINT(1) NOT NULL DEFAULT 0,
    price_difference_percent DECIMAL(20, 12) NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_market_pair (market_pair),
    INDEX idx_volume_usd (volume_usd),
    INDEX idx_last_updated (last_updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS calculator_market_pairs (
    source_market_id BIGINT UNSIGNED NOT NULL,
    market_id DECIMAL(65, 0) NOT NULL,
    rank_no INT NULL,
    exchange_id BIGINT NULL,
    exchange_name VARCHAR(128) NULL,
    exchange_slug VARCHAR(128) NOT NULL,
    market_pair VARCHAR(128) NOT NULL,
    category VARCHAR(32) NULL,
    market_url TEXT NULL,
    base_symbol VARCHAR(64) NULL,
    quote_symbol VARCHAR(64) NULL,
    price DECIMAL(38, 18) NULL,
    volume_usd DECIMAL(38, 12) NULL,
    open_interest_usd DECIMAL(38, 12) NULL,
    funding_rate DECIMAL(38, 18) NULL,
    index_price DECIMAL(38, 18) NULL,
    index_basis DECIMAL(38, 18) NULL,
    last_updated DATETIME NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_market_id, market_id),
    INDEX idx_calculator_exchange (exchange_slug),
    INDEX idx_calculator_market (market_id),
    CONSTRAINT fk_calculator_source_market
        FOREIGN KEY (source_market_id) REFERENCES market_pairs (market_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS opportunity_history (
    history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source_market_id BIGINT UNSIGNED NOT NULL,
    source_pair VARCHAR(128) NOT NULL,
    spread_percent DECIMAL(20, 12) NOT NULL,
    low_exchange VARCHAR(128) NOT NULL,
    low_market_pair VARCHAR(128) NOT NULL,
    low_price DECIMAL(38, 18) NOT NULL,
    high_exchange VARCHAR(128) NOT NULL,
    high_market_pair VARCHAR(128) NOT NULL,
    high_price DECIMAL(38, 18) NOT NULL,
    observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_opportunity_history_time (observed_at),
    INDEX idx_opportunity_history_spread (spread_percent),
    INDEX idx_opportunity_history_pair (source_pair)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS opportunity_lifetimes (
    lifetime_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    source_market_id BIGINT UNSIGNED NOT NULL,
    source_pair VARCHAR(128) NOT NULL,
    threshold_percent DECIMAL(10, 4) NOT NULL,
    spread_percent DECIMAL(20, 12) NOT NULL,
    max_spread_percent DECIMAL(20, 12) NOT NULL,
    buy_exchange VARCHAR(128) NOT NULL,
    buy_market_pair VARCHAR(128) NOT NULL,
    buy_price DECIMAL(38, 18) NOT NULL,
    sell_exchange VARCHAR(128) NOT NULL,
    sell_market_pair VARCHAR(128) NOT NULL,
    sell_price DECIMAL(38, 18) NOT NULL,
    started_at DATETIME(3) NOT NULL,
    last_confirmed_at DATETIME(3) NOT NULL,
    ended_at DATETIME(3) NULL,
    duration_seconds DECIMAL(12, 3) UNSIGNED NULL,
    transfer_seconds DECIMAL(10, 2) NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    INDEX idx_lifetime_active (is_active, threshold_percent),
    INDEX idx_lifetime_time (started_at),
    INDEX idx_lifetime_source (source_market_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
