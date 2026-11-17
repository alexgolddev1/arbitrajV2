"""Авторизованная диагностика сервисов без выполнения команд и доступа к окружению."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PORT = 9197  # Только внутренняя сеть Compose, наружу не публикуется.
LOG_FILES = {"app": "updates.log", "trader": "trader.log", "web": "web.log"}
STARTED = time.monotonic()


def redact(text: str) -> str:
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("SECRET", "PASSWORD", "PASSPHRASE", "TOKEN", "API_KEY")):
            text = text.replace(value, "[СКРЫТО]")
    return re.sub(r"(?i)((?:signature|api[_-]?key|secret|password|passphrase|token)\s*[=:]\s*)[^\s&,;]+", r"\1[СКРЫТО]", text)


def read_log(service: str, lines: int) -> dict:
    if service not in LOG_FILES or not 1 <= lines <= 2000:
        raise ValueError("Недопустимый сервис или число строк")
    path = Path(os.getenv("MAINTENANCE_LOG_DIR", "/app/log")) / LOG_FILES[service]
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - 262144)
        stream.seek(offset)
        data = stream.read(262144)
    if offset:
        data = data.partition(b"\n")[2]
    selected = data.decode("utf-8", errors="replace").splitlines()[-lines:]
    return {"service": service, "lines": [redact(line) for line in selected],
            "file_size_bytes": size, "bounded_tail": bool(offset)}


def process_snapshot(service: str) -> dict:
    processes = []
    root = Path("/proc")
    if root.is_dir():
        for directory in root.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                # Не читаем cmdline/environ: там могут быть ключи и пароли.
                fields = {}
                for line in (directory / "status").read_text().splitlines():
                    key, _, value = line.partition(":")
                    if key in {"Name", "State", "PPid", "VmRSS", "Threads"}:
                        fields[key] = value.strip()
                processes.append({"pid": int(directory.name), **fields})
            except (OSError, ValueError):
                continue
    return {"service": service, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.monotonic() - STARTED, 1),
            "processes": sorted(processes, key=lambda row: row["pid"]),
            "process_inventory_available": root.is_dir(), "diagnostic_pid": os.getpid()}


def _respond(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_request(handler: BaseHTTPRequestHandler, *, local_service: str = "web", internal: bool = False) -> bool:
    parsed = urlparse(handler.path)
    prefix = "/api/maintenance/"
    if not parsed.path.startswith(prefix):
        return False
    token = os.getenv("MAINTENANCE_API_TOKEN", "")
    authorization = handler.headers.get("Authorization", "")
    if not token:
        _respond(handler, 503, {"error": "Диагностика отключена: токен не задан"})
        return True
    if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
        _respond(handler, 401, {"error": "Требуется авторизация"})
        return True
    if handler.command != "GET":
        _respond(handler, 405, {"error": "Доступно только чтение"})
        return True
    action = parsed.path[len(prefix):]
    query = parse_qs(parsed.query)
    service = query.get("service", [local_service])[0]
    try:
        if action not in {"logs", "processes"} or service not in LOG_FILES or (internal and service != local_service):
            raise ValueError("Неизвестный ресурс")
        lines = int(query.get("lines", ["200"])[0])
        if not 1 <= lines <= 2000:
            raise ValueError("Число строк должно быть от 1 до 2000")
        if service == local_service:
            result = read_log(service, lines) if action == "logs" else process_snapshot(service)
        else:
            # Назначение жёстко ограничено сервисами Compose, пользовательский URL не принимается.
            request = Request(f"http://{service}:{PORT}{prefix}{action}?service={service}&lines={lines}",
                              headers={"Authorization": "Bearer " + token})
            with urlopen(request, timeout=5) as response:
                result = json.loads(response.read(2 * 1024 * 1024))
        _respond(handler, 200, result)
    except (ValueError, TypeError):
        _respond(handler, 400, {"error": "Недопустимые параметры"})
    except FileNotFoundError:
        _respond(handler, 404, {"error": "Файл лога ещё не создан"})
    except HTTPError as error:
        if error.code == 404:
            _respond(handler, 404, {"error": "Файл лога ещё не создан", "service": service})
        else:
            _respond(handler, 502, {"error": "Диагностика сервиса недоступна", "service": service})
    except Exception:
        _respond(handler, 502, {"error": "Диагностика сервиса недоступна", "service": service})
    return True


def start_agent(service: str) -> None:
    if not os.getenv("MAINTENANCE_API_TOKEN"):
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not handle_request(self, local_service=service, internal=True):
                self.send_error(404)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="maintenance-api").start()
    logging.getLogger(__name__).info("Диагностика сервиса %s готова", service)
