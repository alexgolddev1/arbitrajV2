from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from main.treider import exchange_networks, fees, market_data, market_rules, tran_emulation


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


class SignalValidationTests(unittest.TestCase):
    def _payload(self, pair: str = "ABC/USDT") -> dict:
        return {
            "event": "opportunity_matured",
            "lifetime_id": 1,
            "source_market_id": 1,
            "source_pair": pair,
            "threshold_percent": "1",
            "spread_percent": "1.2",
            "max_spread_percent": "1.2",
            "buy": {"exchange": "binance", "market_pair": pair, "price": "1"},
            "sell": {"exchange": "mexc", "market_pair": pair, "price": "1.01"},
            "network": "ETHEREUM",
            "estimated_transfer_seconds": 300,
            "opportunity_started_at_utc": "2026-10-19T00:00:00+00:00",
            "lifetime_seconds": 15,
        }

    def test_rejects_quote_currency_outside_screener_allowlist(self) -> None:
        from main.treider.signals import validate_opportunity_signal

        with self.assertRaisesRegex(ValueError, "TRY"):
            validate_opportunity_signal(self._payload("ZIL/TRY"))

    def test_accepts_configured_usdc_quote(self) -> None:
        from main.treider.signals import validate_opportunity_signal

        self.assertEqual(
            validate_opportunity_signal(self._payload("ABC/USDC"))["source_pair"],
            "ABC/USDC",
        )


class WebSocketQuoteFilterTests(unittest.TestCase):
    @patch("main.calculator.websocket_monitor.get_websocket_markets")
    @patch("main.calculator.websocket_monitor.connect_db")
    def test_monitor_loads_usdt_usdc_and_drops_try(self, connect_db: Mock, load_rows: Mock) -> None:
        from main.calculator.websocket_monitor import _load_instruments

        connection = Mock()
        connect_db.return_value = connection
        load_rows.return_value = [
            {
                "market_pair": f"ZIL/{quote}", "source_quote_symbol": quote,
                "exchange_slug": "binance", "source_market_id": index,
                "source_pair": f"ZIL/{quote}", "source_base_symbol": "ZIL",
                "exchange_name": "Binance",
            }
            for index, quote in enumerate(("USDT", "USDC", "TRY"), start=1)
        ]

        instruments = _load_instruments()

        self.assertEqual([item.source_pair for item in instruments], ["ZIL/USDT", "ZIL/USDC"])
        connection.close.assert_called_once_with()


class MarketAndRouteTests(unittest.TestCase):
    @patch("main.treider.trade.fetch_spot_market_rules")
    @patch("main.treider.trade.fetch_spot_order_book")
    @patch("main.treider.trade.TRADE_SIZE_CANDIDATES_USDT", (Decimal("100"), Decimal("25")))
    @patch("main.treider.trade.MIN_NET_PROFIT_USDT", Decimal("0"))
    def test_larger_size_rejected_on_sell_depth_while_smaller_size_is_selected(
        self, fetch_book: Mock, fetch_rules: Mock,
    ) -> None:
        from main.treider import trade

        buy = market_data.SpotBookTicker("binance", "ABC/USDT", Decimal("9.9"), Decimal("100"), Decimal("10"), Decimal("100"))
        sell = market_data.SpotBookTicker("mexc", "ABC/USDT", Decimal("10"), Decimal("100"), Decimal("10.1"), Decimal("100"))
        timestamp = market_data.datetime.now(market_data.timezone.utc)
        fetch_book.side_effect = [
            market_data.SpotOrderBook("binance", "ABC/USDT", [["9.9", "100"]], [["10", "100"]], timestamp),
            market_data.SpotOrderBook("mexc", "ABC/USDT", [["10", "5"]], [["10.1", "100"]], timestamp),
        ]
        fetch_rules.return_value = market_rules.SpotMarketRules(
            Decimal("0.01"), Decimal("0.01"), Decimal("1"), "test",
        )
        rates = fees.TradeFeeRates("binance", "ABCUSDT", Decimal(0), Decimal(0), "test", buy_taker_rate=Decimal(0))
        sell_rates = fees.TradeFeeRates("mexc", "ABCUSDT", Decimal(0), Decimal(0), "test", sell_taker_rate=Decimal(0))
        cursor = Mock()
        cursor.fetchall.return_value = []

        estimate, stop_price, _, _, error = trade._select_trade_estimate(
            cursor, 17, buy, sell, rates, sell_rates, Decimal("100"),
            {"withdraw_fee": "0", "withdraw_fee_currency": "ABC", "withdraw_step": "0.01", "min_withdraw": "0.01"},
            Decimal("11"),
        )

        self.assertIsNone(error)
        self.assertEqual(estimate.buy_cost_usd, Decimal("25.00"))
        self.assertEqual(estimate.quantity_after_fees, Decimal("2.50"))
        self.assertEqual(estimate.expected_profit_after_reserve_usd, Decimal("0.000"))
        self.assertLess(stop_price, Decimal("11"))
        self.assertEqual(fetch_book.call_count, 2)
        self.assertEqual(fetch_rules.call_count, 2)

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
        self.assertEqual(full.book_snapshot["bids"], [["10", "2"], ["9", "3"], ["8", "4"]])
        self.assertNotIn("headers", full.book_snapshot)
        partial = market_data.simulate_market_sell(
            "mexc", "ABC/USDT", Decimal("7"), [["10", "2"], ["9", "3"]]
        )
        self.assertEqual(partial.filled_quantity, Decimal("5"))
        self.assertEqual(partial.remaining_quantity, Decimal("2"))
        self.assertEqual(partial.proceeds, Decimal("47"))

    @patch("main.treider.market_data._get_json")
    def test_fetched_sell_keeps_only_used_public_bid_snapshot(self, get_json: Mock) -> None:
        get_json.return_value = {"bids": [["10", "2"], ["9", "3"]], "apiKey": "private"}
        result = market_data.fetch_market_sell_execution("binance", "ABC/USDT", Decimal("3"))

        self.assertEqual(result.book_snapshot["bids"], [["10", "2"], ["9", "3"]])
        self.assertIn("captured_at_utc", result.book_snapshot)
        self.assertNotIn("apiKey", result.book_snapshot)

    def test_demo_event_insert_uses_stable_key_and_explicit_snapshot_only(self) -> None:
        from main.treider import trade

        cursor = Mock()
        snapshot = {"snapshot_type": "bids", "bids": [["9", "2"]]}
        trade._log_demo_event(cursor, 4, 12, "4:sell:2", "sell", "mexc", "ABC/USDT", "ABC",
                              Decimal("2"), Decimal("9"), Decimal("18"), book_snapshot=snapshot)
        sql, params = cursor.execute.call_args.args
        self.assertIn("ON DUPLICATE KEY UPDATE event_key", sql)
        self.assertEqual(params[0], "4:sell:2")
        self.assertEqual(params[2:6], (4, 12, "sell", "mexc"))
        self.assertEqual(params[-1], '{"snapshot_type":"bids","bids":[["9","2"]]}')

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

    def test_immediate_strategy_ignores_tp_sl_while_legacy_strategy_keeps_them(self) -> None:
        from main.treider import trade

        self.assertEqual(
            trade._strategy_trigger("immediate_after_transfer", Decimal("90"), Decimal("110"), Decimal("95")),
            "immediate_after_transfer",
        )
        self.assertIsNone(trade._strategy_trigger("tp_sl", Decimal("100"), Decimal("110"), Decimal("95")))
        self.assertEqual(
            trade._strategy_trigger("tp_sl", Decimal("90"), Decimal("110"), Decimal("95")),
            "stop_loss",
        )

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

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_quick_polygon_route_matches_binance_and_mexc_labels(self) -> None:
        networks = {
            "binance": [{
                "network_code": "MATIC", "network_name": "Polygon POS",
                "withdraw_enabled": True, "deposit_enabled": True,
            }],
            "mexc": [{
                "network_code": "MATIC", "network_name": "Quickswap",
                "withdraw_enabled": True, "deposit_enabled": True,
            }],
        }

        self.assertEqual(exchange_networks._network_profile(networks["binance"][0]), "POLYGON")
        self.assertEqual(exchange_networks._network_profile(networks["mexc"][0]), "POLYGON")
        routes = exchange_networks.build_transfer_routes(
            "QUICK", "binance", "mexc", {"networks": networks},
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["network_profile"], "POLYGON")
        self.assertEqual(routes[0]["route_status"], "unconfirmed")

    @patch.object(exchange_networks, "ALLOW_UNCONFIRMED_ROUTE_DEMO", True)
    @patch.object(exchange_networks, "ALLOW_DISABLED_ROUTE_DEMO", False)
    def test_polyx_and_polymesh_network_names_match(self) -> None:
        networks = {
            "binance": [{
                "network_code": "POLYX", "network_name": "POLYX",
                "withdraw_enabled": True, "deposit_enabled": True,
            }],
            "mexc": [{
                "network_code": "POLYMESH", "network_name": "POLYX",
                "withdraw_enabled": True, "deposit_enabled": True,
            }],
        }

        self.assertEqual(exchange_networks._network_profile(networks["binance"][0]), "POLYMESH")
        self.assertEqual(exchange_networks._network_profile(networks["mexc"][0]), "POLYMESH")
        routes = exchange_networks.build_transfer_routes(
            "POLYX", "binance", "mexc", {"networks": networks},
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["network_profile"], "POLYMESH")
        self.assertEqual(routes[0]["route_status"], "unconfirmed")

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
    def test_market_filters_parse_documented_quantity_fields(self) -> None:
        binance = market_rules.parse_market_rules("binance", {"symbols": [{
            "symbol": "ABCUSDT", "filters": [
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True},
            ],
        }]}, "ABC/USDT", market_order=True)
        bybit = market_rules.parse_market_rules("bybit", {"retCode": 0, "result": {"list": [{
            "symbol": "ABCUSDT", "lotSizeFilter": {
                "basePrecision": "0.001", "minOrderAmt": "5",
            },
        }]}}, "ABC/USDT", market_order=True)
        okx = market_rules.parse_market_rules("okx", {"code": "0", "data": [{
            "instId": "ABC-USDT", "lotSz": "0.01", "minSz": "0.1",
        }]}, "ABC/USDT", market_order=True)
        mexc = market_rules.parse_market_rules("mexc", {"symbols": [{
            "symbol": "ABCUSDT", "baseAssetPrecision": 3,
            "baseSizePrecision": "0.001", "quoteAmountPrecisionMarket": "5",
        }]}, "ABC/USDT", market_order=True)

        self.assertEqual(binance.quantity_step, Decimal("0.01"))
        self.assertEqual(binance.min_quantity, Decimal("0.1"))
        self.assertEqual(binance.min_notional, Decimal("5"))
        self.assertEqual(bybit.quantity_step, Decimal("0.001"))
        self.assertEqual(bybit.min_notional, Decimal("5"))
        self.assertEqual(okx.min_quantity, Decimal("0.1"))
        self.assertEqual(mexc.quantity_step, Decimal("0.001"))
        self.assertEqual(mexc.min_quantity, Decimal("0.001"))

    def test_mexc_zero_minimum_quantity_uses_documented_base_decimal_precision_for_step(self) -> None:
        mexc = market_rules.parse_market_rules("mexc", {"symbols": [{
            "symbol": "GNSUSDT",
            "status": "1",
            "baseAssetPrecision": 2,
            "baseSizePrecision": "0",
            "quoteAmountPrecisionMarket": "1",
            "filters": [{"filterType": "PERCENT_PRICE_BY_SIDE"}],
        }]}, "GNS/USDT", market_order=True)

        self.assertEqual(mexc.quantity_step, Decimal("0.01"))
        self.assertEqual(mexc.min_quantity, Decimal("0"))
        self.assertEqual(mexc.min_notional, Decimal("1"))

    def test_mexc_missing_step_and_base_precision_remains_unknown(self) -> None:
        with self.assertRaises(market_rules.UnknownMarketRules):
            market_rules.parse_market_rules("mexc", {"symbols": [{
                "symbol": "ABCUSDT",
                "baseSizePrecision": "0",
                "quoteAmountPrecisionMarket": "1",
            }]}, "ABC/USDT", market_order=True)

    def test_unknown_withdraw_increment_is_rejected(self) -> None:
        with self.assertRaises(market_rules.UnknownMarketRules):
            market_rules.withdraw_step({"exchange": "mexc", "withdraw_precision": None})

    def test_arbitrary_quantity_step_rounds_down_without_price_heuristics(self) -> None:
        self.assertEqual(
            market_rules.floor_to_step(Decimal("1.239"), Decimal("0.05")), Decimal("1.20")
        )

    @patch("main.treider.trade.fetch_market_sell_execution")
    @patch("main.treider.trade.PRICE_MOVE_RESERVE_PERCENT", Decimal("0"))
    @patch("main.treider.trade.MIN_NET_PROFIT_USDT", Decimal("0"))
    def test_withdraw_rounding_preserves_dust_and_reprices_expected_profit(self, sell_execution: Mock) -> None:
        from main.treider import trade

        buy = market_data.SpotBookTicker("binance", "ABC/USDT", Decimal("9"), Decimal("100"), Decimal("10"), Decimal("100"))
        sell = market_data.SpotBookTicker("mexc", "ABC/USDT", Decimal("11"), Decimal("100"), Decimal("12"), Decimal("100"))
        rates = fees.TradeFeeRates("binance", "ABCUSDT", Decimal(0), Decimal("0.01"), "now", buy_taker_rate=Decimal("0.01"))
        sell_rates = fees.TradeFeeRates("mexc", "ABCUSDT", Decimal(0), Decimal("0.01"), "now", sell_taker_rate=Decimal("0.01"))
        sell_execution.return_value = SimpleNamespace(
            proceeds=Decimal("9.6") * Decimal("11"), average_price=Decimal("11"), remaining_quantity=Decimal(0),
        )
        with patch.dict(trade.BUY_FEE_CURRENCY, {"binance": "base"}), \
                patch.object(trade, "fetch_spot_market_rules", return_value=market_rules.SpotMarketRules(
                    Decimal("0.01"), Decimal("0.01"), Decimal("1"), "test"
                )):
            estimate = trade._estimate_trade(
                buy, sell, rates, sell_rates, Decimal("100"),
                {"withdraw_fee": "0", "withdraw_fee_currency": "ABC", "withdraw_step": "0.4", "min_withdraw": "1"},
            )

        self.assertEqual(estimate.quantity_after_fees, Decimal("9.6"))
        self.assertEqual(estimate.transfer_dust_quantity, Decimal("0.3"))
        self.assertEqual(estimate.expected_sell_proceeds_usd, Decimal("105.6"))
        sell_execution.assert_called_once_with("mexc", "ABC/USDT", Decimal("9.6"))

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
        with patch.dict(trade.BUY_FEE_CURRENCY, {"binance": "quote"}), \
                patch.object(trade, "fetch_spot_market_rules", return_value=market_rules.SpotMarketRules(
                    Decimal("0.01"), Decimal("0.01"), Decimal("1"), "test"
                )):
            result = trade._estimate_trade(
                buy, sell, buy_rates, sell_rates, Decimal("100"),
                {"withdraw_fee": "0.5", "withdraw_fee_currency": "USDT", "withdraw_step": "0.01", "min_withdraw": "0.01"},
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
        with patch.dict(trade.BUY_FEE_CURRENCY, {"binance": "base"}), \
                patch.object(trade, "fetch_spot_market_rules", return_value=market_rules.SpotMarketRules(
                    Decimal("0.01"), Decimal("0.01"), Decimal("1"), "test"
                )):
            result = trade._estimate_trade(
                buy, sell, buy_rates, sell_rates, Decimal("100"),
                {"withdraw_fee": "0.02", "withdraw_fee_currency": "ABC", "withdraw_step": "0.01", "min_withdraw": "0.01"},
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

    @patch("main.treider.trade._persist_sale_dust")
    @patch("main.treider.trade.fetch_spot_market_rules")
    @patch("main.treider.trade.get_trade_fee_rates")
    @patch("main.treider.trade.fetch_market_sell_execution")
    @patch("main.treider.trade.connect_db")
    @patch("main.treider.trade.fetch_spot_book")
    def test_partial_sell_credits_only_filled_part_and_preserves_remainder(
        self, spot_book: Mock, connect: Mock, sell_execution: Mock, fee_rates: Mock, market_rules: Mock,
        persist_dust: Mock,
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
        spot_book.return_value = SimpleNamespace(bid=Decimal("9"), bid_size=Decimal("3"))
        fee_rates.return_value = fees.TradeFeeRates(
            "mexc", "ABCUSDT", Decimal(0), Decimal("0.001"), "now",
            sell_taker_rate=Decimal("0.001"),
        )
        market_rules.return_value = SimpleNamespace(
            quantity_step=Decimal("0.1"), min_quantity=Decimal("0.1"), min_notional=Decimal("25"),
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
        self.assertEqual(values[1], "stop_loss")
        self.assertEqual(values[7], Decimal("-0.020"))
        self.assertEqual(values[9], Decimal("69.98000000"))
        self.assertEqual(values[16], Decimal("1"))
        sell_execution.assert_called_once_with("mexc", "ABC/USDT", Decimal("3"))
        persist_dust.assert_not_called()


class SettlementClaimTests(unittest.TestCase):
    @patch("main.treider.trade.connect_db")
    def test_open_timeout_position_is_claimed_once_when_due(self, connect: Mock) -> None:
        from main.treider import trade

        cursor = Mock()
        cursor.fetchone.return_value = {
            "settlement_id": 23, "status": "open", "remaining_quantity": Decimal("2.5"),
        }
        cursor.rowcount = 1
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection

        claimed = trade._claim_due_trade()

        self.assertEqual(claimed["settlement_id"], 23)
        self.assertEqual(claimed["remaining_quantity"], Decimal("2.5"))
        select_sql = cursor.execute.call_args_list[0].args[0]
        self.assertIn("status IN ('pending','partial','open')", select_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", select_sql)
        claim_sql, claim_params = cursor.execute.call_args_list[1].args
        self.assertIn("WHERE settlement_id = %s AND status = %s", claim_sql)
        self.assertEqual(claim_params[1:], (23, "open"))
        connection.commit.assert_called_once()

    @patch("main.treider.trade.connect_db")
    def test_timeout_retry_does_not_reset_position_or_cost_accounting(self, connect: Mock) -> None:
        from main.treider import trade

        connection = MagicMock()
        connect.return_value = connection
        trade._retry_settlement(23, "timeout", "lease")

        sql, params = connection.cursor.return_value.__enter__.return_value.execute.call_args.args
        self.assertIn("'open'", sql)
        self.assertNotIn("remaining_quantity", sql)
        self.assertNotIn("buy_debit_usd", sql)
        self.assertIn("settle_after_utc = UTC_TIMESTAMP() + INTERVAL %s SECOND", sql)
        self.assertEqual(params[-2:], (23, "lease"))


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
    @patch("main.treider.network_check._fetch_routes")
    def test_route_keeps_minimum_withdraw_from_source_network(self, fetch_routes: Mock) -> None:
        from main.treider.network_check import check_transfer_network

        fetch_routes.return_value = {"routes": [{
            "route_status": "confirmed", "network_profile": "ERC20", "network_name": "Ethereum",
            "source_network": {"exchange": "binance", "min_withdraw": "0.25"},
            "destination_network": {},
        }]}

        result = check_transfer_network({
            "source_pair": "ABC/USDT", "buy": {"exchange": "binance"}, "sell": {"exchange": "okx"},
        })

        self.assertEqual(result["min_withdraw"], "0.25")
        self.assertEqual(result["source_network"]["min_withdraw"], "0.25")

    @patch("main.treider.account._git_commit", return_value="a" * 40)
    @patch("main.treider.account._demo_config_snapshot", return_value={"strategy": "tp_sl"})
    def test_demo_run_restart_reuses_same_commit_and_configuration(
        self, _config: Mock, _commit: Mock,
    ) -> None:
        from main.treider.account import _ensure_demo_run

        cursor = Mock()
        cursor.fetchone.return_value = {
            "run_id": "existing-run", "config_hash": __import__("hashlib").sha256(b'{"strategy":"tp_sl"}').hexdigest(),
            "git_commit": "a" * 40,
        }
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        self.assertEqual(_ensure_demo_run(connection), "existing-run")
        self.assertEqual(cursor.execute.call_count, 1)

    @patch("main.treider.account._git_commit", return_value="b" * 40)
    @patch("main.treider.account._demo_config_snapshot", return_value={"strategy": "immediate_after_transfer"})
    def test_changed_demo_configuration_creates_a_new_run_without_resetting_balances(
        self, _config: Mock, _commit: Mock,
    ) -> None:
        from main.treider.account import _ensure_demo_run

        cursor = Mock()
        cursor.fetchone.side_effect = [
            {"run_id": "old-run", "config_hash": "old-hash", "git_commit": "a" * 40},
            {"balance_usd": Decimal("7")}, {"active_count": 0},
        ]
        cursor.fetchall.side_effect = [[], [], [
            {"exchange_slug": "binance", "asset": "USDT", "balance": Decimal("73")},
        ]]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        new_run = _ensure_demo_run(connection)

        self.assertNotEqual(new_run, "old-run")
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("status = 'superseded'" in statement for statement in statements))
        self.assertTrue(any("INSERT INTO trader_demo_run_state" in statement for statement in statements))
        inserted_run = next(call for call in cursor.execute.call_args_list if "INSERT INTO trader_demo_runs (run_id" in call.args[0] and "INSERT IGNORE" not in call.args[0])
        starting_balances = __import__("json").loads(inserted_run.args[1][4])
        self.assertIn({"exchange": "binance", "asset": "USDT", "balance": "73"}, starting_balances)

    def test_replay_latency_never_selects_a_snapshot_before_execution_time(self) -> None:
        from main.treider.replay import select_snapshot

        event = {
            "event_at_utc": "2026-10-24T10:00:00+00:00", "exchange_slug": "binance",
            "market_pair": "ABC/USDT", "book_snapshot": {"buy": {"snapshot_type": "depth", "asks": [["10", "2"]]}},
        }
        snapshots = [
            {"exchange_slug": "binance", "market_pair": "ABC/USDT", "observed_at_utc": "2026-10-24T10:00:04+00:00",
             "book_snapshot": {"snapshot_type": "depth", "asks": [["9", "2"]]}},
            {"exchange_slug": "binance", "market_pair": "ABC/USDT", "observed_at_utc": "2026-10-24T10:00:06+00:00",
             "book_snapshot": {"snapshot_type": "depth", "asks": [["11", "2"]]}},
        ]

        selected = select_snapshot(event, snapshots, "buy", Decimal("5"))

        self.assertEqual(selected["asks"][0][0], "11")
        self.assertEqual(select_snapshot(event, snapshots, "buy", Decimal(0))["asks"][0][0], "10")

    def test_replay_reports_missing_delayed_books_and_degraded_depth(self) -> None:
        from main.treider.replay import replay_scenario

        run = {"starting_equity_usd": Decimal("100"), "baseline_complete": True,
               "starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}]}
        events = [{
            "event_id": 1, "event_key": "1:buy", "settlement_id": 1, "event_type": "buy",
            "event_at_utc": "2026-10-24T10:00:00+00:00", "exchange_slug": "binance",
            "market_pair": "ABC/USDT", "asset": "ABC", "quantity": Decimal("2"),
            "quote_amount": Decimal("20"), "book_snapshot": {
                "buy": {"snapshot_type": "depth", "asks": [["10", "2"]], "bids": [["9", "2"]]},
            },
        }]

        missing = replay_scenario(run, events, [], delay_seconds=Decimal("2"))
        reduced = replay_scenario(run, events, [], depth_reduction_percent=Decimal("50"))

        self.assertFalse(missing["complete"])
        self.assertTrue(any("no book snapshot" in gap for gap in missing["gaps"]))
        self.assertFalse(reduced["complete"])
        self.assertTrue(any("partial execution" in gap for gap in reduced["gaps"]))

    def test_replay_marks_remaining_coin_and_matches_sell_fee_by_event_key(self) -> None:
        from main.treider.replay import replay_scenario

        run = {
            "starting_equity_usd": Decimal("100"), "baseline_complete": True,
            "starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}],
        }
        events = [
            {"event_id": 1, "event_key": "8:buy", "settlement_id": 8, "event_type": "buy",
             "event_at_utc": "2026-10-24T10:00:00Z", "exchange_slug": "binance",
             "market_pair": "ABC/USDT", "asset": "ABC", "quantity": "2", "quote_amount": "20",
             "book_snapshot": {"buy": {"snapshot_type": "depth", "asks": [["10", "2"]], "bids": [["9", "2"]]}}},
            {"event_id": 2, "event_key": "8:commission:buy", "settlement_id": 8, "event_type": "commission",
             "event_at_utc": "2026-10-24T10:00:01Z", "exchange_slug": "binance", "fee_amount": "0.1",
             "fee_asset": "USDT", "quote_amount": "0.1"},
            {"event_id": 3, "event_key": "8:sell:1", "settlement_id": 8, "event_type": "sell",
             "event_at_utc": "2026-10-24T10:00:02Z", "exchange_slug": "binance",
             "market_pair": "ABC/USDT", "asset": "ABC", "quantity": "1", "quote_amount": "11",
             "book_snapshot": {"sell": {"snapshot_type": "depth", "bids": [["11", "1"]]}}},
            {"event_id": 4, "event_key": "8:commission:sell:1", "settlement_id": 8, "event_type": "commission",
             "event_at_utc": "2026-10-24T10:00:03Z", "exchange_slug": "binance", "fee_amount": "0.11",
             "fee_asset": "USDT", "quote_amount": "0.11"},
            {"event_id": 5, "event_key": "8:commission:sell:unrelated", "settlement_id": 8, "event_type": "commission",
             "event_at_utc": "2026-10-24T10:00:04Z", "exchange_slug": "binance", "fee_amount": "9",
             "fee_asset": "USDT", "quote_amount": "9"},
        ]

        result = replay_scenario(run, events, [])

        self.assertEqual(result["fees_usd"], Decimal("0.21"))
        self.assertEqual(result["equity_usd"], Decimal("90.79"))
        self.assertTrue(any("insufficient bid depth" in gap for gap in result["gaps"]))
        self.assertFalse(result["complete"])

    def test_replay_sell_consumes_snapshot_depth_and_new_snapshot_restores_it(self) -> None:
        from main.treider.replay import replay_scenario

        run = {"starting_equity_usd": Decimal("100"), "baseline_complete": True,
               "starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}]}
        buy_book = {"snapshot_type": "depth", "captured_at_utc": "2026-10-24T10:00:00Z",
                    "asks": [["10", "2"]], "bids": [["9", "2"]]}
        reused_sell_book = {"snapshot_type": "depth", "captured_at_utc": "2026-10-24T10:00:02Z",
                            "bids": [["11", "1"]]}
        refreshed_sell_book = {"snapshot_type": "depth", "captured_at_utc": "2026-10-24T10:00:03Z",
                               "bids": [["12", "1"]]}
        events = [
            {"event_id": 1, "event_key": "10:buy", "settlement_id": 10, "event_type": "buy",
             "event_at_utc": "2026-10-24T10:00:00Z", "exchange_slug": "binance",
             "market_pair": "ABC/USDT", "asset": "ABC", "quantity": "2",
             "book_snapshot": {"buy": buy_book}},
            {"event_id": 2, "event_key": "10:sell:1", "settlement_id": 10, "event_type": "sell",
             "event_at_utc": "2026-10-24T10:00:02Z", "exchange_slug": "binance",
             "market_pair": "ABC/USDT", "asset": "ABC", "quantity": "1",
             "book_snapshot": {"sell": reused_sell_book}},
            {"event_id": 3, "event_key": "10:sell:2", "settlement_id": 10, "event_type": "sell",
             "event_at_utc": "2026-10-24T10:00:02.500Z", "exchange_slug": "binance",
             "market_pair": "ABC/USDT", "asset": "ABC", "quantity": "1",
             "book_snapshot": {"sell": reused_sell_book}},
            {"event_id": 4, "event_key": "10:sell:3", "settlement_id": 10, "event_type": "sell",
             "event_at_utc": "2026-10-24T10:00:03Z", "exchange_slug": "binance",
             "market_pair": "ABC/USDT", "asset": "ABC", "quantity": "1",
             "book_snapshot": {"sell": refreshed_sell_book}},
        ]

        result = replay_scenario(run, events, [])

        self.assertEqual(result["equity_usd"], Decimal("103"))
        self.assertTrue(any("insufficient book depth" in gap for gap in result["gaps"]))
        self.assertFalse(result["complete"])

    def test_replay_equity_liquidates_full_bid_depth_on_matching_exchange(self) -> None:
        from datetime import datetime, timezone
        from main.treider.replay import _equity

        events = [{"event_type": "buy", "exchange_slug": "binance", "asset": "ABC",
                   "market_pair": "ABC/USDT"}]
        at = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
        gaps: list[str] = []
        value = _equity(
            {("binance", "ABC"): Decimal("10")},
            {("binance", "ABC/USDT"): {"bids": [[Decimal("10"), Decimal("1")],
                                                    [Decimal("1"), Decimal("9")]], "observed_at": at}},
            {("binance", "ABC/USDT"): (Decimal(0), "USDT")}, events, at, Decimal(1), gaps,
        )

        self.assertEqual(value, Decimal("19"))
        self.assertFalse(gaps)

    def test_replay_equity_does_not_borrow_another_exchange_book(self) -> None:
        from datetime import datetime, timezone
        from main.treider.replay import _equity

        events = [{"event_type": "buy", "exchange_slug": "binance", "asset": "ABC",
                   "market_pair": "ABC/USDT"}]
        at = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
        gaps: list[str] = []
        value = _equity(
            {("binance", "ABC"): Decimal("10")},
            {("mexc", "ABC/USDT"): {"bids": [[Decimal("10"), Decimal("10")]], "observed_at": at}},
            {("binance", "ABC/USDT"): (Decimal(0), "USDT")}, events, at, Decimal(1), gaps,
        )

        self.assertEqual(value, Decimal(0))
        self.assertTrue(any("no bid book" in gap for gap in gaps))

    def test_replay_equity_marks_stale_and_insufficient_books_incomplete(self) -> None:
        from datetime import datetime, timedelta, timezone
        from main.treider.replay import _equity

        events = [{"event_type": "buy", "exchange_slug": "binance", "asset": "ABC",
                   "market_pair": "ABC/USDT"}]
        at = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
        fees = {("binance", "ABC/USDT"): (Decimal(0), "USDT")}
        stale_gaps: list[str] = []
        _equity(
            {("binance", "ABC"): Decimal("10")},
            {("binance", "ABC/USDT"): {"bids": [[Decimal("10"), Decimal("10")]],
                                         "observed_at": at - timedelta(seconds=31)}},
            fees, events, at, Decimal(1), stale_gaps,
        )
        shallow_gaps: list[str] = []
        _equity(
            {("binance", "ABC"): Decimal("10")},
            {("binance", "ABC/USDT"): {"bids": [[Decimal("10"), Decimal("1")]], "observed_at": at}},
            fees, events, at, Decimal(1), shallow_gaps,
        )

        self.assertTrue(any("stale bid book" in gap for gap in stale_gaps))
        self.assertTrue(any("insufficient bid depth" in gap for gap in shallow_gaps))

    def test_replay_drawdown_ignores_incomplete_equity_points(self) -> None:
        from datetime import datetime, timezone
        from main.treider.replay import _drawdown_from_points

        at = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
        incomplete_dip = [
            (at, Decimal("0"), False, Decimal("0")),
            (at, Decimal("100"), True, Decimal("0")),
        ]
        trusted_drawdown = [
            (at, Decimal("100"), True, Decimal("0")),
            (at, Decimal("80"), True, Decimal("0")),
            (at, Decimal("100"), True, Decimal("0")),
        ]

        self.assertEqual(_drawdown_from_points(Decimal("100"), incomplete_dip, True), Decimal("0"))
        self.assertEqual(_drawdown_from_points(Decimal("100"), trusted_drawdown, True), Decimal("20"))

    def test_replay_capital_deposit_does_not_create_strategy_pnl_or_drawdown(self) -> None:
        from main.treider.replay import replay_scenario

        run = {"starting_equity_usd": Decimal("100"), "baseline_complete": True,
               "starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}]}
        events = [
            {"event_id": 1, "event_key": "deposit-1", "event_type": "capital_movement",
             "event_at_utc": "2026-10-24T10:00:00Z", "exchange_slug": "binance", "amount_usd": "100"},
            {"event_id": 2, "event_key": "withdrawal-1", "event_type": "capital_movement",
             "event_at_utc": "2026-10-24T10:01:00Z", "exchange_slug": "binance", "amount_usd": "-100"},
        ]

        result = replay_scenario(run, events, [])

        self.assertEqual(result["pnl_usd"], Decimal("0"))
        self.assertEqual(result["max_drawdown_usd"], Decimal("0"))
        self.assertEqual([point["equity_usd"] - point["net_capital_flows_usd"]
                          for point in result["equity_points"]], [Decimal("100"), Decimal("100")])

    def test_replay_transfer_coin_fee_moves_and_deducts_exchange_balances(self) -> None:
        from main.treider.replay import replay_scenario

        run = {"starting_equity_usd": Decimal("100"), "baseline_complete": True,
               "starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}]}
        events = [
            {"event_id": 1, "event_key": "9:buy", "settlement_id": 9, "event_type": "buy",
             "event_at_utc": "2026-10-24T10:00:00Z", "exchange_slug": "binance", "market_pair": "ABC/USDT",
             "asset": "ABC", "quantity": "2", "quote_amount": "20",
             "book_snapshot": {"buy": {"snapshot_type": "depth", "asks": [["10", "2"]], "bids": [["9", "2"]]}}},
            {"event_id": 2, "event_key": "9:transfer_sent", "settlement_id": 9, "event_type": "transfer_sent",
             "event_at_utc": "2026-10-24T10:00:01Z", "exchange_slug": "binance", "asset": "ABC", "quantity": "-1.9"},
            {"event_id": 3, "event_key": "9:transfer_received", "settlement_id": 9, "event_type": "transfer_received",
             "event_at_utc": "2026-10-24T10:00:02Z", "exchange_slug": "mexc", "asset": "ABC", "quantity": "1.9"},
            {"event_id": 4, "event_key": "9:commission:transfer", "settlement_id": 9, "event_type": "commission",
             "event_at_utc": "2026-10-24T10:00:03Z", "exchange_slug": "binance", "asset": "ABC",
             "fee_amount": "0.1", "fee_asset": "ABC", "quote_amount": "1"},
            {"event_id": 5, "event_key": "9:sell:1.9", "settlement_id": 9, "event_type": "sell",
             "event_at_utc": "2026-10-24T10:00:04Z", "exchange_slug": "mexc", "market_pair": "ABC/USDT",
             "asset": "ABC", "quantity": "1.9", "quote_amount": "20.9",
             "book_snapshot": {"sell": {"snapshot_type": "depth", "bids": [["11", "1.9"]]}}},
            {"event_id": 6, "event_key": "9:commission:sell:1.9", "settlement_id": 9, "event_type": "commission",
             "event_at_utc": "2026-10-24T10:00:05Z", "exchange_slug": "mexc", "fee_amount": "0.209",
             "fee_asset": "USDT", "quote_amount": "0.209"},
        ]

        result = replay_scenario(run, events, [])

        self.assertEqual(result["equity_usd"], Decimal("100.691"))
        self.assertEqual(result["fees_usd"], Decimal("1.209"))
        self.assertFalse(any("insufficient" in gap for gap in result["gaps"]), result["gaps"])
        transit_point = next(point for point in result["equity_points"]
                             if point["at_utc"] == "2026-10-24T10:00:01+00:00")
        self.assertEqual(transit_point["equity_usd"], Decimal("98.0"))

    @patch("main.treider.trade.connect_db")
    def test_demo_balance_read_is_scoped_to_requested_exchange(self, connect: Mock) -> None:
        from main.treider.trade import _read_balance

        cursor = Mock()
        cursor.fetchone.return_value = {"balance": Decimal("42.5")}
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection

        self.assertEqual(_read_balance("ByBit"), Decimal("42.5"))
        self.assertIn("trader_balances", cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], ("bybit",))

    @patch("main.treider.account.TRAINING_MODE", True)
    @patch("main.treider.account.INITIAL_BALANCE_USD", Decimal("100"))
    @patch("main.treider.account._ensure_demo_run", return_value="run")
    def test_restart_does_not_reset_existing_demo_balance(self, _ensure_run: Mock) -> None:
        from main.treider.account import initialize_trader_account

        cursor = Mock()
        cursor.fetchone.return_value = {
            "balance_usd": Decimal("73.25"), "training_mode": 1, "initial_capital_funded": 1,
        }
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        balance, training = initialize_trader_account(connection)
        insert_sql = cursor.execute.call_args_list[0].args[0]
        self.assertIn("ON DUPLICATE KEY UPDATE training_mode = VALUES(training_mode)", insert_sql)
        self.assertNotIn("balance_usd =", insert_sql)
        self.assertEqual(balance, Decimal("73.25"))
        self.assertTrue(training)

    @patch("main.treider.account.TRAINING_MODE", True)
    @patch("main.treider.account.INITIAL_BALANCE_USD", Decimal("100"))
    @patch("main.treider.account.INITIAL_EXCHANGE_SLUG", "bybit")
    @patch("main.treider.account._ensure_demo_run", return_value="run")
    def test_first_initial_funding_is_exchange_scoped_and_marked_once(self, _ensure_run: Mock) -> None:
        from main.treider.account import initialize_trader_account

        cursor = Mock()
        cursor.fetchone.return_value = {
            "balance_usd": Decimal("0"), "training_mode": 1, "initial_capital_funded": 0,
        }
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        initialize_trader_account(connection)

        funding_call = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO trader_balances" in call.args[0]
        )
        self.assertEqual(funding_call.args[1], ("bybit", Decimal("100")))
        funded_call = next(
            call for call in cursor.execute.call_args_list
            if "initial_capital_funded = 1" in call.args[0]
        )
        self.assertEqual(funded_call.args[1], ("bybit",))
        connection.commit.assert_called_once()


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
