from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import database
from main.calculator.websocket_monitor import Instrument, _normalized_quote
from main.market_units import (
    get_market_unit_scale, load_market_unit_mappings, save_market_unit_mapping,
    suggest_market_unit_scale,
)
from main.treider.market_data import SpotBookTicker
from main.treider.trade import process_trade
from main.web import _render_market_units_page


class _CalculationCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.batch = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.executed.append((query, params))

    def fetchall(self):
        return self.rows

    def executemany(self, query, rows):
        self.batch.append((query, list(rows)))


class _CalculationConnection:
    def __init__(self, rows):
        self.db_cursor = _CalculationCursor(rows)
        self.committed = False

    def cursor(self):
        return self.db_cursor

    def commit(self):
        self.committed = True


class MarketUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.config_path = Path(self.temp_dir.name) / "market_unit_mappings.json"
        self.config_path.write_text('{"version":1,"mappings":[]}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_candidate_is_only_a_suggestion_until_manual_save(self) -> None:
        self.assertEqual(suggest_market_unit_scale("BABYDOGE", "1MBABYDOGE"), Decimal("1000000"))
        self.assertIsNone(get_market_unit_scale(
            "binance", "baby-doge", "BABYDOGE", "1MBABYDOGE", self.config_path,
        ))
        save_market_unit_mapping(
            exchange="binance", source_slug="baby-doge", source_symbol="BABYDOGE",
            market_symbol="1MBABYDOGE", units_per_market_unit="1000000", path=self.config_path,
        )
        self.assertEqual(get_market_unit_scale(
            "binance", "baby-doge", "BABYDOGE", "1MBABYDOGE", self.config_path,
        ), Decimal("1000000"))

    def test_manual_correction_replaces_existing_mapping_and_invalid_scale_is_rejected(self) -> None:
        args = dict(
            exchange="binance", source_slug="coin", source_symbol="COIN", market_symbol="1000COIN",
            path=self.config_path,
        )
        save_market_unit_mapping(**args, units_per_market_unit="1000")
        save_market_unit_mapping(**args, units_per_market_unit="1000000")
        self.assertEqual(len(load_market_unit_mappings(self.config_path)), 1)
        self.assertEqual(load_market_unit_mappings(self.config_path)[0]["units_per_market_unit"], "1000000")
        with self.assertRaises(ValueError):
            save_market_unit_mapping(**args, units_per_market_unit="0")

    def test_market_comparison_excludes_unconfirmed_alias_and_normalizes_approved_lot(self) -> None:
        rows = [
            (1, "binance", Decimal("0.0003436"), 1, "1MBABYDOGE", "1MBABYDOGE/USDT", "Binance",
             "BABYDOGE/USDT", "BABYDOGE", "baby-doge"),
            (1, "okx", Decimal("0.000000000344"), 1, "BABYDOGE", "BABYDOGE/USDT", "OKX",
             "BABYDOGE/USDT", "BABYDOGE", "baby-doge"),
        ]
        connection = _CalculationConnection(rows)
        with patch.object(database, "get_market_unit_scale", side_effect=lambda ex, slug, source, market: (
            get_market_unit_scale(ex, slug, source, market, self.config_path)
        )):
            flagged = database.calculate_market_opportunities(connection, Decimal("0.1"), [1])
        self.assertEqual(flagged, 0)
        self.assertEqual(connection.db_cursor.batch, [])

        save_market_unit_mapping(
            exchange="binance", source_slug="baby-doge", source_symbol="BABYDOGE",
            market_symbol="1MBABYDOGE", units_per_market_unit="1000000", path=self.config_path,
        )
        connection = _CalculationConnection(rows)
        with patch.object(database, "get_market_unit_scale", side_effect=lambda ex, slug, source, market: (
            get_market_unit_scale(ex, slug, source, market, self.config_path)
        )):
            flagged = database.calculate_market_opportunities(connection, Decimal("0.1"), [1])
        self.assertEqual(flagged, 1)
        history_rows = [batch for query, batch in connection.db_cursor.batch if "opportunity_history" in query]
        self.assertEqual(len(history_rows), 1)
        spread = history_rows[0][0][2]
        self.assertAlmostEqual(float(spread), 0.116414, places=5)

    def test_websocket_quote_uses_normalized_price_and_underlying_depth(self) -> None:
        instrument = Instrument(
            9, "BABYDOGE/USDT", "BABYDOGE", "binance", "Binance",
            "1MBABYDOGE/USDT", "1MBABYDOGEUSDT", Decimal("1000000"),
        )
        quote = _normalized_quote(
            instrument, Decimal("0.0003436"), Decimal("0.0003440"),
            Decimal("2"), Decimal("3"), 1.0,
        )
        self.assertEqual(quote.bid, Decimal("0.0000000003436"))
        self.assertEqual(quote.ask_size, Decimal("3000000"))
        self.assertEqual(quote.native_ask, Decimal("0.0003440"))
        self.assertEqual(quote.native_ask_size, Decimal("3"))

    def test_trader_rejects_denomination_mismatch_before_demo_purchase(self) -> None:
        signal = {
            "source_pair": "BABYDOGE/USDT",
            "buy": {"market_pair": "1MBABYDOGE/USDT", "units_per_market_unit": "1000000"},
            "sell": {"market_pair": "BABYDOGE/USDT", "units_per_market_unit": "1"},
        }
        buy = SpotBookTicker("binance", "1MBABYDOGE/USDT", Decimal("0.00034"), Decimal(2),
                             Decimal("0.00035"), Decimal(2))
        sell = SpotBookTicker("okx", "BABYDOGE/USDT", Decimal("0.000000000344"), Decimal(2),
                              Decimal("0.000000000345"), Decimal(2))
        result = process_trade(signal, 1, buy, sell, None, None, 0)
        self.assertEqual(result[0], "rejected")
        self.assertIn("минимум вывода", result[1])

    def test_confirmation_page_shows_suggestion_but_requires_manual_submit(self) -> None:
        page = _render_market_units_page([{
            "exchange": "binance", "exchange_name": "Binance", "source_slug": "baby-doge",
            "source_symbol": "BABYDOGE", "market_symbol": "1MBABYDOGE",
            "market_pair": "1MBABYDOGE/USDT", "units_per_market_unit": "1000000",
            "approved": False, "source_market_ids": {1},
        }])
        self.assertIn("value=\"1000000\"", page)
        self.assertIn("Подтвердить / сохранить", page)
        self.assertIn("Требует проверки", page)


if __name__ == "__main__":
    unittest.main()
