"""Обработка возможности трейдером; здесь подключается стратегия и исполнение."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from config import CALCULATOR_SPREAD_LEVELS
from main.treider.fees import get_trade_fee_rates
from main.treider.market_data import fetch_current_spread
from main.treider.network_check import check_transfer_network
from main.treider.signals import validate_opportunity_signal
from main.treider.trade import process_trade

logger = logging.getLogger(__name__)


def process_opportunity(payload: dict[str, Any], signal_id: int) -> None:
    signal = validate_opportunity_signal(payload)
    initial_spread = Decimal(signal["spread_percent"])
    minimum_spread = CALCULATOR_SPREAD_LEVELS[0]
    buy = signal["buy"]
    sell = signal["sell"]
    context = (
        "пара=%s, порог сигнала=%s%%, исходный спред=%s%%, покупка=%s %s, продажа=%s %s"
        % (
            signal["source_pair"], signal["threshold_percent"], initial_spread,
            buy["exchange"], buy["market_pair"], sell["exchange"], sell["market_pair"],
        )
    )

    # Этап 1: проверить вывод с биржи покупки и ввод на биржу продажи.
    try:
        transfer = check_transfer_network(signal)
    except Exception as error:
        logger.exception("Связка отклонена: ошибка проверки сети; %s; причина=%s", context, error)
        return
    if not transfer["available"]:
        logger.warning(
            "Связка неактуальна: общего доступного маршрута перевода нет; %s; валюта=%s; "
            "ошибки API сетей=%s",
            context,
            transfer["currency"],
            transfer["errors"],
        )
        return

    logger.info(
        "Маршрут перевода найден: %s → %s, сеть=%s, оценка=%.1f сек., "
        "допущение по биржам: вывод=%s, ввод=%s; %s",
        transfer["source_exchange"], transfer["destination_exchange"],
        transfer["network"], transfer["estimated_seconds"],
        transfer["assumed_source"], transfer["assumed_destination"], context,
    )

    # Персональные ставки аккаунта — отдельно от комиссии вывода по выбранной сети.
    fee_rates = {}
    for side_name, side in (("покупка", buy), ("продажа", sell)):
        try:
            fee_rates[side_name] = get_trade_fee_rates(side["exchange"], side["market_pair"])
        except Exception as error:
            logger.warning(
                "Не удалось получить торговую комиссию для стороны %s (%s %s): %s; %s",
                side_name, side["exchange"], side["market_pair"], error, context,
            )
        else:
            rates = fee_rates[side_name]
            logger.info(
                "Торговая комиссия аккаунта: сторона=%s, биржа=%s, пара=%s, "
                "maker=%.8f%%, taker=%.8f%%, время=%s%s; %s",
                side_name, rates.exchange, rates.symbol,
                rates.maker_rate * 100, rates.taker_rate * 100,
                rates.checked_at_utc,
                f", {rates.details}" if rates.details else "",
                context,
            )

    # Этап 2: независимо запросить свежие лучшие ask/bid на spot-рынках.
    try:
        buy_book, sell_book, current_spread = fetch_current_spread(
            buy["exchange"], buy["market_pair"],
            sell["exchange"], sell["market_pair"],
        )
    except Exception as error:
        logger.exception(
            "Связка неактуальна: не удалось повторно получить spot-котировки; %s; причина=%s",
            context, error,
        )
        return

    change = current_spread - initial_spread
    if current_spread < minimum_spread:
        logger.warning(
            "Связка неактуальна: спред упал ниже минимального уровня %.4f%%; "
            "исходный=%s%%, текущий=%s%%, изменение=%s п.п.; "
            "покупка ask=%s на %s, продажа bid=%s на %s; %s",
            minimum_spread, initial_spread, current_spread, change,
            buy_book.ask, buy_book.exchange, sell_book.bid, sell_book.exchange, context,
        )
        return

    logger.info(
        "Связка актуальна: исходный спред=%s%%, текущий=%s%%, изменение=%s п.п., "
        "минимально допустимый=%s%%; покупка ask=%s (%s), продажа bid=%s (%s), сеть=%s; %s",
        initial_spread, current_spread, change, minimum_spread,
        buy_book.ask, buy_book.exchange, sell_book.bid, sell_book.exchange,
        transfer["network"], context,
    )
    process_trade(
        signal, signal_id, buy_book, sell_book,
        fee_rates.get("покупка"), fee_rates.get("продажа"),
        transfer["estimated_seconds"],
    )
