import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import history, instruments, news
from tests import harness

SERVER_OFFSET = 3 * 3600


def event(minutes_from_now, name="US Non-Farm Payrolls", currency="USD",
          importance=2, actual=None, forecast=180.0, previous=175.0):
    when = int(time.time()) + SERVER_OFFSET + minutes_from_now * 60
    return {"id": abs(hash(name)) % 9999, "event_id": 1, "time": when, "name": name,
            "code": name, "country": "US", "currency": currency,
            "importance": importance, "importance_raw": importance + 1,
            "importance_name": news.importance_name(importance), "unit": 0,
            "digits": 1, "multiplier": 0, "period": 0, "actual": actual,
            "forecast": forecast, "previous": previous}


def write_calendar(folder, events, generated_seconds_ago=0, interval=5):
    path = pathlib.Path(folder) / "mt5trader_calendar.json"
    payload = {
        "generated_server": int(time.time()) + SERVER_OFFSET - generated_seconds_ago,
        "server_now": "2026-10-18 15:48",
        "interval_minutes": interval,
        "terminal_build": 6230,
        "connected": True,
        "source": "terminal economic calendar via MQL5",
        "events": events,
        "event_count": len(events),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Locating(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.clock = history.Clock(offset_seconds=SERVER_OFFSET, source="test")

    def test_the_environment_override_wins(self):
        path = write_calendar(self.tmp.name, [event(30)])
        self.h.settings.env["MT5_CALENDAR_FILE"] = str(path)
        found, exists, source = news.locate(self.h.settings, {})
        self.assertEqual(found, path)
        self.assertTrue(exists)
        self.assertEqual(source, "configured")

    def test_the_terminal_common_folder_is_used_when_there_is_no_override(self):
        common = pathlib.Path(self.tmp.name) / "common"
        (common / "Files").mkdir(parents=True)
        write_calendar(common / "Files", [event(30)])
        found, exists, source = news.locate(
            self.h.settings, {"commondata_path": str(common), "data_path": str(self.tmp.name)})
        self.assertTrue(exists)
        self.assertIn("common", source)

    def test_the_terminal_own_folder_is_the_second_choice(self):
        own = pathlib.Path(self.tmp.name) / "own"
        (own / "MQL5" / "Files").mkdir(parents=True)
        write_calendar(own / "MQL5" / "Files", [event(30)])
        found, exists, source = news.locate(self.h.settings, {"data_path": str(own)})
        self.assertTrue(exists)
        self.assertIn("data folder", source)

    def test_missing_is_reported_with_the_path_that_was_looked_in(self):
        found, exists, source = news.locate(
            self.h.settings, {"commondata_path": str(self.tmp.name)})
        self.assertFalse(exists)
        self.assertEqual(source, "not found")

    def test_a_broken_file_is_a_state_not_a_crash(self):
        path = pathlib.Path(self.tmp.name) / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        data, error = news.load(path)
        self.assertIsNone(data)
        self.assertIn("could not be read", error)

    def test_a_file_of_the_wrong_shape_is_recognised(self):
        path = pathlib.Path(self.tmp.name) / "wrong.json"
        path.write_text('{"hello": 1}', encoding="utf-8")
        data, error = news.load(path)
        self.assertIsNone(data)
        self.assertIn("does not look like", error)

    def test_status_reports_age_and_staleness(self):
        path = write_calendar(self.tmp.name, [event(30)], generated_seconds_ago=60)
        self.h.settings.env["MT5_CALENDAR_FILE"] = str(path)
        status = news.status(self.h.settings, {}, self.clock)
        self.assertTrue(status["available"])
        self.assertFalse(status["stale"])
        self.assertAlmostEqual(status["age_seconds"], 60, delta=5)

    def test_a_stale_export_is_flagged(self):
        path = write_calendar(self.tmp.name, [event(30)], generated_seconds_ago=7200)
        self.h.settings.env["MT5_CALENDAR_FILE"] = str(path)
        status = news.status(self.h.settings, {}, self.clock)
        self.assertTrue(status["stale"])
        self.assertIn("stale", status)


class Normalising(unittest.TestCase):
    def test_events_are_sorted_by_time_then_importance(self):
        raw = {"events": [event(120, importance=2), event(30, importance=0),
                          event(30, importance=2)]}
        out = news.normalise(raw)
        self.assertEqual([e["time"] for e in out], sorted(e["time"] for e in out))
        self.assertEqual(out[0]["importance"], 2)

    def test_an_event_without_a_time_is_dropped(self):
        raw = {"events": [{"name": "broken"}, event(30)]}
        self.assertEqual(len(news.normalise(raw)), 1)

    def test_importance_levels_round_trip(self):
        self.assertEqual(news.importance_level("high"), 2)
        self.assertEqual(news.importance_level("MODERATE"), 1)
        self.assertEqual(news.importance_level("low"), 0)
        self.assertEqual(news.importance_level(2), 2)
        self.assertEqual(news.importance_name(2), "high")


class Windows(unittest.TestCase):
    def setUp(self):
        self.now_server = int(time.time()) + SERVER_OFFSET

    def test_an_event_inside_the_window_is_caught(self):
        events = news.normalise({"events": [event(5)]})
        hits = news.active_blackout(events, self.now_server, 15, 15, 2, {"USD"})
        self.assertEqual(len(hits), 1)
        self.assertIn("Non-Farm", hits[0]["name"])

    def test_an_event_just_outside_the_window_is_not(self):
        events = news.normalise({"events": [event(40)]})
        self.assertIsNone(news.active_blackout(events, self.now_server, 15, 15, 2, {"USD"}))

    def test_the_window_symmetrically_covers_after_the_release(self):
        events = news.normalise({"events": [event(-10)]})
        self.assertIsNotNone(news.active_blackout(events, self.now_server, 15, 15, 2, {"USD"}))
        self.assertIsNone(news.active_blackout(events, self.now_server, 15, 2, 2, {"USD"}))

    def test_another_currency_does_not_block(self):
        events = news.normalise({"events": [event(5, currency="JPY")]})
        self.assertIsNone(news.active_blackout(events, self.now_server, 15, 15, 2, {"EUR", "USD"}))

    def test_a_low_importance_event_does_not_block(self):
        events = news.normalise({"events": [event(5, importance=0)]})
        self.assertIsNone(news.active_blackout(events, self.now_server, 15, 15, 2, {"USD"}))

    def test_upcoming_filters_by_horizon_and_marks_the_distance(self):
        events = news.normalise({"events": [event(30), event(600), event(5000)]})
        out = news.within(events, self.now_server, hours_ahead=48, min_importance=2,
                          currencies={"USD"})
        self.assertEqual(len(out), 2)
        self.assertTrue(all("in_hours" in e for e in out))


class Currencies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)

    def load(self, name):
        return instruments.load(self.h.bridge, self.h.settings, name)[0]

    def test_a_pair_depends_on_both_of_its_currencies(self):
        self.assertEqual(news.currencies_for(self.load("EURUSD")), {"EUR", "USD"})

    def test_gold_is_quoted_in_dollars_and_counted_in_ounces(self):
        self.assertEqual(news.currencies_for(self.load("XAUUSD")), {"XAU", "USD"})

    def test_an_index_cfd_is_a_dollar_instrument(self):
        self.assertEqual(news.currencies_for(self.load("US30")), {"USD"})

    def test_none_is_an_empty_set_not_an_error(self):
        self.assertEqual(news.currencies_for(None), set())


class KernelCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, guard="enforce", events=None, generated_seconds_ago=0, **config):
        guards = {"calendar": guard}
        guards.update(config)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS,
                               config={"guards": guards})
        # A broker at UTC+3, so the kernel's measured offset matches the server
        # clock the calendar events are written in. Without this the window test
        # would be three hours out and would pass for the wrong reason.
        self.h.fake._tick_time = time.time() + SERVER_OFFSET
        self.h.reconnect()
        if events is not None:
            path = write_calendar(self.tmp.name, events,
                                  generated_seconds_ago=generated_seconds_ago)
            self.h.settings.env["MT5_CALENDAR_FILE"] = str(path)
        return self.h

    def codes(self, verdict):
        return verdict["failed_checks"]

    def test_an_event_window_blocks_a_market_order_under_enforce(self):
        self.build(events=[event(5, name="US CPI", currency="USD")])
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertIn("event_blackout", self.codes(verdict))
        blocking = " ".join(verdict["blocking"])
        self.assertIn("US CPI", blocking)
        self.assertIn("slippage", blocking)

    def test_the_same_window_only_warns_under_warn(self):
        self.build(guard="warn", events=[event(5, name="US CPI")])
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertEqual(verdict["verdict"], "PASS", verdict["blocking"])
        self.assertTrue(any("US CPI" in w for w in verdict["warnings"]))

    def test_a_missing_calendar_fails_closed_under_enforce(self):
        """Enable enforcement and forget the service, and the kernel says so instead
        of deciding there is no news."""
        self.build()
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertIn("event_blackout", self.codes(verdict))
        self.assertIn("calendar", " ".join(verdict["blocking"]).lower())

    def test_a_missing_calendar_is_only_a_warning_under_warn(self):
        self.build(guard="warn")
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertEqual(verdict["verdict"], "PASS", verdict["blocking"])
        self.assertTrue(any("event_blackout" in w for w in verdict["warnings"]))
        # The finding is still recorded as a failed check, because it is a finding.
        # What warn changes is whether it blocks.
        self.assertIn("event_blackout", self.codes(verdict))
        self.assertFalse(any("event_blackout" in b for b in verdict["blocking"]))

    def test_a_stale_export_fails_closed_under_enforce(self):
        self.build(events=[event(5)], generated_seconds_ago=7200)
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertIn("event_blackout", self.codes(verdict))
        self.assertIn("stopped", " ".join(verdict["blocking"]))

    def test_turning_the_calendar_off_skips_the_check_entirely(self):
        self.build(guard="off")
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertNotIn("event_blackout", self.codes(verdict))

    def test_an_event_for_an_unrelated_currency_does_not_block(self):
        self.build(events=[event(5, name="JP CPI", currency="JPY")])
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertNotIn("event_blackout", self.codes(verdict))

    def test_a_quiet_calendar_passes_and_says_what_it_checked(self):
        self.build(events=[event(400, name="US CPI")])
        verdict = self.h.kernel.check(harness.clean_plan())
        entry = next(c for c in verdict["checks"] if c["check"] == "event_blackout")
        self.assertTrue(entry["ok"])
        self.assertIn("EUR", entry["currencies"])
        self.assertEqual(entry["calendar_age_seconds"] <= 5, True)

    def test_a_pending_order_is_blocked_too(self):
        """A buy stop placed before a release triggers during it, which is the case
        the window exists for."""
        self.build(events=[event(5, name="US CPI")])
        verdict = self.h.kernel.check({
            "symbol": "EURUSD", "side": "buy", "kind": "pending", "type": "stop",
            "price": 1.09500, "volume": 0.1, "allow_no_sl": True})
        self.assertIn("event_blackout", self.codes(verdict))

    def test_the_importance_threshold_is_honoured(self):
        self.build(events=[event(5, importance=1, name="Mid-tier data")],
                   calendar_min_importance="high")
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertNotIn("event_blackout", self.codes(verdict))

    def test_lowering_the_threshold_catches_moderate_events(self):
        self.build(events=[event(5, importance=1, name="Mid-tier data")],
                   calendar_min_importance="moderate")
        verdict = self.h.kernel.check(harness.clean_plan())
        self.assertIn("event_blackout", self.codes(verdict))


if __name__ == "__main__":
    unittest.main()
