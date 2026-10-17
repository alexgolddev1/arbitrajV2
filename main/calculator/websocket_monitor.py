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

from config import CALCULATOR_SPREAD_LEVELS
from database import (
    connect_db,
    finalize_interrupted_lifetimes,
    finalize_lifetime_at_checkpoint,
    get_websocket_markets,
    record_opportunity_lifetime,
    touch_opportunity_lifetimes,
)
from .mexc_proto import PushDataV3ApiWrapper

logger = logging.getLogger(__name__)
STALE_SECONDS = 60


@dataclass(frozen=True)
class Instrument:
    source_id: int
    source_pair: str
    exchange: str
    exchange_name: str
    market_pair: str
    symbol: str
    unit_count: Decimal


@dataclass
class Quote:
    bid: Decimal
    ask: Decimal
    received_at: float


@dataclass
class Episode:
    lifetime_id: int
    started_at: datetime
    max_spread: Decimal
    last_checkpoint: datetime
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
        if quote_symbol != str(row["source_quote_symbol"]).upper():
            continue
        symbol = f"{base}{quote_symbol}"
        if exchange == "okx":
            symbol = f"{base}-{quote_symbol}"
        instruments.append(Instrument(
            source_id=int(row["source_market_id"]),
            source_pair=str(row["source_pair"] or market_pair),
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
        active_sources = {source_id for source_id, _ in self.episodes}
        return {
            "candidate_pairs": sum(len(exchanges) >= 2 for exchanges in exchanges_by_source.values()),
            "quoted_pairs": len(self._cycle_sources_with_two_quotes),
            "max_spread": self._cycle_max_spread,
            "quote_updates": dict(self._cycle_quote_updates),
            "confirmed_pairs": len(self._cycle_confirmed_sources),
            "confirmed_levels": dict(self._cycle_confirmed_by_threshold),
            "active_pairs": len(active_sources),
            "active_levels": active_by_threshold,
        }

    def reconfigure(self, instruments: list[Instrument]) -> None:
        self.instruments = instruments
        self.by_exchange_symbol.clear()
        self.quotes.clear()
        for instrument in instruments:
            self.by_exchange_symbol.setdefault(
                (instrument.exchange, instrument.symbol.upper()), []
            ).append(instrument)

    async def quote(self, exchange: str, symbol: str, bid: Any, ask: Any) -> None:
        try:
            bid_price = Decimal(str(bid))
            ask_price = Decimal(str(ask))
        except Exception:
            return
        if bid_price <= 0 or ask_price <= 0 or bid_price > ask_price:
            return
        key = (exchange, symbol.upper())
        for instrument in self.by_exchange_symbol.get(key, ()):
            self._cycle_quote_updates[exchange] = self._cycle_quote_updates.get(exchange, 0) + 1
            normalized = Quote(
                bid_price / instrument.unit_count,
                ask_price / instrument.unit_count,
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
            return
        self._cycle_sources_with_two_quotes.add(source_id)

        buy = min(by_exchange.values(), key=lambda item: item[1].ask)
        sell = max(by_exchange.values(), key=lambda item: item[1].bid)
        if buy[0].exchange == sell[0].exchange or buy[1].ask <= 0:
            return
        spread = (sell[1].bid - buy[1].ask) / buy[1].ask * Decimal(100)
        if self._cycle_max_spread is None or spread > self._cycle_max_spread:
            self._cycle_max_spread = spread
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for threshold in CALCULATOR_SPREAD_LEVELS:
            episode_key = (source_id, threshold)
            episode = self.episodes.get(episode_key)
            if spread > threshold:
                if episode is None:
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
                    )
                    if episode_id is not None:
                        self.episodes[episode_key] = Episode(
                            episode_id, now, spread, now, spread,
                            buy[0].exchange_name, buy[0].market_pair, buy[1].ask,
                            sell[0].exchange_name, sell[0].market_pair, sell[1].bid,
                        )
                        self._cycle_confirmed_sources.add(source_id)
                        self._cycle_confirmed_by_threshold[threshold] += 1
                        logger.info(
                            "WebSocket подтвердил %s: спред %.4f%% выше %.4f%%",
                            buy[0].source_pair, spread, threshold,
                        )
                if episode is not None:
                    episode.max_spread = max(episode.max_spread, spread)
                    episode.spread = spread
                    episode.buy_exchange = buy[0].exchange_name
                    episode.buy_market_pair = buy[0].market_pair
                    episode.buy_price = buy[1].ask
                    episode.sell_exchange = sell[0].exchange_name
                    episode.sell_market_pair = sell[0].market_pair
                    episode.sell_price = sell[1].bid
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
                    last_confirmed_at=now,
                    ended_at=now,
                )
                del self.episodes[episode_key]
                logger.info(
                    "Возможность %s завершилась: порог %.4f%%, длилась %s сек.",
                    buy[0].source_pair, threshold, int((now - episode.started_at).total_seconds()),
                )

    async def checkpoint(self) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        active = [episode for episode in self.episodes.values()
                  if now - episode.last_checkpoint >= timedelta(seconds=30)]
        rows = [
            (now, episode.spread, episode.max_spread, episode.buy_exchange,
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
        await _listen_json(url, stop, lambda data: confirmer.quote("binance", data.get("s", ""), data.get("b"), data.get("a")))
    await asyncio.gather(*(listen_batch(streams[offset:offset + 200]) for offset in range(0, len(streams), 200)))


async def _listen_bybit(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    topics = sorted({f"tickers.{item.symbol}" for item in instruments})
    async def process(data: dict[str, Any]) -> None:
        topic = str(data.get("topic", ""))
        ticker = data.get("data") or {}
        if isinstance(ticker, dict):
            await confirmer.quote("bybit", ticker.get("symbol", topic.removeprefix("tickers.")), ticker.get("bid1Price"), ticker.get("ask1Price"))
    await _listen_subscribed("wss://stream.bybit.com/v5/public/spot", topics, 10, stop, process, "bybit")


async def _listen_okx(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    args = [
        {"channel": "tickers", "instId": symbol}
        for symbol in sorted({item.symbol for item in instruments})
    ]
    async def process(data: dict[str, Any]) -> None:
        for ticker in data.get("data", []):
            await confirmer.quote("okx", ticker.get("instId", ""), ticker.get("bidPx"), ticker.get("askPx"))
    await _listen_subscribed("wss://ws.okx.com/ws/v5/public", args, 100, stop, process, "okx")


async def _listen_mexc(instruments: list[Instrument], confirmer: OpportunityConfirmer, stop: asyncio.Event) -> None:
    subscriptions = sorted({f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{item.symbol}" for item in instruments})
    async def process(data: dict[str, Any]) -> None:
        ticker = data.get("publicbookticker") or {}
        await confirmer.quote("mexc", data.get("symbol", ""), ticker.get("bidprice"), ticker.get("askprice"))
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
                                    "askprice": ticker.askPrice,
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
                await asyncio.to_thread(finalize_lifetime_at_checkpoint, episode.lifetime_id)
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
                await asyncio.to_thread(finalize_lifetime_at_checkpoint, episode.lifetime_id)
                del _confirmer.episodes[episode_key]
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
                quote_updates = ", ".join(
                    f"{exchange}: {count}" for exchange, count in sorted(stats["quote_updates"].items())
                ) or "нет"
                logger.info(
                    "Итог WebSocket-проверки: подписок на рынки %s; пар с двумя биржами %s, получали котировки с двух бирж %s; обновлений котировок [%s]; максимальный bid/ask-спред %s%%; подтверждено пар %s, эпизодов по порогам [%s]; сейчас активно пар %s, эпизодов [%s]",
                    len(instruments), stats["candidate_pairs"], stats["quoted_pairs"], quote_updates,
                    f"{stats['max_spread']:.4f}" if stats["max_spread"] is not None else "нет данных",
                    stats["confirmed_pairs"], confirmed_by_level, stats["active_pairs"], active_by_level,
                )


def monitor_forever(refresh_callback: Callable[[], int]) -> None:
    """Maintain live subscriptions while scheduled REST refreshes run in parallel."""
    asyncio.run(_monitor_forever(refresh_callback))
