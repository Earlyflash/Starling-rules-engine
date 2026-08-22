import unittest

from starling_rules_engine.safety import SafetyLimits, SafetyRejection, check


class TestSafetyCheck(unittest.TestCase):
    def setUp(self):
        self.limits = SafetyLimits(max_transfer_minor_units=10000, max_daily_total_minor_units=20000)

    def test_allows_within_limits(self):
        check(self.limits, 5000, already_transferred_today_minor_units=0)  # must not raise

    def test_rejects_over_single_transfer_cap(self):
        with self.assertRaises(SafetyRejection):
            check(self.limits, 10001, already_transferred_today_minor_units=0)

    def test_allows_exactly_at_single_transfer_cap(self):
        check(self.limits, 10000, already_transferred_today_minor_units=0)  # must not raise

    def test_rejects_over_daily_cap(self):
        with self.assertRaises(SafetyRejection):
            check(self.limits, 5000, already_transferred_today_minor_units=16000)

    def test_allows_exactly_at_daily_cap(self):
        check(self.limits, 5000, already_transferred_today_minor_units=15000)  # must not raise

    def test_rejects_non_positive_amount(self):
        with self.assertRaises(SafetyRejection):
            check(self.limits, 0, already_transferred_today_minor_units=0)
        with self.assertRaises(SafetyRejection):
            check(self.limits, -100, already_transferred_today_minor_units=0)


if __name__ == "__main__":
    unittest.main()
