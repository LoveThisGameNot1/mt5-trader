import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import instruments, sizing
from mt5trader.fixtures import (btcusd, make_account, one_lot_minimum, testm, us30,
                                xauusd)
from tests import harness


class Sizing(unittest.TestCase):
    def setUp(self):
        self.eurusd = instruments.build(harness._default_symbols()[0])

    def test_a_hundred_dollars_with_a_hundred_point_stop_is_one_lot(self):
        out = sizing.size_for_money(self.eurusd, 100.0, 1.08512, 1.08412)
        self.assertAlmostEqual(out["lots"], 1.0)
        self.assertAlmostEqual(out["risk_money"], 100.0, places=2)
        self.assertEqual(out["stop_points"], 100.0)

    def test_it_floors_and_never_exceeds_the_budget(self):
        for budget in (55.0, 54.99, 3.33, 1.01):
            out = sizing.size_for_money(self.eurusd, budget, 1.08512, 1.08412)
            self.assertIsNotNone(out["lots"], budget)
            self.assertLessEqual(out["risk_money"], budget + 0.01, budget)
            self.assertGreaterEqual(out["lots"], self.eurusd.volume_min)

    def test_the_reason_names_the_minimum_possible_risk(self):
        """When the smallest tradeable size is already too big, the model needs a
        number it can tell the user, not just 'too small'."""
        instrument = instruments.build(one_lot_minimum())
        out = sizing.size_for_money(instrument, 3.0, 100.5, 99.5)
        self.assertIsNone(out["lots"])
        self.assertAlmostEqual(out["minimum_possible_risk"], 5.0, places=2)
        self.assertIn("smallest size", out["reason"])

    def test_a_stop_equal_to_the_entry_is_refused(self):
        out = sizing.size_for_money(self.eurusd, 100.0, 1.08512, 1.08512)
        self.assertIsNone(out["lots"])
        self.assertIn("same price", out["reason"])

    def test_percentages_use_equity_not_balance(self):
        out = sizing.size_for_percent(self.eurusd, 10000.0, 0.5, 1.08512, 1.08412)
        self.assertAlmostEqual(out["lots"], 0.5)
        self.assertAlmostEqual(out["risk_money"], 50.0, places=2)
        self.assertAlmostEqual(out["actual_risk_percent"], 0.5, places=3)

    def test_the_same_code_sizes_four_different_instrument_types(self):
        cases = [
            (xauusd(), 2400.35, 2390.35, 100.0),   # $100 risk, $10 stop, $1000/lot
            (us30(), 39453.0, 39403.0, 100.0),     # 50 index points at $1/point/lot
            (btcusd(), 63025.0, 62525.0, 100.0),   # $500 stop at $1 per $1
        ]
        for info, entry, stop, budget in cases:
            instrument = instruments.build(info)
            out = sizing.size_for_money(instrument, budget, entry, stop)
            self.assertIsNotNone(out["lots"], info["name"])
            self.assertLessEqual(out["risk_money"], budget + 0.01, info["name"])

    def test_step_symbol_floors_to_a_valid_size(self):
        instrument = instruments.build(testm())  # min 0.1 step 0.1, tick 0.001 @ $0.5
        # per lot: 0.5 / 0.001 = $500 per price unit, so a 0.02 move is $10 a lot
        out = sizing.size_for_money(instrument, 100.0, 10.02, 10.0)
        self.assertAlmostEqual(out["lots"], 5.0)  # 10 lots wanted, volume_max is 5
        self.assertTrue(out["capped_at_max"])
        self.assertAlmostEqual(out["requested_lots"], 10.0)
        self.assertLessEqual(out["risk_money"], 100.01)
        self.assertIn("smaller than the risk allowance", out["note"])


class RewardRisk(unittest.TestCase):
    def test_two_to_one(self):
        self.assertAlmostEqual(sizing.reward_risk(1.08512, 1.08412, 1.08712), 2.0, places=6)

    def test_target_on_the_wrong_side_is_negative_not_hidden(self):
        self.assertLess(sizing.reward_risk(1.08512, 1.08412, 1.08312), 0)

    def test_missing_target(self):
        self.assertIsNone(sizing.reward_risk(1.0, 0.9, None))

    def test_r_now_underwater_is_negative(self):
        self.assertAlmostEqual(sizing.r_now("buy", 1.08512, 1.08412, 1.08462), -0.5, places=6)
        self.assertAlmostEqual(sizing.r_now("sell", 1.08512, 1.08612, 1.08462), 0.5, places=6)


class Margin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name)
        # Loaded through the bridge so the account leverage comes with it, which
        # is what the margin fallback needs.
        self.instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")

    def test_terminal_figure_is_used_and_says_so(self):
        margin, source = sizing.margin_required(self.h.bridge, self.instrument, "buy",
                                               1.0, 1.085)
        self.assertAlmostEqual(margin, 1085.0, places=2)  # 1 lot x 100k x 1.085 / 100
        self.assertEqual(source, "terminal")

    def test_reserve_is_kept(self):
        out = sizing.check_margin_fit(self.h.bridge, self.instrument, "buy", 1.0, 1.085,
                                      free_margin=10000.0, reserve_fraction=0.25)
        self.assertTrue(out["fits"])
        self.assertAlmostEqual(out["reserve_kept"], 2500.0, places=2)

    def test_too_big_for_the_free_margin_is_caught(self):
        out = sizing.check_margin_fit(self.h.bridge, self.instrument, "buy", 5.0, 1.085,
                                      free_margin=2000.0, reserve_fraction=0.25)
        self.assertFalse(out["fits"])
        self.assertIsNotNone(out["max_lots_for_this_free_margin"])

    def test_no_free_margin(self):
        out = sizing.check_margin_fit(self.h.bridge, self.instrument, "buy", 0.01, 1.085,
                                      free_margin=0.0)
        self.assertFalse(out["fits"])


class StopDistanceHelper(unittest.TestCase):
    def test_turns_a_money_budget_into_a_price_distance(self):
        instrument = instruments.build(harness._default_symbols()[0])
        self.assertAlmostEqual(sizing.stop_distance(1.0, instrument, 100.0), 0.001, places=6)

    def test_unknown_when_the_symbol_has_no_tick_value(self):
        instrument = instruments.build({"name": "NOTICK", "tick_size": 0, "tick_value": 0})
        self.assertIsNone(sizing.stop_distance(1.0, instrument, 100.0))


if __name__ == "__main__":
    unittest.main()
