from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from main.treider import trade as _trade_module  # Load before tests temporarily patch trade_checks.
from main.treider.return_transfer import (
    _complete_return_transfer,
    _start_return_transfer,
    validate_return_route,
)


class DemoReturnRouteTests(unittest.TestCase):
    def test_unknown_return_fee_or_minimum_is_not_treated_as_zero(self) -> None:
        base = {
            "available": True, "source_exchange": "mexc", "destination_exchange": "binance",
            "estimated_seconds": 60, "withdraw_fee_currency": "USDT",
        }

        normalized, error = validate_return_route({**base, "withdraw_fee": None, "min_withdraw": "1"})
        self.assertIsNone(normalized)
        self.assertIn("неизвестна комиссия", error)

        normalized, error = validate_return_route({**base, "withdraw_fee": "0.5", "min_withdraw": None})
        self.assertIsNone(normalized)
        self.assertIn("неизвестен минимум", error)

    def test_return_route_requires_usdt_fee_currency(self) -> None:
        route, error = validate_return_route({
            "available": True, "withdraw_fee": "0.1", "withdraw_fee_currency": "BTC",
            "min_withdraw": "1", "estimated_seconds": 10,
        })

        self.assertIsNone(route)
        self.assertIn("не в USDT", error)


class DemoReturnSettlementTests(unittest.TestCase):
    def _mock_connection(self, fetchone_values: list[dict[str, object]]) -> tuple[MagicMock, Mock]:
        cursor = Mock()
        cursor.fetchone.side_effect = fetchone_values
        cursor.rowcount = 1
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        return connection, cursor

    @patch("main.treider.return_transfer.check_return_route")
    @patch("main.treider.return_transfer.connect_db")
    def test_return_send_charges_network_fee_and_schedules_network_delay(
        self, connect: Mock, route_check: Mock,
    ) -> None:
        from main.treider import return_transfer

        row = {
            "return_transfer_id": 4, "settlement_id": 8, "signal_id": 17, "run_id": "run-1",
            "sale_event_key": "8:sell:10", "source_exchange": "mexc", "destination_exchange": "binance",
            "amount_usdt": Decimal("25"), "status": "pending", "claim_token": "lease",
        }
        route_check.return_value = {
            "available": True, "withdraw_fee": "0.75", "withdraw_fee_currency": "USDT",
            "min_withdraw": "1", "estimated_seconds": 90, "network": "TRC20",
            "network_profile": "TRON", "route_status": "confirmed",
        }
        connection, cursor = self._mock_connection([row, {"balance": Decimal("30")}])
        connect.return_value = connection

        return_transfer._start_return_transfer(row)

        debit = next(call for call in cursor.execute.call_args_list
                     if "UPDATE trader_balances SET balance = balance - %s" in call.args[0])
        self.assertEqual(debit.args[1], (Decimal("25.75"), "mexc", Decimal("25.75")))
        fee_event = next(call for call in cursor.execute.call_args_list
                         if "commission:return_transfer" in repr(call.args[1]))
        self.assertIn(Decimal("0.75"), fee_event.args[1])
        transfer_update = next(call for call in cursor.execute.call_args_list
                               if "SET status = 'in_transit'" in call.args[0])
        self.assertEqual(transfer_update.args[1][0], Decimal("25"))
        self.assertEqual(transfer_update.args[1][1], Decimal("0.75"))
        self.assertEqual(transfer_update.args[1][5], Decimal("90"))
        connection.commit.assert_called_once()

    @patch("main.treider.return_transfer.connect_db")
    def test_return_receipt_credits_buy_exchange_once(self, connect: Mock) -> None:
        from main.treider import return_transfer

        row = {
            "return_transfer_id": 4, "settlement_id": 8, "signal_id": 17, "run_id": "run-1",
            "sale_event_key": "8:sell:10", "source_exchange": "mexc", "destination_exchange": "binance",
            "amount_usdt": Decimal("25"), "status": "in_transit", "claim_token": "lease",
        }
        connection, cursor = self._mock_connection([
            {**row, "fee_usdt": Decimal("0.75")}, {"balance": Decimal("10")},
        ])
        connect.return_value = connection

        return_transfer._complete_return_transfer(row)

        credit = next(call for call in cursor.execute.call_args_list
                      if "INSERT INTO trader_balances" in call.args[0])
        self.assertEqual(credit.args[1], ("binance", Decimal("25")))
        received_event = next(call for call in cursor.execute.call_args_list
                              if "return_transfer:received" in repr(call.args[1]))
        self.assertIn(Decimal("25"), received_event.args[1])
        completion = next(call for call in cursor.execute.call_args_list
                          if "SET status = 'completed'" in call.args[0])
        self.assertEqual(completion.args[1], (4, "lease"))
        connection.commit.assert_called_once()


class DemoReturnEstimateTests(unittest.TestCase):
    @patch("main.treider.trade_checks.fetch_market_sell_execution")
    @patch("main.treider.trade_checks.PRICE_MOVE_RESERVE_PERCENT", Decimal("0"))
    @patch("main.treider.trade_checks.MIN_NET_PROFIT_USDT", Decimal("0"))
    def test_return_fee_reduces_profit_and_requires_minimum(
        self, sell_execution: Mock,
    ) -> None:
        from main.treider import fees, market_data, market_rules, trade

        buy = market_data.SpotBookTicker(
            "binance", "ABC/USDT", Decimal("9.9"), Decimal("100"), Decimal("10"), Decimal("100"),
        )
        sell = market_data.SpotBookTicker(
            "mexc", "ABC/USDT", Decimal("11"), Decimal("100"), Decimal("12"), Decimal("100"),
        )
        buy_fees = fees.TradeFeeRates("binance", "ABCUSDT", Decimal(0), Decimal(0), "test", buy_taker_rate=Decimal(0))
        sell_fees = fees.TradeFeeRates("mexc", "ABCUSDT", Decimal(0), Decimal("0.01"), "test", sell_taker_rate=Decimal("0.01"))
        sell_execution.return_value = SimpleNamespace(
            proceeds=Decimal("110"), average_price=Decimal("11"), remaining_quantity=Decimal(0),
        )
        limits = market_rules.SpotMarketRules(Decimal("0.01"), Decimal("0.01"), Decimal("1"), "test")
        return_route = {
            "available": True, "source_exchange": "mexc", "destination_exchange": "binance",
            "withdraw_fee": "0.75", "withdraw_fee_currency": "USDT", "min_withdraw": "1",
            "estimated_seconds": 30, "network": "TRC20", "route_status": "confirmed",
        }

        with patch("main.treider.trade_checks.fetch_spot_market_rules", return_value=limits), \
                patch("main.treider.trade_checks.BUY_FEE_CURRENCY", {"binance": "quote"}):
            estimate = trade._estimate_trade(
                buy, sell, buy_fees, sell_fees, Decimal("100"),
                {
                    "withdraw_fee": "0", "withdraw_fee_currency": "USDT", "withdraw_step": "0.01",
                    "min_withdraw": "0.01", "return_transfer": return_route,
                },
            )

        self.assertEqual(estimate.return_transfer_fee_usd, Decimal("0.75"))
        self.assertEqual(estimate.expected_profit_before_reserve_usd, Decimal("8.15"))
        sell_execution.assert_called_once()

        too_high_minimum = {**return_route, "min_withdraw": "200"}
        with patch("main.treider.trade_checks.fetch_spot_market_rules", return_value=limits), \
                patch("main.treider.trade_checks.BUY_FEE_CURRENCY", {"binance": "quote"}):
            with self.assertRaisesRegex(ValueError, "ниже минимума обратного вывода"):
                trade._estimate_trade(
                    buy, sell, buy_fees, sell_fees, Decimal("100"),
                    {
                        "withdraw_fee": "0", "withdraw_fee_currency": "USDT", "withdraw_step": "0.01",
                        "min_withdraw": "0.01", "return_transfer": too_high_minimum,
                    },
                )

    @patch("main.treider.trade_checks.SAME_ROUTE_REENTRY_COOLDOWN_SECONDS", 0)
    @patch("main.treider.trade_checks._get_route_quality_stats")
    @patch("main.treider.trade_checks._should_enter_by_quality")
    @patch("main.treider.trade_checks._select_trade_estimate")
    @patch("main.treider.trade_checks.connect_db")
    def test_expected_return_fee_is_persisted_with_trade_estimate(
        self, connect: Mock, select: Mock, assess: Mock, route_stats: Mock,
    ) -> None:
        from main.treider.market_data import SpotBookTicker
        from main.treider.trade_checks import TradeEstimate
        from main.treider.trade_checks import _prepare_training_trade

        cursor = Mock()
        cursor.fetchone.side_effect = [
            {"balance_usd": Decimal("100")}, None,
            {"balance": Decimal("100")}, None,
        ]
        cursor.fetchall.return_value = []
        cursor.rowcount = 1
        cursor.lastrowid = 25
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection
        snapshot = {"snapshot_type": "depth", "captured_at_utc": "2026-11-16T10:00:00+00:00", "bids": [], "asks": []}
        estimate = TradeEstimate(
            quantity=Decimal("10"), quantity_after_fees=Decimal("10"),
            transfer_dust_quantity=Decimal(0), buy_average_price=Decimal("10"),
            buy_cost_usd=Decimal("100"), expected_sell_average_price=Decimal("11"),
            expected_sell_proceeds_usd=Decimal("110"), buy_fee_usd=Decimal(0),
            buy_fee_base_quantity=Decimal(0), transfer_fee_amount=Decimal(0),
            transfer_fee_currency="USDT", transfer_fee_usd=Decimal(0),
            expected_sell_fee_usd=Decimal("1"), expected_profit_before_reserve_usd=Decimal("8.25"),
            price_reserve_usd=Decimal(0), expected_profit_after_reserve_usd=Decimal("8.25"),
            shortfall_usd=Decimal(0), available_balance_usd=Decimal("100"),
            return_transfer_fee_usd=Decimal("0.75"),
            buy_book_snapshot=snapshot, sell_book_snapshot=snapshot,
        )
        select.return_value = (estimate, Decimal("10.5"), Decimal("1"), "test budget", None)
        assess.return_value = SimpleNamespace(
            accepted=True, status="unproven", expected_profit_usd=Decimal("8.25"),
            expected_roi_percent=Decimal("8"), route_sample_count=0,
            route_expectancy_usd=Decimal(0), route_win_rate_percent=Decimal(0),
            route_max_drawdown_usd=Decimal(0), reason="test", as_dict=lambda: {"accepted": True},
        )
        route_stats.return_value = SimpleNamespace(
            sample_count=0, expectancy_usd=Decimal(0), win_rate_percent=Decimal(0),
            average_roi_percent=Decimal(0), max_drawdown_usd=Decimal(0),
        )
        buy = SpotBookTicker("binance", "ABC/USDT", Decimal("10"), Decimal("10"), Decimal("10.1"), Decimal("10"))
        sell = SpotBookTicker("mexc", "ABC/USDT", Decimal("11"), Decimal("10"), Decimal("11.1"), Decimal("10"))
        fee_rates = SimpleNamespace(source="test", taker_for=lambda side: Decimal(0))
        return_route = {
            "available": True, "source_exchange": "mexc", "destination_exchange": "binance",
            "withdraw_fee": "0.75", "withdraw_fee_currency": "USDT", "min_withdraw": "1",
            "estimated_seconds": 60, "network": "TRC20", "route_status": "confirmed",
        }

        result, message = _prepare_training_trade(
            {"source_pair": "ABC/USDT", "sell": {"price": "12"}}, 17, buy, sell,
            fee_rates, fee_rates, 60,
            {"route_status": "confirmed", "withdraw_fee": "0", "withdraw_fee_currency": "ABC",
             "min_withdraw": "0.1", "return_transfer": return_route},
        )

        self.assertIs(result, estimate)
        self.assertIn("записана", message)
        insert = next(call for call in cursor.execute.call_args_list
                      if "INSERT INTO trader_training_settlements" in call.args[0])
        self.assertEqual(insert.args[0].count("%s"), len(insert.args[1]))
        self.assertIn(Decimal("0.75"), insert.args[1])


class DemoReturnReplayTests(unittest.TestCase):
    def test_replay_counts_return_transfer_fee_once_and_moves_cash_to_buy_exchange(self) -> None:
        from main.treider.replay import replay_scenario

        timestamp = "2026-11-16T10:00:00+00:00"
        buy_snapshot = {
            "snapshot_type": "depth", "captured_at_utc": timestamp,
            "bids": [["10", "20"]], "asks": [["10", "10"]],
        }
        sell_snapshot = {
            "snapshot_type": "depth", "captured_at_utc": timestamp,
            "bids": [["11", "10"]], "asks": [["11.1", "20"]],
        }

        def event(event_id: int, key: str, kind: str, exchange: str, at: int, **values: object) -> dict[str, object]:
            return {
                "event_id": event_id, "event_key": key, "run_id": "run-1", "settlement_id": 7,
                "signal_id": 17, "event_type": kind,
                "event_at_utc": f"2026-11-16T10:00:{at:02d}+00:00", "exchange_slug": exchange,
                **values,
            }

        events = [
            event(1, "7:buy", "buy", "binance", 0, market_pair="ABC/USDT", asset="ABC",
                  quantity="10", price="10", quote_amount="100", book_snapshot=buy_snapshot),
            event(2, "7:commission:buy", "commission", "binance", 0, asset="USDT",
                  fee_amount="0", fee_asset="USDT", quote_amount="0"),
            event(3, "7:transfer_sent", "transfer_sent", "binance", 1, asset="ABC", quantity="-10"),
            event(4, "7:transfer_received", "transfer_received", "mexc", 2, asset="ABC", quantity="10"),
            event(5, "7:sell:10", "sell", "mexc", 3, market_pair="ABC/USDT", asset="ABC",
                  quantity="10", price="11", quote_amount="110", book_snapshot=sell_snapshot),
            event(6, "7:commission:sell:10", "commission", "mexc", 3, asset="USDT",
                  fee_amount="0", fee_asset="USDT", quote_amount="0"),
            event(7, "7:sell:10:return_transfer:sent", "transfer_sent", "mexc", 4,
                  asset="USDT", quantity="-110", quote_amount="110"),
            event(8, "7:sell:10:commission:return_transfer", "commission", "mexc", 4,
                  asset="USDT", fee_amount="1", fee_asset="USDT", quote_amount="1"),
            event(9, "7:sell:10:return_transfer:received", "transfer_received", "binance", 5,
                  asset="USDT", quantity="110", quote_amount="110"),
        ]
        result = replay_scenario(
            {"starting_equity_usd": "101", "baseline_complete": True, "starting_balances": [
                {"exchange": "binance", "asset": "USDT", "balance": "100"},
                {"exchange": "mexc", "asset": "USDT", "balance": "1"},
            ]},
            events,
            [],
        )

        self.assertEqual(result["equity_usd"], Decimal("110"))
        self.assertEqual(result["pnl_usd"], Decimal("9"))
        self.assertEqual(result["fees_usd"], Decimal("1"))
