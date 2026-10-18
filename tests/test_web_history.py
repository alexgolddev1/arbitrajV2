from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from main.web import _aggregate_lifetime_groups, _count_trader_positions


class TraderHistoryTests(unittest.TestCase):
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
