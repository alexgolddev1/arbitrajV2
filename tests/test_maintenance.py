"""Проверка авторизации и ограничений диагностического API."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from main.maintenance import handle_request, read_log, redact


class FakeHandler:
    def __init__(self, path, token="test-token", command="GET"):
        self.path = path
        self.headers = {"Authorization": "Bearer " + token}
        self.command = command
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


class MaintenanceTests(unittest.TestCase):
    @patch.dict(os.environ, {"MAINTENANCE_API_TOKEN": "test-token"})
    @patch("main.maintenance.urlopen")
    def test_authentication_and_invalid_targets_never_contact_services(self, request):
        for path, token, expected in [
            ("/api/maintenance/logs?service=app", "wrong", 401),
            ("/api/maintenance/logs?service=../../.env", "test-token", 400),
            ("/api/maintenance/logs?service=http://example.com", "test-token", 400),
            ("/api/maintenance/logs?lines=999999", "test-token", 400),
            ("/api/maintenance/exec", "test-token", 400),
        ]:
            handler = FakeHandler(path, token)
            self.assertTrue(handle_request(handler))
            self.assertEqual(handler.status, expected)
        request.assert_not_called()

    @patch.dict(os.environ, {"MAINTENANCE_API_TOKEN": ""})
    def test_disabled_without_token(self):
        handler = FakeHandler("/api/maintenance/processes")
        handle_request(handler)
        self.assertEqual(handler.status, 503)

    @patch.dict(os.environ, {"MAINTENANCE_API_TOKEN": "test-token"})
    def test_writes_rejected(self):
        handler = FakeHandler("/api/maintenance/processes", command="POST")
        handle_request(handler)
        self.assertEqual(handler.status, 405)

    def test_log_tail_is_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "trader.log"
            path.write_text("x" * 300000 + "\nfirst\nAPI_KEY=my-key signature=private\nlast\n", encoding="utf-8")
            with patch.dict(os.environ, {"MAINTENANCE_LOG_DIR": directory, "MEXC_API_KEY": "my-key"}):
                result = read_log("trader", 2)
            self.assertEqual(len(result["lines"]), 2)
            self.assertNotIn("my-key", repr(result))
            self.assertNotIn("private", repr(result))
            self.assertTrue(result["bounded_tail"])
            with self.assertRaises(ValueError):
                read_log("../.env", 2)

    @patch.dict(os.environ, {"MAINTENANCE_API_TOKEN": "test-token"})
    @patch("main.maintenance.urlopen")
    def test_remote_processes_uses_only_fixed_internal_target(self, request):
        request.return_value.__enter__.return_value.read.return_value = b'{"service":"trader","processes":[]}'
        handler = FakeHandler("/api/maintenance/processes?service=trader")
        handle_request(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(json.loads(handler.wfile.getvalue())["service"], "trader")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "http://trader:9197/api/maintenance/processes?service=trader&lines=200")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-token")

    @patch.dict(os.environ, {"MAINTENANCE_API_TOKEN": "test-token"})
    def test_internal_agent_cannot_proxy_other_container(self):
        handler = FakeHandler("/api/maintenance/processes?service=app")
        handle_request(handler, local_service="trader", internal=True)
        self.assertEqual(handler.status, 400)

    @patch.dict(os.environ, {"MAINTENANCE_API_TOKEN": "test-token"})
    @patch("main.maintenance.urlopen", side_effect=OSError("password=private"))
    def test_failed_service_reports_unavailable_without_exception_text(self, _request):
        handler = FakeHandler("/api/maintenance/processes?service=app")
        handle_request(handler)
        self.assertEqual(handler.status, 502)
        self.assertNotIn(b"private", handler.wfile.getvalue())
