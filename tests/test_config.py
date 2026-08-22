import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starling_rules_engine.config import ConfigError, load_config

VALID_YAML = """
employer_names:
  - "ACME CORP LTD"
credit_card_payee_name: "My Credit Card"
safety:
  max_transfer_minor_units: 50000
  max_daily_total_minor_units: 100000
"""


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmpdir.name) / "config.yaml"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write(self, text: str) -> None:
        self.config_path.write_text(text, encoding="utf-8")

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_token_env_var_raises(self):
        self._write(VALID_YAML)
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_valid_config_loads_with_defaults(self):
        self._write(VALID_YAML)
        config = load_config(self.config_path)
        self.assertEqual(config.employer_names, ["ACME CORP LTD"])
        self.assertEqual(config.credit_card_payee_name, "My Credit Card")
        self.assertTrue(config.dry_run)  # defaults to safe
        self.assertFalse(config.sandbox)
        self.assertIsNone(config.account_uid)
        self.assertIsNone(config.alert_webhook_url)

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_missing_employer_names_raises(self):
        self._write(
            """
credit_card_payee_name: "My Credit Card"
safety:
  max_transfer_minor_units: 50000
  max_daily_total_minor_units: 100000
"""
        )
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_empty_employer_names_raises(self):
        self._write(
            """
employer_names: []
credit_card_payee_name: "My Credit Card"
safety:
  max_transfer_minor_units: 50000
  max_daily_total_minor_units: 100000
"""
        )
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    @patch.dict(
        os.environ,
        {"STARLING_PERSONAL_ACCESS_TOKEN": "tok", "STARLING_RULES_ALERT_WEBHOOK_URL": "https://example.invalid/hook"},
        clear=True,
    )
    def test_reads_optional_webhook_from_env(self):
        self._write(VALID_YAML)
        config = load_config(self.config_path)
        self.assertEqual(config.alert_webhook_url, "https://example.invalid/hook")


if __name__ == "__main__":
    unittest.main()
