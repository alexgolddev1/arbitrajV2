"""Deterministic-by-default transfer delay profiles for research simulations."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

from main.treider.config import (
    RANDOM_TRANSFER_TIME,
    TRANSFER_RANDOM_SEED,
)


class TransferTimeEmulator:
    PROFILE_VERSION = "network-reference-2026-10-25-v1"
    # Sourced estimates for on-chain confirmation/finality, not guaranteed
    # exchange credit times. The default simulation uses each profile's average.
    NETWORK_PROFILES: dict[str, dict[str, Any]] = {
        "TRON": {
            "avg": Decimal(57), "min": Decimal(30), "max": Decimal(90),
            "basis": "OKX: 19 confirmations × TRON 3-second block interval",
            "source_url": "https://www.okx.com/en-gb/help/how-long-does-it-take-for-a-deposit-to-be-completed",
            "source_url_secondary": "https://developers.tron.network/docs/block",
        },
        "BSC": {
            "avg": Decimal("1.125"), "min": Decimal("1.125"), "max": Decimal("3.75"),
            "basis": "BNB Chain: two-block fast finality; current docs estimate ~1.125 seconds",
            "source_url": "https://docs.bnbchain.org/bnb-smart-chain/introduction/",
        },
        "SOLANA": {
            "avg": Decimal(13), "min": Decimal(1), "max": Decimal(13),
            "basis": "Solana: finalized transactions typically reach finality in about 13 seconds",
            "source_url": "https://solana.com/docs/payments/accept-payments/verification-tools",
        },
        "POLYGON": {
            "avg": Decimal("3.5"), "min": Decimal(2), "max": Decimal(5),
            "basis": "Polygon PoS: midpoint estimate from the documented 2–5-second finality range",
            "source_url": "https://agentic-docs.polygon.technology/general/stablecoins/offramps/",
        },
        "POLYMESH": {
            "avg": Decimal(15), "min": Decimal(15), "max": Decimal(15),
            "basis": "Polymesh SDK: transaction finalization usually takes about 15 seconds",
            "source_url": "https://developers.polymesh.live/sdk-docs/28.0.x/",
        },
        "TON": {
            "avg": Decimal(1), "min": Decimal(1), "max": Decimal(1),
            "basis": "TON: transaction finality typically occurs within one second",
            "source_url": "https://docs.ton.org/applications/payments/overview",
        },
        "ETHEREUM": {
            "avg": Decimal(384), "min": Decimal(144), "max": Decimal(900),
            "basis": "OKX USDT example: 32 confirmations × 12-second slots; full finality is ~15 minutes",
            "source_url": "https://www.okx.com/en-gb/help/how-long-does-it-take-for-a-deposit-to-be-completed",
            "source_url_secondary": "https://ethereum.org/developers/docs/blocks",
        },
        "BITCOIN": {
            "avg": Decimal(600), "min": Decimal(600), "max": Decimal(5400),
            "basis": "Bitcoin: average block interval is about 10 minutes; actual confirmation time varies",
            "source_url": "https://blog.ethereum.org/2014/07/11/toward-a-12-second-block-time",
        },
        "ONTOLOGY": {
            "avg": Decimal(6), "min": Decimal(1), "max": Decimal(30),
            "basis": "Ontology documentation: a new block is generated about every 6 seconds",
            "source_url": "https://docs.ont.io/glossary",
        },
    }

    def __init__(self, *, random_mode: bool | None = None, seed: str | int | None = None):
        self.random_mode = RANDOM_TRANSFER_TIME if random_mode is None else random_mode
        selected_seed = TRANSFER_RANDOM_SEED if seed is None else seed
        self.seed = str(selected_seed) if selected_seed not in (None, "") else None
        self._random = random.Random(self.seed)

    def get_emulated_duration(self, network: str) -> float:
        profile = self.NETWORK_PROFILES.get(network.strip().upper())
        if profile is None:
            raise ValueError(f"Для сети {network!r} не задан профиль времени перевода")
        if not self.random_mode:
            return float(profile["avg"])
        return round(float(self._random.triangular(
            float(profile["min"]), float(profile["max"]), float(profile["avg"])
        )), 2)
