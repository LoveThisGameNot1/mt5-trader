import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import history, instruments, sessions
from tests import harness


class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")

    def compute(self, offset_hours=3, days=20):
        clock = history.Clock(offset_seconds=int(offset_hours * 3600), source="test")
        return sessions.compute(self.h.bridge, "EURUSD", instrument=self.instrument,
                                clock=clock, days=days)

    def test_every_server_hour_gets_a_row(self):
        out = self.compute()
        self.assertEqual(len(out["by_server_hour"]), 24)
        self.assertEqual([row["server_hour"] for row in out["by_server_hour"]],
                         list(range(24)))

    def test_a_broker_at_utc_three_shows_london_at_ten_server(self):
        """The whole point of measuring the offset: a UTC window placed on the
        broker's clock. Getting this wrong shifts every session by three hours."""
        out = self.compute(offset_hours=3)
        london = next(s for s in out["sessions"] if s["session"] == "London")
        self.assertEqual(london["server_hours"], "10:00-19:00")
        self.assertEqual(london["hours"][0], 10)

    def test_a_broker_at_utc_zero_shows_london_at_seven(self):
        out = self.compute(offset_hours=0)
        london = next(s for s in out["sessions"] if s["session"] == "London")
        self.assertEqual(london["server_hours"], "07:00-16:00")

    def test_the_wrapping_session_is_labelled_across_midnight(self):
        out = self.compute(offset_hours=0)
        sydney = next(s for s in out["sessions"] if s["session"] == "Sydney")
        self.assertEqual(sydney["server_hours"], "21:00-06:00")
        self.assertEqual(sorted(sydney["hours"]), [0, 1, 2, 3, 4, 5, 21, 22, 23])

    def test_the_overlap_is_converted_too(self):
        out = self.compute(offset_hours=3)
        self.assertEqual(out["overlap"]["server_hours"], [15, 16, 17, 18])

    def test_the_current_hour_is_reported_with_its_share(self):
        out = self.compute()
        self.assertIsNotNone(out["current"])
        self.assertIn(out["current"]["server_hour"], range(24))
        self.assertIsInstance(out["current"]["sessions_open"], list)

    def test_busiest_and_quietest_are_disjoint_when_there_is_enough_data(self):
        out = self.compute()
        self.assertEqual(len(set(out["busiest_server_hours"]) &
                             set(out["quietest_server_hours"])), 0)

    def test_share_of_a_typical_hour_is_the_uniform_share_as_one(self):
        """A fair share of a day is 1/24, so a ratio of 1.0 means an average hour."""
        out = self.compute()
        self.assertAlmostEqual(100.0 / 24, 4.1666, places=3)
        self.assertIsInstance(out["current"]["vs_fair_share"], float)

    def test_sessions_do_not_claim_to_add_up_to_a_day(self):
        """The windows overlap, so a 'share of the day' figure would double count."""
        out = self.compute()
        for entry in out["sessions"]:
            self.assertNotIn("share_of_daily_range_pct", entry)
            self.assertIn("avg_range_per_hour", entry)

    def test_no_bars_is_reported_honestly(self):
        self.h.fake.rates["EURUSD"] = []
        out = sessions.compute(self.h.bridge, "EURUSD", instrument=self.instrument,
                               clock=self.h.kernel.clock())
        self.assertEqual(out["bars"], 0)
        self.assertIn("no bars", out["note"])

    def test_the_caveat_says_the_hours_are_the_server_clock(self):
        out = self.compute()
        self.assertIn("server-hour", out["caveat"])
        self.assertIn("UTC+3", out["caveat"])


if __name__ == "__main__":
    unittest.main()
