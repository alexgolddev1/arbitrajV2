"""One-shot entry point for refreshing market data."""

import logging

from .service import update_market_data


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    try:
        update_market_data()
    except Exception:
        logger.exception("Ошибка при обновлении рынков")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
