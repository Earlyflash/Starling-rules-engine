import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from starling_rules_engine.config import Config
from starling_rules_engine.engine import run_once
from starling_rules_engine.notifier import Notifier
from starling_rules_engine.safety import SafetyLimits
from starling_rules_engine.starling_client import Account, FeedItem, Payee
from starling_rules_engine.state import ProcessedRecord, State


def make_config(tmpdir, **overrides):
    defaults = dict(
        employer_names=["ACME CORP LTD"],
        credit_card_payee_name="My Credit Card",
        account_uid=None,
        currency="GBP",
        safety=SafetyLimits(max_transfer_minor_units=100000, max_daily_total_minor_units=200000),
        dry_run=False,
        sandbox=True,
        poll_lookback_minutes=1440,
        state_path=Path(tmpdir) / "state.json",
        starling_token="test-token",
        alert_webhook_url=None,
        signing_key_uid=None,
        signing_private_key_path=None,
        reconciliation_match_window_days=14,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_item(**overrides):
    defaults = dict(
        feed_item_uid="feed-1",
        amount_minor_units=5000,
        currency="GBP",
        direction="IN",
        counter_party_name="ACME CORP LTD",
        reference="EXPENSES",
        status="SETTLED",
        transaction_time="2026-08-22T09:00:00Z",
    )
    defaults.update(overrides)
    return FeedItem(**defaults)


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmpdir.name)
        self.client = MagicMock()
        self.client.list_accounts.return_value = [
            Account(account_uid="acc-1", default_category="cat-1", currency="GBP", name="Personal")
        ]
        self.client.list_payees.return_value = [
            Payee(payee_uid="payee-1", name="My Credit Card", payee_account_uid="payee-acc-1")
        ]
        self.state = State(self.config.state_path)
        self.notifier = Notifier()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_transfers_matching_employer_payment(self):
        self.client.list_feed_items_between.return_value = [make_item()]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_called_once()
        kwargs = self.client.make_local_payment.call_args.kwargs
        self.assertEqual(kwargs["amount_minor_units"], 5000)
        self.assertEqual(kwargs["payee_account_uid"], "payee-acc-1")
        self.assertEqual(kwargs["external_identifier"], "feed-1")
        self.assertTrue(self.state.is_processed("feed-1"))

    def test_ignores_non_matching_counterparty(self):
        self.client.list_feed_items_between.return_value = [
            make_item(feed_item_uid="feed-2", counter_party_name="RANDOM SENDER")
        ]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_not_called()
        self.assertFalse(self.state.is_processed("feed-2"))

    def test_skips_over_cap_amount_without_transferring(self):
        self.config.safety.max_transfer_minor_units = 1000
        self.client.list_feed_items_between.return_value = [make_item(feed_item_uid="feed-3")]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_not_called()
        self.assertTrue(self.state.is_processed("feed-3"))

    def test_does_not_reprocess_already_processed_feed_item(self):
        self.state.record(ProcessedRecord("feed-4", "2026-08-22T08:00:00+00:00", "transferred", 5000))
        self.client.list_feed_items_between.return_value = [make_item(feed_item_uid="feed-4")]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_not_called()

    def test_dry_run_records_but_does_not_transfer(self):
        self.config.dry_run = True
        self.client.list_feed_items_between.return_value = [make_item(feed_item_uid="feed-5")]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_not_called()
        self.assertTrue(self.state.is_processed("feed-5"))

    def test_raises_when_configured_payee_not_found(self):
        self.config.credit_card_payee_name = "Nonexistent Payee"
        self.client.list_feed_items_between.return_value = [make_item()]

        with self.assertRaises(RuntimeError):
            run_once(self.config, self.client, self.state, self.notifier)


def make_outbound(**overrides):
    defaults = dict(
        feed_item_uid="outbound-1",
        amount_minor_units=5000,
        currency="GBP",
        direction="OUT",
        counter_party_name="My Credit Card",
        reference="paid by hand",
        status="SETTLED",
        transaction_time="2026-08-20T09:00:00Z",
    )
    defaults.update(overrides)
    return FeedItem(**defaults)


class TestAlreadyPaidGuard(unittest.TestCase):
    """Covers engine._find_already_paid / _handle_already_paid - the
    check that skips auto-paying a reimbursement someone already settled
    by hand (see reconciler.py)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmpdir.name)
        self.client = MagicMock()
        self.client.list_accounts.return_value = [
            Account(account_uid="acc-1", default_category="cat-1", currency="GBP", name="Personal")
        ]
        self.client.list_payees.return_value = [
            Payee(payee_uid="payee-1", name="My Credit Card", payee_account_uid="payee-acc-1")
        ]
        self.state = State(self.config.state_path)
        self.notifier = Notifier()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_skips_transfer_when_already_paid_manually(self):
        self.client.list_feed_items_between.side_effect = [[make_item()], [make_outbound()]]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_not_called()
        record = self.state._data["processed"]["feed-1"]
        self.assertEqual(record["outcome"], "skipped_already_paid")
        self.assertIn("outbound-1", self.state.claimed_outbound_uids())

    def test_does_not_match_outside_window(self):
        outbound = make_outbound(transaction_time="2026-01-01T09:00:00Z")  # >> 14 days from the inbound item
        self.client.list_feed_items_between.side_effect = [[make_item()], [outbound]]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_called_once()

    def test_ignores_its_own_auto_payment_reference(self):
        # An outbound item carrying the engine's own reference prefix must
        # never be treated as a manual payment - otherwise it would wrongly
        # consume the "claim" that a genuinely separate, same-amount
        # reimbursement needs.
        outbound = make_outbound(reference="REIMB abcd1234")
        self.client.list_feed_items_between.side_effect = [[make_item()], [outbound]]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_called_once()

    def test_second_same_amount_match_is_not_reused(self):
        inbound_a = make_item(feed_item_uid="feed-a")
        inbound_b = make_item(feed_item_uid="feed-b")
        self.client.list_feed_items_between.side_effect = [[inbound_a, inbound_b], [make_outbound()]]

        run_once(self.config, self.client, self.state, self.notifier)

        self.assertEqual(self.client.make_local_payment.call_count, 1)
        outcomes = sorted(rec["outcome"] for rec in self.state._data["processed"].values())
        self.assertEqual(outcomes, ["skipped_already_paid", "transferred"])

    def test_disabled_when_match_window_days_is_zero(self):
        self.config.reconciliation_match_window_days = 0
        self.client.list_feed_items_between.return_value = [make_item()]

        run_once(self.config, self.client, self.state, self.notifier)

        self.client.make_local_payment.assert_called_once()
        self.client.list_feed_items_between.assert_called_once()  # no second (outbound) fetch


if __name__ == "__main__":
    unittest.main()
