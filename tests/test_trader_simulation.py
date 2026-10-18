from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from main.treider import exchange_networks, fees, market_data, tran_emulation


class FeeTests(unittest.TestCase):
    def tearDown(self) -> None:
        fees._cache.clear()

    @patch.object(fees, "OKX_API_PASSPHRASE", "pass")
    @patch.object(fees, "OKX_API_SECRET", "secret")
    @patch.object(fees, "OKX_API_KEY", "key")
    @patch("main.treider.fees.requests.get")
    def test_okx_sign_is_normalized_without_abs(self, request: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "code": "0", "data": [{"maker": "-0.0008", "taker": "0.001"}]
        }
        request.return_value = response
        maker, taker, *_ = fees._okx("ABC-USDT")
        self.assertEqual(maker, Decimal("0.0008"))
        self.assertEqual(taker, Decimal("-0.001"))

    @patch.object(fees, "BINANCE_API_KEY", "bin-key")
    @patch.object(fees, "BINANCE_API_SECRET", "bin-secret")
    @patch("main.treider.fees.requests.get")
    def test_binance_auth_header_and_side_commissions(self, request: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "standardCommission": {"maker": "0.001", "taker": "0.002", "buyer": "0.0001", "seller": "0.0002"},
            "specialCommission": {"maker": "0", "taker": "0.0003", "buyer": "0", "seller": "0"},
            "taxCommission": {"maker": "0", "taker": "0.0004", "buyer": "0", "seller": "0"},
            "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discount": "0.75"},
        }
        request.return_value = response
        maker, taker, details, buyer, seller = fees._binance("ABCUSDT")
        self.assertEqual(request.call_args.kwargs["headers"], {"X-MBX-APIKEY": "bin-key"})
        self.assertEqual(maker, Decimal("0.001"))
        self.assertEqual(taker, Decimal("0.0027"))
        self.assertEqual(buyer, Decimal("0.0028"))
        self.assertEqual(seller, Decimal("0.0029"))
        self.assertIn("not applied", details)

    @patch.object(fees, "MEXC_API_KEY", "mx-key")
    @patch.object(fees, "MEXC_API_SECRET", "mx-secret")
    @patch("main.treider.fees.requests.get")
    def test_mexc_auth_header(self, request: Mock) -> None:
        response = Mock()
        response.json.return_value = {"code": 0, "data": {"makerCommission": "0.001", "takerCommission": "0.002"}}
        request.return_value = response
        fees._mexc("ABCUSDT")
        self.assertEqual(request.call_args.kwargs["headers"], {"X-MEXC-APIKEY": "mx-key"})

    @patch.dict(fees.CONFIGURED_TAKER_FEES, {"mexc": Decimal("0.003")})
    @patch.dict(fees._FETCHERS, {"mexc": Mock(side_effect=TimeoutError("offline"))})
    def test_configured_rate_is_used_when_api_fails(self) -> None:
        result = fees.get_trade_fee_rates("mexc", "ABC/USDT")
        self.assertEqual(result.source, "config")
        self.assertEqual(result.taker_rate, Decimal("0.003"))

    @patch.dict(fees.CONFIGURED_TAKER_FEES, {"mexc": None})
    @patch.dict(fees._FETCHERS, {"mexc": Mock(side_effect=TimeoutError("offline"))})
    def test_missing_api_and_config_fee_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ставка отсутствует"):
            fees.get_trade_fee_rates("mexc", "ABC/USDT")

    def test_http_error_logs_exchange_code_without_signed_url(self) -> None:
        response = Mock()
        response.status_code = 400
        response.json.return_value = {"code": 700002, "msg": "Signature for this request is invalid."}
        error = fees.requests.HTTPError(
            "400 Client Error: Bad Request for url: https://example.test?signature=secret-signature",
            response=response,
        )
        details = fees._api_error_details(error)
        self.assertIn("700002", details)
        self.assertIn("Signature for this request is invalid", details)
        self.assertNotIn("secret-signature", details)


class MarketAndRouteTests(unittest.TestCase):
    @patch("main.treider.market_data._get_json")
    def test_mexc_spot_book_keeps_last_price_separate_from_best_bid(self, get_json: Mock) -> None:
        get_json.return_value = {
            "bidPrice": "0.0006900", "bidQty": "1000",
            "askPrice": "0.0006920", "askQty": "1200", "lastPrice": "0.0006910",
        }
        book = market_data.fetch_spot_book("mexc", "MBL/USDT")

        self.assertEqual(book.bid, Decimal("0.0006900"))
        self.assertEqual(book.last_price, Decimal("0.0006910"))
        self.assertIn("/api/v3/ticker/24hr", get_json.call_args.args[0])

    def test_market_sell_walks_depth_and_preserves_remainder(self) -> None:
        full = market_data.simulate_market_sell(
            "mexc", "ABC/USDT", Decimal("7"),
            [["10", "2"], ["9", "3"], ["8", "4"]],
        )
        self.assertEqual(full.filled_quantity, Decimal("7"))
        self.assertEqual(full.remaining_quantity, Decimal(0))
        # 2 * 10 + 3 * 9 + 2 * 8 = 63 USDT.
        self.assertEqual(full.proceeds, Decimal("63"))
        self.assertEqual(full.average_price, Decimal("63") / Decimal("7"))
        self.assertEqual(full.levels_used, 3)
        partial = market_data.simulate_market_sell(
            "mexc", "ABC/USDT", Decimal("7"), [["10", "2"], ["9", "3"]]
        )
        self.assertEqual(partial.filled_quantity, Decimal("5"))
        self.assertEqual(partial.remaining_quantity, Decimal("2"))
        self.assertEqual(partial.proceeds, Decimal("47"))

    def test_take_profit_limit_ignores_bids_below_limit(self) -> None:
        result = market_data.simulate_market_sell(
            "mexc", "ABC/USDT", Decimal("4"),
            [["12", "1"], ["11", "1"], ["9", "5"]],
            minimum_price=Decimal("10"),
        )
        self.assertEqual(result.filled_quantity, Decimal("2"))
        self.assertEqual(result.remaining_quantity, Decimal("2"))
        self.assertEqual(result.proceeds, Decimal("23"))
        self.assertEqual(result.best_bid, Decimal("12"))

    def test_stop_loss_and_take_profit_levels(self) -> None:
        from main.treider import trade

        self.assertEqual(trade.calculate_stop_loss_price(Decimal("100"), Decimal("0.5")), Decimal("99.500"))
        self.assertEqual(trade._exit_trigger(Decimal("101"), Decimal("101"), Decimal("99.5")), "take_profit")
        self.assertEqual(trade._exit_trigger(Decimal("99.5"), Decimal("101"), Decimal("99.5")), "stop_loss")
        self.assertIsNone(trade._exit_trigger(Decimal("100"), Decimal("101"), Decimal("99.5")))

    def test_network_check_logs_exchange_networks_and_transfer_flags(self) -> None:
        from main.treider.network_check import _log_exchange_networks

        result = {
            "source_exchange": "binance", "destination_exchange": "mexc",
            "networks": {
                "binance": [{
                    "network_code": "CHZ2", "network_name": "CHZ2",
                    "withdraw_enabled": True, "deposit_enabled": True,
                }],
                "mexc": [{
                    "network_code": "CHZ2", "network_name": "CHZ2",
                    "withdraw_enabled": True, "deposit_enabled": True,
                }],
            },
        }
        with self.assertLogs("main.treider.network_check", level="INFO") as captured:
            _log_exchange_networks("ACM", result)
        output = "\n".join(captured.output)
        self.assertIn("Сети ACM на BINANCE: CHZ2 / CHZ2", output)
        self.assertIn("Сети ACM на MEXC: CHZ2 / CHZ2", output)

    def test_ethereum_and_arbitrum_are_distinct_exact_matches(self) -> None:
        self.assertEqual(exchange_networks._network_profile({"network_code": "ETH", "network_name": "Ethereum"}), "ETHEREUM")
        self.assertEqual(exchange_networks._network_profile({"network_code": "Arbitrum One", "network_name": "ARB"}), "ARBITRUM")
        self.assertEqual(exchange_networks._network_profile({"network_code": "ETH-ARB", "network_name": "unknown"}), "UNKNOWN:ETHARB")
        self.assertTrue(exchange_networks._network_profile({"network_code": "ARB", "network_name": "Ethereum"}).startswith("UNKNOWN:"))

    def test_compound_ethereum_labels_match_but_mixed_chain_labels_do_not(self) -> None:
        self.assertEqual(
            exchange_networks._network_profile({"network_code": "MEME-ERC20", "network_name": "Ethereum (ERC20)"}),
            "ETHEREUM",
        )
        self.assertTrue(
            exchange_networks._network_profile({"network_code": "ETH-ARB", "network_name": "Ethereum / Arbitrum"})
            .startswith("UNKNOWN:")
        )

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_ontology_ont_network_matches_binance_and_mexc(self) -> None:
        binance_network = {
            "network_code": "ONT", "network_name": "Ontology",
            "withdraw_enabled": True, "deposit_enabled": True,
            "contract_address": "0xabc",
        }
        mexc_network = {
            "network_code": "Ontology", "network_name": "ONT",
            "withdraw_enabled": True, "deposit_enabled": True,
            "contract_address": "0xabc",
        }
        routes = exchange_networks.build_transfer_routes(
            "MBL", "binance", "mexc",
            {"networks": {"binance": [binance_network], "mexc": [mexc_network]}},
        )
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["network_profile"], "ONTOLOGY")
        self.assertEqual(routes[0]["route_status"], "confirmed")

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_meme_ethereum_route_matches_compound_exchange_labels(self) -> None:
        routes = exchange_networks.build_transfer_routes(
            "MEME", "okx", "mexc",
            {"networks": {
                "okx": [{
                    "network_code": "MEME-ERC20", "network_name": "MEME-ERC20",
                    "withdraw_enabled": True, "deposit_enabled": True, "withdraw_fee": "10",
                }],
                "mexc": [{
                    "network_code": "Ethereum(ERC20)", "network_name": "Ethereum(ERC20)",
                    "withdraw_enabled": True, "deposit_enabled": True,
                }],
            }},
        )
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["network_profile"], "ETHEREUM")
        self.assertEqual(routes[0]["route_status"], "unconfirmed")

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_disabled_shared_route_is_rejected(self) -> None:
        row = {"network_code": "ARBITRUM", "network_name": "Arbitrum", "withdraw_enabled": False, "deposit_enabled": True}
        destination = {**row, "withdraw_enabled": True, "deposit_enabled": True}
        routes = exchange_networks.build_transfer_routes(
            "ABC", "binance", "mexc",
            {"networks": {"binance": [row], "mexc": [destination]}},
        )
        self.assertEqual(routes, [])

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_configured_disabled_network_does_not_fall_back_to_another_chain(self) -> None:
        disabled_source = {
            "network_code": "ARBITRUM", "network_name": "Arbitrum",
            "withdraw_enabled": False, "deposit_enabled": True,
        }
        enabled_destination = {**disabled_source, "withdraw_enabled": True, "deposit_enabled": True}
        ethereum_source = {
            "network_code": "ETH", "network_name": "Ethereum",
            "withdraw_enabled": True, "deposit_enabled": True, "contract_address": "0xabc",
        }
        ethereum_destination = {**ethereum_source, "contract_address": "0xabc"}
        routes = exchange_networks.build_transfer_routes(
            "ABC", "binance", "mexc",
            {"networks": {
                "binance": [disabled_source, ethereum_source],
                "mexc": [enabled_destination, ethereum_destination],
            }},
            configured_profile="ARBITRUM",
        )
        self.assertEqual(routes, [])

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", True)
    def test_explicitly_allowed_disabled_route_stays_marked_disabled(self) -> None:
        disabled = {
            "network_code": "ARBITRUM", "network_name": "Arbitrum",
            "withdraw_enabled": False, "deposit_enabled": True,
        }
        routes = exchange_networks.build_transfer_routes(
            "ABC", "binance", "mexc",
            {"networks": {"binance": [disabled], "mexc": []}},
            configured_profile="ARBITRUM",
        )
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["route_status"], "disabled")

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_assumed_route_is_explicitly_marked_when_one_exchange_lacks_data(self) -> None:
        source = {
            "network_code": "ARBITRUM", "network_name": "Arbitrum One",
            "withdraw_enabled": True, "deposit_enabled": True,
            "withdraw_fee": "0.01", "contract_address": "0xabc",
        }
        routes = exchange_networks.build_transfer_routes(
            "ABC", "binance", "mexc",
            {"networks": {"binance": [source], "mexc": []}},
            configured_profile="ARBITRUM",
        )
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["route_status"], "unconfirmed")
        self.assertFalse(routes[0]["source_assumed"])
        self.assertTrue(routes[0]["destination_assumed"])
        self.assertEqual(routes[0]["withdraw_fee"], "0.01")

    def test_fixed_delay_is_reproducible_and_random_seed_repeats(self) -> None:
        with patch.object(tran_emulation, "FIXED_TRANSFER_SECONDS", Decimal("42")):
            self.assertEqual(tran_emulation.TransferTimeEmulator().get_emulated_duration("ETHEREUM"), 42.0)
        first = tran_emulation.TransferTimeEmulator(random_mode=True, seed=7)
        second = tran_emulation.TransferTimeEmulator(random_mode=True, seed=7)
        self.assertEqual(first.get_emulated_duration("ARBITRUM"), second.get_emulated_duration("ARBITRUM"))


class TradeCalculationTests(unittest.TestCase):
    @patch("main.treider.trade.fetch_market_sell_execution")
    @patch("main.treider.trade.PRICE_MOVE_RESERVE_PERCENT", Decimal("1"))
    @patch("main.treider.trade.MIN_NET_PROFIT_USDT", Decimal("0"))
    def test_buy_fee_and_transfer_fee_reduce_sellable_quantity_and_profit(self, sell_execution: Mock) -> None:
        from main.treider import trade

        buy = market_data.SpotBookTicker("binance", "ABC/USDT", Decimal("9"), Decimal("100"), Decimal("10"), Decimal("100"))
        sell = market_data.SpotBookTicker("mexc", "ABC/USDT", Decimal("11"), Decimal("1"), Decimal("12"), Decimal("1"))
        buy_rates = fees.TradeFeeRates("binance", "ABCUSDT", Decimal("0"), Decimal("0.01"), "now", buy_taker_rate=Decimal("0.01"))
        sell_rates = fees.TradeFeeRates("mexc", "ABCUSDT", Decimal("0"), Decimal("0.01"), "now", sell_taker_rate=Decimal("0.01"))
        def simulate(_exchange: str, _pair: str, qty: Decimal) -> SimpleNamespace:
            return SimpleNamespace(proceeds=qty * Decimal("11"), average_price=Decimal("11"), remaining_quantity=Decimal(0))
        sell_execution.side_effect = simulate
        with patch.dict(trade.BUY_FEE_CURRENCY, {"binance": "quote"}):
            result = trade._estimate_trade(
                buy, sell, buy_rates, sell_rates, Decimal("100"),
                {"withdraw_fee": "0.5", "withdraw_fee_currency": "USDT"},
            )
        self.assertLessEqual(result.buy_cost_usd + result.buy_fee_usd + result.transfer_fee_usd, Decimal("100"))
        self.assertEqual(result.transfer_fee_usd, Decimal("0.5"))
        self.assertGreater(result.price_reserve_usd, Decimal(0))
        self.assertLess(result.expected_profit_before_reserve_usd, result.expected_sell_proceeds_usd)
        sell_execution.assert_called_once()

    @patch("main.treider.trade.fetch_market_sell_execution")
    @patch("main.treider.trade.PRICE_MOVE_RESERVE_PERCENT", Decimal("0"))
    @patch("main.treider.trade.MIN_NET_PROFIT_USDT", Decimal("0"))
    def test_base_currency_buy_and_transfer_fees_reduce_sell_quantity(
        self, sell_execution: Mock,
    ) -> None:
        from main.treider import trade

        buy = market_data.SpotBookTicker("binance", "ABC/USDT", Decimal("9"), Decimal("100"), Decimal("10"), Decimal("100"))
        sell = market_data.SpotBookTicker("mexc", "ABC/USDT", Decimal("11"), Decimal("100"), Decimal("12"), Decimal("100"))
        buy_rates = fees.TradeFeeRates("binance", "ABCUSDT", Decimal(0), Decimal("0.01"), "now", buy_taker_rate=Decimal("0.01"))
        sell_rates = fees.TradeFeeRates("mexc", "ABCUSDT", Decimal(0), Decimal("0.01"), "now", sell_taker_rate=Decimal("0.01"))
        sell_execution.return_value = SimpleNamespace(
            proceeds=Decimal("100"), average_price=Decimal("10"), remaining_quantity=Decimal(0),
        )
        with patch.dict(trade.BUY_FEE_CURRENCY, {"binance": "base"}):
            result = trade._estimate_trade(
                buy, sell, buy_rates, sell_rates, Decimal("100"),
                {"withdraw_fee": "0.02", "withdraw_fee_currency": "ABC"},
            )
        self.assertEqual(result.quantity, Decimal("10"))
        self.assertEqual(result.buy_fee_base_quantity, Decimal("0.10"))
        self.assertEqual(result.quantity_after_fees, Decimal("9.88"))
        sell_execution.assert_called_once_with("mexc", "ABC/USDT", Decimal("9.88"))

    @patch("main.treider.trade.connect_db")
    def test_duplicate_signal_with_existing_settlement_does_not_insert_again(self, connect: Mock) -> None:
        from main.treider import trade

        cursor = Mock()
        cursor.fetchone.side_effect = [
            {"balance_usd": Decimal("100")},
            {"status": "pending"},
        ]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        result, reason = trade._prepare_training_trade(
            {"source_pair": "ABC/USDT"}, 17, Mock(), Mock(), Mock(), Mock(), 300,
        )
        self.assertIsNone(result)
        self.assertIn("уже запущен", reason)
        self.assertFalse(any("INSERT INTO trader_training_settlements" in call.args[0] for call in cursor.execute.call_args_list))

    @patch("main.treider.trade.get_trade_fee_rates")
    @patch("main.treider.trade.fetch_market_sell_execution")
    @patch("main.treider.trade.connect_db")
    @patch("main.treider.trade.fetch_spot_book")
    def test_partial_sell_credits_only_filled_part_and_preserves_remainder(
        self, spot_book: Mock, connect: Mock, sell_execution: Mock, fee_rates: Mock,
    ) -> None:
        from main.treider import trade

        cursor = Mock()
        cursor.fetchone.side_effect = [
            {"balance_usd": Decimal("50")},
            {
                "status": "settling", "claim_token": "lease", "position_status": "partial",
                "remaining_quantity": Decimal("3"), "sold_quantity": Decimal("0"),
                "sell_proceeds_accumulated_usd": Decimal("0"),
                "sell_fees_accumulated_usd": Decimal("0"), "buy_debit_usd": Decimal("30"),
                "settlement_attempts": 1, "planned_sell_at": None,
            },
            {"elapsed_seconds": 10},
        ]
        cursor.rowcount = 1
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        sell_execution.return_value = SimpleNamespace(
            proceeds=Decimal("20"), filled_quantity=Decimal("2"), remaining_quantity=Decimal("1"),
            average_price=Decimal("10"), best_bid=Decimal("10"), levels_used=1,
        )
        spot_book.return_value = SimpleNamespace(bid=Decimal("10"))
        fee_rates.return_value = fees.TradeFeeRates(
            "mexc", "ABCUSDT", Decimal(0), Decimal("0.001"), "now",
            sell_taker_rate=Decimal("0.001"),
        )
        trade._settle_training_trade({
            "settlement_id": 2, "signal_id": 17, "source_pair": "ABC/USDT",
            "buy_exchange": "binance", "buy_market_pair": "ABC/USDT", "buy_price": Decimal("10"),
            "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT", "quantity": Decimal("3"),
            "expected_sell_price": Decimal("11"), "stop_loss_price": Decimal("10.5"),
            "remaining_quantity": Decimal("3"), "claim_token": "lease", "settle_after_utc": None,
        })
        updates = [call for call in cursor.execute.call_args_list if "UPDATE trader_training_settlements SET status" in call.args[0]]
        self.assertEqual(len(updates), 1)
        values = updates[0].args[1]
        self.assertEqual(values[0], "partial")
        self.assertEqual(values[7], Decimal("-10.020"))
        self.assertEqual(values[9], Decimal("69.98000000"))
        self.assertEqual(values[14], Decimal("1"))


class RetryTests(unittest.TestCase):
    @patch("main.treider.service.mark_retry")
    @patch("main.treider.service.claim_next")
    @patch("main.treider.service.process_opportunity", side_effect=RuntimeError("temporary API failure"))
    def test_temporary_failure_is_retried_with_same_lease_and_attempt(
        self, process: Mock, claim_next: Mock, mark_retry: Mock,
    ) -> None:
        from main.treider import service
        from main.treider.queue import ClaimedSignal

        claim_next.return_value = ClaimedSignal(12, {"event": "matured"}, "lease-12", 3)
        self.assertTrue(service._consume_one())
        mark_retry.assert_called_once_with(12, "lease-12", "temporary API failure", 3)
        process.assert_called_once()

    @patch("main.treider.queue.connect_db")
    def test_retry_persists_backoff_attempt_and_max_age_guard(self, connect: Mock) -> None:
        from main.treider import queue

        cursor = Mock()
        cursor.rowcount = 1
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        self.assertTrue(queue.mark_retry(12, "lease", "network timeout", 3))
        sql = cursor.execute.call_args.args[0]
        self.assertIn("attempt_count >= %s", sql)
        self.assertIn("created_at < UTC_TIMESTAMP() - INTERVAL %s SECOND", sql)
        self.assertIn("next_attempt_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND)", sql)


class PersistenceTests(unittest.TestCase):
    @patch("main.treider.account.TRAINING_MODE", True)
    @patch("main.treider.account.INITIAL_BALANCE_USD", Decimal("100"))
    def test_restart_does_not_reset_existing_demo_balance(self) -> None:
        from main.treider.account import initialize_trader_account

        cursor = Mock()
        cursor.fetchone.return_value = {"balance_usd": Decimal("73.25"), "training_mode": 1}
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        balance, training = initialize_trader_account(connection)
        insert_sql = cursor.execute.call_args_list[0].args[0]
        self.assertIn("ON DUPLICATE KEY UPDATE training_mode = VALUES(training_mode)", insert_sql)
        self.assertNotIn("balance_usd =", insert_sql)
        self.assertEqual(balance, Decimal("73.25"))
        self.assertTrue(training)


class QueueFencingTests(unittest.TestCase):
    @patch("main.treider.queue.connect_db")
    def test_claim_assigns_a_unique_lease_and_increments_attempt(self, connect: Mock) -> None:
        from main.treider import queue

        cursor = Mock()
        cursor.fetchone.side_effect = [{"signal_id": 4, "payload": {"event": "opportunity_matured"}, "attempt_count": 1}]
        cursor.rowcount = 1
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        claim = queue.claim_next()
        self.assertEqual(claim.signal_id, 4)
        self.assertEqual(claim.attempt_count, 2)
        self.assertTrue(claim.lease_token)
        claim_update = cursor.execute.call_args_list[-1]
        self.assertIn("lease_version=lease_version+1", claim_update.args[0])

    @patch("main.treider.queue.connect_db")
    def test_stale_worker_cannot_finish_a_reclaimed_signal(self, connect: Mock) -> None:
        from main.treider import queue

        cursor = Mock()
        cursor.rowcount = 0
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        self.assertFalse(queue.mark_processed(4, "old-lease", "simulation_started"))
        self.assertIn("lease_token=%s", cursor.execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
