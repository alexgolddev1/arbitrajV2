"""Настройки порога зрелости возможности и очереди трейдера."""

import os
from decimal import Decimal, InvalidOperation


# Минимальное непрерывное время жизни возможности до отправки JSON трейдеру.
MIN_OPPORTUNITY_LIFETIME_SECONDS = max(
    1, float(os.getenv("TRADER_MIN_OPPORTUNITY_LIFETIME_SECONDS", "15"))
)

# Частота проверки очереди сообщений MySQL; это не таймер жизни возможности.
SIGNAL_POLL_INTERVAL_SECONDS = max(
    0.1, float(os.getenv("TRADER_SIGNAL_POLL_INTERVAL_SECONDS", "0.5"))
)

# Таймаут REST-перепроверки публичных стаканов на биржах.
MARKET_DATA_TIMEOUT_SECONDS = max(
    1, float(os.getenv("TRADER_MARKET_DATA_TIMEOUT_SECONDS", "5"))
)

# Срок кэширования проверки сетей; сетевые статусы меняются редко, а API ограничивают запросы.
NETWORK_CACHE_TTL_SECONDS = max(
    0, float(os.getenv("TRADER_NETWORK_CACHE_TTL_SECONDS", "60"))
)

# Кэш персональных торговых комиссий по бирже и спотовой паре.
FEE_CACHE_TTL_SECONDS = max(
    0, float(os.getenv("TRADER_FEE_CACHE_TTL_SECONDS", "300"))
)

# Режим тренировки: 1 — виртуальная торговля, 0 — обычный режим.
_training_mode = os.getenv("TRADER_TRAINING_MODE", "1").strip()
if _training_mode not in {"0", "1"}:
    raise ValueError("TRADER_TRAINING_MODE должен быть равен 1 (тренировка) или 0")
TRAINING_MODE = _training_mode == "1"

# Баланс, которым инициализируется счёт трейдера только при первой установке.
try:
    INITIAL_BALANCE_USD = Decimal(os.getenv("TRADER_INITIAL_BALANCE_USD", "100"))
except InvalidOperation as error:
    raise ValueError("TRADER_INITIAL_BALANCE_USD должен быть числом") from error
if not INITIAL_BALANCE_USD.is_finite() or INITIAL_BALANCE_USD < 0:
    raise ValueError("TRADER_INITIAL_BALANCE_USD должен быть конечным неотрицательным числом")


def _decimal_setting(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as error:
        raise ValueError(f"{name} должен быть десятичным числом") from error
    if not value.is_finite():
        raise ValueError(f"{name} должен быть конечным числом")
    return value


# Явные ставки taker для демо. Пустое значение означает, что запасной ставки нет.
CONFIGURED_TAKER_FEES: dict[str, Decimal | None] = {}
for _exchange in ("BINANCE", "BYBIT", "OKX", "MEXC"):
    _raw_fee = os.getenv(f"TRADER_{_exchange}_TAKER_FEE", "").strip()
    CONFIGURED_TAKER_FEES[_exchange.lower()] = (
        _decimal_setting(f"TRADER_{_exchange}_TAKER_FEE", _raw_fee)
        if _raw_fee else None
    )

# Исследовательский деморежим допускает предполагаемый маршрут без подтверждения API.
ALLOW_UNCONFIRMED_ROUTE_DEMO = os.getenv("TRADER_ALLOW_UNCONFIRMED_ROUTE_DEMO", "1").strip() == "1"
ALLOW_DISABLED_ROUTE_DEMO = os.getenv("TRADER_ALLOW_DISABLED_ROUTE_DEMO", "0").strip() == "1"
FIXED_TRANSFER_SECONDS = _decimal_setting("TRADER_FIXED_TRANSFER_SECONDS", "300")
RANDOM_TRANSFER_TIME = os.getenv("TRADER_RANDOM_TRANSFER_TIME", "0").strip() == "1"
TRANSFER_RANDOM_SEED = os.getenv("TRADER_TRANSFER_RANDOM_SEED", "").strip()
TRANSFER_NETWORK = os.getenv("TRADER_TRANSFER_NETWORK", "").strip().upper()
_transfer_fee_raw = os.getenv("TRADER_TRANSFER_FEE_AMOUNT", "").strip()
TRANSFER_FEE_AMOUNT = (
    _decimal_setting("TRADER_TRANSFER_FEE_AMOUNT", _transfer_fee_raw)
    if _transfer_fee_raw else None
)
TRANSFER_FEE_CURRENCY = os.getenv("TRADER_TRANSFER_FEE_CURRENCY", "").strip().upper()
MIN_NET_PROFIT_USDT = _decimal_setting("TRADER_MIN_NET_PROFIT_USDT", "0")
PRICE_MOVE_RESERVE_PERCENT = _decimal_setting("TRADER_PRICE_MOVE_RESERVE_PERCENT", "0")
# Стоп-лосс в процентах от цены покупки; изменяйте значение здесь.
STOP_LOSS_PERCENT = Decimal("0.5")
SETTLEMENT_RETRY_SECONDS = max(1, int(os.getenv("TRADER_SETTLEMENT_RETRY_SECONDS", "15")))
SETTLEMENT_MAX_WAIT_SECONDS = max(SETTLEMENT_RETRY_SECONDS, int(os.getenv("TRADER_SETTLEMENT_MAX_WAIT_SECONDS", "3600")))
SIGNAL_MAX_AGE_SECONDS = max(1, int(os.getenv("TRADER_SIGNAL_MAX_AGE_SECONDS", "300")))
SIGNAL_MAX_ATTEMPTS = max(1, int(os.getenv("TRADER_SIGNAL_MAX_ATTEMPTS", "5")))
BUY_FEE_CURRENCY = {
    exchange: os.getenv(f"TRADER_{exchange.upper()}_BUY_FEE_CURRENCY", "quote").strip().lower()
    for exchange in ("binance", "bybit", "okx", "mexc")
}
if any(value not in {"base", "quote"} for value in BUY_FEE_CURRENCY.values()):
    raise ValueError("TRADER_*_BUY_FEE_CURRENCY принимает только base или quote")
if (MIN_NET_PROFIT_USDT < 0 or PRICE_MOVE_RESERVE_PERCENT < 0
        or STOP_LOSS_PERCENT < 0 or STOP_LOSS_PERCENT >= 100
        or TRANSFER_FEE_AMOUNT is not None and TRANSFER_FEE_AMOUNT < 0):
    raise ValueError("Порог прибыли, резерв и комиссия перевода не могут быть отрицательными; стоп-лосс должен быть от 0 до 100 процентов")
