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
    PROFILE_VERSION = "network-reference-2026-11-17-v2"
    # Sourced estimates for on-chain confirmation/finality, not guaranteed
    # exchange credit times. The default simulation uses each profile's average.
    NETWORK_PROFILES: dict[str, dict[str, Any]] = {
        "TRON": {
            "avg": Decimal(57), "min": Decimal(30), "max": Decimal(90),
            "basis": "Пример OKX: 19 подтверждений × 3 секунды на блок TRON",
            "source_url": "https://www.okx.com/en-gb/help/how-long-does-it-take-for-a-deposit-to-be-completed",
            "source_url_secondary": "https://developers.tron.network/docs/block",
        },
        "BSC": {
            "avg": Decimal("1.125"), "min": Decimal("1.125"), "max": Decimal("3.75"),
            "basis": "BNB Chain: быстрая финальность за два блока; справочная оценка 1.125 секунды",
            "source_url": "https://docs.bnbchain.org/bnb-smart-chain/introduction/",
        },
        "SOLANA": {
            "avg": Decimal(13), "min": Decimal(1), "max": Decimal(13),
            "basis": "Solana: прежний справочный ориентир finalized около 13 секунд; новые параметры консенсуса требуют отдельной сверки",
            "source_url": "https://solana.com/docs/payments/accept-payments/verification-tools",
        },
        "POLYGON": {
            "avg": Decimal("3.5"), "min": Decimal(2), "max": Decimal(5),
            "basis": "Polygon PoS: середина опубликованного диапазона финальности 2–5 секунд",
            "source_url": "https://agentic-docs.polygon.technology/general/stablecoins/offramps/",
        },
        "POLYMESH": {
            "avg": Decimal(15), "min": Decimal(15), "max": Decimal(15),
            "basis": "Polymesh SDK: финализация транзакции обычно занимает около 15 секунд",
            "source_url": "https://developers.polymesh.live/sdk-docs/28.0.x/",
        },
        "TON": {
            "avg": Decimal(1), "min": Decimal(1), "max": Decimal(1),
            "basis": "TON: справочная оценка финальности около одной секунды",
            "source_url": "https://docs.ton.org/applications/payments/overview",
        },
        "ETHEREUM": {
            "avg": Decimal(384), "min": Decimal(144), "max": Decimal(900),
            "basis": "Пример OKX для USDT: 32 подтверждения × 12 секунд на слот; полная финальность Ethereum наступает отдельно",
            "source_url": "https://www.okx.com/en-gb/help/how-long-does-it-take-for-a-deposit-to-be-completed",
            "source_url_secondary": "https://ethereum.org/developers/docs/blocks",
        },
        "BITCOIN": {
            "avg": Decimal(600), "min": Decimal(600), "max": Decimal(5400),
            "basis": "Bitcoin: средний интервал одного блока около 10 минут; фактическое время и требуемое число подтверждений отличаются",
            "source_url": "https://developer.bitcoin.org/devguide/payment_processing.html",
        },
        "ONTOLOGY": {
            "avg": Decimal(6), "min": Decimal(1), "max": Decimal(30),
            "basis": "Документация Ontology: новый блок создаётся примерно каждые 6 секунд",
            "source_url": "https://docs.ont.io/glossary",
        },
    }

    # Диапазоны относятся к указанному уровню подтверждения, не к зачислению биржей.
    # Если опубликована только одна оценка, случайный режим не выдумывает разброс.
    _AXELAR = "https://docs.axelar.dev/gmp/how-it-works/finality-and-timing"
    for _network, _average, _minimum, _maximum, _basis, _source in (
        ("ARBITRUM", "990", "780", "1200", "Финальность на L1: 13–20 мин.; середина диапазона 16.5 мин. Подтверждение секвенсора 0.25 сек. не является финальностью L1", _AXELAR),
        ("BASE", "1200", "1200", "1200", "Финальность на Ethereum: ориентировочно 20 минут; не время L2-блока", _AXELAR),
        ("OPTIMISM", "1350", "900", "1800", "Финальность на Ethereum: 15–30 минут; середина диапазона 22.5 минуты", "https://docs.optimism.io/op-stack/transactions/transaction-finality"),
        ("CELO", "1500", "1200", "1800", "Celo L2: финальность на Ethereum 20–30 минут; середина диапазона 25 минут", _AXELAR),
        ("MANTLE", "1800", "1800", "1800", "Финальность на Ethereum: ориентировочно 30 минут", _AXELAR),
        ("LINEA", "7200", "7200", "7200", "Финальность L1 с проверкой доказательства: медианная оценка около 2 часов", _AXELAR),
        ("SCROLL", "9000", "7200", "10800", "Финальность L1: 2–3 часа; середина диапазона 2.5 часа", _AXELAR),
        ("ZKSYNC_ERA", "10800", "10800", "10800", "Полная финальность пакета на Ethereum: около 3 часов, не создание L2-блока", "https://docs.zksync.io/zksync-protocol/rollup/finality"),
        ("BITCOIN_CASH", "600", "600", "600", "Целевой средний интервал одного блока: 600 секунд; биржа может требовать несколько подтверждений", "https://documentation.cash/protocol/blockchain/proof-of-work/difficulty-adjustment-algorithm.html"),
        ("BITCOIN_SV", "600", "600", "600", "Целевой интервал одного блока из параметров консенсуса: 10 минут; не окончательная финальность", "https://github.com/bitcoin-sv/bitcoin-sv/blob/master/src/chainparams.cpp"),
        ("LITECOIN", "150", "150", "150", "Средний интервал одного блока: 2.5 минуты; число подтверждений задаёт биржа", "https://litecoin.com/what-is-litecoin"),
        ("DOGECOIN", "60", "60", "60", "Один блок производится примерно раз в минуту; не гарантия зачисления", "https://dogecoin.com/dogepedia/articles/what-is-a-miner/"),
        ("DIGIBYTE", "15", "15", "15", "Интервал одного блока: 15 секунд; биржа может ждать несколько блоков", "https://www.digibyte.org/en/"),
        ("RIPPLE", "4", "3", "5", "Расчёт в XRP Ledger: 3–5 секунд; середина диапазона 4 секунды", "https://xrpl.org/about/xrp"),
        ("STELLAR", "5", "5", "5", "Закрытие и финальность одного реестра: около 5 секунд", "https://developers.stellar.org/"),
        ("CARDANO", "20", "20", "20", "Ожидаемый интервал блока: 20 секунд. Это одно подтверждение, не окончательная финальность", "https://docs.cardano.org/about-cardano/explore-more/time"),
        ("POLKADOT", "6", "6", "6", "Слот производства блока BABE: около 6 секунд; финальность GRANDPA наступает отдельно", "https://docs.polkadot.com/polkadot-protocol/architecture/polkadot-chain/pos-consensus/"),
        ("KUSAMA", "6", "6", "6", "Слот BABE: около 6 секунд; аналогичная схема Polkadot, финальность GRANDPA наступает отдельно", "https://docs.polkadot.com/polkadot-protocol/architecture/polkadot-chain/pos-consensus/"),
        ("COSMOS", "4", "1", "7", "Модель одного блока CometBFT: середина общего диапазона 1–7 секунд; ориентир семейства, не измерение Cosmos Hub", _AXELAR),
        ("OSMOSIS", "1.5", "1.5", "1.5", "Опубликованный Osmosis интервал блока после оптимизации CometBFT: 1.5 секунды", "https://medium.com/osmosis/2024-highlights-a0edc7468973"),
        ("ALGORAND", "2.82", "2.82", "2.82", "Среднее время блока с немедленной финальностью: 2.82 секунды", "https://preview.dev.algorand.co/concepts/transactions/blocks/"),
        ("NEAR", "1.2", "1.2", "1.2", "Опубликованная финальность после перехода к блокам 600 мс: 1.2 секунды", "https://www.near.org/blog/near-2025-milestones"),
        ("APTOS", "1", "1", "1", "Консервативный ориентир 1 секунда: документация указывает обычно менее секунды до фиксации", "https://aptos.dev/build/guides/payments"),
        ("SUI", "0.55", "0.4", "0.7", "Финальность 400–700 мс; середина диапазона 550 мс", "https://docs.sui.io/develop/transactions/transaction-lifecycle"),
        ("AVALANCHE", "1", "1", "1", "Финальность C-Chain: около 1 секунды", "https://docs.avax.network/"),
        ("FANTOM", "1.5", "1", "2", "Финальность Opera: 1–2 секунды; середина диапазона 1.5 секунды", "https://docs.fantom.foundation/technology/faq"),
        ("SONIC", "1", "1", "1", "Консервативный ориентир 1 секунда: документация указывает финальность менее секунды", "https://docs.soniclabs.com/"),
        ("GNOSIS", "150", "150", "150", "Gnosis Chain: около 2.5 минут до полной финальности; не быстрый ответ узла", "https://docs.gnosischain.com/eez/gnosis-eez"),
        ("KAVA", "6", "6", "6", "Kava EVM: блок 6 секунд, финальность за один блок", "https://docs.kava.io/docs/ethereum/overview/"),
        ("FILECOIN", "750", "600", "900", "Ориентир финальности Filecoin 10–15 минут; середина диапазона 12.5 минуты", _AXELAR),
        ("INJECTIVE", "0.64", "0.64", "0.64", "Опубликованный интервал блока: 640 мс", "https://injective.com/inj"),
        ("SEI", "0.4", "0.4", "0.4", "Документация Sei: финальность 400 мс", "https://github.com/sei-protocol/sei-docs/blob/main/llms.txt"),
        ("MULTIVERSX", "6", "6", "6", "Ориентир финальности после обновления v1.9: 6 секунд; ускорения будущих версий не подставляются", "https://docs.multiversx.com/welcome/terminology/"),
    ):
        NETWORK_PROFILES[_network] = {
            "avg": Decimal(_average), "min": Decimal(_minimum), "max": Decimal(_maximum),
            "basis": _basis, "source_url": _source, "checked_on": "2026-11-17",
        }

    MISSING_PROFILE_REASONS = {
        "BLAST": "В доступной официальной документации не подтверждён актуальный срок финальности; время блока из сторонних каталогов не используется",
        "CRONOS": "Нужно уточнить EVM или PoS и завершение обновления времени блока; старое значение 5.6 сек. и целевое менее секунды не подменяют измерение",
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
