"""Confirm CMC candidates against live spot order books and measure lifetimes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from websockets.asyncio.client import connect

from config import CALCULATOR_SPREAD_LEVELS, SCREENER_QUOTE_SYMBOLS, TRANSFER_NETWORK_MAP
from database import (
    connect_db,
    finalize_interrupted_lifetimes,
    finalize_lifetime_at_checkpoint,
    get_websocket_markets,
    record_opportunity_lifetime,
    touch_opportunity_lifetimes,
)
from main.treider.tran_emulation import TransferTimeEmulator
from main.treider.config import MIN_OPPORTUNITY_LIFETIME_SECONDS
from main.treider.queue import enqueue as enqueue_trader_signal
from main.treider.signals import build_opportunity_signal
from .mexc_proto import PushDataV3ApiWrapper

logger = logging.getLogger(__name__)
STALE_SECONDS = 60


@dataclass(frozen=True)
class Instrument:
    source_id: int
    source_pair: str
    source_base_symbol: str
    exchange: str
    exchange_name: str
    market_pair: str
    symbol: str
    unit_count: Decimal


@dataclass
class Quote:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    received_at: float


@dataclass
class Episode:
    lifetime_id: int
    started_at: datetime
    started_monotonic: float
    max_spread: Decimal
    last_checkpoint: datetime
    last_confirmed_at: datetime
    spread: Decimal
    buy_exchange: str
    buy_market_pair: str
    buy_price: Decimal
    buy_size: Decimal
    sell_exchange: str
    sell_market_pair: str
    sell_price: Decimal
    sell_size: Decimal
    network: str
    transfer_seconds: float
    trader_signal_sent: bool = False


@dataclass
class PendingEpisode:
    started_at: datetime
    started_monotonic: float
    transfer_seconds: float
    network: str
    max_spread: Decimal
    last_confirmed_at: datetime
    spread: Decimal
    buy_exchange: str
    buy_market_pair: str
    buy_price: Decimal
    sell_exchange: str
    sell_market_pair: str
    sell_price: Decimal


def _symbol_pair(market_pair: str) -> tuple[str, str] | None:
    parts = re.split(r"[/_\-]", market_pair.strip().upper())
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def _unit_count(pair_base: str) -> Decimal:
    match = re.match(r"^(\d+)(?=[A-Z])", pair_base)
    if match:
        count = Decimal(match.group(1))
        if count > 0:
            return count
    return Decimal(1)


def _load_instruments() -> list[Instrument]:
    connection = connect_db()
    try:
        rows = get_websocket_markets(connection)
    finally:
        connection.close()
    instruments: list[Instrument] = []
    for row in rows:
        market_pair = str(row["market_pair"] or "")
        pair = _symbol_pair(market_pair)
        if not pair:
            continue
        exchange = str(row["exchange_slug"]).lower()
        base, quote_symbol = pair
        if (
            quote_symbol != str(row["source_quote_symbol"]).upper()
            or SCREENER_QUOTE_SYMBOLS and quote_symbol not in SCREENER_QUOTE_SYMBOLS
        ):
            continue
        symbol = f"{base}{quote_symbol}"
        if exchange == "okx":
            symbol = f"{base}-{quote_symbol}"
        instruments.append(Instrument(
            source_id=int(row["source_market_id"]),
            source_pair=str(row["source_pair"] or market_pair),
            source_base_symbol=str(row["source_base_symbol"] or "").upper(),
            exchange=exchange,
            exchange_name=str(row["exchange_name"] or exchange.upper()),
            market_pair=market_pair,
            symbol=symbol,
            unit_count=_unit_count(base),
        ))
    return instruments


class OpportunityConfirmer:
    def __init__(self, instruments: list[Instrument]) -> None:
        self.instruments = instruments
        self.by_exchange_symbol: dict[tuple[str, str], list[Instrument]] = {}
        for instrument in instruments:
            self.by_exchange_symbol.setdefault(
                (instrument.exchange, instrument.symbol.upper()), []
            ).append(instrument)
        self.quotes: dict[tuple[int, str], Quote] = {}
        self.episodes: dict[tuple[int, Decimal], Episode] = {}
        self.pending: dict[tuple[int, Decimal], PendingEpisode] = {}
        self.transfer_emulator = TransferTimeEmulator()
        self._evaluation_locks: dict[int, asyncio.Lock] = {}
        self._cycle_confirmed_sources: set[int] = set()
        self._cycle_confirmed_by_threshold = {threshold: 0 for threshold in CALCULATOR_SPREAD_LEVELS}
        self._cycle_sources_with_two_quotes: set[int] = set()
        self._cycle_quote_updates: dict[str, int] = {}
        self._cycle_max_spread: Decimal | None = None

    def reset_cycle_stats(self) -> None:
        self._cycle_confirmed_sources.clear()
        for threshold in CALCULATOR_SPREAD_LEVELS:
            self._cycle_confirmed_by_threshold[threshold] = 0
        self._cycle_sources_with_two_quotes.clear()
        self._cycle_quote_updates = {exchange: 0 for exchange, _ in self.by_exchange_symbol}
        self._cycle_max_spread = None

    def cycle_stats(self) -> dict[str, Any]:
        exchanges_by_source: dict[int, set[str]] = {}
        for instrument in self.instruments:
            exchanges_by_source.setdefault(instrument.source_id, set()).add(instrument.exchange)
        active_by_threshold = {
            threshold: sum(1 for _, active_threshold in self.episodes if active_threshold == threshold)
            for threshold in CALCULATOR_SPREAD_LEVELS
        }
        pending_by_threshold = {
            threshold: sum(1 for _, active_threshold in self.pending if active_threshold == threshold)
            for threshold in CALCULATOR_SPREAD_LEVELS
        }
        active_sources = {source_id for source_id, _ in self.episodes}
        pending_sources = {source_id for source_id, _ in self.pending}
        return {
            "candidate_pairs": sum(len(exchanges) >= 2 for exchanges in exchanges_by_source.values()),
            "quoted_pairs": len(self._cycle_sources_with_two_quotes),
            "max_spread": self._cycle_max_spread,
            "quote_updates": dict(self._cycle_quote_updates),
            "confirmed_pairs": len(self._cycle_confirmed_sources),
            "confirmed_levels": dict(self._cycle_confirmed_by_threshold),
            "active_pairs": len(active_sources),
            "active_levels": active_by_threshold,
            "pending_pairs": len(pending_sources),
            "pending_levels": pending_by_threshold,
        }

    def reconfigure(self, instruments: list[Instrument]) -> None:
        previous_symbols = {
            (item.source_id, item.exchange): item.symbol.upper()
            for item in self.instruments
        }
        current_symbols = {
            (item.source_id, item.exchange): item.symbol.upper()
            for item in instruments
        }
        retained_quote_keys = {
            key for key, symbol in current_symbols.items()
            if previous_symbols.get(key) == symbol and key in self.quotes
        }
        self.quotes = {
            key: quote_data
            for key, quote_data in self.quotes.items()
            if key in retained_quote_keys
        }
        self.instruments = instruments
        self.by_exchange_symbol.clear()
        for instrument in instruments:
            self.by_exchange_symbol.setdefault(
                (instrument.exchange, instrument.symbol.upper()), []
            ).append(instrument)

    async def quote(
        self,
        exchange: str,
        symbol: str,
        bid: Any,
        ask: Any,
        bid_size: Any,
        ask_size: Any,
    ) -> None:
        try:
            bid_price = Decimal(str(bid or "0"))
            ask_price = Decimal(str(ask or "0"))
            bid_quantity = Decimal(str(bid_size or "0"))
            ask_quantity = Decimal(str(ask_size or "0"))
        except Exception:
            return
        if not all(value.is_finite() for value in (
            bid_price, ask_price, bid_quantity, ask_quantity
        )):
            return
        key = (exchange, symbol.upper())
        for instrument in self.by_exchange_symbol.get(key, ()):
            self._cycle_quote_updates[exchange] = self._cycle_quote_updates.get(exchange, 0) + 1
            if bid_price > 0 and ask_price > 0 and bid_price > ask_price:
                bid_quantity = ask_quantity = Decimal(0)
            normalized = Quote(
                bid_price / instrument.unit_count if bid_price > 0 else Decimal(0),
                ask_price / instrument.unit_count if ask_price > 0 else Decimal(0),
                max(Decimal(0), bid_quantity),
                max(Decimal(0), ask_quantity),
                time.monotonic(),
            )
            self.quotes[(instrument.source_id, instrument.exchange)] = normalized
            await self._evaluate(instrument.source_id)

    async def _evaluate(self, source_id: int) -> None:
        lock = self._evaluation_locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            await self._evaluate_locked(source_id)

    async def _evaluate_locked(self, source_id: int) -> None:
        now_mono = time.monotonic()
        current = [
            (instrument, self.quotes.get((source_id, instrument.exchange)))
            for instrument in self.instruments
            if instrument.source_id == source_id
        ]
        by_exchange: dict[str, tuple[Instrument, Quote]] = {}
        for instrument, quote_data in current:
            if quote_data and now_mono - quote_data.received_at <= STALE_SECONDS:
                by_exchange[instrument.exchange] = (instrument, quote_data)
        if len(by_exchange) < 2:
            # Закрываем эпизоды, если уже нельзя подтвердить котировки как минимум
            # на двух биржах. Длительность заканчивается на последней проверке,
            # которая действительно подтвердила спред.
            for episode_key, episode in list(self.episodes.items()):
                if episode_key[0] != source_id:
                    continue
                await asyncio.to_thread(
                    finalize_lifetime_at_checkpoint,
                    episode.lifetime_id,
                    episode.last_confirmed_at,
                )
                del self.episodes[episode_key]
                logger.info(
                    "Возможность %s завершена: недостаточно свежих котировок; "
                    "последнее подтверждение %s UTC, длительность %s сек.",
                    source_id,
                    episode.last_confirmed_at.isoformat(sep=" "),
                    int((episode.last_confirmed_at - episode.started_at).total_seconds()),
                )
                self.pending.pop(episode_key, None)
            for pending_key, pending in list(self.pending.items()):
                if pending_key[0] == source_id:
                    del self.pending[pending_key]
                    logger.info(
                        "Кандидат %s отброшен: котировки устарели до завершения перевода сети %s",
                        source_id, pending.network,
                    )
            return
        self._cycle_sources_with_two_quotes.add(source_id)

        executable_pairs = [
            (buy, sell)
            for buy in by_exchange.values()
            if buy[1].ask > 0 and buy[1].ask_size > 0
            for sell in by_exchange.values()
            if sell[0].exchange != buy[0].exchange
            and sell[1].bid > 0 and sell[1].bid_size > 0
        ]
        if not executable_pairs:
            for episode_key, episode in list(self.episodes.items()):
                if episode_key[0] != source_id:
                    continue
                await asyncio.to_thread(
                    finalize_lifetime_at_checkpoint,
                    episode.lifetime_id,
                    episode.last_confirmed_at,
                )
                del self.episodes[episode_key]
                logger.info(
                    "Возможность %s завершена: нет встречных уровней стакана "
                    "с доступным объёмом на разных биржах; длительность %s сек.",
                    source_id,
                    int((episode.last_confirmed_at - episode.started_at).total_seconds()),
                )
                self.pending.pop(episode_key, None)
            for pending_key, pending in list(self.pending.items()):
                if pending_key[0] == source_id:
                    del self.pending[pending_key]
                    logger.info(
                        "Кандидат %s отброшен: нет доступных уровней стакана до завершения перевода сети %s",
                        source_id, pending.network,
                    )
            return
        buy, sell = max(
            executable_pairs,
            key=lambda pair: (pair[1][1].bid - pair[0][1].ask) / pair[0][1].ask,
        )
        spread = (sell[1].bid - buy[1].ask) / buy[1].ask * Decimal(100)
        if self._cycle_max_spread is None or spread > self._cycle_max_spread:
            self._cycle_max_spread = spread
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for threshold in CALCULATOR_SPREAD_LEVELS:
            episode_key = (source_id, threshold)
            episode = self.episodes.get(episode_key)
            pending = self.pending.get(episode_key)
            route = (buy[0].exchange_name, buy[0].market_pair,
                     sell[0].exchange_name, sell[0].market_pair)

            if episode is not None and route != (
                episode.buy_exchange, episode.buy_market_pair,
                episode.sell_exchange, episode.sell_market_pair,
            ):
                await asyncio.to_thread(
                    record_opportunity_lifetime,
                    lifetime_id=episode.lifetime_id,
                    source_market_id=source_id,
                    source_pair=buy[0].source_pair,
                    threshold_percent=threshold,
                    spread_percent=episode.spread,
                    max_spread_percent=episode.max_spread,
                    buy_exchange=episode.buy_exchange,
                    buy_market_pair=episode.buy_market_pair,
                    buy_price=episode.buy_price,
                    sell_exchange=episode.sell_exchange,
                    sell_market_pair=episode.sell_market_pair,
                    sell_price=episode.sell_price,
                    started_at=episode.started_at,
                    last_confirmed_at=episode.last_confirmed_at,
                    ended_at=now,
                )
                del self.episodes[episode_key]
                self.pending.pop(episode_key, None)
                pending = None
                episode = None

            if spread > threshold:
                if episode is None:
                    if pending is not None and route != (
                        pending.buy_exchange, pending.buy_market_pair,
                        pending.sell_exchange, pending.sell_market_pair,
                    ):
                        del self.pending[episode_key]
                        pending = None
                    if pending is None:
                        network = TRANSFER_NETWORK_MAP.get(
                            buy[0].source_base_symbol, "UNKNOWN"
                        )
                        transfer_seconds = self.transfer_emulator.get_emulated_duration(network)
                        pending = PendingEpisode(
                            started_at=now,
                            started_monotonic=time.monotonic(),
                            transfer_seconds=transfer_seconds,
                            network=network,
                            max_spread=spread,
                            last_confirmed_at=now,
                            spread=spread,
                            buy_exchange=buy[0].exchange_name,
                            buy_market_pair=buy[0].market_pair,
                            buy_price=buy[1].ask,
                            sell_exchange=sell[0].exchange_name,
                            sell_market_pair=sell[0].market_pair,
                            sell_price=sell[1].bid,
                        )
                        self.pending[episode_key] = pending
                        episode_id = await asyncio.to_thread(
                            record_opportunity_lifetime,
                            source_market_id=source_id,
                            source_pair=buy[0].source_pair,
                            threshold_percent=threshold,
                            spread_percent=spread,
                            max_spread_percent=spread,
                            buy_exchange=buy[0].exchange_name,
                            buy_market_pair=buy[0].market_pair,
                            buy_price=buy[1].ask,
                            sell_exchange=sell[0].exchange_name,
                            sell_market_pair=sell[0].market_pair,
                            sell_price=sell[1].bid,
                            started_at=now,
                            last_confirmed_at=now,
                            transfer_seconds=transfer_seconds,
                        )
                        if episode_id is not None:
                            episode = Episode(
                                episode_id, now, pending.started_monotonic, spread, now, now, spread,
                                buy[0].exchange_name, buy[0].market_pair, buy[1].ask, buy[1].ask_size,
                                sell[0].exchange_name, sell[0].market_pair, sell[1].bid, sell[1].bid_size,
                                network, transfer_seconds,
                            )
                            self.episodes[episode_key] = episode
                            self._cycle_confirmed_sources.add(source_id)
                            self._cycle_confirmed_by_threshold[threshold] += 1
                        logger.info(
                            "Кандидат %s: спред %.4f%% выше порога %.4f%%; "
                            "покупка %s ask=%s (котировка %.2f сек.), "
                            "продажа %s bid=%s (котировка %.2f сек.); "
                            "сигнал трейдеру после %.1f сек. непрерывного спреда; "
                            "сеть %s, оценка перевода %.1f сек.",
                            buy[0].source_pair, spread, threshold,
                            buy[0].exchange_name, buy[1].ask,
                            max(0.0, now_mono - buy[1].received_at),
                            sell[0].exchange_name, sell[1].bid,
                            max(0.0, now_mono - sell[1].received_at),
                            MIN_OPPORTUNITY_LIFETIME_SECONDS, network, transfer_seconds,
                        )
                    else:
                        pending.last_confirmed_at = now
                        pending.max_spread = max(pending.max_spread, spread)
                        pending.spread = spread
                        pending.buy_exchange = buy[0].exchange_name
                        pending.buy_market_pair = buy[0].market_pair
                        pending.buy_price = buy[1].ask
                        pending.sell_exchange = sell[0].exchange_name
                        pending.sell_market_pair = sell[0].market_pair
                        pending.sell_price = sell[1].bid

                    if time.monotonic() - pending.started_monotonic >= pending.transfer_seconds:
                        del self.pending[episode_key]
                        logger.info(
                            "Возможность %s сохраняла спред выше %.4f%% в течение %.1f сек. "
                            "эмуляции сети %s",
                            buy[0].source_pair, threshold, pending.transfer_seconds, pending.network,
                        )
                if episode is not None:
                    episode.last_confirmed_at = now
                    episode.max_spread = max(episode.max_spread, spread)
                    episode.spread = spread
                    episode.buy_exchange = buy[0].exchange_name
                    episode.buy_market_pair = buy[0].market_pair
                    episode.buy_price = buy[1].ask
                    episode.buy_size = buy[1].ask_size
                    episode.sell_exchange = sell[0].exchange_name
                    episode.sell_market_pair = sell[0].market_pair
                    episode.sell_price = sell[1].bid
                    episode.sell_size = sell[1].bid_size
                    # Life is measured independently of the simulated transfer countdown.
                    elapsed_seconds = time.monotonic() - episode.started_monotonic
                    if (
                        not episode.trader_signal_sent
                        and elapsed_seconds >= MIN_OPPORTUNITY_LIFETIME_SECONDS
                    ):
                        signal_key, payload = build_opportunity_signal(
                            lifetime_id=episode.lifetime_id,
                            source_market_id=source_id,
                            source_pair=buy[0].source_pair,
                            threshold_percent=threshold,
                            spread_percent=episode.spread,
                            max_spread_percent=episode.max_spread,
                            buy_exchange=episode.buy_exchange,
                            buy_market_pair=episode.buy_market_pair,
                            buy_price=episode.buy_price,
                            buy_size=episode.buy_size,
                            sell_exchange=episode.sell_exchange,
                            sell_market_pair=episode.sell_market_pair,
                            sell_price=episode.sell_price,
                            sell_size=episode.sell_size,
                            network=episode.network,
                            estimated_transfer_seconds=episode.transfer_seconds,
                            started_at_utc=episode.started_at.isoformat(sep=" "),
                            lifetime_seconds=elapsed_seconds,
                        )
                        await asyncio.to_thread(enqueue_trader_signal, signal_key, payload)
                        episode.trader_signal_sent = True
                        logger.info(
                            "Скринер отправил трейдеру JSON по %s: порог %.4f%%, жизнь %.1f сек.",
                            buy[0].source_pair,
                            threshold,
                            elapsed_seconds,
                        )
            elif pending is not None:
                if episode is not None:
                    await asyncio.to_thread(
                        record_opportunity_lifetime,
                        lifetime_id=episode.lifetime_id,
                        source_market_id=source_id,
                        source_pair=buy[0].source_pair,
                        threshold_percent=threshold,
                        spread_percent=spread,
                        max_spread_percent=episode.max_spread,
                        buy_exchange=buy[0].exchange_name,
                        buy_market_pair=buy[0].market_pair,
                        buy_price=buy[1].ask,
                        sell_exchange=sell[0].exchange_name,
                        sell_market_pair=sell[0].market_pair,
                        sell_price=sell[1].bid,
                        started_at=episode.started_at,
                        last_confirmed_at=episode.last_confirmed_at,
                        ended_at=now,
                    )
                    del self.episodes[episode_key]
                del self.pending[episode_key]
                elapsed_seconds = time.monotonic() - pending.started_monotonic
                signal_status = (
                    "сигнал трейдеру уже отправлен"
                    if episode is not None and episode.trader_signal_sent
                    else f"сигнал трейдеру не отправлен: нужно {MIN_OPPORTUNITY_LIFETIME_SECONDS:.1f} сек."
                )
                logger.info(
                    "Кандидат %s отброшен: текущий спред %.4f%% ниже порога %.4f%%; "
                    "покупка %s ask=%s (котировка %.2f сек.), "
                    "продажа %s bid=%s (котировка %.2f сек.); прошло %.1f сек.; %s; "
                    "сеть %s, оценка перевода %.1f сек.",
                    buy[0].source_pair, spread, threshold,
                    buy[0].exchange_name, buy[1].ask,
                    max(0.0, now_mono - buy[1].received_at),
                    sell[0].exchange_name, sell[1].bid,
                    max(0.0, now_mono - sell[1].received_at),
                    elapsed_seconds, signal_status, pending.network,
                    pending.transfer_seconds,
                )
            elif episode is not None:
                await asyncio.to_thread(
                    record_opportunity_lifetime,
                    lifetime_id=episode.lifetime_id,
                    source_market_id=source_id,
                    source_pair=buy[0].source_pair,
                    threshold_percent=threshold,
                    spread_percent=spread,
                    max_spread_percent=episode.max_spread,
                    buy_exchange=buy[0].exchange_name,
                    buy_market_pair=buy[0].market_pair,
                    buy_price=buy[1].ask,
                    sell_exchange=sell[0].exchange_name,
                    sell_market_pair=sell[0].market_pair,
                    sell_price=sell[1].bid,
                    started_at=episode.started_at,
                    last_confirmed_at=episode.last_confirmed_at,
                    ended_at=now,
                )
                del self.episodes[episode_key]
                logger.info(
                    "Возможность %s завершилась: порог %.4f%%, длилась %s сек.",
                    buy[0].source_pair, threshold, int((now - episode.started_at).total_seconds()),
                )

    async def checkpoint(self) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Периодически проверяем свежесть даже если биржа перестала присылать
        # сообщения: иначе UI продолжал бы считать такой эпизод активным.
        source_ids = {source_id for source_id, _ in self.episodes}
        source_ids.update(source_id for source_id, _ in self.pending)
        for source_id in source_ids:
            now_mono = time.monotonic()
            fresh_exchanges = {
                instrument.exchange
                for instrument in self.instruments
                if instrument.source_id == source_id
                and (quote_data := self.quotes.get((source_id, instrument.exchange))) is not None
                and now_mono - quote_data.received_at <= STALE_SECONDS
            }
            if len(fresh_exchanges) < 2:
                await self._evaluate(source_id)
        active = [episode for episode in self.episodes.values()
                  if now - episode.last_checkpoint >= timedelta(seconds=30)]
        rows = [
            (episode.last_confirmed_at, episode.spread, episode.max_spread, episode.buy_exchange,
             episode.buy_market_pair, episode.buy_price, episode.sell_exchange,
             episode.sell_market_pair, episode.sell_price, episode.lifetime_id)
            for episode in active
        ]
        if rows:
            await asyncio.to_thread(touch_opportunity_lifetimes, rows)
            for episode in active:
                episode.last_checkpoint = now

async def _listen_binance(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    streams = sorted({f"{item.symbol.lower()}@bookTicker" for item in instruments})
    async def listen_batch(batch: list[str]) -> None:
        url = "wss://stream.binance.com:9443/stream?streams=" + quote("/".join(batch), safe="@/")
        await _listen_json(url, stop, lambda data: confirmer.quote(
            "binance", data.get("s", ""), data.get("b"), data.get("a"),
            data.get("B"), data.get("A"),
        ))
    await asyncio.gather(*(listen_batch(streams[offset:offset + 200]) for offset in range(0, len(streams), 200)))


async def _listen_bybit(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    topics = sorted({f"tickers.{item.symbol}" for item in instruments})
    async def process(data: dict[str, Any]) -> None:
        topic = str(data.get("topic", ""))
        ticker = data.get("data") or {}
        if isinstance(ticker, dict):
            await confirmer.quote(
                "bybit", ticker.get("symbol", topic.removeprefix("tickers.")),
                ticker.get("bid1Price"), ticker.get("ask1Price"),
                ticker.get("bid1Size"), ticker.get("ask1Size"),
            )
    await _listen_subscribed("wss://stream.bybit.com/v5/public/spot", topics, 10, stop, process, "bybit")


async def _listen_okx(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    args = [
        {"channel": "tickers", "instId": symbol}
        for symbol in sorted({item.symbol for item in instruments})
    ]
    async def process(data: dict[str, Any]) -> None:
        for ticker in data.get("data", []):
            await confirmer.quote(
                "okx", ticker.get("instId", ""), ticker.get("bidPx"), ticker.get("askPx"),
                ticker.get("bidSz"), ticker.get("askSz"),
            )
    await _listen_subscribed("wss://ws.okx.com/ws/v5/public", args, 100, stop, process, "okx")


async def _listen_mexc(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    subscriptions = sorted({f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{item.symbol}" for item in instruments})
    async def process(data: dict[str, Any]) -> None:
        ticker = data.get("publicbookticker") or {}
        await confirmer.quote(
            "mexc", data.get("symbol", ""), ticker.get("bidprice"), ticker.get("askprice"),
            ticker.get("bidquantity"), ticker.get("askquantity"),
        )
    batches = [subscriptions[offset:offset + 30] for offset in range(0, len(subscriptions), 30)]
    await asyncio.gather(*(
        _listen_subscribed("wss://wbs-api.mexc.com/ws", batch, len(batch), stop, process, "mexc", mexc_ping=True)
        for batch in batches
    ))


async def _listen_json(
    url: str,
    stop: asyncio.Event,
    handler: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    delay = 1
    while not stop.is_set():
        try:
            async with connect(url, ping_interval=20, ping_timeout=20, close_timeout=3) as websocket:
                delay = 1
                async for message in websocket:
                    payload = json.loads(message)
                    if "data" in payload:
                        payload = payload["data"]
                    if isinstance(payload, dict):
                        await handler(payload)
                    if stop.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebSocket Binance отключился; повтор через %s сек.", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def _listen_subscribed(
    url: str,
    subscriptions: list[Any],
    batch_size: int,
    stop: asyncio.Event,
    handler: Callable[[dict[str, Any]], Awaitable[None]],
    exchange: str,
    mexc_ping: bool = False,
) -> None:
    delay = 1
    while not stop.is_set():
        try:
            async with connect(url, ping_interval=20, ping_timeout=20, close_timeout=3) as websocket:
                delay = 1
                for offset in range(0, len(subscriptions), batch_size):
                    batch = subscriptions[offset:offset + batch_size]
                    request = (
                        {"method": "SUBSCRIPTION", "params": batch}
                        if exchange == "mexc"
                        else {"op": "subscribe", "args": batch}
                    )
                    await websocket.send(json.dumps(request))
                    await asyncio.sleep(0.12)

                async def mexc_heartbeat() -> None:
                    while True:
                        await asyncio.sleep(20)
                        await websocket.send(json.dumps({"method": "PING"}))

                ping_task = asyncio.create_task(mexc_heartbeat()) if mexc_ping else None
                try:
                    async for message in websocket:
                        if mexc_ping and isinstance(message, bytes):
                            protobuf_message = PushDataV3ApiWrapper()
                            protobuf_message.ParseFromString(message)
                            if not protobuf_message.HasField("publicAggreBookTicker"):
                                continue
                            ticker = protobuf_message.publicAggreBookTicker
                            data = {
                                "symbol": protobuf_message.symbol,
                                "publicbookticker": {
                                    "bidprice": ticker.bidPrice,
                                    "bidquantity": ticker.bidQuantity,
                                    "askprice": ticker.askPrice,
                                    "askquantity": ticker.askQuantity,
                                },
                            }
                        else:
                            data = json.loads(message)
                        if isinstance(data, dict) and "data" in data and exchange == "binance":
                            data = data["data"]
                        if isinstance(data, dict):
                            await handler(data)
                        if stop.is_set():
                            break
                finally:
                    if ping_task:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WebSocket %s отключился; повтор через %s сек.", exchange, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def _configure_confirmer(instruments: list[Instrument]) -> OpportunityConfirmer | None:
    global _confirmer
    if not instruments:
        if _confirmer:
            for episode in _confirmer.episodes.values():
                # Preserve confirmed time up to the last periodic checkpoint.
                await asyncio.to_thread(
                    finalize_lifetime_at_checkpoint,
                    episode.lifetime_id,
                    episode.last_confirmed_at,
                )
            _confirmer = None
        return None
    if _confirmer is None:
        _confirmer = OpportunityConfirmer(instruments)
    else:
        covered_sources = {
            source_id for source_id in {item.source_id for item in instruments}
            if len({item.exchange for item in instruments if item.source_id == source_id}) >= 2
        }
        for episode_key, episode in list(_confirmer.episodes.items()):
            if episode_key[0] not in covered_sources:
                await asyncio.to_thread(
                    finalize_lifetime_at_checkpoint,
                    episode.lifetime_id,
                    episode.last_confirmed_at,
                )
                del _confirmer.episodes[episode_key]
        for pending_key, pending in list(_confirmer.pending.items()):
            if pending_key[0] not in covered_sources:
                del _confirmer.pending[pending_key]
                logger.info(
                    "Кандидат %s отброшен: в обновлённом списке нет двух биржевых рынков",
                    pending_key[0],
                )
        _confirmer.reconfigure(instruments)
    return _confirmer


def _listener_tasks(
    instruments: list[Instrument], confirmer: OpportunityConfirmer | None
) -> tuple[asyncio.Event, list[asyncio.Task[Any]]]:
    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    if confirmer is None:
        return stop, tasks
    by_exchange = {
        exchange: [item for item in instruments if item.exchange == exchange]
        for exchange in {item.exchange for item in instruments}
    }
    listeners = {
        "binance": _listen_binance,
        "bybit": _listen_bybit,
        "okx": _listen_okx,
        "mexc": _listen_mexc,
    }
    for exchange, markets in by_exchange.items():
        listener = listeners.get(exchange)
        if listener:
            tasks.append(asyncio.create_task(listener(markets, confirmer, stop)))
        else:
            logger.warning("Нет WebSocket адаптера для биржи %s", exchange)
    return stop, tasks


_confirmer: OpportunityConfirmer | None = None


def initialize_monitor() -> None:
    """Reset episodes left active by an unexpected process stop."""
    global _confirmer
    _confirmer = None
    interrupted = finalize_interrupted_lifetimes()
    if interrupted:
        logger.warning("Закрыто прерванных WebSocket-интервалов: %s", interrupted)


async def _monitor_forever(refresh_callback: Callable[[], int]) -> None:
    interval_seconds = max(1, await asyncio.to_thread(refresh_callback))
    while True:
        instruments = await asyncio.to_thread(_load_instruments)
        logger.info("WebSocket-наблюдение: рынков %s, интервал обновления %s сек.", len(instruments), interval_seconds)
        confirmer = await _configure_confirmer(instruments)
        if confirmer:
            confirmer.reset_cycle_stats()
        stop, tasks = _listener_tasks(instruments, confirmer)
        deadline = time.monotonic() + interval_seconds
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(min(30, max(0.1, deadline - time.monotonic())))
                if confirmer:
                    await confirmer.checkpoint()
            # Keep the current exchange sockets open during this REST refresh.
            try:
                interval_seconds = max(1, await asyncio.to_thread(refresh_callback))
            except Exception:
                logger.exception("Ошибка REST-цикла; продолжу наблюдать и повторю по расписанию")
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if confirmer:
                stats = confirmer.cycle_stats()
                confirmed_by_level = ", ".join(
                    f"{threshold}%: {stats['confirmed_levels'][threshold]}"
                    for threshold in CALCULATOR_SPREAD_LEVELS
                )
                active_by_level = ", ".join(
                    f"{threshold}%: {stats['active_levels'][threshold]}"
                    for threshold in CALCULATOR_SPREAD_LEVELS
                )
                pending_by_level = ", ".join(
                    f"{threshold}%: {stats['pending_levels'][threshold]}"
                    for threshold in CALCULATOR_SPREAD_LEVELS
                )
                quote_updates = ", ".join(
                    f"{exchange}: {count}" for exchange, count in sorted(stats["quote_updates"].items())
                ) or "нет"
                logger.info(
                    "Итог WebSocket-проверки: подписок на рынки %s; пар с двумя биржами %s, получали котировки с двух бирж %s; обновлений котировок [%s]; максимальный bid/ask-спред %s%%; подтверждено пар %s, эпизодов по порогам [%s]; сейчас активно пар %s, эпизодов [%s]",
                    len(instruments), stats["candidate_pairs"], stats["quoted_pairs"], quote_updates,
                    f"{stats['max_spread']:.4f}" if stats["max_spread"] is not None else "нет данных",
                    stats["confirmed_pairs"], confirmed_by_level, stats["active_pairs"], active_by_level,
                )
                if stats["pending_pairs"]:
                    logger.info(
                        "Ожидают эмулируемый перевод: кандидатов %s, пороги [%s]",
                        stats["pending_pairs"], pending_by_level,
                    )


def monitor_forever(refresh_callback: Callable[[], int]) -> None:
    """Maintain live subscriptions while scheduled REST refreshes run in parallel."""
    asyncio.run(_monitor_forever(refresh_callback))
