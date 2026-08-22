import unittest
from datetime import datetime, timezone

from starling_rules_engine.reconcile import build_monthly_report, build_report, months_ago
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


class TestBuildReport(unittest.TestCase):
    def test_matches_and_totals(self):
        inbound = [make_inbound()]
        outbound = [make_outbound()]

        report = build_report(inbound, outbound, window_days=14, window_start="2026-08-01", window_end="2026-08-22")

        self.assertEqual(len(report.matched), 1)
        self.assertEqual(report.unmatched_inbound, [])
        self.assertEqual(report.unmatched_outbound, [])
        self.assertEqual(report.total_inbound_minor_units, 5000)
        self.assertEqual(report.total_outbound_minor_units, 5000)

    def test_flags_unmatched_on_both_sides(self):
        inbound = [make_inbound(feed_item_uid="in-1"), make_inbound(feed_item_uid="in-2", amount_minor_units=7000)]
        outbound = [make_outbound(feed_item_uid="out-1"), make_outbound(feed_item_uid="out-2", amount_minor_units=9000)]

        report = build_report(inbound, outbound, window_days=14, window_start="2026-08-01", window_end="2026-08-22")

        self.assertEqual(len(report.matched), 1)
        self.assertEqual([i.feed_item_uid for i in report.unmatched_inbound], ["in-2"])
        self.assertEqual([o.feed_item_uid for o in report.unmatched_outbound], ["out-2"])
        self.assertEqual(report.total_inbound_minor_units, 12000)
        self.assertEqual(report.total_outbound_minor_units, 14000)

    def test_includes_auto_paid_outbound_in_matching(self):
        # Unlike engine.py's live duplicate guard, the report matches against
        # ALL card payments, including ones this tool made itself.
        inbound = [make_inbound()]
        outbound = [make_outbound(reference="REIMB abcd1234")]

        report = build_report(inbound, outbound, window_days=14, window_start="2026-08-01", window_end="2026-08-22")

        self.assertEqual(len(report.matched), 1)


class TestMonthsAgo(unittest.TestCase):
    def test_shifts_back_whole_months(self):
        dt = datetime(2026, 8, 22, tzinfo=timezone.utc)
        self.assertEqual(months_ago(dt, 3), datetime(2026, 5, 22, tzinfo=timezone.utc))

    def test_crosses_year_boundary(self):
        dt = datetime(2026, 2, 15, tzinfo=timezone.utc)
        self.assertEqual(months_ago(dt, 3), datetime(2025, 11, 15, tzinfo=timezone.utc))

    def test_clamps_day_for_shorter_month(self):
        dt = datetime(2026, 3, 31, tzinfo=timezone.utc)
        self.assertEqual(months_ago(dt, 1), datetime(2026, 2, 28, tzinfo=timezone.utc))  # 2026 not a leap year


class TestBuildMonthlyReport(unittest.TestCase):
    def test_buckets_by_calendar_month_of_transaction_time(self):
        inbound = [
            make_inbound(feed_item_uid="in-jun", transaction_time="2026-06-05T09:00:00Z"),
            make_inbound(feed_item_uid="in-aug", transaction_time="2026-08-10T09:00:00Z"),
        ]
        outbound = [make_outbound(feed_item_uid="out-jun", transaction_time="2026-06-07T09:00:00Z")]

        buckets = build_monthly_report(inbound, outbound, window_days=14)

        self.assertEqual([b.month for b in buckets], ["2026-06", "2026-08"])
        june, august = buckets

        self.assertEqual(june.inbound_total_minor_units, 5000)
        self.assertEqual(june.outbound_total_minor_units, 5000)
        self.assertEqual(june.matched_total_minor_units, 5000)
        self.assertEqual(june.matched_count, 1)
        self.assertEqual(june.unmatched_inbound_count, 0)

        self.assertEqual(august.inbound_total_minor_units, 5000)
        self.assertEqual(august.outbound_total_minor_units, 0)
        self.assertEqual(august.matched_count, 0)
        self.assertEqual(august.unmatched_inbound_count, 1)

    def test_matched_pair_across_month_boundary_attributed_to_inbound_month(self):
        # Reimbursement lands 31 Jul, manual card payment 2 Aug - still
        # within the 14-day match window, but the two straddle a month
        # boundary. The matched amount should count against July (the
        # inbound item's month), not August.
        inbound = [make_inbound(transaction_time="2026-07-31T09:00:00Z")]
        outbound = [make_outbound(transaction_time="2026-08-02T09:00:00Z")]

        buckets = build_monthly_report(inbound, outbound, window_days=14)
        by_month = {b.month: b for b in buckets}

        self.assertEqual(by_month["2026-07"].matched_total_minor_units, 5000)
        self.assertEqual(by_month["2026-07"].matched_count, 1)
        self.assertEqual(by_month["2026-08"].matched_count, 0)
        self.assertEqual(by_month["2026-08"].outbound_total_minor_units, 5000)

    def test_empty_input_returns_no_buckets(self):
        self.assertEqual(build_monthly_report([], [], window_days=14), [])


if __name__ == "__main__":
    unittest.main()
