"""Однократный запуск обновления цен калькулятора."""

import logging
import time

from .service import update_calculator_prices

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    started_at = time.perf_counter()
    try:
        update_calculator_prices()
    except KeyboardInterrupt:
        logger.warning("Обновление калькулятора остановлено пользователем")
        raise SystemExit(130)
    except Exception:
        logger.exception("Ошибка обновления цен калькулятора")
        raise SystemExit(1)
    finally:
        elapsed_seconds = time.perf_counter() - started_at
        logger.info(
            "Время обработки запросов: %.1f сек. (%.2f мин.)",
            elapsed_seconds,
            elapsed_seconds / 60,
        )


if __name__ == "__main__":
    main()
