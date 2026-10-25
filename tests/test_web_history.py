from __future__ import annotations

import unittest
import csv
import json
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from main.web import (
    _aggregate_lifetime_groups, _count_trader_positions, _load_logged_failures, _load_opportunities, _record_capital_movement,
    _demo_operation_csv, _render_history, _render_page, _render_replay_page,
    _render_trader_page, _value_trader_positions, _drawdown_metrics,
)


class TraderHistoryTests(unittest.TestCase):
    def test_history_keeps_old_logged_failure_even_after_later_success(self) -> None:
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {"lifetime_id": 7, "decision": "processed", "error_message": None,
             "created_at": datetime.now(timezone.utc).replace(tzinfo=None)},
            {"lifetime_id": 7, "decision": "rejected", "error_message": "нет доступного маршрута",
             "created_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)},
        ]

        result = _load_logged_failures(cursor, [7], historical=True)

        self.assertTrue(result[7]["route_unavailable"])

    @patch("main.web.read_config", return_value={"interval_minutes": 1})
    @patch("main.web.connect_db")
    def test_opportunity_route_status_query_uses_non_reserved_alias(self, connect_db, _read_config) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cursor.fetchall.side_effect = [
            [{
                "lifetime_id": 7, "source_market_id": 1, "source_pair": "ABC/USDT",
                "base_symbol": "ABC", "source_quote": "USDT", "price_difference_percent": Decimal("2"),
                "exchange_name": "binance", "venue_pair": "ABC/USDT", "price": Decimal("1"),
                "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT", "sell_price": Decimal("1.02"),
                "last_updated": now, "started_at": now,
            }],
            [{
                "lifetime_id": 7, "decision": "rejected", "error_message": "нет доступного маршрута",
                "created_at": now,
            }],
        ]
        connect_db.return_value = connection

        rows, interval = _load_opportunities()

        self.assertEqual(interval, 1)
        self.assertTrue(rows[0]["route_unavailable"])
        self.assertTrue(any("FROM trader_signals AS sig" in call.args[0] for call in cursor.execute.call_args_list))

    def test_drawdown_reports_current_and_historical_maximum_separately(self) -> None:
        current, maximum, percent, peak = _drawdown_metrics(
            Decimal("100"),
            [(Decimal("100"), Decimal("0"), True),
             (Decimal("80"), Decimal("0"), True),
             (Decimal("100"), Decimal("0"), True)],
            Decimal("100"), Decimal("0"), True, baseline_complete=True,
        )

        self.assertEqual(current, Decimal("0"))
        self.assertEqual(maximum, Decimal("20"))
        self.assertEqual(percent, Decimal("20"))
        self.assertEqual(peak, Decimal("100"))

    def test_incomplete_equity_cannot_set_drawdown_peak(self) -> None:
        current, maximum, _, peak = _drawdown_metrics(
            Decimal("100"),
            [(Decimal("150"), Decimal("0"), False),
             (Decimal("90"), Decimal("0"), True)],
            Decimal("90"), Decimal("0"), False, baseline_complete=True,
        )

        self.assertIsNone(current)
        self.assertEqual(maximum, Decimal("10"))
        self.assertEqual(peak, Decimal("100"))

    def test_replay_page_compares_scenarios_and_displays_data_gaps(self) -> None:
        page = _render_replay_page(
            {"run_id": "run-1", "git_commit": "abc"},
            {"equity_usd": Decimal("101"), "pnl_usd": Decimal("1"),
             "max_drawdown_usd": Decimal("2"), "fees_usd": Decimal("0.1"),
             "complete": True, "gaps": []},
            {"equity_usd": Decimal("98"), "pnl_usd": Decimal("-2"),
             "max_drawdown_usd": Decimal("4"), "fees_usd": Decimal("0.3"),
             "complete": False, "gaps": ["нет более позднего снимка"]},
            Decimal("2"), Decimal("0.1"), Decimal("0.2"), Decimal("25"),
        )

        self.assertIn("Ухудшенный", page)
        self.assertIn("Equity", page)
        self.assertIn("Макс. просадка", page)
        self.assertIn("нет более позднего снимка", page)
        self.assertIn("без заданной задержки", page)

    def test_demo_operation_export_includes_ids_snapshot_and_no_unlisted_secrets(self) -> None:
        exported = _demo_operation_csv([{
            "event_id": 8, "event_key": "8:buy", "run_id": "run-id", "settlement_id": 8,
            "signal_id": 9, "event_type": "buy", "event_at_utc": "2026-10-24 12:00:00",
            "exchange_slug": "binance", "asset": "ABC", "quantity": Decimal("2"),
            "book_snapshot": {"snapshot_type": "best_quote", "asks": [["10", "2"]]},
            "api_key": "must-not-export",
        }]).decode("utf-8-sig")

        self.assertIn("event_id,event_key,run_id,settlement_id,signal_id,event_type,event_at_utc", exported)
        self.assertIn("application_realized_profit_usd", exported)
        row = next(csv.DictReader(io.StringIO(exported)))
        self.assertEqual(json.loads(row["book_snapshot"])["snapshot_type"], "best_quote")
        self.assertNotIn("must-not-export", exported)

    @patch("main.web.fetch_market_sell_execution")
    def test_equity_values_positions_once_and_lists_transfer_subset(self, sell: MagicMock) -> None:
        sell.return_value = MagicMock(
            proceeds=Decimal("45"), filled_quantity=Decimal("5"),
            remaining_quantity=Decimal(0),
        )
        result = _value_trader_positions([
            {
                "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT",
                "buy_market_pair": "ABC/USDT",
                "remaining_quantity": Decimal("2"), "expected_sell_taker_fee_rate": Decimal("0.01"),
                "position_status": "pending_transfer",
            },
            {
                "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT",
                "remaining_quantity": Decimal("3"), "expected_sell_taker_fee_rate": Decimal("0.01"),
                "position_status": "partial",
            },
        ])

        self.assertEqual(result["position_value_usd"], Decimal("44.55"))
        self.assertEqual(result["transfer_quantity"], Decimal("2"))
        self.assertEqual(result["transfer_value_usd"], Decimal("17.82"))
        self.assertTrue(result["valuation_complete"])
        sell.assert_called_once_with("mexc", "ABC/USDT", Decimal("5"))

    @patch("main.web.fetch_market_sell_execution")
    def test_incomplete_and_failed_depth_are_marked(self, sell: MagicMock) -> None:
        position = {
            "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT",
            "remaining_quantity": Decimal("2"), "expected_sell_taker_fee_rate": Decimal("0.01"),
            "position_status": "open", "valuation_usd": Decimal("20"),
        }
        sell.return_value = MagicMock(
            proceeds=Decimal("9"), filled_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
        )
        partial = _value_trader_positions([position])
        self.assertEqual(partial["position_value_usd"], Decimal("8.91"))
        self.assertFalse(partial["valuation_complete"])
        self.assertFalse(partial["valuation_stale"])

        sell.side_effect = RuntimeError("book unavailable")
        stale = _value_trader_positions([position])
        self.assertEqual(stale["position_value_usd"], Decimal("19.80"))
        self.assertTrue(stale["valuation_stale"])

        sell.side_effect = None
        sell.return_value.book_timestamp_utc = datetime.now(timezone.utc) - timedelta(seconds=45)
        old_book = _value_trader_positions([position])
        self.assertEqual(old_book["position_value_usd"], Decimal("19.80"))
        self.assertTrue(old_book["valuation_stale"])

    def test_legacy_position_without_exchange_is_not_assigned_or_valued(self) -> None:
        position = {
            "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT",
            "asset_exchange": None, "remaining_quantity": Decimal("2"),
        }
        with patch("main.web.fetch_market_sell_execution") as sell:
            result = _value_trader_positions([position])
        sell.assert_not_called()
        self.assertFalse(result["valuation_complete"])
        self.assertEqual(result["unallocated_position_quantity"], Decimal("2"))

    def test_trader_page_shows_equity_pnl_and_capital_flow_controls(self) -> None:
        page = _render_trader_page(
            {
                "balance_usd": Decimal("80"), "training_mode": True,
                "updated_at": datetime(2026, 10, 20),
            },
            {
                "equity_usd": Decimal("100"), "equity_pnl_usd": Decimal("-5"),
                "initial_capital_usd": Decimal("100"), "net_capital_movements": Decimal("5"),
                "position_value_usd": Decimal("20"), "transfer_quantity": Decimal("1"),
                "transfer_value_usd": Decimal("10"), "valuation_complete": True,
                "valuation_stale": False,
                "strategy_stats": [
                    {"strategy": "tp_sl", "settled_count": 1, "pending_count": 0, "open_count": 0,
                     "realized_profit": Decimal("2"), "fees": Decimal("0.1")},
                    {"strategy": "immediate_after_transfer", "settled_count": 2, "pending_count": 1,
                     "open_count": 0, "realized_profit": Decimal("3"), "fees": Decimal("0.2")},
                ],
                "active_demo_run": {
                    "run_id": "run-123", "git_commit": "abc123", "config": {"strategy": "tp_sl"},
                    "starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}],
                },
                "demo_runs": [{
                    "run_id": "run-123", "git_commit": "abc123", "reports": [{
                        "report_day_kyiv": "2026-10-24", "equity_usd": Decimal("102"),
                        "pnl_usd": Decimal("2"), "fees_usd": Decimal("0.1"), "trade_count": 3,
                        "closed_trade_count": 1, "drawdown_usd": Decimal("1"),
                        "drawdown_percent": Decimal("0.98"), "position_value_usd": Decimal("20"),
                        "transfer_value_usd": Decimal("5"), "valuation_complete": False,
                        "valuation_stale": True,
                    }],
                }],
            },
            [],
        )

        self.assertIn("Общий Equity", page)
        self.assertIn("-5.000 USD", page)
        self.assertIn("В переводе · включено в Equity", page)
        self.assertIn('action="/trader/capital"', page)
        self.assertIn("Статистика по стратегиям", page)
        self.assertIn("immediate_after_transfer", page)
        self.assertIn("Дневной отчёт · Europe/Kyiv", page)
        self.assertIn("2026-10-24", page)
        self.assertIn("abc123", page)
        self.assertIn("устарела", page)

    @patch("main.web.connect_db")
    def test_withdrawal_records_negative_flow_and_updates_only_demo_balance(self, connect: MagicMock) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = {"balance_usd": Decimal("20"), "training_mode": 1}
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect.return_value = connection

        cursor.fetchone.side_effect = [
            {"balance_usd": Decimal("20"), "training_mode": 1},
            {"balance": Decimal("15")},
        ]
        _record_capital_movement("withdrawal", Decimal("5"), "binance")

        self.assertIn("trader_balances", cursor.execute.call_args_list[1].args[0])
        self.assertEqual(cursor.execute.call_args_list[-1].args[1], ("withdrawal", Decimal("-5"), "binance"))
        connection.commit.assert_called_once()

    def test_history_lifetime_rows_include_live_quote_button_and_modal(self) -> None:
        now = datetime(2026, 10, 18, 12, 0, 0)
        page = _render_history(
            [],
            {},
            [{
                "source_pair": "ABC/USDT",
                "source_market_id": 17,
                "threshold_percent": Decimal("1"),
                "spread_percent": Decimal("1.2"),
                "max_spread_percent": Decimal("1.3"),
                "buy_exchange": "binance",
                "buy_market_pair": "ABC/USDT",
                "buy_price": Decimal("10"),
                "sell_exchange": "mexc",
                "sell_market_pair": "ABC/USDT",
                "sell_price": Decimal("10.12"),
                "started_at": now,
                "last_confirmed_at": now,
                "ended_at": now,
                "duration_seconds": 30,
                "is_active": False,
                "route_unavailable": True,
                "route_status_label": "В журнале: нет маршрута",
                "balance_unavailable": True,
                "balance_status_label": "В журнале: недостаточный баланс",
            }],
        )

        self.assertIn('class="live-pair-button"', page)
        self.assertIn('data-buy-exchange="binance"', page)
        self.assertIn('data-sell-exchange="mexc"', page)
        self.assertIn('id="history-live-modal"', page)
        self.assertIn("historyConnectLive('buy'", page)
        self.assertIn('status-icon route no', page)
        self.assertIn('status-icon balance no', page)

    def test_screener_rows_include_pair_specific_live_quote_controls(self) -> None:
        now = datetime(2026, 10, 18, 12, 0, 0)
        page = _render_page(
            [{
                "source_market_id": 17,
                "source_pair": "ABC/USDT",
                "base_symbol": "ABC",
                "source_quote": "USDT",
                "price_difference_percent": Decimal("1.2"),
                "exchange_name": "binance",
                "venue_pair": "ABC/USDT",
                "price": Decimal("10"),
                "sell_exchange": "mexc",
                "sell_market_pair": "ABC/USDT",
                "sell_price": Decimal("10.12"),
                "last_updated": now,
                "started_at": now,
            }],
            1,
        )

        self.assertIn('data-buy-exchange="binance"', page)
        self.assertIn('data-sell-exchange="mexc"', page)
        self.assertIn('data-buy-pair="ABC/USDT"', page)
        self.assertIn('data-sell-pair="ABC/USDT"', page)
        self.assertIn("wss://stream.binance.com:9443/ws/", page)
        self.assertIn("wss://wbs-api.mexc.com/ws", page)

    def test_screener_shows_route_and_exchange_balance_status_icons(self) -> None:
        now = datetime(2026, 10, 18, 12, 0, 0)
        page = _render_page(
            [{
                "source_market_id": 18, "source_pair": "ABC/USDT", "base_symbol": "ABC",
                "source_quote": "USDT", "price_difference_percent": Decimal("1.2"),
                "exchange_name": "binance", "venue_pair": "ABC/USDT", "price": Decimal("10"),
                "sell_exchange": "mexc", "sell_market_pair": "ABC/USDT", "sell_price": Decimal("10.12"),
                "last_updated": now, "started_at": now,
                "route_unavailable": True, "route_status_label": "В журнале: нет маршрута",
                "balance_unavailable": True, "balance_status_label": "В журнале: недостаточный баланс",
            }],
            1,
        )

        self.assertIn('status-icon route no', page)
        self.assertIn('status-icon balance no', page)
        self.assertNotIn('status-icon balance yes', page)
        self.assertIn('aria-label="В журнале: нет маршрута"', page)
        self.assertIn('aria-label="В журнале: недостаточный баланс"', page)

    def test_period_counters_include_only_created_trader_positions(self) -> None:
        now = datetime(2026, 10, 18, 12, 0, 0)
        positions = [
            {"threshold_percent": Decimal("1.0000"), "created_at": now - timedelta(minutes=30)},
            {"threshold_percent": Decimal("1.5000"), "created_at": now - timedelta(hours=2)},
            {"threshold_percent": Decimal("1.0000"), "created_at": now - timedelta(hours=13)},
        ]

        counters = _count_trader_positions(
            positions, (Decimal("1"), Decimal("1.5")), now
        )

        self.assertEqual(counters[60], {"threshold_0": 1, "threshold_1": 0})
        self.assertEqual(counters[720], {"threshold_0": 1, "threshold_1": 1})
        self.assertEqual(counters[1440], {"threshold_0": 2, "threshold_1": 1})

    def test_lifetime_summary_sums_episodes_without_transfer_estimate(self) -> None:
        started = datetime(2026, 10, 18, 12, 0, 0)
        common = {
            "source_market_id": 17,
            "source_pair": "ABC/USDT",
            "threshold_percent": Decimal("1"),
            "buy_exchange": "binance",
            "buy_market_pair": "ABC/USDT",
            "sell_exchange": "mexc",
            "sell_market_pair": "ABC/USDT",
            "is_active": False,
        }
        rows = [
            {**common, "started_at": started, "duration_seconds": 20},
            {
                **common,
                "started_at": started + timedelta(seconds=30),
                "duration_seconds": 25,
            },
        ]

        _aggregate_lifetime_groups(rows)

        self.assertEqual([row["_lifetime_total_seconds"] for row in rows], [45, 45])
        self.assertEqual([row["_lifetime_episode_count"] for row in rows], [2, 2])


if __name__ == "__main__":
    unittest.main()
