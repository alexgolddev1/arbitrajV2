"""Запрос и разбор рынков криптовалют из CoinMarketCap."""

from __future__ import annotations

import json
import logging
import time
import threading
from decimal import Decimal
from typing import Any

import requests

from config import (
    CALCULATOR_API_URL,
    CALCULATOR_CATEGORY,
    CALCULATOR_CENTER_TYPE,
    CALCULATOR_DIRECTION,
    CALCULATOR_PAGE_SIZE,
    CALCULATOR_REQUEST_DELAY_SECONDS,
    CALCULATOR_SORT,
    CALCULATOR_SPOT_UNTRACKED,
    CMC_REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)
_request_schedule_lock = threading.Lock()
_next_request_at = 0.0
_TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
_MAX_REQUEST_ATTEMPTS = 3


def _wait_for_request_slot() -> None:
    """Распределить старт API-запросов с паузой между потоками."""
    global _next_request_at
    delay = CALCULATOR_REQUEST_DELAY_SECONDS
    if delay <= 0:
        return
    with _request_schedule_lock:
        now = time.monotonic()
        wait_seconds = max(0.0, _next_request_at - now)
        _next_request_at = max(now, _next_request_at) + delay
    if wait_seconds:
        time.sleep(wait_seconds)


def fetch_currency_markets(
    slug: str,
    target_exchanges: set[str],
) -> list[dict[str, Any]]:
    """Получить рынки до обнаружения всех настроенных бирж или конца выдачи."""
    if not 1 <= CALCULATOR_PAGE_SIZE <= 500:
        raise ValueError("CALCULATOR_PAGE_SIZE должен быть от 1 до 500")
    if CALCULATOR_REQUEST_DELAY_SECONDS < 0:
        raise ValueError("CALCULATOR_REQUEST_DELAY_SECONDS не может быть отрицательным")

    markets: list[dict[str, Any]] = []
    found_exchanges: set[str] = set()
    start = 1
    while True:
        logger.debug(
            "Запрос CoinMarketCap: slug=%s, start=%s, limit=%s",
            slug, start, CALCULATOR_PAGE_SIZE,
        )
        params = {
            "slug": slug.lower(),
            "start": start,
            "limit": CALCULATOR_PAGE_SIZE,
            "category": CALCULATOR_CATEGORY,
            "centerType": CALCULATOR_CENTER_TYPE,
            "sort": CALCULATOR_SORT,
            "direction": CALCULATOR_DIRECTION,
            "spotUntracked": CALCULATOR_SPOT_UNTRACKED,
        }
        response = None
        for attempt in range(1, _MAX_REQUEST_ATTEMPTS + 1):
            _wait_for_request_slot()
            try:
                response = requests.get(
                    CALCULATOR_API_URL,
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                    timeout=CMC_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                if attempt >= _MAX_REQUEST_ATTEMPTS:
                    logger.exception(
                        "Сбой запроса CoinMarketCap после %s попыток: slug=%s, start=%s",
                        attempt, slug, start,
                    )
                    raise
                logger.warning(
                    "Сбой запроса CoinMarketCap, повтор %s/%s через %s сек.: slug=%s, start=%s, ошибка=%s",
                    attempt, _MAX_REQUEST_ATTEMPTS, attempt, slug, start, error,
                )
                time.sleep(attempt)
                continue

            if not response.ok:
                # CMC/proxy errors often include a useful body for diagnosis.
                body = response.text[:2000]
                retryable = response.status_code in _TRANSIENT_HTTP_STATUSES
                logger.log(
                    logging.WARNING if retryable and attempt < _MAX_REQUEST_ATTEMPTS else logging.ERROR,
                    "Ответ CoinMarketCap с ошибкой: slug=%s, start=%s, HTTP %s, URL=%s, body=%r%s%s",
                    slug,
                    start,
                    response.status_code,
                    response.url,
                    body,
                    " … [тело обрезано]" if len(response.text) > 2000 else "",
                    (f"; повтор {attempt}/{_MAX_REQUEST_ATTEMPTS} через {attempt} сек."
                     if retryable and attempt < _MAX_REQUEST_ATTEMPTS else ""),
                )
                if retryable and attempt < _MAX_REQUEST_ATTEMPTS:
                    time.sleep(attempt)
                    continue
            break
        assert response is not None
        response.raise_for_status()
        payload = json.loads(response.text, parse_float=Decimal)
        status = payload.get("status", {})
        if str(status.get("error_code", "0")) != "0":
            raise RuntimeError(
                f"Ошибка CoinMarketCap для {slug}: {status.get('error_message')}"
            )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError(
                f"Ответ CoinMarketCap для {slug} содержит некорректное поле data"
            )
        if "marketPairs" not in data:
            # CMC возвращает SUCCESS и только sort/direction для валют без рынков.
            if not markets:
                logger.debug("CoinMarketCap не вернул рынки для %s", slug)
            break
        page = data.get("marketPairs")
        if not isinstance(page, list):
            status_message = status.get("error_message") or status.get("message")
            data_keys = sorted(data.keys()) if isinstance(data, dict) else []
            raise ValueError(
                f"Ответ CoinMarketCap для {slug} не содержит массив data.marketPairs; "
                f"status={status_message!r}, data_keys={data_keys}"
            )

        markets.extend(page)
        found_exchanges.update(
            str(market.get("exchangeSlug", "")).strip().lower()
            for market in page
            if market.get("exchangeSlug")
            and str(market.get("category", "")).strip().lower()
            == CALCULATOR_CATEGORY.strip().lower()
        )
        if target_exchanges.issubset(found_exchanges):
            logger.debug(
                "Все биржи из конфига найдены для %s; дальнейшие страницы не запрашиваются",
                slug,
            )
            break
        if not page:
            break
        start += len(page)
        total = data.get("numMarketPairs")
        if total is not None:
            if start > int(total):
                break
        elif len(page) < CALCULATOR_PAGE_SIZE:
            break
    return markets
