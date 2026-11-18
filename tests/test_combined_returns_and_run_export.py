"""Регрессии объединённых возвратов и воспроизводимой сверки демопрогона."""

import csv
import io
import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

from main.treider import account, return_transfer
from main.web import _demo_operation_csv
from scripts.reconcile_demo_journal import reconcile


class CombinedReturnsTests(unittest.TestCase):
    def test_partial_fills_share_one_key_and_retries_do_not_add_amount(self):
        cursor = Mock()
        cursor.fetchone.return_value = {"queued_amount": Decimal("3")}
        for total in (Decimal("10"), Decimal("25"), Decimal("25")):
            return_transfer.record_pending_return_transfer(
                cursor, settlement_id=8, signal_id=17, run_id="run",
                sale_event_key=f"8:sell:{total}", source_exchange="mexc",
                destination_exchange="binance", amount_usdt=total,
            )
        inserts = [call for call in cursor.execute.call_args_list if "INSERT INTO" in call.args[0]]
        self.assertEqual([call.args[1][3] for call in inserts], ["8:return:combined"] * 3)
        self.assertEqual([call.args[1][6] for call in inserts], [Decimal("7"), Decimal("22"), Decimal("22")])
        self.assertIn("IF(status = 'pending', VALUES(amount_usdt), amount_usdt)", inserts[-1].args[0])

    @patch("main.treider.return_transfer.connect_db")
    def test_claim_waits_for_closed_position_but_receipt_does_not(self, connect):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        connect.return_value = connection
        self.assertIsNone(return_transfer._claim_due_return())
        sql = cursor.execute.call_args.args[0]
        self.assertIn("settlement.remaining_quantity = 0", sql)
        self.assertIn("settlement.status = 'settled'", sql)
        self.assertIn("OR (status = 'in_transit'", sql)

    @patch("main.treider.return_transfer._release_return_claim")
    @patch("main.treider.return_transfer.connect_db")
    @patch("main.treider.return_transfer.check_return_route")
    def test_small_return_does_not_top_up_from_other_funds(self, route, connect, release):
        route.return_value = {"available": True, "withdraw_fee": "0.5", "min_withdraw": "5",
                              "withdraw_fee_currency": "USDT", "estimated_seconds": 60}
        row = {"source_exchange": "mexc", "destination_exchange": "binance", "amount_usdt": "5.2"}
        return_transfer._start_return_transfer(row)
        connect.assert_not_called()
        release.assert_called_once()


class RunExportTests(unittest.TestCase):
    def test_capital_movements_are_counted_once_and_current_balance_is_checked(self):
        common = {"run_id": "run", "settlement_id": "", "event_type": "balance_change",
                  "event_at_utc": "2026-11-18T10:00:00Z", "exchange_slug": "binance",
                  "asset": "USDT", "quantity": Decimal(0), "run_baseline_complete": "1",
                  "run_starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}],
                  "run_capital_movements": [
                      {"movement_id": 1, "exchange_slug": "binance", "amount_usd": "25", "created_at": "2026-11-18T10:01:00Z"},
                      {"movement_id": 2, "exchange_slug": "binance", "amount_usd": "-5", "created_at": "2026-11-18T10:02:00Z"}],
                  "application_balances": [{"exchange": "binance", "asset": "USDT", "balance": "119"}]}
        result = reconcile([{**common, "event_id": str(i), "event_key": f"check:{i}"} for i in (1, 2)])
        balance = result["balances"][0]
        self.assertEqual(balance["reconstructed_final_balance"], "120")
        self.assertEqual(balance["difference"], "1")
        self.assertTrue(balance["compared_with_current_balance"])
        self.assertTrue(any(item["kind"] == "final balance" for item in result["discrepancies"]))

    @patch.dict("os.environ", {"GIT_COMMIT": "a" * 40})
    def test_build_revision_is_used_without_git(self):
        self.assertEqual(account._git_commit(), "a" * 40)

    @patch.dict("os.environ", {"GIT_COMMIT": "unknown"})
    @patch("main.treider.account.subprocess.run", side_effect=FileNotFoundError)
    def test_container_source_has_reproducible_identity(self, run):
        revision = account._git_commit()
        self.assertTrue(revision.startswith("source:"))
        self.assertEqual(len(revision), 39)
        self.assertEqual(account._git_commit(), revision)

    def test_export_preserves_structured_configuration_and_dust(self):
        body = _demo_operation_csv([{"event_id": 1, "run_config": {"strategy": "tp_sl"},
                                    "run_starting_balances": [{"exchange": "binance", "asset": "USDT", "balance": "100"}],
                                    "application_transfer_dust_quantity": "0.1"}])
        row = next(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(json.loads(row["run_config"]), {"strategy": "tp_sl"})
        self.assertEqual(json.loads(row["run_starting_balances"])[0]["balance"], "100")
        self.assertEqual(row["application_transfer_dust_quantity"], "0.1")

    def test_runtime_config_captures_quality_and_return_policy(self):
        config = account._demo_config_snapshot()
        self.assertEqual(config["quality_min_route_trades"], account.QUALITY_MIN_ROUTE_TRADES)
        self.assertEqual(config["quality_max_book_age_seconds"], account.QUALITY_MAX_BOOK_AGE_SECONDS)
        self.assertEqual(config["return_transfer_policy"], "combined_after_close_fee_from_proceeds_v1")
        self.assertNotIn("API_KEY", json.dumps(config))

    def test_reconciliation_tracks_dust_return_fee_and_opening_balance(self):
        common = {"run_id": "run", "settlement_id": "1", "run_baseline_complete": "1",
                  "run_starting_balances": '[{"exchange":"binance","asset":"USDT","balance":"100"}]',
                  "event_at_utc": "2026-11-18T10:00:00Z", "exchange_slug": "binance",
                  "application_remaining_quantity": Decimal(0),
                  "application_transfer_dust_quantity": Decimal(1),
                  "application_sale_dust_quantity": Decimal(0)}
        definitions = [
            ("1:buy", "buy", {"asset": "ABC", "quantity": Decimal(10), "quote_amount": Decimal(100),
                               "book_snapshot": {"buy": {"asks": [["10", "10"]]}}}),
            ("1:commission:buy", "commission", {"fee_asset": "USDT", "fee_amount": Decimal(0), "quote_amount": Decimal(0)}),
            ("1:balance:buy:abc", "balance_change", {"asset": "ABC", "quantity": Decimal(9)}),
            ("1:commission:transfer", "commission", {"fee_asset": "ABC", "fee_amount": Decimal(1), "quote_amount": Decimal(0)}),
            ("1:sell:8", "sell", {"asset": "ABC", "quantity": Decimal(8), "quote_amount": Decimal(96),
                                    "book_snapshot": {"bids": [["12", "8"]]}}),
            ("1:commission:sell:8", "commission", {"fee_asset": "USDT", "fee_amount": Decimal(0), "quote_amount": Decimal(0)}),
            ("1:return:combined:commission:return_transfer", "commission", {"fee_asset": "USDT", "fee_amount": Decimal(1), "quote_amount": Decimal(1)}),
        ]
        rows = [{**common, "event_id": str(i), "event_key": key, "event_type": kind, **values}
                for i, (key, kind, values) in enumerate(definitions, 1)]
        result = reconcile(rows)
        trade = result["trades"][0]
        self.assertEqual(trade["quantity_remaining_from_journal"], "1")
        self.assertEqual(trade["return_fees_usd"], "1")
        self.assertEqual(trade["total_cost_basis_usd"], "100")
        self.assertEqual(Decimal(trade["independent_realized_pnl_usd"]), Decimal(95) - Decimal(100) * Decimal(8) / Decimal(9))
        self.assertFalse(any(item.get("kind") == "remaining quantity" for item in result["discrepancies"]))
        self.assertFalse(any("opening" in item.get("issue", "") for item in result["gaps"]))
