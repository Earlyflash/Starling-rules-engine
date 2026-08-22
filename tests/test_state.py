import tempfile
import unittest
from pathlib import Path

from starling_rules_engine.state import ProcessedRecord, State


class TestState(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "state.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_records_and_detects_processed(self):
        state = State(self.path)
        self.assertFalse(state.is_processed("abc"))
        state.record(ProcessedRecord("abc", "2026-08-22T10:00:00+00:00", "transferred", 5000))
        self.assertTrue(state.is_processed("abc"))

    def test_persists_across_instances(self):
        state = State(self.path)
        state.record(ProcessedRecord("abc", "2026-08-22T10:00:00+00:00", "transferred", 5000))
        state.set_last_poll_at("2026-08-22T10:00:00+00:00")
        state.save()

        reloaded = State(self.path)
        self.assertTrue(reloaded.is_processed("abc"))
        self.assertEqual(reloaded.last_poll_at, "2026-08-22T10:00:00+00:00")

    def test_daily_total_sums_only_matching_day_and_outcome(self):
        state = State(self.path)
        state.record(ProcessedRecord("a", "2026-08-22T09:00:00+00:00", "transferred", 3000))
        state.record(ProcessedRecord("b", "2026-08-22T10:00:00+00:00", "transferred", 2000))
        state.record(ProcessedRecord("c", "2026-08-21T10:00:00+00:00", "transferred", 9000))
        state.record(ProcessedRecord("d", "2026-08-22T11:00:00+00:00", "skipped_cap", 4000))
        self.assertEqual(state.transferred_total_today_minor_units("2026-08-22"), 5000)


if __name__ == "__main__":
    unittest.main()
