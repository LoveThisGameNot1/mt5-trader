import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import instruments
from mt5trader.fixtures import (btcusd, eurusd, eurusd_wide_stops, make_symbol,
                                one_lot_minimum, testm, us30, xauusd)


class Metadata(unittest.TestCase):
    def test_missing_fields_fall_back_instead_of_raising(self):
        instrument = instruments.build({"name": "SPARSE"})
        self.assertEqual(instrument.symbol, "SPARSE")
        self.assertEqual(instrument.volume_min, 0.01)
        self.assertGreater(instrument.tick_size, 0)

    def test_build_requires_metadata(self):
        with self.assertRaises(instruments.InstrumentError):
            instruments.build(None)

    def test_the_tick_fields_are_read_under_their_real_mt5_names(self):
        """trade_tick_size and trade_tick_value_loss, not tick_size and tick_value.
        Reading a name the terminal does not have yields a zero, and a zero tick
        value sizes every position at nothing."""
        instrument = instruments.build(eurusd())
        self.assertTrue(instrument.tick_size_reported)
        self.assertAlmostEqual(instrument.tick_size, 0.00001, places=12)
        self.assertAlmostEqual(instrument.tick_value_loss, 1.0, places=9)
        self.assertTrue(instrument.pricing_ok)

    def test_the_schema_check_names_a_renamed_field(self):
        """The fixture below is what the code looked like before this check existed:
        the fields under names MetaTrader does not use."""
        wrong = {"name": "RENAMED", "digits": 5, "point": 0.00001,
                 "tick_size": 0.00001, "tick_value_loss": 1.0,
                 "contract_size": 100000.0, "volume_min": 0.01, "volume_step": 0.01}
        report = instruments.check_schema(wrong)
        self.assertFalse(report["ok"])
        self.assertIn("trade_tick_size", report["required_missing"])
        self.assertIn("trade_tick_value_loss", report["required_missing"])
        self.assertIn("divide by a zero tick value", report["meaning"])

    def test_the_schema_check_passes_a_really_shaped_symbol(self):
        report = instruments.check_schema(eurusd())
        self.assertTrue(report["ok"], report["required_missing"])
        self.assertEqual(report["required_missing"], [])
        self.assertIn("trade_tick_value", report["present"])

    def test_pricing_is_refused_when_the_tick_value_is_zero(self):
        instrument = instruments.build(make_symbol("ZERO", trade_tick_value=0))
        self.assertFalse(instrument.pricing_ok)


class VolumeNormalisation(unittest.TestCase):
    def test_valid_sizes_are_min_plus_n_times_step(self):
        """TESTM: min 0.1, step 0.1. 0.25 is not a size; the size below it is 0.2."""
        instrument = instruments.build(testm())
        self.assertAlmostEqual(instrument.normalize_volume(0.25), 0.2)
        self.assertAlmostEqual(instrument.normalize_volume(0.19), 0.1)
        self.assertAlmostEqual(instrument.normalize_volume(0.1), 0.1)
        self.assertAlmostEqual(instrument.normalize_volume(0.2), 0.2)

    def test_below_the_minimum_is_an_error_not_a_round_up(self):
        instrument = instruments.build(testm())
        with self.assertRaises(instruments.InstrumentError):
            instrument.normalize_volume(0.05)

    def test_never_rounds_up_past_the_maximum(self):
        instrument = instruments.build(testm())
        self.assertAlmostEqual(instrument.normalize_volume(99.0), 5.0)

    def test_float_step_does_not_lose_a_step(self):
        """0.3 / 0.1 is 2.9999999999999996 in binary floating point, which would
        floor to 0.2 without Decimal."""
        instrument = instruments.build(us30())  # min 0.1, step 0.1
        self.assertAlmostEqual(instrument.normalize_volume(0.3), 0.3)
        self.assertAlmostEqual(instrument.normalize_volume(0.7), 0.7)

    def test_rejects_zero_and_negative(self):
        instrument = instruments.build(eurusd())
        for bad in (0, -1, None, "abc"):
            with self.assertRaises(instruments.InstrumentError):
                instrument.normalize_volume(bad)


class PriceNormalisation(unittest.TestCase):
    def test_rounds_half_away_from_zero_like_normalize_double(self):
        instrument = instruments.build(make_symbol("X", digits=2, point=0.01,
                                                   trade_tick_size=0.01))
        self.assertAlmostEqual(instrument.normalize_price(1.005), 1.01)
        self.assertAlmostEqual(instrument.normalize_price(1.004), 1.0)
        self.assertAlmostEqual(instrument.normalize_price(2.675), 2.68)

    def test_five_digit_forex_keeps_all_digits(self):
        instrument = instruments.build(eurusd())
        self.assertAlmostEqual(instrument.normalize_price(1.084123456), 1.08412)

    def test_none_passes_through(self):
        self.assertIsNone(instruments.build(eurusd()).normalize_price(None))


class MoneyMath(unittest.TestCase):
    def test_forex_gives_ten_dollars_a_pip_without_a_contract_size_assumption(self):
        instrument = instruments.build(eurusd())
        self.assertAlmostEqual(instrument.value_per_point_per_lot, 1.0, places=9)
        self.assertAlmostEqual(instrument.pip_size, 0.0001, places=9)

    def test_gold(self):
        instrument = instruments.build(xauusd())
        self.assertAlmostEqual(instrument.risk_money(1.0, 2400.0, 2390.0), 1000.0, places=6)

    def test_index_cfd(self):
        instrument = instruments.build(us30())
        self.assertAlmostEqual(instrument.value_per_point_per_lot, 0.1, places=9)
        self.assertAlmostEqual(instrument.risk_money(2.0, 39450.0, 39400.0), 100.0, places=6)

    def test_crypto(self):
        instrument = instruments.build(btcusd())
        self.assertAlmostEqual(instrument.risk_money(0.5, 63000.0, 62000.0), 500.0, places=6)

    def test_forex_risk_for_a_hundred_point_stop(self):
        instrument = instruments.build(eurusd())
        self.assertAlmostEqual(instrument.risk_money(1.0, 1.08512, 1.08412), 100.0, places=6)

    def test_money_to_points_round_trips(self):
        instrument = instruments.build(eurusd())
        points = instrument.money_to_points(1.0, 100.0)
        self.assertAlmostEqual(points, 100.0, places=6)


class FillingModes(unittest.TestCase):
    def test_market_execution_symbol_gets_return_as_the_fallback(self):
        """FOK and IOC come from the bitmask; RETURN is what makes a market order
        succeed when only part of the volume can be filled."""
        instrument = instruments.build(eurusd())  # filling_mode 3 = FOK|IOC
        candidates = instrument.filling_candidates()
        self.assertEqual(candidates[0], instruments.ORDER_FILLING_IOC)
        self.assertIn(instruments.ORDER_FILLING_FOK, candidates)
        self.assertIn(instruments.ORDER_FILLING_RETURN, candidates)

    def test_ioc_only_symbol(self):
        instrument = instruments.build(testm())  # filling_mode 2 = IOC
        self.assertEqual(instrument.filling_candidates()[0], instruments.ORDER_FILLING_IOC)

    def test_fok_only_symbol(self):
        instrument = instruments.build(us30())  # filling_mode 1 = FOK
        candidates = instrument.filling_candidates()
        self.assertEqual(candidates[0], instruments.ORDER_FILLING_FOK)
        self.assertNotIn(instruments.ORDER_FILLING_IOC, candidates)

    def test_explicit_preference_is_honoured_first(self):
        instrument = instruments.build(eurusd())
        self.assertEqual(instrument.filling_candidates("fok")[0], instruments.ORDER_FILLING_FOK)

    def test_a_symbol_with_no_bits_still_yields_a_usable_order(self):
        instrument = instruments.build(make_symbol("NONE", filling_mode=0))
        self.assertTrue(instrument.filling_candidates())


class Stops(unittest.TestCase):
    def test_buy_stop_above_entry_is_rejected(self):
        instrument = instruments.build(eurusd())
        with self.assertRaises(instruments.InstrumentError):
            instrument.normalize_stops("buy", 1.08512, sl=1.08600)

    def test_sell_take_profit_above_entry_is_rejected(self):
        instrument = instruments.build(eurusd())
        with self.assertRaises(instruments.InstrumentError):
            instrument.normalize_stops("sell", 1.08500, tp=1.08600)

    def test_broker_minimum_distance_is_enforced(self):
        """trade_stops_level is in points, not price and not pips."""
        instrument = instruments.build(eurusd_wide_stops())  # 50 points
        with self.assertRaises(instruments.InstrumentError):
            instrument.normalize_stops("buy", 1.08512, sl=1.08502)  # 10 points

    def test_clamp_mode_pushes_the_stop_out_and_says_so(self):
        instrument = instruments.build(eurusd_wide_stops())
        sl, tp, notes = instrument.normalize_stops("buy", 1.08512, sl=1.08502, clamp=True)
        self.assertAlmostEqual(sl, 1.08512 - 0.0005, places=6)
        self.assertTrue(any("clamped" in n for n in notes))

    def test_a_valid_stop_pass_exists_cleanly(self):
        instrument = instruments.build(eurusd_wide_stops())
        sl, tp, notes = instrument.normalize_stops("buy", 1.08512, sl=1.08400, tp=1.08800)
        self.assertAlmostEqual(sl, 1.08400, places=6)
        self.assertAlmostEqual(tp, 1.08800, places=6)
        self.assertEqual(notes, [])

    def test_freeze_level_is_reported(self):
        instrument = instruments.build(eurusd_wide_stops())  # freeze 10 points
        message = instrument.check_freeze(sl=1.08505, current=1.08512)
        self.assertIsNotNone(message)
        self.assertIn("freeze", message)
        self.assertIsNone(instrument.check_freeze(sl=1.08400, current=1.08512))

    def test_no_freeze_level_means_no_check(self):
        instrument = instruments.build(eurusd())
        self.assertIsNone(instrument.check_freeze(sl=1.08511, current=1.08512))


class Session(unittest.TestCase):
    def test_disabled_symbol(self):
        instrument = instruments.build(make_symbol("OFF", trade_mode=0))
        self.assertFalse(instrument.market_open)
        self.assertFalse(instrument.new_positions_allowed)

    def test_long_only(self):
        instrument = instruments.build(make_symbol("LO", trade_mode=1))
        self.assertTrue(instrument.side_allowed("buy"))
        self.assertFalse(instrument.side_allowed("sell"))

    def test_short_only(self):
        instrument = instruments.build(make_symbol("SO", trade_mode=2))
        self.assertFalse(instrument.side_allowed("buy"))
        self.assertTrue(instrument.side_allowed("sell"))

    def test_entry_and_exit_use_the_right_side_of_the_book(self):
        instrument = instruments.build(eurusd())
        self.assertAlmostEqual(instrument.entry_price("buy"), instrument.ask)
        self.assertAlmostEqual(instrument.entry_price("sell"), instrument.bid)
        self.assertAlmostEqual(instrument.exit_price("buy"), instrument.bid)


class Summary(unittest.TestCase):
    def test_summary_is_json_safe_and_names_the_filling_modes(self):
        import json
        instrument = instruments.build(eurusd(), tick={"bid": 1.085, "ask": 1.08512,
                                                       "time": 1700000000})
        payload = json.loads(json.dumps(instrument.summary()))
        self.assertEqual(payload["symbol"], "EURUSD")
        self.assertIn("IOC", payload["filling_modes"])
        self.assertEqual(payload["trade_mode"], "full")


if __name__ == "__main__":
    unittest.main()
