"""Автопредложения не становятся торговыми правилами без подтверждения."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from main.market_unit_proposals import load_proposals, propose_market_unit, refresh_market_unit_proposals
from main.market_units import get_market_unit_scale, save_market_unit_mapping
from main.web import _load_market_unit_candidates, _render_market_units_page


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.reference = Path(self.directory.name) / "references.json"
        self.cache = Path(self.directory.name) / "proposals.json"
        self.row = {
            "exchange": "mexc", "source_slug": "coin", "source_symbol": "COIN",
            "market_symbol": "ALIAS", "units_per_market_unit": "1",
            "network": "ETHEREUM", "contract": "0xAbCd", "reason": "Другой тикер",
            "checked_on": "2026-11-16", "sources": ["https://www.mexc.com/exchange/ALIAS_USDT"],
        }
        self.reference.write_text(json.dumps({"version": 1, "references": [self.row]}), encoding="utf-8")

    @patch("main.treider.exchange_networks.get_exchange_networks")
    def test_api_identity_cache_sanitization_and_manual_approval(self, fetch):
        fetch.return_value = {"networks": {"mexc": [{
            "network_code": "ETH", "network_name": "ERC20", "contract_address": "0xabcd",
            "raw": {"secret": "never-save"}, "apiKey": "private",
        }]}, "errors": {}}
        refresh_market_unit_proposals(self.reference, self.cache)
        refresh_market_unit_proposals(self.reference, self.cache)
        fetch.assert_called_once_with("ALIAS", ("mexc",))
        self.assertNotIn("never-save", self.cache.read_text())
        self.assertNotIn("private", self.cache.read_text())
        proposal = propose_market_unit("mexc", "coin", "COIN", "ALIAS", load_proposals(self.reference, self.cache))
        self.assertEqual(proposal["api_status"], "matched")
        approved = Path(self.directory.name) / "approved.json"
        self.assertIsNone(get_market_unit_scale("mexc", "coin", "COIN", "ALIAS", approved))
        self.assertFalse(approved.exists())

    @patch("main.treider.exchange_networks.get_exchange_networks")
    def test_network_must_match_and_contract_conflict_is_visible(self, fetch):
        fetch.return_value = {"networks": {"mexc": [
            {"network_code": "BSC", "contract_address": "0xabcd"},
            {"network_code": "ETH", "contract_address": "0xwrong"},
        ]}}
        refresh_market_unit_proposals(self.reference, self.cache)
        proposal = next(iter(load_proposals(self.reference, self.cache).values()))
        self.assertEqual(proposal["api_status"], "conflict")

    @patch("main.treider.exchange_networks.get_exchange_networks")
    def test_api_errors_and_stale_observations_are_not_matches(self, fetch):
        fetch.return_value = {"errors": {"mexc": "signed-url?apiKey=secret"}}
        old = datetime.now(timezone.utc) - timedelta(hours=7)
        refresh_market_unit_proposals(self.reference, self.cache, now=old)
        self.assertNotIn("secret", self.cache.read_text())
        self.assertEqual(next(iter(load_proposals(self.reference, self.cache).values()))["api_status"], "stale")
        refresh_market_unit_proposals(self.reference, self.cache)
        self.assertEqual(next(iter(load_proposals(self.reference, self.cache).values()))["api_status"], "unavailable")

    def test_unknown_alias_has_no_silent_one_and_reference_requires_exact_identity(self):
        references = load_proposals(self.reference, self.cache)
        unknown = propose_market_unit("mexc", "other-project", "COIN", "ALIAS", references)
        self.assertEqual(unknown["units_per_market_unit"], "")
        lot = propose_market_unit("mexc", "sats", "1000SATS", "SATS", references)
        self.assertEqual(lot["units_per_market_unit"], "0.001")

    def test_paxg_parentheses_can_be_saved_and_read(self):
        approved = Path(self.directory.name) / "approved.json"
        save_market_unit_mapping(exchange="mexc", source_slug="pax-gold", source_symbol="PAXG",
                                 market_symbol="GOLD(PAXG)", units_per_market_unit="1", path=approved)
        self.assertEqual(get_market_unit_scale("mexc", "pax-gold", "PAXG", "GOLD(PAXG)", approved), 1)

    @patch("main.web.load_proposals")
    @patch("main.web.load_market_unit_mappings")
    @patch("main.web.connect_db")
    def test_candidate_filled_from_reference_keeps_manual_correction(self, connect, mappings, proposals):
        connection = MagicMock()
        connect.return_value = connection
        connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [{
            "source_market_id": 1, "exchange_slug": "mexc", "source_slug": "coin",
            "source_symbol": "COIN", "market_pair": "ALIAS/USDT", "market_price": "2",
        }]
        proposals.return_value = load_proposals(self.reference, self.cache)
        mappings.return_value = []
        candidates = _load_market_unit_candidates()
        self.assertEqual(candidates[0]["units_per_market_unit"], "1")
        self.assertFalse(candidates[0]["approved"])
        page = _render_market_units_page(candidates)
        self.assertIn("2026-11-16", page)
        self.assertIn("https://www.mexc.com/exchange/ALIAS_USDT", page)
        self.assertIn("Проверка API ещё не выполнена", page)
        mappings.return_value = [{**self.row, "units_per_market_unit": "2"}]
        self.assertEqual(_load_market_unit_candidates()[0]["units_per_market_unit"], "2")
        proposals.return_value = {key: {**value, "api_status": "conflict"} for key, value in proposals.return_value.items()}
        mappings.return_value = []
        candidates = _load_market_unit_candidates()
        self.assertEqual(candidates[0]["units_per_market_unit"], "")
        self.assertIn("КОНФЛИКТ", _render_market_units_page(candidates))
