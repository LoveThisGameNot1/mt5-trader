import datetime
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import risk
from mt5trader.fixtures import btcusd, eurusd, make_account, make_position, make_terminal
from tests import harness

MAGIC = 770001


class RiskTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = None

    def make(self, env=None, **kwargs):
        self.h = harness.build(self.tmp.name, env=env if env is not None else harness.AUTONOMOUS,
                               **kwargs)
        return self.h

    def set_state(self, **fields):
        def apply(state):
            state.update(fields)
            return state
        return self.h.store.mutate(apply)

    def today(self):
        return self.h.kernel.clock().day_key()

    def check(self, plan=None, **overrides):
        plan = dict(plan or harness.clean_plan())
        plan.update(overrides)
        return self.h.kernel.check(plan)

    def codes(self, verdict):
        return verdict["failed_checks"]


class CleanPass(RiskTestCase):
    def test_a_sane_demo_plan_passes(self):
        h = self.make()
        verdict = self.check()
        self.assertEqual(verdict["verdict"], risk.PASS, verdict["blocking"])
        self.assertEqual(verdict["order"]["volume"], 0.5)
        self.assertAlmostEqual(verdict["order"]["risk_money"], 50.0, places=2)
        self.assertIsNotNone(verdict["fingerprint"])

    def test_every_check_is_reported_not_just_the_failures(self):
        self.make()
        verdict = self.check()
        names = {c["check"] for c in verdict["checks"]}
        for expected in ("live_account_guard", "terminal_autotrading",
                         "kill_switch", "unresolved_intents", "market_open", "spread",
                         "max_lots", "risk_per_trade", "total_risk", "daily_loss",
                         "drawdown", "orders_per_day", "orders_per_minute", "margin",
                         "stopless_positions", "loss_cooldown"):
            self.assertIn(expected, names)

    def test_the_verdict_carries_the_limits_actually_in_force(self):
        self.make()
        verdict = self.check()
        self.assertEqual(verdict["limits"]["hard_ceilings"], risk.HARD_CEILINGS)
        self.assertLessEqual(verdict["limits"]["per_trade_pct"],
                             risk.HARD_CEILINGS["risk_per_trade_pct"])


class EnvironmentGates(RiskTestCase):
    def test_propose_mode_still_validates_a_perfect_plan(self):
        """Autonomy decides who may send, not whether the plan is sound. Blocking
        validation in propose mode would make the one mode whose entire job is
        producing a plan unable to produce one."""
        self.make(env=harness.PROPOSE)
        verdict = self.check()
        self.assertEqual(verdict["verdict"], risk.PASS, verdict["blocking"])
        self.assertFalse(verdict["execution_gate"]["can_send"])
        self.assertEqual(verdict["execution_gate"]["autonomy"], "propose")
        self.assertIn("propose mode", verdict["execution_gate"]["reason"])

    def test_auto_mode_can_send_but_needs_a_confirmation(self):
        self.make(env=harness.CONFIRM)
        gate = self.check()["execution_gate"]
        self.assertTrue(gate["can_send"])
        self.assertTrue(gate["needs_confirm"])

    def test_full_mode_needs_no_confirmation(self):
        self.make()
        gate = self.check()["execution_gate"]
        self.assertTrue(gate["can_send"])
        self.assertFalse(gate["needs_confirm"])

    def test_a_real_account_is_read_only_without_the_switch(self):
        self.make(env=harness.AUTONOMOUS,
                  account=make_account(trade_mode=2))
        verdict = self.check()
        self.assertEqual(verdict["verdict"], risk.BLOCK)
        self.assertIn("live_account_guard", self.codes(verdict))
        self.assertIn("MT5_ALLOW_LIVE", " ".join(verdict["blocking"]))

    def test_a_real_account_is_allowed_when_the_operator_said_so(self):
        env = dict(harness.AUTONOMOUS, MT5_ALLOW_LIVE="1")
        self.make(env=env, account=make_account(trade_mode=2))
        verdict = self.check()
        self.assertEqual(verdict["verdict"], risk.PASS, verdict["blocking"])

    def test_algo_trading_off_blocks_with_the_local_fix_in_the_message(self):
        self.make()
        self.h.fake.trade_allowed = False
        verdict = self.check()
        self.assertIn("terminal_autotrading", self.codes(verdict))
        self.assertIn("Algo Trading", " ".join(verdict["blocking"]))

    def test_account_trading_disabled_blocks(self):
        self.make(account=make_account(trade_allowed=False))
        self.assertIn("account_trading", self.codes(self.check()))

    def test_a_disconnected_terminal_fails_closed(self):
        self.make()
        self.h.fake.terminal["connected"] = False
        self.h.fake.trade_allowed = False
        self.assertIn("terminal_autotrading", self.codes(self.check()))


class KillSwitch(RiskTestCase):
    def test_tripping_blocks_everything_after_it(self):
        self.make()
        self.assertEqual(self.check()["verdict"], risk.PASS)
        self.h.kernel.trip("test trip")
        verdict = self.check()
        self.assertEqual(verdict["verdict"], risk.BLOCK)
        self.assertIn("kill_switch", self.codes(verdict))
        self.assertIn("test trip", " ".join(verdict["blocking"]))

    def test_release_needs_the_environment_switch(self):
        self.make()
        self.h.kernel.trip("t")
        with self.assertRaises(PermissionError):
            self.h.kernel.release()

    def test_release_works_when_the_operator_allowed_it(self):
        env = dict(harness.AUTONOMOUS, MT5_ALLOW_KILL_RELEASE="1")
        self.make(env=env)
        self.h.kernel.trip("t")
        self.h.kernel.release("operator checked the account")
        self.assertEqual(self.check()["verdict"], risk.PASS)

    def test_the_trip_is_written_to_the_journal(self):
        self.make()
        self.h.kernel.trip("journal check")
        kinds = [e["kind"] for e in self.h.store.events()]
        self.assertIn("kill_switch_tripped", kinds)


class UnresolvedIntents(RiskTestCase):
    def test_an_unconfirmed_intent_blocks_new_exposure(self):
        """The order may already be live; opening on top of it is how a position
        gets doubled."""
        self.make()
        self.h.store.save_intent({"intent_id": "i1", "status": "ambiguous",
                                  "symbol": "EURUSD"})
        verdict = self.check()
        self.assertEqual(verdict["verdict"], risk.BLOCK)
        self.assertIn("unresolved_intents", self.codes(verdict))
        self.assertIn("i1", " ".join(verdict["blocking"]))


class PlanNormalisation(RiskTestCase):
    def test_a_missing_stop_blocks_when_required(self):
        self.make()
        verdict = self.check(volume=0.1, stop=None)
        self.assertIn("risk_per_trade", self.codes(verdict))

    def test_a_stopless_plan_cannot_be_sized_from_risk(self):
        """Without a stop there is no distance to divide the allowance by, and the
        error has to say that rather than complaining about a missing number."""
        self.make()
        verdict = self.check(stop=None)
        self.assertIn("plan", self.codes(verdict))
        self.assertIn("no stop loss cannot be sized", " ".join(verdict["blocking"]))

    def test_an_explicit_no_stop_is_a_warning_not_a_silent_pass(self):
        self.make()
        verdict = self.check(volume=0.1, stop=None, allow_no_sl=True)
        self.assertEqual(verdict["verdict"], risk.PASS, verdict["blocking"])
        self.assertTrue(any("unbounded" in w for w in verdict["warnings"]))

    def test_with_require_sl_disabled_a_stopless_order_passes(self):
        self.make(config={"guards": {"require_sl": False}})
        verdict = self.check(volume=0.1, stop=None)
        self.assertEqual(verdict["verdict"], risk.PASS, verdict["blocking"])

    def test_an_unsizeable_plan_blocks_with_the_arithmetic_in_the_message(self):
        self.make()
        verdict = self.check(risk_percent=None, risk_money=None, volume=None)
        self.assertEqual(verdict["verdict"], risk.BLOCK)
        self.assertIn("plan", self.codes(verdict))

    def test_a_buy_stop_above_the_entry_blocks(self):
        self.make()
        verdict = self.check(stop=1.09000)
        self.assertIn("plan", self.codes(verdict))

    def test_the_fingerprint_changes_when_anything_that_matters_changes(self):
        self.make()
        first = self.check()["fingerprint"]
        self.assertNotEqual(first, self.check(volume=0.4)["fingerprint"])
        self.assertNotEqual(first, self.check(stop=1.08312)["fingerprint"])
        self.assertEqual(first, self.check()["fingerprint"])

    def test_volume_is_normalised_not_taken_on_trust(self):
        self.make()
        verdict = self.check(volume=0.333, risk_percent=None)
        self.assertEqual(verdict["order"]["volume"], 0.33)

    def test_pending_order_type_is_inferred_from_the_side_and_the_price(self):
        self.make()
        below = self.check(kind="pending", price=1.08000, risk_percent=None, volume=0.1,
                           stop=None, allow_no_sl=True)
        self.assertEqual(below["order"]["type"], "limit")
        above = self.check(kind="pending", price=1.09000, risk_percent=None, volume=0.1,
                           stop=None, allow_no_sl=True)
        self.assertEqual(above["order"]["type"], "stop")


class InstrumentChecks(RiskTestCase):
    def test_a_disabled_symbol_blocks(self):
        self.make(symbols=[dict(eurusd(), trade_mode=0)])
        self.assertIn("market_open", self.codes(self.check()))

    def test_a_long_only_symbol_blocks_a_sell(self):
        self.make(symbols=[dict(eurusd(), trade_mode=1)])
        verdict = self.check(side="sell", stop=1.08612)
        self.assertIn("side_allowed", self.codes(verdict))

    def test_a_blocked_symbol_blocks(self):
        self.make(config={"guards": {"blocked_symbols": ["EURUSD"]}})
        self.assertIn("blocked_symbol", self.codes(self.check()))

    def test_a_side_not_in_allowed_sides_blocks(self):
        self.make(config={"guards": {"allowed_sides": ["sell"]}})
        self.assertIn("config_side", self.codes(self.check()))

    def test_a_wide_spread_blocks_a_market_order(self):
        self.make(config={"execution": {"max_spread_points": 5}})
        verdict = self.check()
        self.assertIn("spread", self.codes(verdict))
        self.assertIn("12", " ".join(verdict["blocking"]))

    def test_a_market_order_on_a_symbol_with_no_quotes_is_refused_in_plain_words(self):
        from mt5trader.fixtures import make_symbol
        self.make(symbols=[make_symbol("NOQUOTES", bid=0, ask=0)])
        verdict = self.check(symbol="NOQUOTES")
        self.assertIn("plan", self.codes(verdict))
        blocking = " ".join(verdict["blocking"])
        self.assertIn("no live bid or ask", blocking)
        self.assertIn("pending order", blocking)

    def test_quotes_vanishing_between_plan_and_execution_blocks_before_the_send(self):
        """A plan made on a live price and executed after the session closed. The
        plan still normalises, so this is the check that catches it."""
        from mt5trader.fixtures import make_symbol
        self.make(symbols=[make_symbol("NOQUOTES", bid=0, ask=0)])
        verdict = self.check(symbol="NOQUOTES", price=1.08512, risk_percent=None,
                             volume=0.1, stop=1.08412)
        self.assertIn("quotes", self.codes(verdict))
        blocking = " ".join(verdict["blocking"])
        self.assertIn("no live bid or ask", blocking)
        self.assertIn("pending order does not need quotes", blocking)
        self.assertNotIn("None points wide", blocking)

    def test_a_stale_tick_blocks_even_though_a_bid_and_an_ask_exist(self):
        """A Friday tick still has a bid and an ask and still shows a one-point
        spread, so on a Sunday the market looks open until the broker refuses the
        order. Freshness is the check that catches it."""
        self.make()
        self.h.fake._tick_time = time.time() - 40 * 3600
        verdict = self.check()
        self.assertIn("quote_freshness", self.codes(verdict))
        blocking = " ".join(verdict["blocking"])
        self.assertIn("hours old", blocking)
        self.assertIn("MARKET_CLOSED", blocking)

    def test_a_fresh_tick_passes_the_freshness_check(self):
        self.make()
        self.h.fake._tick_time = time.time()
        self.assertNotIn("quote_freshness", self.codes(self.check()))

    def test_the_freshness_threshold_can_be_tightened(self):
        self.make(config={"guards": {"max_quote_age_seconds": 1}})
        self.h.fake._tick_time = time.time() - 5
        self.assertIn("quote_freshness", self.codes(self.check()))

    def test_the_freshness_threshold_cannot_be_loosened_past_its_ceiling(self):
        self.make(config={"guards": {"max_quote_age_seconds": 999999}})
        self.assertEqual(self.h.settings.guards["max_quote_age_seconds"],
                         risk.HARD_CEILINGS["max_quote_age_seconds"])
        self.assertTrue(any("max_quote_age_seconds" in n for n in self.h.settings.notes))

    def test_a_pending_order_is_not_checked_for_freshness(self):
        """It rests until the price arrives, so a closed session is exactly when a
        breakout order is meant to be placed."""
        self.make()
        self.h.fake._tick_time = time.time() - 40 * 3600
        verdict = self.check(kind="pending", type="buy_stop", price=1.09500,
                             volume=0.1, stop=None, allow_no_sl=True)
        self.assertNotIn("quote_freshness", self.codes(verdict))
        self.assertNotIn("quotes", self.codes(verdict))

    def test_a_pending_order_does_not_check_the_spread(self):
        self.make(config={"execution": {"max_spread_points": 1}})
        verdict = self.check(kind="pending", price=1.08000, risk_percent=None,
                             volume=0.1, stop=None, allow_no_sl=True)
        self.assertNotIn("spread", self.codes(verdict))

    def test_a_blocked_server_hour_blocks(self):
        now_hour = datetime.datetime.utcnow().hour
        self.make(config={"guards": {"blocked_server_hours": [now_hour]}})
        self.assertIn("server_hour", self.codes(self.check()))

    def test_a_blackout_window_blocks(self):
        now = datetime.datetime.utcnow()
        window = {"start": (now - datetime.timedelta(minutes=2)).strftime("%H:%M"),
                  "end": (now + datetime.timedelta(minutes=2)).strftime("%H:%M"),
                  "label": "CPI release"}
        self.make(config={"guards": {"news_blackout": [window]}})
        verdict = self.check()
        self.assertIn("news_blackout", self.codes(verdict))
        self.assertIn("CPI release", " ".join(verdict["blocking"]))

    def test_a_symbol_the_terminal_does_not_have_is_a_clear_error(self):
        self.make()
        verdict = self.check(symbol="NOTHERE")
        self.assertEqual(verdict["verdict"], risk.BLOCK)
        self.assertIn("no symbol matching", " ".join(verdict["blocking"]))


class SizeAndRiskLimits(RiskTestCase):
    def test_over_the_per_trade_limit_blocks_and_names_both_numbers(self):
        self.make()
        verdict = self.check(risk_percent=2.0)  # $200 against a $50 allowance
        self.assertIn("risk_per_trade", self.codes(verdict))
        self.assertIn("200", " ".join(verdict["blocking"]))

    def test_over_the_lot_limit_blocks(self):
        self.make(config={"risk": {"max_lots_per_order": 0.2}})
        self.assertIn("max_lots", self.codes(self.check()))

    def test_the_lot_ceiling_is_enforced_even_if_the_config_is_absurd(self):
        self.make(config={"risk": {"max_lots_per_order": 9999}})
        # the config is clamped down to the hard ceiling at load time
        self.assertEqual(self.h.settings.risk["max_lots_per_order"],
                         risk.HARD_CEILINGS["max_lots_per_order"])

    def test_a_bad_reward_risk_blocks(self):
        self.make()
        # 100 points of risk, 50 points of reward: 0.5R against a 1.0R minimum
        verdict = self.check(target=1.08562, stop=1.08412)
        self.assertIn("reward_risk", self.codes(verdict))

    def test_a_missing_target_is_not_checked_at_all(self):
        self.make()
        self.assertNotIn("reward_risk", self.codes(self.check()))


class PortfolioLimits(RiskTestCase):
    def position(self, ticket=1, symbol="EURUSD", **kwargs):
        return make_position(ticket, symbol, "buy", 0.1, 1.085, MAGIC, **kwargs)

    def test_the_position_count_limit_blocks(self):
        positions = [self.position(ticket=i, symbol="XAUUSD") for i in range(1, 6)]
        self.make(positions=positions)  # default limit is 5
        self.assertIn("open_positions", self.codes(self.check()))

    def test_a_second_position_on_the_same_symbol_blocks(self):
        self.make(positions=[self.position()])
        verdict = self.check()
        self.assertIn("positions_per_symbol", self.codes(verdict))
        self.assertIn("losing position", " ".join(verdict["blocking"]))

    def test_a_position_without_a_stop_blocks_new_trades(self):
        """Its risk is not measurable, so it is treated as eating the whole
        allowance. Anything else is pretending to know a number nobody knows."""
        self.make(positions=[make_position(1, "XAUUSD", "buy", 0.1, 2400.0, MAGIC)])
        verdict = self.check()
        self.assertIn("stopless_positions", self.codes(verdict))

    def test_a_stopless_position_can_be_tolerated_by_config(self):
        self.make(positions=[make_position(1, "XAUUSD", "buy", 0.1, 2400.0, MAGIC)],
                  config={"guards": {"treat_stopless_as_full_risk": False}})
        self.assertNotIn("stopless_positions", self.codes(self.check()))

    def test_total_open_risk_is_added_up(self):
        # 0.5 lots on gold with a $10 stop is $500 at risk, over the 2% of 10000 total
        self.make(positions=[make_position(2, "XAUUSD", "buy", 0.5, 2400.0, MAGIC,
                                           sl=2390.0)])
        verdict = self.check()
        self.assertIn("total_risk", self.codes(verdict))
        self.assertAlmostEqual(verdict["open_risk"]["total"], 500.0, places=2)

    def test_foreign_positions_are_visible_but_do_not_count(self):
        self.make(positions=[make_position(3, "XAUUSD", "buy", 1.0, 2400.0, 999999)])
        verdict = self.check()
        self.assertEqual(verdict["positions"], 0)
        self.assertEqual(len(verdict["foreign_positions"]), 1)
        self.assertEqual(verdict["open_risk"]["total"], 0.0)


class DayLimits(RiskTestCase):
    def anchor(self, equity, day_start, peak=None):
        self.h.kernel.day_state()
        self.set_state(day_start_equity=day_start, peak_equity=peak or day_start)
        self.h.fake.account["equity"] = equity
        self.h.fake.account["margin_free"] = equity

    def test_the_daily_loss_limit_blocks_and_trips_the_switch(self):
        self.make()
        self.anchor(equity=9700.0, day_start=10000.0)
        verdict = self.check()
        self.assertIn("daily_loss", self.codes(verdict))
        self.assertIn("kill_switch", self.codes(verdict))
        self.assertTrue(self.h.store.load()["kill_switch"])
        self.assertIn("daily loss limit", self.h.store.load()["kill_reason"])

    def test_a_small_loss_does_not_block(self):
        self.make()
        self.anchor(equity=9950.0, day_start=10000.0)
        self.assertEqual(self.check()["verdict"], risk.PASS)

    def test_the_drawdown_limit_blocks(self):
        self.make()
        self.anchor(equity=8800.0, day_start=9950.0, peak=10000.0)  # 12% against 10%
        verdict = self.check()
        self.assertIn("drawdown", self.codes(verdict))

    def test_orders_per_day_blocks(self):
        self.make()
        self.h.kernel.day_state()
        self.set_state(orders_today=self.h.settings.risk["orders_per_day"])
        self.assertIn("orders_per_day", self.codes(self.check()))

    def test_the_per_minute_rate_limit_blocks_a_runaway_loop(self):
        self.make()
        self.h.kernel.day_state()
        now = time.time()
        self.set_state(order_times=[now - 5, now - 10, now - 20], orders_today=3)
        verdict = self.check()
        self.assertIn("orders_per_minute", self.codes(verdict))
        self.assertIn("talking to itself", " ".join(verdict["blocking"]))

    def test_the_loss_cooldown_blocks_revenge_trading(self):
        self.make()
        self.h.kernel.day_state()
        self.set_state(last_loss_ts=int(time.time()) - 30)
        verdict = self.check()
        self.assertIn("loss_cooldown", self.codes(verdict))
        self.assertIn("bad day", " ".join(verdict["blocking"]))

    def test_the_cooldown_expires(self):
        self.make(config={"risk": {"loss_cooldown_seconds": 10}})
        self.h.kernel.day_state()
        self.set_state(last_loss_ts=int(time.time()) - 60)
        self.assertNotIn("loss_cooldown", self.codes(self.check()))

    def test_a_duplicate_right_after_a_send_is_blocked(self):
        self.make(positions=[make_position(4, "EURUSD", "buy", 0.1, 1.085, MAGIC)])
        self.h.kernel.day_state()
        self.set_state(last_order_ts=int(time.time()) - 10)
        verdict = self.check()
        self.assertIn("duplicate", self.codes(verdict))

    def test_a_new_trading_day_re_anchors_the_counters(self):
        self.make()
        self.h.kernel.day_state()
        self.set_state(day="1999-01-01", orders_today=9, day_start_equity=1234.0)
        day = self.h.kernel.day_state()
        self.assertEqual(day["orders_today"], 0)
        self.assertAlmostEqual(day["day_start_equity"], 10000.0, places=2)


class MarginCheck(RiskTestCase):
    def test_insufficient_free_margin_blocks(self):
        self.make(account=make_account(margin_free=300.0, equity=10000.0))
        verdict = self.check()
        self.assertIn("margin", self.codes(verdict))

    def test_the_margin_figure_is_reported(self):
        self.make()
        verdict = self.check()
        # 0.5 lots x 100000 contract x the ask 1.08512 / leverage 100
        self.assertAlmostEqual(verdict["margin"]["margin_required"], 542.56, places=2)
        self.assertTrue(verdict["margin"]["fits"])


class Reporting(RiskTestCase):
    def test_the_report_states_what_autonomy_permits(self):
        self.make(env=harness.PROPOSE)
        report = self.h.kernel.report()
        self.assertEqual(report["autonomy"], "propose")
        self.assertIn("no order tool", report["autonomy_meaning"])

    def test_the_report_lists_the_ceiling_next_to_every_limit(self):
        self.make()
        report = self.h.kernel.report()
        self.assertEqual(report["limits"]["hard_ceilings"]["risk_per_trade_pct"], 2.0)

    def test_the_clock_caveat_is_stated(self):
        self.make()
        self.assertIn("server midnight", self.h.kernel.report()["clock"]["caveat"])


if __name__ == "__main__":
    unittest.main()
