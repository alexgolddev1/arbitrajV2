"""Профили времени имеют источники и воспроизводимые границы модели."""

import unittest
from decimal import Decimal

from main.treider.tran_emulation import TransferTimeEmulator
from main.treider.exchange_networks import NETWORK_ALIASES, build_transfer_routes


class TransferTimeProfilesTests(unittest.TestCase):
    def test_all_networks_have_estimate_or_specific_reason(self):
        emulator = TransferTimeEmulator(random_mode=False)
        for network in NETWORK_ALIASES:
            with self.subTest(network=network):
                profile = emulator.NETWORK_PROFILES.get(network)
                if profile is None:
                    self.assertTrue(emulator.MISSING_PROFILE_REASONS[network])
                    with self.assertRaises(ValueError):
                        emulator.get_emulated_duration(network)
                    continue
                self.assertGreater(profile["min"], 0)
                self.assertLessEqual(profile["min"], profile["avg"])
                self.assertLessEqual(profile["avg"], profile["max"])
                self.assertTrue(profile["source_url"].startswith("https://"))
                self.assertTrue(profile["basis"])
                self.assertEqual(emulator.get_emulated_duration(network), float(profile["avg"]))

    def test_l2_uses_finality_not_fast_sequencer_receipt(self):
        emulator = TransferTimeEmulator(random_mode=False)
        self.assertEqual(emulator.get_emulated_duration("ARBITRUM"), 990)
        self.assertEqual(emulator.get_emulated_duration("OPTIMISM"), 1350)
        self.assertEqual(emulator.get_emulated_duration("ZKSYNC_ERA"), 10800)
        row = {"network_code": "ARB", "network_name": "Arbitrum One", "withdraw_enabled": True,
               "deposit_enabled": True, "contract_address": "0xabc", "withdraw_fee": "1"}
        routes = build_transfer_routes("ABC", "binance", "mexc", {"networks": {"binance": [row], "mexc": [row]}})
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["estimated_seconds"], 990)

    def test_random_mode_is_repeatable_within_documented_bounds(self):
        a = TransferTimeEmulator(random_mode=True, seed="test")
        b = TransferTimeEmulator(random_mode=True, seed="test")
        for network, profile in a.NETWORK_PROFILES.items():
            value = a.get_emulated_duration(network)
            self.assertEqual(value, b.get_emulated_duration(network))
            self.assertGreaterEqual(Decimal(str(value)), profile["min"])
            self.assertLessEqual(Decimal(str(value)), profile["max"])
