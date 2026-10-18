import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import market


def series(values, start=1_700_000_000):
    return [{"time": start + index * 86400, "open": v, "high": v * 1.001,
             "low": v * 0.999, "close": v, "tick_volume": 100 + index,
             "spread": 1, "real_volume": 0}
            for index, v in enumerate(values)]


class Indicators(unittest.TestCase):
    def test_ema_needs_a_full_period(self):
        self.assertIsNone(market.ema([1, 2, 3], 5))
        self.assertAlmostEqual(market.ema([1, 2, 3, 4, 5], 3), 4.0, places=6)

    def test_sma_is_the_tail(self):
        self.assertAlmostEqual(market.sma([1, 2, 3, 4], 2), 3.5, places=6)

    def test_true_range_includes_gaps(self):
        bars = series([100.0, 100.0])
        bars[1]["high"] = 130.0
        bars[1]["low"] = 129.0
        bars[1]["close"] = 129.5
        spans = market.true_ranges(bars)
        # the gap up from 100 to 129 counts, not just the 1.0 range of the bar
        self.assertAlmostEqual(spans[-1], 30.0, places=6)

    def test_atr_is_the_mean_of_the_window(self):
        bars = series([100.0, 101.0, 102.0, 103.0, 104.0])
        value = market.atr(bars, 4)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_atr_is_none_with_too_little_history(self):
        self.assertIsNone(market.atr(series([1.0, 2.0]), 14))

    def test_returns_skip_a_zero_denominator(self):
        self.assertEqual(market.returns([0, 5, 10]), [1.0])

    def test_stdev_of_a_flat_series_is_zero(self):
        self.assertAlmostEqual(market.stdev([2, 2, 2]), 0.0, places=9)


class Correlation(unittest.TestCase):
    def test_a_series_with_itself_is_one(self):
        self.assertAlmostEqual(market.correlation([1, 2, 3, 5, 8, 13], [1, 2, 3, 5, 8, 13]),
                               1.0, places=9)

    def test_a_series_with_its_negative_is_minus_one(self):
        self.assertAlmostEqual(market.correlation([1, 2, 3, 5, 8], [-1, -2, -3, -5, -8]),
                               -1.0, places=9)

    def test_too_few_points_is_none_not_zero(self):
        """A coefficient from two observations is not a small number, it is no number."""
        self.assertIsNone(market.correlation([1, 2], [2, 3]))

    def test_a_flat_series_has_no_correlation(self):
        self.assertIsNone(market.correlation([1, 1, 1, 1], [1, 2, 3, 4]))


class Alignment(unittest.TestCase):
    def test_returns_are_aligned_on_shared_timestamps(self):
        left = series([100.0, 101.0, 102.0, 103.0, 104.0])
        right = series([50.0, 51.0, 52.0, 53.0, 54.0])
        # drop one bar from the right, as a symbol that closed for a day would
        del right[1]
        aligned, stamps = market.align_returns({"LEFT": left, "RIGHT": right})
        self.assertEqual(len(stamps), 4)
        self.assertEqual(len(aligned["LEFT"]), 3)
        self.assertEqual(len(aligned["RIGHT"]), 3)

    def test_fewer_than_four_shared_stamps_is_refused_not_guessed_at(self):
        """Three shared bars give two returns, and a correlation from two points is
        not a small number, it is no number."""
        left = series([100.0, 101.0, 102.0])
        right = series([50.0, 51.0, 52.0])
        aligned, stamps = market.align_returns({"LEFT": left, "RIGHT": right})
        self.assertEqual(stamps, [])
        self.assertEqual(aligned["LEFT"], [])

    def test_no_overlap_returns_empty_series(self):
        left = series([100.0, 101.0, 102.0, 103.0])
        right = series([50.0, 51.0, 52.0, 53.0], start=1_600_000_000)
        aligned, stamps = market.align_returns({"LEFT": left, "RIGHT": right})
        self.assertEqual(stamps, [])
        self.assertEqual(aligned["LEFT"], [])


class Timeframes(unittest.TestCase):
    def test_names_map_to_the_mt5_codes(self):
        self.assertEqual(market.timeframe_code("M1"), 1)
        self.assertEqual(market.timeframe_code("h4"), 4 | 0x4000)
        self.assertEqual(market.timeframe_code("D1"), 24 | 0x4000)
        self.assertEqual(market.timeframe_code("MN1"), 1 | 0xC000)

    def test_an_unknown_name_lists_the_good_ones(self):
        with self.assertRaises(ValueError) as caught:
            market.timeframe_code("M7")
        self.assertIn("H1", str(caught.exception))

    def test_every_code_is_also_a_seconds_entry(self):
        self.assertEqual(sorted(market.TIMEFRAMES), sorted(market.TIMEFRAME_SECONDS))


if __name__ == "__main__":
    unittest.main()
