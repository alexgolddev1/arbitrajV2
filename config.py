"""Настройки приложения.

Значения по умолчанию находятся здесь. Параметры скринера, уже записанные в
таблицу MySQL `config`, имеют приоритет. Пароли задаются через переменные
окружения, обычно из файла `.env`, и не хранятся в этом файле.
"""

import os
from decimal import Decimal


# Подключение к MySQL. В Docker DB_HOST должен быть равен имени сервиса `mysql`.
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "arbitraj")
DB_USER = os.getenv("DB_USER", "bot")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10"))
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "12"))
DB_RETRY_DELAY_SECONDS = int(os.getenv("DB_RETRY_DELAY_SECONDS", "5"))

# Адрес CoinMarketCap и таймаут HTTP-запроса: подключение и ожидание ответа.
CMC_API_URL = os.getenv(
    "CMC_API_URL",
    "https://api.coinmarketcap.com/data-api/v3/exchange/market-pairs/latest",
)
CMC_REQUEST_TIMEOUT_SECONDS = (10, 45)

# Параметры калькулятора. Список бирж задаётся slug через запятую в .env.
CALCULATOR_API_URL = os.getenv(
    "CALCULATOR_API_URL",
    "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/market-pairs/latest",
)
CALCULATOR_EXCHANGES = tuple(
    exchange.strip().lower()
    for exchange in os.getenv(
        "CALCULATOR_EXCHANGES", "binance,bybit,okx,mexc"
    ).split(",")
    if exchange.strip()
)
CALCULATOR_CATEGORY = os.getenv("CALCULATOR_CATEGORY", "spot")
CALCULATOR_PAGE_SIZE = int(os.getenv("CALCULATOR_PAGE_SIZE", "500"))
CALCULATOR_CENTER_TYPE = os.getenv("CALCULATOR_CENTER_TYPE", "all")
CALCULATOR_SORT = os.getenv("CALCULATOR_SORT", "cmc_rank_advanced")
CALCULATOR_DIRECTION = os.getenv("CALCULATOR_DIRECTION", "desc")
CALCULATOR_SPOT_UNTRACKED = os.getenv("CALCULATOR_SPOT_UNTRACKED", "true")
CALCULATOR_SPREAD_LEVELS = tuple(
    Decimal(level.strip())
    for level in os.getenv("CALCULATOR_SPREAD_LEVELS", "0.5,1,1.5").split(",")
    if level.strip()
)
if (
    len(CALCULATOR_SPREAD_LEVELS) != 3
    or tuple(sorted(set(CALCULATOR_SPREAD_LEVELS))) != CALCULATOR_SPREAD_LEVELS
):
    raise ValueError(
        "CALCULATOR_SPREAD_LEVELS должен содержать три возрастающих значения, например 0.5,1,1.5"
    )
CALCULATOR_MIN_SPREAD_PERCENT = CALCULATOR_SPREAD_LEVELS[0]
CALCULATOR_REQUEST_DELAY_SECONDS = float(
    os.getenv("CALCULATOR_REQUEST_DELAY_SECONDS", "0.2")
)
CALCULATOR_WORKERS = int(os.getenv("CALCULATOR_WORKERS", "4"))

# Фильтр направления сделки: true — покупка дешевле продажи (прибыльное направление),
# false — обратное условие.
BUY_PRICE_MUST_BE_LOWER = os.getenv(
    "BUY_PRICE_MUST_BE_LOWER", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# Значения добавляются в MySQL-таблицу `config`, только если ключа ещё нет.
# Чтобы изменить настройки во время работы, отредактируйте эту таблицу в phpMyAdmin.
DEFAULT_CONFIG = {
    "slug": "binance",
    "category": "spot",
    "start": "1",
    "stop": "1380",
    "limit": "300",
    "includeUntracked": "false",
    "interval_minutes": "2",
}
DEFAULT_INTERVAL_MINUTES = int(DEFAULT_CONFIG["interval_minutes"])

# Оставлять рынки с включёнными границами объёма за 24 часа в USD.
MIN_VOLUME_USD = Decimal("300000")
MAX_VOLUME_USD = Decimal("500000")
