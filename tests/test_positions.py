import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import positions
from mt5trader.fixtures import make_account, make_position
from tests import harness

MAGIC = 770001


class PositionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, position=None, env=None, **kwargs):
        self.h = harness.build(self.tmp.name,
                               env=env if env is not None else harness.AUTONOMOUS,
                               positions=[position] if position else None, **kwargs)
        return self.h

    def quote(self, symbol, bid, ask):
        self.h.fake.symbols[symbol]["bid"] = bid
        self.h.fake.symbols[symbol]["ask"] = ask
        self.h.reconnect()

    def eurusd(self, **overrides):
        defaults = dict(ticket=101, symbol="EURUSD", side="buy", volume=0.5,
                        price_open=1.08500, magic=MAGIC, sl=1.08400, tp=1.08700,
                        price_current=1.08500)
        defaults.update(overrides)
        return make_position(defaults.pop("ticket"), defaults.pop("symbol"),
                             defaults.pop("side"), defaults.pop("volume"),
                             defaults.pop("price_open"), defaults.pop("magic"),
                             **defaults)


class Closing(PositionTestCase):
    def test_a_full_close_removes_the_position(self):
        self.make(self.eurusd())
        out = positions.close(self.h.bridge, self.h.settings, 101)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["closed_volume"], 0.5)
        self.assertEqual(self.h.fake.positions, [])
        self.assertEqual(len(self.h.fake.deals), 1)

    def test_the_close_order_carries_the_ticket_so_it_is_a_close_not_a_hedge(self):
        self.make(self.eurusd())
        positions.close(self.h.bridge, self.h.settings, 101)
        sent = self.h.fake.sent[-1]
        self.assertEqual(sent["position"], 101)
        self.assertEqual(sent["type"], 1)  # closing a buy means selling

    def test_a_partial_close_leaves_the_remainder(self):
        self.make(self.eurusd())
        out = positions.close(self.h.bridge, self.h.settings, 101, volume=0.2)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["remaining_volume"], 0.3)
        self.assertAlmostEqual(self.h.fake.positions[0]["volume"], 0.3)

    def test_a_remainder_below_the_minimum_closes_everything_and_says_so(self):
        """The broker refuses to leave 0.01 lots when the minimum is 0.1, so the
        whole position goes and the caller is told that is what happened."""
        self.make(make_position(202, "FHEDGE", "buy", 0.15, 1.085, MAGIC,
                                sl=1.084, price_current=1.085))
        out = positions.close(self.h.bridge, self.h.settings, 202, volume=0.14)
        self.assertEqual(out["closed_volume"], 0.15)
        self.assertEqual(self.h.fake.positions, [])
        self.assertTrue(any("under the minimum" in n for n in out["notes"]))

    def test_a_position_that_vanished_is_not_silently_ignored(self):
        self.make()
        with self.assertRaises(positions.PositionError):
            positions.close(self.h.bridge, self.h.settings, 999)

    def test_a_foreign_position_is_refused(self):
        self.make(make_position(303, "EURUSD", "buy", 0.5, 1.085, 999999))
        with self.assertRaises(positions.PositionError) as caught:
            positions.close(self.h.bridge, self.h.settings, 303)
        self.assertIn("does not touch it", str(caught.exception))

    def test_a_foreign_position_needs_the_explicit_flag(self):
        self.make(make_position(303, "EURUSD", "buy", 0.5, 1.085, 999999))
        out = positions.close(self.h.bridge, self.h.settings, 303, allow_foreign=True)
        self.assertEqual(out["status"], "done")

    def test_a_broker_refusal_is_reported_with_its_reason(self):
        self.make(self.eurusd())
        self.h.fake.script_send = [10018, 10018, 10018]
        out = positions.close(self.h.bridge, self.h.settings, 101)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("MARKET_CLOSED", out["message"])


class Modifying(PositionTestCase):
    def test_changing_only_the_stop_keeps_the_target(self):
        """MT5's SLTP action is absolute: omitting the target would clear it."""
        self.make(self.eurusd())
        out = positions.modify(self.h.bridge, self.h.settings, 101, sl=1.08450)
        self.assertEqual(out["status"], "done", out)
        self.assertAlmostEqual(out["position"]["sl"], 1.08450, places=6)
        self.assertAlmostEqual(out["position"]["tp"], 1.08700, places=6)

    def test_clearing_the_target_has_to_be_asked_for(self):
        self.make(self.eurusd())
        out = positions.modify(self.h.bridge, self.h.settings, 101, clear_tp=True)
        self.assertIsNone(out["position"]["tp"])
        self.assertAlmostEqual(out["position"]["sl"], 1.08400, places=6)

    def test_a_stop_on_the_wrong_side_is_refused_not_sent(self):
        self.make(self.eurusd())
        out = positions.modify(self.h.bridge, self.h.settings, 101, sl=1.09000)
        self.assertEqual(out["status"], "refused")
        self.assertIn("sit below the price", out["message"])

    def test_an_existing_target_the_market_ran_past_is_kept_not_validated(self):
        """Holding a target that price has already gone through is legal. Refusing
        to move the stop because of it would block every working trail."""
        self.make(self.eurusd())
        self.quote("EURUSD", 1.09000, 1.09012)
        out = positions.modify(self.h.bridge, self.h.settings, 101, sl=1.08600)
        self.assertEqual(out["status"], "done", out)
        self.assertAlmostEqual(out["position"]["tp"], 1.08700, places=6)
        self.assertAlmostEqual(out["position"]["sl"], 1.08600, places=6)

    def test_a_stop_inside_the_brokers_minimum_distance_is_refused(self):
        self.make(make_position(404, "EURUSDW", "buy", 0.1, 1.085, MAGIC,
                                sl=1.0800, price_current=1.085))
        out = positions.modify(self.h.bridge, self.h.settings, 404, sl=1.08490)
        self.assertEqual(out["status"], "refused")
        self.assertIn("minimum", out["message"])

    def test_a_stop_inside_the_freeze_level_is_refused_with_the_reason(self):
        self.make(make_position(505, "FRZ", "buy", 0.1, 1.085, MAGIC,
                                sl=1.0800, price_current=1.085))
        out = positions.modify(self.h.bridge, self.h.settings, 505, sl=1.08490)
        self.assertEqual(out["status"], "refused")
        self.assertIn("freeze", out["message"])

    def test_a_read_back_that_disagrees_is_drifted_not_done(self):
        self.make(self.eurusd())
        original = self.h.fake._modify

        def lying(request, found):
            result = original(request, found)
            for position in self.h.fake.positions:
                if position["ticket"] == request["position"]:
                    position["sl"] = 0.0
            return result
        self.h.fake._modify = lying
        out = positions.modify(self.h.bridge, self.h.settings, 101, sl=1.08450)
        self.assertEqual(out["status"], "drifted")
        self.assertIn("reads back differently", out["message"])


class BreakEven(PositionTestCase):
    def test_it_refuses_while_the_trade_is_underwater(self):
        self.make(self.eurusd())
        out = positions.break_even(self.h.bridge, self.h.settings, 101)
        self.assertEqual(out["status"], "refused")
        self.assertIn("not in profit", out["message"])

    def test_it_moves_the_stop_to_the_entry_once_in_profit(self):
        self.make(self.eurusd())
        self.quote("EURUSD", 1.09000, 1.09012)
        out = positions.break_even(self.h.bridge, self.h.settings, 101)
        self.assertEqual(out["status"], "done", out)
        self.assertAlmostEqual(out["position"]["sl"], 1.08500, places=6)

    def test_an_offset_carries_the_stop_past_the_entry(self):
        self.make(self.eurusd())
        self.quote("EURUSD", 1.09000, 1.09012)
        out = positions.break_even(self.h.bridge, self.h.settings, 101, offset_points=20)
        self.assertAlmostEqual(out["position"]["sl"], 1.08520, places=6)


class Trailing(PositionTestCase):
    def test_a_trailing_step_moves_the_stop_behind_the_price(self):
        self.make(make_position(606, "EURUSD", "buy", 0.1, 1.0850, MAGIC,
                                sl=1.08300, price_current=1.09000))
        self.quote("EURUSD", 1.09000, 1.09012)
        out = positions.trail(self.h.bridge, self.h.settings, 606, distance_points=50)
        self.assertEqual(out["status"], "done", out)
        self.assertAlmostEqual(out["position"]["sl"], 1.08950, places=6)

    def test_it_never_moves_the_stop_backwards(self):
        self.make(make_position(707, "EURUSD", "buy", 0.1, 1.0850, MAGIC,
                                sl=1.08950, price_current=1.09000))
        self.quote("EURUSD", 1.09000, 1.09012)
        out = positions.trail(self.h.bridge, self.h.settings, 707, distance_points=50)
        self.assertEqual(out["status"], "no-change")
        self.assertIn("not better", out["message"])

    def test_a_step_filter_skips_a_tiny_improvement(self):
        self.make(make_position(808, "EURUSD", "buy", 0.1, 1.0850, MAGIC,
                                sl=1.08900, price_current=1.09000))
        self.quote("EURUSD", 1.09000, 1.09012)
        out = positions.trail(self.h.bridge, self.h.settings, 808, distance_points=50,
                              step_points=100)
        self.assertEqual(out["status"], "no-change")
        self.assertIn("step", out["message"])

    def test_a_sell_trails_from_the_other_direction(self):
        self.make(make_position(909, "EURUSD", "sell", 0.1, 1.0900, MAGIC,
                                sl=1.09500, price_current=1.09000))
        # a sell exits at the ask, so the stop sits 50 points above 1.08512
        out = positions.trail(self.h.bridge, self.h.settings, 909, distance_points=50)
        self.assertEqual(out["status"], "done", out)
        self.assertAlmostEqual(out["position"]["sl"], 1.08562, places=6)


class Flattening(PositionTestCase):
    def test_close_all_only_touches_our_own_positions(self):
        self.make()
        self.h.fake.positions = [
            make_position(1, "EURUSD", "buy", 0.1, 1.085, MAGIC),
            make_position(2, "XAUUSD", "sell", 0.1, 2400.0, MAGIC),
            make_position(3, "US30", "buy", 0.1, 39450.0, 999999),
        ]
        out = positions.close_all(self.h.bridge, self.h.settings)
        self.assertEqual(out["closed"], 2)
        self.assertEqual(out["remaining"], 0)
        self.assertEqual(len(self.h.fake.positions), 1)
        self.assertEqual(self.h.fake.positions[0]["magic"], 999999)

    def test_scope_all_needs_the_operators_switch(self):
        self.make(make_position(3, "US30", "buy", 0.1, 39450.0, 999999))
        with self.assertRaises(PermissionError):
            positions.close_all(self.h.bridge, self.h.settings, scope="all")

    def test_scope_all_works_when_allowed(self):
        env = dict(harness.AUTONOMOUS, MT5_ALLOW_FLATTEN="1")
        self.make(make_position(3, "US30", "buy", 0.1, 39450.0, 999999), env=env)
        out = positions.close_all(self.h.bridge, self.h.settings, scope="all")
        self.assertEqual(out["closed"], 1)
        self.assertEqual(self.h.fake.positions, [])

    def test_a_symbol_filter_narrows_the_flatten(self):
        self.make()
        self.h.fake.positions = [
            make_position(1, "EURUSD", "buy", 0.1, 1.085, MAGIC),
            make_position(2, "XAUUSD", "buy", 0.1, 2400.0, MAGIC),
        ]
        out = positions.close_all(self.h.bridge, self.h.settings, symbol="XAUUSD")
        self.assertEqual(out["closed"], 1)
        self.assertEqual(len(self.h.fake.positions), 1)
        self.assertEqual(self.h.fake.positions[0]["symbol"], "EURUSD")


class Pendings(PositionTestCase):
    def test_a_pending_order_can_be_cancelled(self):
        self.make()
        self.h.fake.orders = [{"ticket": 555, "symbol": "EURUSD", "type": 2,
                               "volume_current": 0.1, "price_open": 1.0800,
                               "magic": MAGIC, "comment": ""}]
        out = positions.cancel_pending(self.h.bridge, self.h.settings, 555)
        self.assertFalse(out["still_there"])
        self.assertEqual(self.h.fake.orders, [])

    def test_cancelling_something_that_is_not_there_is_an_error(self):
        self.make()
        with self.assertRaises(positions.PositionError):
            positions.cancel_pending(self.h.bridge, self.h.settings, 555)


class AccountMode(PositionTestCase):
    def test_hedging_is_detected(self):
        self.make()
        mode = positions.account_margin_mode(self.h.bridge)
        self.assertTrue(mode["hedging"])

    def test_netting_is_detected(self):
        self.make(account=make_account(margin_mode=0))
        mode = positions.account_margin_mode(self.h.bridge)
        self.assertFalse(mode["hedging"])
        self.assertEqual(mode["name"], "netting")


if __name__ == "__main__":
    unittest.main()
