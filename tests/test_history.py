import datetime
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import history
from mt5trader.fixtures import make_deal
from tests import harness

MAGIC = 770001


class DealMoney(unittest.TestCase):
    def test_all_four_columns_are_summed_not_just_profit(self):
        """Swap, commission and fee are separate columns. A loss limit that only
        reads profit is a loss limit that does not fire on an overnight hold."""
        deal = make_deal(1, "EURUSD", 0.1, 1.085, MAGIC, entry=1, profit_value=10.0,
                         swap=-3.5, commission=-7.0)
        deal["fee"] = -0.5
        self.assertAlmostEqual(history.deal_money(deal), -1.0, places=6)

    def test_a_balance_operation_is_not_a_trade(self):
        deal = make_deal(1, "", 0.0, 0.0, 0, entry=0, profit_value=500.0)
        deal["type"] = 2  # DEAL_TYPE_BALANCE
        self.assertFalse(history.is_trade(deal))


class RealizedSummary(unittest.TestCase):
    def test_totals_are_broken_out_by_source(self):
        deals = [
            make_deal(1, "EURUSD", 0.1, 1.085, MAGIC, entry=0, position_id=1),
            make_deal(2, "EURUSD", 0.1, 1.087, MAGIC, entry=1, position_id=1,
                      profit_value=20.0, swap=-2.0, commission=-4.0),
        ]
        summary = history.realized_summary(deals)
        self.assertAlmostEqual(summary["realized_net"], 14.0, places=2)
        self.assertAlmostEqual(summary["profit_gross"], 20.0, places=2)
        self.assertAlmostEqual(summary["swap"], -2.0, places=2)
        self.assertAlmostEqual(summary["commission"], -4.0, places=2)
        self.assertEqual(summary["closing_deals"], 1)


class RoundTrips(unittest.TestCase):
    def test_an_open_and_a_close_become_one_position(self):
        deals = [
            make_deal(1, "EURUSD", 0.5, 1.085, MAGIC, entry=0, position_id=77,
                      when=1000),
            make_deal(2, "EURUSD", 0.5, 1.087, MAGIC, entry=1, position_id=77,
                      profit_value=100.0, when=2000, reason=5),
        ]
        trips = history.round_trips(deals)
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0]["net"], 100.0, places=2)
        self.assertAlmostEqual(trips[0]["entry_price"], 1.085, places=6)
        self.assertEqual(trips[0]["close_reason"], "take-profit")

    def test_a_scaled_in_position_is_averaged_not_double_counted(self):
        deals = [
            make_deal(1, "EURUSD", 0.5, 1.080, MAGIC, entry=0, position_id=88, when=1000),
            make_deal(2, "EURUSD", 0.5, 1.090, MAGIC, entry=0, position_id=88, when=1100),
            make_deal(3, "EURUSD", 1.0, 1.085, MAGIC, entry=1, position_id=88,
                      profit_value=0.0, when=1200),
        ]
        trips = history.round_trips(deals)
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0]["entry_price"], 1.085, places=6)

    def test_a_partly_closed_position_is_not_reported_as_closed(self):
        deals = [
            make_deal(1, "EURUSD", 1.0, 1.085, MAGIC, entry=0, position_id=99, when=1000),
            make_deal(2, "EURUSD", 0.4, 1.090, MAGIC, entry=1, position_id=99,
                      profit_value=20.0, when=1200),
        ]
        self.assertEqual(history.round_trips(deals), [])

    def test_a_stop_out_is_distinguishable_from_a_take_profit(self):
        deals = [
            make_deal(1, "EURUSD", 0.5, 1.085, MAGIC, entry=0, position_id=5, when=1000),
            make_deal(2, "EURUSD", 0.5, 1.080, MAGIC, entry=1, position_id=5,
                      profit_value=-50.0, when=2000, reason=4),
        ]
        self.assertEqual(history.round_trips(deals)[0]["close_reason"], "stop-loss")


class Stats(unittest.TestCase):
    def build(self, nets):
        deals = []
        for index, net in enumerate(nets, start=1):
            deals.append(make_deal(index * 2 - 1, "EURUSD", 0.1, 1.085, MAGIC, entry=0,
                                   position_id=index, when=1000 * index))
            deals.append(make_deal(index * 2, "EURUSD", 0.1, 1.086, MAGIC, entry=1,
                                   position_id=index, profit_value=net,
                                   when=1000 * index + 100))
        return deals

    def test_the_numbers_a_report_needs(self):
        out = history.stats(self.build([100.0, -50.0, 80.0, -20.0]))
        self.assertEqual(out["closed_positions"], 4)
        self.assertEqual(out["wins"], 2)
        self.assertEqual(out["losses"], 2)
        self.assertAlmostEqual(out["win_rate"], 50.0, places=2)
        self.assertAlmostEqual(out["net"], 110.0, places=2)
        self.assertAlmostEqual(out["profit_factor"], 2.571, places=2)
        self.assertAlmostEqual(out["expectancy"], 27.5, places=2)
        self.assertAlmostEqual(out["best"], 100.0, places=2)
        self.assertAlmostEqual(out["worst"], -50.0, places=2)

    def test_the_longest_losing_streak_is_reported(self):
        out = history.stats(self.build([-10.0, -20.0, -30.0, 40.0, -5.0]))
        self.assertEqual(out["longest_loss_streak"], 3)

    def test_a_realized_drawdown_is_measured(self):
        out = history.stats(self.build([100.0, -80.0, -80.0, 50.0]))
        self.assertAlmostEqual(out["max_drawdown_realized"], 160.0, places=2)

    def test_per_symbol_breakdown(self):
        out = history.stats(self.build([100.0, -50.0]))
        self.assertEqual(out["per_symbol"]["EURUSD"]["trades"], 2)
        self.assertAlmostEqual(out["per_symbol"]["EURUSD"]["net"], 50.0, places=2)

    def test_no_trades_is_not_an_error(self):
        out = history.stats([])
        self.assertEqual(out["closed_positions"], 0)
        self.assertIn("no closed positions", out["note"])


class Clock(unittest.TestCase):
    def test_a_measured_offset_shifts_the_day_key(self):
        clock = history.Clock(offset_seconds=7200, source="test")
        expected = (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).strftime("%Y-%m-%d")
        self.assertEqual(clock.day_key(), expected)

    def test_the_day_starts_at_server_midnight(self):
        clock = history.Clock(offset_seconds=10800, source="test")
        start = clock.day_start()
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.minute, 0)
        self.assertEqual(start.day, clock.now.day)

    def test_the_description_states_where_the_offset_came_from(self):
        described = history.Clock(offset_seconds=3600, source="tick_msc",
                                  note="measured").describe()
        self.assertEqual(described["server_offset_hours"], 1.0)
        self.assertEqual(described["source"], "tick_msc")
        self.assertIn("server midnight", described["caveat"])


class ServerOffset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)

    def test_the_offset_is_derived_from_the_newest_tick(self):
        self.h.fake._tick_time = time.time() + 7200
        offset, source, note = history.server_offset(self.h.bridge)
        self.assertEqual(source, "tick_msc")
        self.assertAlmostEqual(offset, 7200, delta=2)
        self.assertIn("tick", note)

    def test_the_offset_is_snapped_to_a_quarter_hour(self):
        """Broker servers sit on whole hours; a few seconds of latency must not
        become a few seconds of drift in the day boundary."""
        self.h.fake._tick_time = time.time() + 7200 + 37
        offset, _, _ = history.server_offset(self.h.bridge)
        self.assertEqual(offset % 900, 0)

    def test_no_tick_falls_back_and_says_so(self):
        self.h.fake.symbols = {}
        offset, source, note = history.server_offset(self.h.bridge)
        self.assertEqual(offset, 0)
        self.assertEqual(source, "fallback_local")
        self.assertIn("wrong", note)

    def test_a_weekend_of_old_ticks_is_not_reported_as_a_time_zone(self):
        """The newest tick being 36 hours old means the market is closed. Reading
        that as an offset of -36 hours would put the trading day somewhere in the
        previous week."""
        self.h.fake._tick_time = time.time() - 36 * 3600
        offset, source, note = history.server_offset(self.h.bridge)
        self.assertEqual(offset, 0)
        self.assertEqual(source, "stale_tick")
        self.assertIn("market is closed", note)

    def test_a_stale_offset_is_remeasured_instead_of_being_cached(self):
        self.h.fake._tick_time = time.time() - 36 * 3600
        self.assertEqual(self.h.kernel.clock().source, "stale_tick")
        self.h.fake._tick_time = time.time() + 10800
        self.assertEqual(self.h.kernel.clock().source, "tick_msc")

    def test_a_measured_offset_is_cached(self):
        self.h.fake._tick_time = time.time() + 3600
        first = self.h.kernel.clock()
        second = self.h.kernel.clock()
        self.assertIs(first, second)


class EquitySeries(unittest.TestCase):
    def test_the_running_total_follows_the_deals_in_time_order(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        deals = [
            make_deal(2, "EURUSD", 0.1, 1.086, MAGIC, entry=1, profit_value=-20.0,
                      when=int(time.time()) - 100),
            make_deal(1, "EURUSD", 0.1, 1.085, MAGIC, entry=0, when=int(time.time()) - 200),
            make_deal(3, "EURUSD", 0.1, 1.087, MAGIC, entry=1, profit_value=50.0,
                      when=int(time.time()) - 50),
        ]
        h = harness.build(tmp.name, env=harness.AUTONOMOUS, deals=deals)
        series = history.equity_series(h.bridge,
                                       datetime.datetime.utcnow() - datetime.timedelta(days=1))
        self.assertEqual(series[-1]["cumulative"], 30.0)


if __name__ == "__main__":
    unittest.main()
