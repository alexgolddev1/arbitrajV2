from __future__ import annotations

import unittest
from decimal import Decimal

from main.screener.filter import filter_markets


class ScreenerFilterTests(unittest.TestCase):
    def test_keeps_usdt_and_usdc_within_volume_range(self) -> None:
        markets = [
            {"marketId": 1, "quoteSymbol": "USDT", "volumeUsd": Decimal("350000")},
            {"marketId": 2, "quoteSymbol": "USDC", "volumeUsd": Decimal("350000")},
            {"marketId": 3, "quoteSymbol": "TRY", "volumeUsd": Decimal("350000")},
            {"marketId": 4, "quoteSymbol": "USDT", "volumeUsd": Decimal("250000")},
        ]

        self.assertEqual([row["marketId"] for row in filter_markets(markets)], [1, 2])


if __name__ == "__main__":
    unittest.main()
