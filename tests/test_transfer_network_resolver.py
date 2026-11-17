import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main.transfer_network_resolver import resolve_transfer_network


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "networks.json"

    @patch("main.transfer_network_resolver.check_transfer_network_for_currency")
    def test_json_reused_and_direction_is_separate(self, check):
        check.return_value = {"available": True, "network_profile": "ETHEREUM", "estimated_seconds": 384,
                              "raw": {"apiKey": "secret"}}
        first = resolve_transfer_network("amp", "Binance", "mexc", path=self.path)
        second = resolve_transfer_network("AMP", "binance", "mexc", path=self.path)
        self.assertEqual(first, second)
        check.assert_called_once_with("AMP", "binance", "mexc")
        self.assertNotIn("secret", self.path.read_text())
        resolve_transfer_network("AMP", "mexc", "binance", path=self.path)
        self.assertEqual(check.call_count, 2)
        saved = json.loads(self.path.read_text())
        saved["routes"]["AMP:binance:mexc"]["checked_at_utc"] = "2000-01-01T00:00:00+00:00"
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        resolve_transfer_network("AMP", "binance", "mexc", path=self.path)
        self.assertEqual(check.call_count, 3)

    @patch("main.transfer_network_resolver.check_transfer_network_for_currency")
    def test_unknown_time_and_api_failure_have_no_fabricated_duration(self, check):
        check.return_value = {"available": True, "network": "ETHEREUM"}
        result = resolve_transfer_network("AMP", "binance", "mexc", path=self.path)
        self.assertFalse(result["available"])
        self.assertIsNone(result["estimated_seconds"])
        check.side_effect = RuntimeError("signature=secret")
        result = resolve_transfer_network("SKL", "binance", "mexc", path=self.path)
        self.assertFalse(result["available"])
        self.assertNotIn("secret", self.path.read_text())
