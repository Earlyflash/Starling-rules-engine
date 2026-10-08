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
        self.assertIsNone(config.signing_key_uid)
        self.assertIsNone(config.signing_private_key_path)
        self.assertIsNone(config.credit_card_payment_reference)
        self.assertEqual(config.reconciliation_match_window_days, 14)  # defaults to safe/sane
        self.assertEqual(config.poll_overlap_minutes, 4320)

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_reads_credit_card_payment_reference(self):
        self._write(VALID_YAML + '\ncredit_card_payment_reference: "1234567890123456"\n')
        config = load_config(self.config_path)
        self.assertEqual(config.credit_card_payment_reference, "1234567890123456")

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

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_reads_optional_signing_config(self):
        self._write(
            VALID_YAML
            + """
signing_key_uid: "11111111-1111-1111-1111-111111111111"
signing_private_key_path: "/path/to/key.pem"
"""
        )
        config = load_config(self.config_path)
        self.assertEqual(config.signing_key_uid, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(config.signing_private_key_path, Path("/path/to/key.pem"))

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok", "HOME": "/home/testuser"}, clear=True)
    def test_expands_tilde_in_signing_private_key_path(self):
        self._write(VALID_YAML + '\nsigning_private_key_path: "~/keys/starling-signing-private.pem"\n')
        config = load_config(self.config_path)
        self.assertEqual(config.signing_private_key_path, Path("/home/testuser/keys/starling-signing-private.pem"))

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_reads_custom_reconciliation_window(self):
        self._write(VALID_YAML + "\nreconciliation:\n  match_window_days: 7\n")
        config = load_config(self.config_path)
        self.assertEqual(config.reconciliation_match_window_days, 7)

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_negative_reconciliation_window_raises(self):
        self._write(VALID_YAML + "\nreconciliation:\n  match_window_days: -1\n")
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    @patch.dict(os.environ, {"STARLING_PERSONAL_ACCESS_TOKEN": "tok"}, clear=True)
    def test_negative_poll_overlap_raises(self):
        self._write(VALID_YAML + "\npoll_overlap_minutes: -1\n")
        with self.assertRaises(ConfigError):
            load_config(self.config_path)


if __name__ == "__main__":
    unittest.main()
