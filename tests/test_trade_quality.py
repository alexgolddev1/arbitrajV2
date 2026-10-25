from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

from main.treider.trade_checks import TradeEstimate, _should_enter_by_quality
from main.treider.trade_statistics import (
    RouteQualityStats,
    _get_route_quality_stats,
    _measure_realized_quality,
)


def _estimate(expected_profit: str = "1", book_age_seconds: int = 0) -> TradeEstimate:
    captured_at = datetime.now(timezone.utc) - timedelta(seconds=book_age_seconds)
    snapshot = {"captured_at_utc": captured_at.isoformat()}
    return TradeEstimate(
        quantity=Decimal("10"),
        quantity_after_fees=Decimal("10"),
        transfer_dust_quantity=Decimal(0),
        buy_average_price=Decimal("10"),
        buy_cost_usd=Decimal("100"),
        expected_sell_average_price=Decimal("10.2"),
        expected_sell_proceeds_usd=Decimal("102"),
        buy_fee_usd=Decimal(0),
        buy_fee_base_quantity=Decimal(0),
        transfer_fee_amount=Decimal(0),
        transfer_fee_currency="USDT",
        transfer_fee_usd=Decimal(0),
        expected_sell_fee_usd=Decimal(0),
        expected_profit_before_reserve_usd=Decimal(expected_profit),
        price_reserve_usd=Decimal(0),
        expected_profit_after_reserve_usd=Decimal(expected_profit),
        shortfall_usd=Decimal(0),
        available_balance_usd=Decimal("100"),
        buy_book_snapshot=snapshot,
        sell_book_snapshot=snapshot,
    )


class TradeQualityTests(unittest.TestCase):
    def test_realized_quality_compares_forecast_and_actual_roi(self) -> None:
        result = _measure_realized_quality({
            "realized_profit_usd": Decimal("-0.25"),
            "expected_profit_after_reserve_usd": Decimal("0.40"),
            "buy_debit_usd": Decimal("25"),
        })

        self.assertEqual(result.realized_profit_usd, Decimal("-0.25"))
        self.assertEqual(result.forecast_error_usd, Decimal("-0.65"))
        self.assertEqual(result.roi_percent, Decimal("-1.00"))

    def test_route_statistics_keep_strategy_and_signal_tier_separate(self) -> None:
        def row(profit: str, strategy: str, threshold: str, run_id: str = "run-1") -> dict[str, object]:
            return {
                "route_assumptions": json.dumps({
                    "demo_run_id": run_id,
                    "demo_strategy": strategy,
                    "signal_threshold_percent": threshold,
                }),
                "realized_profit_usd": Decimal(profit),
                "buy_debit_usd": Decimal("100"),
                "expected_profit_after_reserve_usd": Decimal("1"),
                "expected_profit_usd": Decimal("1"),
            }

        cursor = Mock()
        cursor.fetchall.return_value = [
            row("2", "tp_sl", "1.5"),
            row("-1", "tp_sl", "1.5"),
            row("50", "tp_sl", "1"),
            row("50", "immediate_after_transfer", "1.5"),
            row("500", "tp_sl", "1.5", run_id="older-run"),
        ]

        stats = _get_route_quality_stats(
            cursor,
            source_pair="ABC/USDT",
            buy_exchange="Binance",
            sell_exchange="MEXC",
            network_name="POLYGON",
            route_status="confirmed",
            strategy="tp_sl",
            run_id="run-1",
            signal_threshold_percent="1.5",
        )

        self.assertEqual(stats.sample_count, 2)
        self.assertEqual(stats.profitable_count, 1)
        self.assertEqual(stats.win_rate_percent, Decimal("50"))
        self.assertEqual(stats.expectancy_usd, Decimal("0.5"))
        self.assertEqual(stats.max_drawdown_usd, Decimal("1"))
        self.assertIn("network_name", cursor.execute.call_args.args[0])

    def test_new_route_is_marked_unproven_but_uses_current_profit_check(self) -> None:
        result = _should_enter_by_quality(
            _estimate("0.5"),
            {"route_status": "unconfirmed"},
            RouteQualityStats(),
            min_profit_usd=Decimal("0.1"),
            min_route_trades=5,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "caution")
        self.assertEqual(result.expected_roi_percent, Decimal("0.5"))
        self.assertIn("истории маршрута недостаточно", result.reason)
        self.assertIn("unconfirmed", result.reason)

    def test_negative_route_expectancy_blocks_after_minimum_sample(self) -> None:
        result = _should_enter_by_quality(
            _estimate("0.5"),
            {"route_status": "confirmed"},
            RouteQualityStats(sample_count=5, expectancy_usd=Decimal("-0.01")),
            min_profit_usd=Decimal(0),
            min_route_trades=5,
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "rejected")
        self.assertIn("историческое ожидание маршрута", result.reason)

    def test_current_estimate_below_minimum_profit_is_rejected(self) -> None:
        result = _should_enter_by_quality(
            _estimate("0"),
            {"route_status": "confirmed"},
            RouteQualityStats(sample_count=10, expectancy_usd=Decimal("1")),
            min_profit_usd=Decimal("0.01"),
            min_route_trades=5,
        )

        self.assertFalse(result.accepted)
        self.assertIn("ниже минимума", result.reason)

    def test_stale_order_book_rejects_the_entry(self) -> None:
        result = _should_enter_by_quality(
            _estimate("0.5", book_age_seconds=60),
            {"route_status": "confirmed"},
            RouteQualityStats(),
            min_profit_usd=Decimal(0),
            min_route_trades=5,
        )

        self.assertFalse(result.accepted)
        self.assertFalse(result.books_fresh)
        self.assertIn("устарел", result.reason)


if __name__ == "__main__":
    unittest.main()
