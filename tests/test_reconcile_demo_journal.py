from __future__ import annotations

import unittest
from decimal import Decimal

from scripts.reconcile_demo_journal import reconcile


class DemoJournalReconciliationTests(unittest.TestCase):
    def test_reconstructs_cost_pnl_balances_and_compares_with_application(self) -> None:
        common = {
            "run_id": "run-a", "settlement_id": "12", "signal_id": "7",
            "application_realized_profit_usd": Decimal("9.79"),
            "application_buy_debit_usd": Decimal("100.1"),
            "application_sell_proceeds_accumulated_usd": Decimal("110"),
            "application_sell_fees_accumulated_usd": Decimal("0.11"),
            "application_remaining_quantity": Decimal("0"), "application_status": "settled",
        }
        rows = [
            {**common, "event_id": "1", "event_key": "12:buy", "event_type": "buy",
             "event_at_utc": "2026-10-24 10:00:00", "exchange_slug": "binance", "market_pair": "ABC/USDT",
             "asset": "ABC", "quantity": Decimal("1"), "quote_amount": Decimal("100"),
             "book_snapshot": {"buy": {"asks": [["100", "1"]]}}},
            {**common, "event_id": "2", "event_key": "12:commission:buy", "event_type": "commission",
             "event_at_utc": "2026-10-24 10:00:00", "exchange_slug": "binance", "asset": "USDT",
             "fee_asset": "USDT", "fee_amount": Decimal("0.1"), "quote_amount": Decimal("0.1")},
            {**common, "event_id": "3", "event_key": "12:balance:buy:usdt", "event_type": "balance_change",
             "event_at_utc": "2026-10-24 10:00:00", "exchange_slug": "binance", "asset": "USDT",
             "quantity": Decimal("-100.1"), "balance_before": Decimal("1000"), "balance_after": Decimal("899.9")},
            {**common, "event_id": "4", "event_key": "12:balance:buy:abc", "event_type": "balance_change",
             "event_at_utc": "2026-10-24 10:00:00", "exchange_slug": "binance", "asset": "ABC",
             "quantity": Decimal("1"), "balance_before": Decimal("0"), "balance_after": Decimal("1")},
            {**common, "event_id": "5", "event_key": "12:sell:1", "event_type": "sell",
             "event_at_utc": "2026-10-24 10:01:00", "exchange_slug": "binance", "market_pair": "ABC/USDT",
             "asset": "ABC", "quantity": Decimal("1"), "quote_amount": Decimal("110"),
             "book_snapshot": {"bids": [["110", "1"]]}},
            {**common, "event_id": "6", "event_key": "12:commission:sell:1", "event_type": "commission",
             "event_at_utc": "2026-10-24 10:01:00", "exchange_slug": "binance", "asset": "USDT",
             "fee_asset": "USDT", "fee_amount": Decimal("0.11"), "quote_amount": Decimal("0.11")},
            {**common, "event_id": "7", "event_key": "12:balance:sell:coin:1", "event_type": "balance_change",
             "event_at_utc": "2026-10-24 10:01:00", "exchange_slug": "binance", "asset": "ABC",
             "quantity": Decimal("-1")},
            {**common, "event_id": "8", "event_key": "12:balance:sell:usdt:1", "event_type": "balance_change",
             "event_at_utc": "2026-10-24 10:01:00", "exchange_slug": "binance", "asset": "USDT",
             "quantity": Decimal("109.89"), "balance_before": Decimal("899.9"), "balance_after": Decimal("1009.79")},
        ]

        result = reconcile(rows)

        self.assertEqual(result["trades"][0]["independent_realized_pnl_usd"], "9.79")
        self.assertEqual(result["trades"][0]["independent_vs_application_pnl_difference_usd"], "0.00")
        usdt = next(item for item in result["balances"] if item["asset"] == "USDT")
        self.assertEqual(usdt["reconstructed_final_balance"], "1009.79")
        self.assertEqual(result["fees_by_run"]["run-a"]["fees_total_usd"], "0.21")
        self.assertEqual(result["discrepancies"], [])

    def test_unknown_fee_value_stays_unknown_and_duplicate_is_reported(self) -> None:
        row = {
            "event_id": "1", "event_key": "1:commission:buy", "run_id": "run-a",
            "settlement_id": "1", "event_type": "commission", "event_at_utc": "2026-10-24T10:00:00Z",
            "fee_amount": Decimal("0.01"), "fee_asset": "ABC", "quote_amount": Decimal("0"),
        }

        result = reconcile([row, dict(row, event_id="2")])

        self.assertEqual(len(result["duplicates"]), 1)
        self.assertIsNone(result["fees_by_run"]["run-a"]["fees_total_usd"])
        self.assertFalse(result["fees_by_run"]["run-a"]["fee_value_complete"])
        self.assertEqual(result["processed_events"], 1)


if __name__ == "__main__":
    unittest.main()
