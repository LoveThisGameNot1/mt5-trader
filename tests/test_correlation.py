import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import correlation, instruments
from mt5trader.fixtures import make_position
from tests import harness

MAGIC = 770001


def walk(seed, count=140, base=1.0):
    """A deterministic pseudo-random walk, so two walks are uncorrelated."""
    state = seed
    price, out = base, []
    for _ in range(count):
        state = (1103515245 * state + 12345) % (2 ** 31)
        price *= 1 + ((state / float(2 ** 31)) - 0.5) * 0.01
        out.append(round(price, 6))
    return out


class Matrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, **kwargs):
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS, **kwargs)
        return self.h

    def test_independent_series_are_not_correlated(self):
        self.make()
        out = correlation.matrix(self.h.bridge, ["EURUSD", "XAUUSD", "US30"],
                                 count=140, threshold=0.8)
        self.assertEqual(out["missing"], [])
        for pair in out["pairs"]:
            self.assertLess(abs(pair["r"]), 0.8, pair)
        self.assertEqual(out["clusters"], [])

    def test_identical_series_cluster(self):
        self.make()
        same = walk(11)
        for symbol in ("EURUSD", "XAUUSD"):
            self.h.fake.set_rates(symbol, same)
        out = correlation.matrix(self.h.bridge, ["EURUSD", "XAUUSD"], count=140,
                                 threshold=0.8)
        self.assertAlmostEqual(out["correlations"]["EURUSD"]["XAUUSD"], 1.0, places=6)
        self.assertEqual(len(out["clusters"]), 1)
        self.assertEqual(out["clusters"][0]["symbols"], ["EURUSD", "XAUUSD"])
        self.assertIn("one position", out["clusters"][0]["note"])

    def test_an_inverse_series_is_just_as_correlated(self):
        self.make()
        base = walk(23)
        self.h.fake.set_rates("EURUSD", base)
        self.h.fake.set_rates("XAUUSD", [2 - v for v in base])
        out = correlation.matrix(self.h.bridge, ["EURUSD", "XAUUSD"], count=140)
        self.assertLess(out["correlations"]["EURUSD"]["XAUUSD"], -0.9)
        self.assertEqual(len(out["clusters"]), 1)

    def test_too_little_history_is_reported_not_silently_dropped(self):
        self.make()
        self.h.fake.set_rates("US30", walk(5, count=8))
        out = correlation.matrix(self.h.bridge, ["EURUSD", "US30"], count=140)
        self.assertEqual(out["missing"][0]["symbol"], "US30")
        self.assertEqual(out["symbols"], ["EURUSD"])

    def test_price_levels_never_enter_the_measure(self):
        """Two rising series that have nothing to do with each other. On levels the
        correlation would be near 1; on returns it is near zero."""
        self.make()
        self.h.fake.set_rates("EURUSD", [1.0 + i * 0.01 for i in range(60)])
        self.h.fake.set_rates("XAUUSD", [100.0 + i * (1 + i * 0.01) for i in range(60)])
        out = correlation.matrix(self.h.bridge, ["EURUSD", "XAUUSD"], count=60)
        self.assertLess(abs(out["correlations"]["EURUSD"]["XAUUSD"]), 0.9)


class Exposure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_it_reports_the_coefficient_per_open_symbol(self):
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        same = walk(31)
        self.h.fake.set_rates("EURUSD", same)
        self.h.fake.set_rates("XAUUSD", same)
        out = correlation.exposure(self.h.bridge, "EURUSD", ["XAUUSD", "US30"],
                                   count=140)
        measured = {item["symbol"]: item["r"] for item in out["compared"]}
        self.assertAlmostEqual(measured["XAUUSD"], 1.0, places=6)
        self.assertLess(abs(measured["US30"]), 0.8)
        self.assertGreater(out["points"], 100)

    def test_the_same_symbol_is_not_compared_to_itself(self):
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        out = correlation.exposure(self.h.bridge, "EURUSD", ["EURUSD"], count=140)
        self.assertEqual(out["compared"], [])

    def test_nothing_open_is_not_an_error(self):
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        out = correlation.exposure(self.h.bridge, "EURUSD", [], count=140)
        self.assertEqual(out["compared"], [])
        self.assertEqual(out["unverified"], [])

    def test_a_symbol_without_history_is_unverified_not_ignored(self):
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.h.fake.set_rates("XAUUSD", walk(9, count=5))
        out = correlation.exposure(self.h.bridge, "EURUSD", ["XAUUSD"], count=140)
        self.assertEqual(out["compared"], [])
        self.assertEqual(out["unverified"][0]["symbol"], "XAUUSD")


class KernelCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, position_symbol=None, **kwargs):
        position = (make_position(1, position_symbol, "buy", 0.1, 1.0850, MAGIC,
                                  sl=1.0800, price_current=1.0850)
                    if position_symbol else None)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS,
                               positions=[position] if position else None, **kwargs)
        return self.h

    def codes(self, verdict):
        return verdict["failed_checks"]

    def test_a_correlated_same_direction_position_blocks(self):
        self.build("XAUUSD")
        same = walk(41)
        self.h.fake.set_rates("EURUSD", same)
        self.h.fake.set_rates("XAUUSD", same)
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertIn("portfolio_correlation", self.codes(verdict))
        blocking = " ".join(verdict["blocking"])
        self.assertIn("same bet", blocking)
        self.assertIn("XAUUSD", blocking)

    def test_an_uncorrelated_position_does_not_block(self):
        self.build("US30")
        self.h.fake.set_rates("EURUSD", walk(51))
        self.h.fake.set_rates("US30", walk(97))
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertNotIn("portfolio_correlation", self.codes(verdict))

    def test_an_opposite_position_warns_instead_of_blocking(self):
        """A short on a correlated symbol partly offsets the new long, so it lowers
        the risk rather than doubling it."""
        self.build("XAUUSD")
        same = walk(61)
        self.h.fake.set_rates("EURUSD", same)
        self.h.fake.set_rates("XAUUSD", same)
        self.h.fake.positions[0]["type"] = 1  # the open one is a sell
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertNotIn("portfolio_correlation", self.codes(verdict))
        self.assertTrue(any("partly offsets" in w for w in verdict["warnings"]))

    def test_the_limit_can_be_loosened_only_up_to_its_ceiling(self):
        self.build("XAUUSD", config={"guards": {"correlation_limit": 5}})
        from mt5trader.config import HARD_CEILINGS
        self.assertEqual(self.h.settings.guards["correlation_limit"],
                         HARD_CEILINGS["correlation_limit"])

    def test_a_low_limit_blocks_even_weakly_related_symbols(self):
        self.build("US30", config={"guards": {"correlation_limit": 0.05}})
        self.h.fake.set_rates("EURUSD", walk(7))
        self.h.fake.set_rates("US30", walk(13))
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertIn("portfolio_correlation", self.codes(verdict))

    def test_warn_mode_reports_without_blocking(self):
        self.build("XAUUSD", config={"guards": {"correlation_action": "warn"}})
        same = walk(71)
        self.h.fake.set_rates("EURUSD", same)
        self.h.fake.set_rates("XAUUSD", same)
        verdict = self.h.kernel.check(harness.clean_plan())
        # The finding is still recorded as a failed check, because it is a finding.
        # What warn changes is whether it blocks.
        self.assertEqual(verdict["verdict"], "PASS", verdict["blocking"])
        self.assertTrue(any("same bet" in w for w in verdict["warnings"]))
        self.assertFalse(any("same bet" in b for b in verdict["blocking"]))
        entry = next(c for c in verdict["checks"] if c["check"] == "portfolio_correlation")
        self.assertIn("reported and not blocked", entry["detail"])

    def test_turning_the_limit_off_is_allowed(self):
        self.build("XAUUSD", config={"guards": {"correlation_limit": 0}})
        same = walk(81)
        self.h.fake.set_rates("EURUSD", same)
        self.h.fake.set_rates("XAUUSD", same)
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertNotIn("portfolio_correlation", self.codes(verdict))

    def test_the_check_reports_the_points_it_used(self):
        self.build("XAUUSD")
        same = walk(91)
        self.h.fake.set_rates("EURUSD", same)
        self.h.fake.set_rates("XAUUSD", same)
        verdict = self.h.kernel.check(harness.clean_plan())
        entry = next(c for c in verdict["checks"] if c["check"] == "portfolio_correlation")
        self.assertGreater(entry["points"], 100)


if __name__ == "__main__":
    unittest.main()

