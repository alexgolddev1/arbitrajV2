from decimal import Decimal
import unittest
from unittest.mock import patch

from main.calculator.websocket_monitor import Instrument, OpportunityConfirmer


class MissingTransferProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_manual_rule_uses_api_route_for_episode(self):
        instruments = [
            Instrument(3, "AMP/USDT", "AMP", exchange, exchange, "AMP/USDT", "AMPUSDT", Decimal(1))
            for exchange in ("binance", "mexc")
        ]
        confirmer = OpportunityConfirmer(instruments)
        with patch("main.calculator.websocket_monitor.TRANSFER_NETWORK_MAP", {}), \
                patch("main.calculator.websocket_monitor.CALCULATOR_SPREAD_LEVELS", [Decimal(1)]), \
                patch("main.calculator.websocket_monitor.resolve_transfer_network", return_value={
                    "available": True, "network": "ETHEREUM", "estimated_seconds": 384,
                }) as resolve, \
                patch("main.calculator.websocket_monitor.record_opportunity_lifetime", return_value=8):
            await confirmer.quote("binance", "AMPUSDT", "9", "10", "100", "100")
            await confirmer.quote("mexc", "AMPUSDT", "11", "12", "100", "100")
        resolve.assert_called_once_with("AMP", "binance", "mexc")
        self.assertEqual(confirmer.episodes[(3, Decimal(1))].network, "ETHEREUM")
        self.assertEqual(confirmer.episodes[(3, Decimal(1))].transfer_seconds, 384)

    async def test_missing_profile_keeps_quotes_alive_and_known_profile_recovers(self):
        instruments = [
            Instrument(1, "ABC/USDT", "ABC", exchange, exchange, "ABC/USDT", "ABCUSDT", Decimal(1))
            for exchange in ("binance", "mexc")
        ]
        confirmer = OpportunityConfirmer(instruments)
        with patch("main.calculator.websocket_monitor.TRANSFER_NETWORK_MAP", {}), \
                patch("main.calculator.websocket_monitor.resolve_transfer_network", return_value={"available": False, "reason": "UNKNOWN: нет маршрута"}), \
                patch("main.calculator.websocket_monitor.CALCULATOR_SPREAD_LEVELS", [Decimal(1)]), \
                patch("main.calculator.websocket_monitor.record_opportunity_lifetime", return_value=7) as record:
            with self.assertLogs("main.calculator.websocket_monitor", level="WARNING") as logs:
                await confirmer.quote("binance", "ABCUSDT", "9", "10", "100", "100")
                await confirmer.quote("mexc", "ABCUSDT", "11", "12", "100", "100")
                await confirmer.quote("mexc", "ABCUSDT", "11.1", "12", "100", "100")
            self.assertEqual(len(logs.output), 1)
            self.assertIn("UNKNOWN", logs.output[0])
            self.assertEqual(confirmer.quotes[(1, "mexc")].bid, Decimal("11.1"))
            self.assertFalse(confirmer.pending)
            record.assert_not_called()
            with patch("main.calculator.websocket_monitor.TRANSFER_NETWORK_MAP", {"ABC": "TRON"}):
                await confirmer.quote("mexc", "ABCUSDT", "11.2", "12", "100", "100")
            record.assert_called_once()
            self.assertEqual(confirmer.pending[(1, Decimal(1))].transfer_seconds, 57.0)

    async def test_named_network_without_profile_does_not_escape_handler(self):
        instruments = [
            Instrument(2, "XYZ/USDT", "XYZ", exchange, exchange, "XYZ/USDT", "XYZUSDT", Decimal(1))
            for exchange in ("bybit", "mexc")
        ]
        confirmer = OpportunityConfirmer(instruments)
        with patch("main.calculator.websocket_monitor.TRANSFER_NETWORK_MAP", {"XYZ": "UNSUPPORTED"}), \
                self.assertLogs("main.calculator.websocket_monitor", level="WARNING"):
            await confirmer.quote("bybit", "XYZUSDT", "9", "10", "100", "100")
            await confirmer.quote("mexc", "XYZUSDT", "11", "12", "100", "100")
        self.assertFalse(confirmer.episodes)
