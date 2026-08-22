import unittest

from starling_rules_engine.matcher import is_employer_payment
from starling_rules_engine.starling_client import FeedItem


def make_item(**overrides):
    defaults = dict(
        feed_item_uid="abc123",
        amount_minor_units=5000,
        currency="GBP",
        direction="IN",
        counter_party_name="ACME CORP LTD",
        reference="EXPENSES AUG",
        status="SETTLED",
        transaction_time="2026-08-20T10:00:00Z",
    )
    defaults.update(overrides)
    return FeedItem(**defaults)


class TestIsEmployerPayment(unittest.TestCase):
    def test_matches_exact_name_case_insensitive(self):
        item = make_item(counter_party_name="acme corp ltd")
        self.assertTrue(is_employer_payment(item, ["ACME CORP LTD"]))

    def test_matches_substring(self):
        item = make_item(counter_party_name="ACME CORP LTD PAYROLL")
        self.assertTrue(is_employer_payment(item, ["ACME CORP LTD"]))

    def test_rejects_outbound(self):
        item = make_item(direction="OUT")
        self.assertFalse(is_employer_payment(item, ["ACME CORP LTD"]))

    def test_rejects_unsettled(self):
        item = make_item(status="UPCOMING")
        self.assertFalse(is_employer_payment(item, ["ACME CORP LTD"]))

    def test_rejects_non_matching_name(self):
        item = make_item(counter_party_name="SOME OTHER COMPANY")
        self.assertFalse(is_employer_payment(item, ["ACME CORP LTD"]))

    def test_rejects_blank_counterparty(self):
        item = make_item(counter_party_name="")
        self.assertFalse(is_employer_payment(item, ["ACME CORP LTD"]))

    def test_ignores_blank_configured_names(self):
        item = make_item(counter_party_name="ACME CORP LTD")
        self.assertTrue(is_employer_payment(item, ["", "ACME CORP LTD"]))


if __name__ == "__main__":
    unittest.main()
