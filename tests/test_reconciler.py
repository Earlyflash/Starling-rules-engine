import unittest

from starling_rules_engine.reconciler import (
    is_card_payment,
    is_manual_card_payment,
    match_payments,
)
from starling_rules_engine.starling_client import FeedItem


def make_inbound(**overrides):
    defaults = dict(
        feed_item_uid="in-1",
        amount_minor_units=5000,
        currency="GBP",
        direction="IN",
        counter_party_name="ACME CORP LTD",
        reference="EXPENSES",
        status="SETTLED",
        transaction_time="2026-08-10T09:00:00Z",
    )
    defaults.update(overrides)
    return FeedItem(**defaults)


def make_outbound(**overrides):
    defaults = dict(
        feed_item_uid="out-1",
        amount_minor_units=5000,
        currency="GBP",
        direction="OUT",
        counter_party_name="My Credit Card",
        reference="paid by hand",
        status="SETTLED",
        transaction_time="2026-08-12T09:00:00Z",
    )
    defaults.update(overrides)
    return FeedItem(**defaults)


class TestIsCardPayment(unittest.TestCase):
    def test_matches_settled_outbound_to_payee(self):
        self.assertTrue(is_card_payment(make_outbound(), "My Credit Card"))

    def test_case_insensitive(self):
        self.assertTrue(is_card_payment(make_outbound(counter_party_name="my credit card"), "MY CREDIT CARD"))

    def test_rejects_inbound(self):
        self.assertFalse(is_card_payment(make_outbound(direction="IN"), "My Credit Card"))

    def test_rejects_unsettled(self):
        self.assertFalse(is_card_payment(make_outbound(status="PENDING"), "My Credit Card"))

    def test_rejects_different_counterparty(self):
        self.assertFalse(is_card_payment(make_outbound(counter_party_name="Someone Else"), "My Credit Card"))

    def test_does_not_substring_match(self):
        # Unlike matcher.is_employer_payment, this must be an exact match -
        # it's comparing against one specific configured payee, not a list
        # of heuristic name fragments.
        self.assertFalse(is_card_payment(make_outbound(counter_party_name="My Credit Card Ltd"), "My Credit Card"))


class TestIsManualCardPayment(unittest.TestCase):
    def test_true_for_manual_payment(self):
        self.assertTrue(is_manual_card_payment(make_outbound(), "My Credit Card"))

    def test_false_for_this_tools_own_auto_payment(self):
        self.assertFalse(is_manual_card_payment(make_outbound(reference="REIMB abcd1234"), "My Credit Card"))


class TestMatchPayments(unittest.TestCase):
    def test_matches_same_amount_within_window(self):
        inbound = make_inbound()
        outbound = make_outbound()  # 2 days later
        result = match_payments([inbound], [outbound], window_days=14)
        self.assertEqual(len(result.matched), 1)
        self.assertAlmostEqual(result.matched[0].day_gap, 2.0, places=3)
        self.assertEqual(result.unmatched_inbound, [])
        self.assertEqual(result.unmatched_outbound, [])

    def test_no_match_outside_window(self):
        inbound = make_inbound()
        outbound = make_outbound(transaction_time="2026-09-15T09:00:00Z")  # > 14 days later
        result = match_payments([inbound], [outbound], window_days=14)
        self.assertEqual(result.matched, [])
        self.assertEqual(result.unmatched_inbound, [inbound])
        self.assertEqual(result.unmatched_outbound, [outbound])

    def test_no_match_different_amount(self):
        inbound = make_inbound(amount_minor_units=5000)
        outbound = make_outbound(amount_minor_units=6000)
        result = match_payments([inbound], [outbound], window_days=14)
        self.assertEqual(result.matched, [])

    def test_each_outbound_item_used_at_most_once(self):
        inbound_a = make_inbound(feed_item_uid="in-a", transaction_time="2026-08-10T09:00:00Z")
        inbound_b = make_inbound(feed_item_uid="in-b", transaction_time="2026-08-11T09:00:00Z")
        outbound = make_outbound()  # only one, same amount as both

        result = match_payments([inbound_a, inbound_b], [outbound], window_days=14)

        self.assertEqual(len(result.matched), 1)
        # in-a is chronologically first and closer to the outbound date, so
        # it should be the one that claims the single outbound item.
        self.assertEqual(result.matched[0].inbound.feed_item_uid, "in-a")
        self.assertEqual([i.feed_item_uid for i in result.unmatched_inbound], ["in-b"])

    def test_picks_closest_date_when_multiple_candidates(self):
        inbound = make_inbound(transaction_time="2026-08-10T09:00:00Z")
        far = make_outbound(feed_item_uid="out-far", transaction_time="2026-08-20T09:00:00Z")
        close = make_outbound(feed_item_uid="out-close", transaction_time="2026-08-11T09:00:00Z")

        result = match_payments([inbound], [far, close], window_days=14)

        self.assertEqual(len(result.matched), 1)
        self.assertEqual(result.matched[0].outbound.feed_item_uid, "out-close")
        self.assertEqual([o.feed_item_uid for o in result.unmatched_outbound], ["out-far"])

    def test_excluded_outbound_uids_are_never_matched(self):
        inbound = make_inbound()
        outbound = make_outbound()

        result = match_payments(
            [inbound], [outbound], window_days=14, excluded_outbound_uids=frozenset({"out-1"})
        )

        self.assertEqual(result.matched, [])
        self.assertEqual(result.unmatched_inbound, [inbound])
        self.assertEqual(result.unmatched_outbound, [])  # excluded, not just unmatched


if __name__ == "__main__":
    unittest.main()
