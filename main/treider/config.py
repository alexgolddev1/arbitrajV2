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
