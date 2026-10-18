import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import instruments, levels, market
from tests import harness


class Levels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")

    def compute(self, **kwargs):
        return levels.compute(self.h.bridge, "EURUSD", instrument=self.instrument,
                              clock=self.h.kernel.clock(), **kwargs)

    def labels(self, out):
        return {entry["level"] for entry in out["levels"]}

    def test_the_period_extremes_are_present(self):
        out = self.compute()
        for label in ("last completed daily high", "last completed daily low",
                      "previous daily high", "last completed weekly high",
                      "last completed weekly low", "last completed monthly high"):
            self.assertIn(label, self.labels(out), label)

    def test_levels_use_only_closed_bars(self):
        """The last bar MT5 returns is the one still forming, and a level drawn at a
        price that is still moving is not a level."""
        with_forming = market.bars(self.h.bridge, "EURUSD", "D1", 90, include_forming=True)
        closed = market.bars(self.h.bridge, "EURUSD", "D1", 90)
        out = self.compute()
        entry = next(e for e in out["levels"] if e["level"] == "last completed daily high")
        self.assertEqual(entry["date"], market_records_iso(closed[-1]["time"]))
        self.assertNotEqual(entry["date"], market_records_iso(with_forming[-1]["time"]))

    def test_each_level_carries_its_distance_in_three_units(self):
        out = self.compute()
        entry = next(e for e in out["levels"] if e["level"] == "last completed daily high")
        self.assertIn("distance", entry)
        self.assertIn("distance_points", entry)
        self.assertIn("distance_atr", entry)

    def test_the_label_is_a_date_not_a_word(self):
        """Bar-derived levels carry the date of the bar. ATR bands and round numbers
        are arithmetic, so they carry no date at all rather than a wrong one."""
        out = self.compute()
        dated = [e for e in out["levels"] if e.get("date")]
        self.assertTrue(dated)
        for entry in dated:
            self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}")
        for entry in out["levels"]:
            if entry["source"] in ("ATR band",):
                self.assertIsNone(entry["date"])

    def test_atr_bands_are_one_atr_either_side(self):
        out = self.compute()
        atr = out["atr"]["D1"]
        upper = next(e for e in out["levels"] if e["level"] == "price + 1 ATR (D1)")
        self.assertAlmostEqual(upper["price"], out["price"] + atr, places=6)
        self.assertAlmostEqual(upper["distance_atr"], -1.0, places=3)

    def test_round_numbers_come_from_the_price_magnitude(self):
        out = self.compute()
        self.assertTrue(out["round_numbers"])
        self.assertIn("orders of magnitude", out["round_number_rule"])

    def test_round_numbers_for_four_digit_and_five_digit_prices(self):
        eurusd_steps = levels._round_number_steps(1.1234)
        gold_steps = levels._round_number_steps(4164.0)
        index_steps = levels._round_number_steps(39450.0)
        self.assertAlmostEqual(eurusd_steps[0], 0.01, places=9)
        self.assertAlmostEqual(gold_steps[0], 10.0, places=9)
        self.assertAlmostEqual(index_steps[0], 100.0, places=9)

    def test_the_reach_filter_keeps_the_near_ones(self):
        wide = self.compute(reach_atr=10.0)
        narrow = self.compute(reach_atr=0.01)
        self.assertGreaterEqual(len(wide["nearest"]), len(narrow["nearest"]))

    def test_nearest_is_sorted_by_distance(self):
        out = self.compute()
        distances = [abs(e["distance"]) for e in out["nearest"] if e.get("distance") is not None]
        self.assertEqual(distances, sorted(distances))

    def test_the_caveat_names_the_label_trap(self):
        out = self.compute()
        self.assertIn("Friday", out["caveat"])

    def test_a_symbol_with_no_history_is_reported_not_guessed(self):
        self.h.fake.rates["EMPTY"] = []
        out = levels.compute(self.h.bridge, "EURUSD", instrument=self.instrument,
                             clock=self.h.kernel.clock())
        self.assertTrue(out["levels"])


def market_records_iso(epoch):
    from mt5trader import records
    return records.iso_time(epoch)


if __name__ == "__main__":
    unittest.main()
