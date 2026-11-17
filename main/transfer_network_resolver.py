"""Определение сети для монет без ручного правила с сохранением результата в JSON."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from main.treider.config import NETWORK_CACHE_TTL_SECONDS
from main.treider.network_check import check_transfer_network_for_currency

PATH = Path(__file__).resolve().parents[1] / "config" / "detected_transfer_networks.json"
_LOCK = threading.Lock()


def resolve_transfer_network(
    currency: str, source: str, destination: str, *, path: Path = PATH,
) -> dict[str, Any]:
    """Читает актуальное направление из JSON или заново проверяет API бирж."""
    currency, source, destination = currency.strip().upper(), source.strip().lower(), destination.strip().lower()
    key = f"{currency}:{source}:{destination}"
    with _LOCK:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {"version": 1, "routes": {}}
        if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("routes"), dict):
            saved = {"version": 1, "routes": {}}
        now = datetime.now(timezone.utc)
        previous = saved["routes"].get(key)
        if isinstance(previous, dict):
            try:
                age = (now - datetime.fromisoformat(previous["checked_at_utc"])).total_seconds()
                if 0 <= age < NETWORK_CACHE_TTL_SECONDS:
                    return dict(previous)
            except (KeyError, TypeError, ValueError):
                pass
        try:
            route = check_transfer_network_for_currency(currency, source, destination)
            network = route.get("network_profile") or route.get("network")
            seconds = Decimal(str(route.get("estimated_seconds")))
            available = bool(route.get("available") and network and network != "UNKNOWN"
                             and seconds.is_finite() and seconds > 0)
            reason = "" if available else "API не нашёл доступный маршрут с известным временем перевода"
        except (InvalidOperation, TypeError, ValueError):
            network, seconds, available = None, Decimal(0), False
            reason = "Нет достоверных данных о сети или времени перевода"
        except Exception:
            network, seconds, available = None, Decimal(0), False
            reason = "Не удалось получить сведения о сети через API"
        result = {
            "currency": currency, "source_exchange": source, "destination_exchange": destination,
            "available": available, "network": network if available else None,
            "estimated_seconds": float(seconds) if available else None,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
        }
        saved["routes"][key] = result
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return result
