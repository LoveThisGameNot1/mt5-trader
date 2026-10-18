import datetime
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import instruments, orders
from mt5trader.fixtures import make_deal, make_position
from tests import harness

MAGIC = 770001


class BuildRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")

    def spec(self, **overrides):
        spec = {
            "symbol": "EURUSD", "side": "buy", "kind": "market", "type": "market",
            "volume": 0.5, "price": 1.08512, "stop": 1.08412, "target": 1.08712,
            "deviation": 15, "filling": "auto", "magic": MAGIC, "comment": "test",
        }
        spec.update(overrides)
        return spec

    def test_a_market_buy_is_priced_at_the_ask(self):
        """Leaving the price out, or using the mid, is the most common cause of an
        invalid-price rejection on an otherwise correct order."""
        request = orders.build_request(self.h.bridge, self.h.settings, self.spec(),
                                       self.instrument)
        self.assertEqual(request["action"], orders.ACTION_DEAL)
        self.assertAlmostEqual(request["price"], self.instrument.ask)
        self.assertEqual(request["type"], 0)

    def test_a_market_sell_is_priced_at_the_bid(self):
        request = orders.build_request(self.h.bridge, self.h.settings,
                                       self.spec(side="sell", stop=1.08612, target=1.08312),
                                       self.instrument)
        self.assertAlmostEqual(request["price"], self.instrument.bid)
        self.assertEqual(request["type"], 1)

    def test_the_filling_mode_comes_from_the_symbol_not_from_a_guess(self):
        request = orders.build_request(self.h.bridge, self.h.settings, self.spec(),
                                       self.instrument)
        self.assertIn(request["type_filling"], self.instrument.filling_candidates())

    def test_stops_are_rounded_to_the_symbol_digits(self):
        request = orders.build_request(self.h.bridge, self.h.settings,
                                       self.spec(stop=1.084123456), self.instrument)
        self.assertAlmostEqual(request["sl"], 1.08412)

    def test_magic_and_comment_are_always_set(self):
        request = orders.build_request(self.h.bridge, self.h.settings, self.spec(),
                                       self.instrument)
        self.assertEqual(request["magic"], MAGIC)
        self.assertEqual(request["comment"], "test")

    def test_a_pending_order_carries_a_time_in_force(self):
        request = orders.build_request(self.h.bridge, self.h.settings,
                                       self.spec(kind="pending", type="limit",
                                                 price=1.08000, stop=None), self.instrument)
        self.assertEqual(request["action"], orders.ACTION_PENDING)
        self.assertEqual(request["type"], 2)
        self.assertEqual(request["type_time"], orders.TIME_IN_FORCE["gtc"])

    def test_a_pending_order_with_a_specified_expiry_gets_a_datetime(self):
        spec = self.spec(kind="pending", type="limit", price=1.08000, stop=None,
                         time_in_force="specified")
        spec["_clock"] = self.h.kernel.clock()
        request = orders.build_request(self.h.bridge, self.h.settings, spec, self.instrument)
        self.assertEqual(request["type_time"], orders.TIME_IN_FORCE["specified"])
        self.assertIsInstance(request["expiration"], datetime.datetime)

    def test_a_stop_limit_order_needs_its_second_price(self):
        spec = self.spec(kind="pending", type="stop_limit", price=1.09000, stop=None)
        with self.assertRaises(orders.OrderError):
            orders.build_request(self.h.bridge, self.h.settings, spec, self.instrument)

    def test_a_market_order_on_a_symbol_with_no_quotes_is_refused(self):
        spec = self.spec()
        self.instrument.tick = {"bid": 0, "ask": 0}
        with self.assertRaises(orders.OrderError):
            orders.build_request(self.h.bridge, self.h.settings, spec, self.instrument)

    def test_a_close_carries_the_position_ticket(self):
        request = orders.build_request(self.h.bridge, self.h.settings, self.spec(),
                                       self.instrument, position=12345)
        self.assertEqual(request["position"], 12345)


class Preflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)

    def test_a_clean_request_reports_the_margin(self):
        verdict = self.h.kernel.check(harness.clean_plan())
        instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")
        request = orders.build_request(self.h.bridge, self.h.settings, verdict["order"],
                                       instrument)
        out = orders.preflight(self.h.bridge, request)
        self.assertTrue(out["ok"])
        self.assertIsNotNone(out["margin"])

    def test_the_brokers_own_refusal_is_surfaced(self):
        self.h.fake.script_check = 10019
        verdict = self.h.kernel.check(harness.clean_plan())
        instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")
        request = orders.build_request(self.h.bridge, self.h.settings, verdict["order"],
                                       instrument)
        out = orders.preflight(self.h.bridge, request)
        self.assertFalse(out["ok"])
        self.assertEqual(out["retcode"], 10019)


class Send(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.instrument, _ = instruments.load(self.h.bridge, self.h.settings, "EURUSD")

    def send(self, spec=None, **kwargs):
        spec = spec or self.h.kernel.check(harness.clean_plan())["order"]
        spec = dict(spec)
        spec["_clock"] = self.h.kernel.clock()
        request = orders.build_request(self.h.bridge, self.h.settings, spec,
                                       self.instrument)
        return orders.send(self.h.bridge, self.h.store, spec, self.instrument, request,
                           self.h.settings, clock=self.h.kernel.clock(), **kwargs)

    def test_a_fill_reports_sent_and_leaves_a_position(self):
        out = self.send()
        self.assertEqual(out["status"], "sent")
        self.assertEqual(len(self.h.fake.positions), 1)
        self.assertEqual(self.h.fake.positions[0]["magic"], MAGIC)

    def test_an_intent_is_written_before_the_request_goes_out(self):
        out = self.send()
        intent = self.h.store.load_intent(out["intent_id"])
        self.assertEqual(intent["status"], "sent")
        self.assertEqual(intent["symbol"], "EURUSD")
        self.assertEqual(len(intent["attempts"]), 1)

    def test_a_requote_is_retried_with_a_fresh_price(self):
        self.h.fake.script_send = [10004, 10009]
        out = self.send()
        self.assertEqual(out["status"], "sent")
        self.assertEqual(len(out["attempts"]), 2)
        self.assertEqual(out["attempts"][0]["name"], "REQUOTE")

    def test_an_unsupported_filling_mode_rotates_to_the_next_candidate(self):
        """10030 is the most common rejection after sizing; it must not end the
        attempt."""
        self.h.fake.script_send = [10030, 10009]
        out = self.send()
        self.assertEqual(out["status"], "sent")
        first, second = out["attempts"][0]["filling"], out["attempts"][1]["filling"]
        self.assertNotEqual(first, second)

    def test_a_timeout_is_ambiguous_and_never_retried(self):
        self.h.fake.script_send = [10012]
        out = self.send()
        self.assertEqual(out["status"], "ambiguous")
        self.assertIn("reconcile", out["message"].lower())
        self.assertIn(out["intent_id"],
                      [i["intent_id"] for i in self.h.store.unresolved_intents()])

    def test_a_closed_market_is_fatal_and_not_retried(self):
        self.h.fake.script_send = [10018, 10009]
        out = self.send()
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(len(out["attempts"]), 1)
        self.assertIn("closed", out["message"].lower())

    def test_no_money_is_not_resent(self):
        self.h.fake.script_send = [10019]
        out = self.send()
        self.assertEqual(len(out["attempts"]), 1)

    def test_a_bridge_failure_is_treated_as_ambiguous(self):
        """A dead IPC is not a rejection. The order may have reached the server, so
        it must be reconciled rather than resent."""
        original = self.h.bridge.call

        def failing(name, *args, **kwargs):
            if name == "order_send":
                raise RuntimeError("IPC died")
            return original(name, *args, **kwargs)

        self.h.kernel.clock()
        self.h.bridge.call = failing  # type: ignore[assignment]
        out = self.send()
        self.assertEqual(out["status"], "ambiguous")

    def test_every_attempt_is_recorded_in_the_journal(self):
        self.h.fake.script_send = [10018]
        self.send()
        kinds = [e["kind"] for e in self.h.store.events()]
        self.assertIn("order_intent", kinds)
        self.assertIn("order_rejected", kinds)


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.now = int(__import__("time").time())

    def test_a_matching_deal_means_it_executed(self):
        self.h.fake.deals = [make_deal(1, "EURUSD", 0.5, 1.08512, MAGIC, entry=0,
                                       position_id=555, when=self.now)]
        intent = {"intent_id": "i1", "symbol": "EURUSD", "volume": 0.5, "magic": MAGIC,
                  "sent_epoch": self.now - 5, "status": "ambiguous"}
        out = orders.reconcile(self.h.bridge, self.h.settings, intent)
        self.assertEqual(out["status"], "executed")
        self.assertEqual(len(out["matched_deals"]), 1)

    def test_a_deal_of_the_wrong_volume_does_not_count(self):
        self.h.fake.deals = [make_deal(1, "EURUSD", 0.1, 1.08512, MAGIC, entry=0,
                                       position_id=555, when=self.now)]
        intent = {"intent_id": "i1", "symbol": "EURUSD", "volume": 0.5, "magic": MAGIC,
                  "sent_epoch": self.now - 5}
        out = orders.reconcile(self.h.bridge, self.h.settings, intent)
        self.assertEqual(out["status"], "not_executed")

    def test_a_deal_from_another_magic_does_not_count(self):
        self.h.fake.deals = [make_deal(1, "EURUSD", 0.5, 1.08512, 999999, entry=0,
                                       position_id=555, when=self.now)]
        intent = {"intent_id": "i1", "symbol": "EURUSD", "volume": 0.5, "magic": MAGIC,
                  "sent_epoch": self.now - 5}
        self.assertEqual(orders.reconcile(self.h.bridge, self.h.settings,
                                          intent)["status"], "not_executed")

    def test_a_position_without_a_deal_yet_is_maybe_executed_not_missing(self):
        """A fill report can lag the position. Calling this 'not executed' is how a
        bot sends the same order twice."""
        self.h.fake.positions = [make_position(7, "EURUSD", "buy", 0.5, 1.08512, MAGIC)]
        intent = {"intent_id": "i1", "symbol": "EURUSD", "volume": 0.5, "magic": MAGIC,
                  "sent_epoch": self.now - 5}
        out = orders.reconcile(self.h.bridge, self.h.settings, intent)
        self.assertEqual(out["status"], "maybe_executed")
        self.assertIn("lag", out["reason"])

    def test_nothing_at_all_means_it_did_not_reach_the_market(self):
        intent = {"intent_id": "i1", "symbol": "EURUSD", "volume": 0.5, "magic": MAGIC,
                  "sent_epoch": self.now - 5}
        out = orders.reconcile(self.h.bridge, self.h.settings, intent)
        self.assertEqual(out["status"], "not_executed")

    def test_an_intent_without_a_timestamp_is_not_guessed_at(self):
        out = orders.reconcile(self.h.bridge, self.h.settings,
                               {"intent_id": "i1", "symbol": "EURUSD"})
        self.assertEqual(out["status"], "unknown")

    def test_reconcile_all_clears_the_blocking_intents(self):
        self.h.fake.deals = [make_deal(1, "EURUSD", 0.5, 1.08512, MAGIC, entry=0,
                                       position_id=555, when=self.now)]
        self.h.store.save_intent({"intent_id": "i1", "symbol": "EURUSD", "volume": 0.5,
                                  "magic": MAGIC, "sent_epoch": self.now - 5,
                                  "status": "ambiguous"})
        out = orders.reconcile_all(self.h.bridge, self.h.store, self.h.settings)
        self.assertEqual(out[0]["status"], "executed")
        self.assertEqual(self.h.store.unresolved_intents(), [])


if __name__ == "__main__":
    unittest.main()


