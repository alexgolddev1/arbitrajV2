"""Deterministic-by-default transfer delay profiles for research simulations."""

from __future__ import annotations

import random
from decimal import Decimal

from main.treider.config import (
    FIXED_TRANSFER_SECONDS,
    RANDOM_TRANSFER_TIME,
    TRANSFER_RANDOM_SEED,
)


class TransferTimeEmulator:
    NETWORK_PROFILES: dict[str, dict[str, Decimal]] = {
        "TRON": {"avg": Decimal(180), "min": Decimal(60), "max": Decimal(350)},
        "BSC": {"avg": Decimal(120), "min": Decimal(45), "max": Decimal(240)},
        "SOLANA": {"avg": Decimal(30), "min": Decimal(10), "max": Decimal(90)},
        "POLYGON": {"avg": Decimal(120), "min": Decimal(30), "max": Decimal(300)},
        "TON": {"avg": Decimal(40), "min": Decimal(15), "max": Decimal(120)},
        "ETHEREUM": {"avg": Decimal(600), "min": Decimal(240), "max": Decimal(1800)},
        "ARBITRUM": {"avg": Decimal(90), "min": Decimal(20), "max": Decimal(240)},
        "BITCOIN": {"avg": Decimal(2400), "min": Decimal(900), "max": Decimal(5400)},
        "ONTOLOGY": {"avg": Decimal(120), "min": Decimal(30), "max": Decimal(300)},
    }

    def __init__(self, *, random_mode: bool | None = None, seed: str | int | None = None):
        self.random_mode = RANDOM_TRANSFER_TIME if random_mode is None else random_mode
        selected_seed = TRANSFER_RANDOM_SEED if seed is None else seed
        self.seed = str(selected_seed) if selected_seed not in (None, "") else None
        self._random = random.Random(self.seed)

    def get_emulated_duration(self, network: str) -> float:
        if not self.random_mode:
            return float(FIXED_TRANSFER_SECONDS)
        profile = self.NETWORK_PROFILES.get(network.upper())
        if profile is None:
            raise ValueError(f"Для сети {network!r} не задан профиль времени перевода")
        return round(float(self._random.triangular(
            float(profile["min"]), float(profile["max"]), float(profile["avg"])
        )), 2)
