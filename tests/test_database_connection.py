from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from database import connect_db


class DatabaseConnectionTests(unittest.TestCase):
    @patch("database.mysql.connector.connect")
    def test_sessions_use_utc_for_timestamp_defaults(self, connect: object) -> None:
        connect_db()

        self.assertEqual(connect.call_args.kwargs["time_zone"], "+00:00")

    def test_schema_uses_mysql_compatible_utc_timestamp_defaults(self) -> None:
        schema = Path(__file__).resolve().parents[1].joinpath("schema.sql").read_text(encoding="utf-8")

        self.assertNotIn("DEFAULT UTC_TIMESTAMP(6)", schema)
        self.assertNotIn("ON UPDATE UTC_TIMESTAMP(6)", schema)
        self.assertIn("DEFAULT CURRENT_TIMESTAMP(6)", schema)


if __name__ == "__main__":
    unittest.main()
