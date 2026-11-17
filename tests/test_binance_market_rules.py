"""Регрессии нормализации ограничений количества Binance."""

import unittest
from decimal import Decimal

from main.treider.market_rules import UnknownMarketRules, floor_to_step, parse_market_rules


class BinanceMarketRulesTests(unittest.TestCase):
    def parse(self, filters, market_order=True):
        return parse_market_rules(
            "binance", {"symbols": [{"symbol": "BREVUSDT", "filters": filters}]},
            "BREV/USDT", market_order=market_order,
        )

    def test_live_brev_and_skl_zero_market_step_uses_lot_size(self):
        for step in ("0.10000000", "1.00000000"):
            with self.subTest(step=step):
                rules = self.parse([
                    {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0"},
                    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
                ])
                self.assertEqual(rules.quantity_step, Decimal(step))
                self.assertEqual(rules.min_quantity, Decimal(step))
                self.assertEqual(rules.min_notional, Decimal("5"))
                quantity = floor_to_step(Decimal("12.345"), rules.quantity_step)
                self.assertEqual(quantity % Decimal(step), 0)
                self.assertLessEqual(quantity, Decimal("12.345"))

    def test_both_positive_steps_and_minimums_are_enforced(self):
        rules = self.parse([
            {"filterType": "LOT_SIZE", "stepSize": "0.02", "minQty": "0.1"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.03", "minQty": "0.2"},
        ])
        self.assertEqual(rules.quantity_step, Decimal("0.06"))
        self.assertEqual(rules.min_quantity, Decimal("0.2"))

    def test_unknown_or_invalid_step_is_rejected(self):
        for value in (None, "", "NaN", "-1", "0"):
            with self.subTest(value=value), self.assertRaises(UnknownMarketRules):
                self.parse([
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0"},
                    {"filterType": "LOT_SIZE", "stepSize": value, "minQty": "0.1"},
                ])
        with self.assertRaises(UnknownMarketRules):
            self.parse([{"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0"}])

    def test_limit_order_uses_only_lot_size(self):
        rules = self.parse([
            {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "2"},
        ], market_order=False)
        self.assertEqual(rules.quantity_step, Decimal("0.1"))
        self.assertEqual(rules.min_quantity, Decimal("0.1"))
