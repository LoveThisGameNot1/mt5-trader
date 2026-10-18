import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader.server import PROTOCOL_VERSION, Server
from tests import harness

MAGIC = 770001


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, env=None, **kwargs):
        h = harness.build(self.tmp.name, env=env if env is not None else harness.PROPOSE,
                          **kwargs)
        self.h = h
        self.server = Server(settings=h.settings, bridge=h.bridge, store=h.store)
        return self.server

    def call(self, name, arguments=None):
        result = self.server.call_tool(name, arguments or {})
        text = result["content"][0]["text"]
        return json.loads(text), result["isError"]


class Protocol(ServerTestCase):
    def test_initialize_advertises_the_protocol_and_the_name(self):
        self.build()
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": PROTOCOL_VERSION}})
        self.assertEqual(response["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(response["result"]["serverInfo"]["name"], "mt5-trader")
        self.assertIn("mt5_status", response["result"]["instructions"])

    def test_the_client_may_ask_for_another_protocol_version_and_gets_it_echoed(self):
        self.build()
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")

    def test_a_notification_gets_no_response(self):
        self.build()
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0",
                                              "method": "notifications/initialized"}))

    def test_tools_list_carries_a_schema_for_every_tool(self):
        self.build()
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = response["result"]["tools"]
        self.assertGreaterEqual(len(tools), 20)
        for tool in tools:
            self.assertTrue(tool["name"].startswith("mt5_"), tool)
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertGreater(len(tool["description"]), 30, tool["name"])

    def test_every_declared_required_argument_exists_in_the_schema(self):
        self.build()
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for tool in response["result"]["tools"]:
            properties = tool["inputSchema"].get("properties", {})
            for name in tool["inputSchema"].get("required", []):
                self.assertIn(name, properties, "%s requires %s but does not declare it"
                              % (tool["name"], name))

    def test_ping(self):
        self.build()
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 3,
                                             "method": "ping"})["result"], {})

    def test_an_unknown_method_is_a_json_rpc_error(self):
        self.build()
        response = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_an_unknown_tool_is_an_error_result_not_a_crash(self):
        self.build()
        payload, is_error = self.call("mt5_nothing")
        self.assertTrue(is_error)
        self.assertIn("unknown tool", payload["error"])
        self.assertIn("mt5_status", payload["error"])

    def test_stdout_carries_only_json_lines(self):
        """A stray print breaks the client's parser in a way that looks like a
        crash on the client side."""
        self.build()
        stdin = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "",
            "this is not json",
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        ]))
        stdout = io.StringIO()
        self.server.serve(stdin=stdin, stdout=stdout)
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            payload = json.loads(line)
            self.assertEqual(payload["jsonrpc"], "2.0")


class ReadTools(ServerTestCase):
    def test_status_answers_with_the_gate_summary(self):
        self.build()
        payload, is_error = self.call("mt5_status")
        self.assertFalse(is_error)
        self.assertEqual(payload["server"]["name"], "mt5-trader")
        self.assertIn("autonomy", payload["gates"])
        self.assertFalse(payload["gates"]["can_place_orders"])

    def test_status_asserts_the_symbol_field_names_against_the_terminal(self):
        """""The only thing that catches a terminal build that renamed a field, and the
        difference between a loud failure and a silent zero."""""
        self.build()
        payload, _ = self.call("mt5_status")
        schema = payload["symbol_schema"]
        self.assertTrue(schema["ok"], schema["required_missing"])
        self.assertIn("trade_tick_size", schema["present"])
        self.assertEqual(schema["required_missing"], [])

    def test_status_reports_a_broken_terminal_instead_of_raising(self):
        """The doctor skill has to work when nothing works, otherwise it is useless
        exactly when it is needed."""
        self.build()
        self.h.fake.account = None
        payload, is_error = self.call("mt5_status")
        self.assertFalse(is_error)
        self.assertFalse(payload["connected"])
        self.assertIn("MetaTrader 5", " ".join(payload["problems"]))

    def test_symbol_returns_metadata_that_decides_sizing(self):
        self.build()
        payload, is_error = self.call("mt5_symbol", {"symbol": "XAUUSD"})
        self.assertFalse(is_error)
        self.assertEqual(payload["instrument"]["symbol"], "XAUUSD")
        self.assertAlmostEqual(payload["instrument"]["value_per_point_per_lot"], 1.0,
                               places=6)

    def test_quote_reports_the_spread_in_points(self):
        self.build()
        payload, _ = self.call("mt5_quote", {"symbols": ["EURUSD", "US30"]})
        quotes = {q["symbol"]: q for q in payload["quotes"]}
        self.assertEqual(quotes["EURUSD"]["spread_points"], 12)
        self.assertEqual(quotes["US30"]["spread_points"], 30)

    def test_risk_status_lists_the_ceilings_next_to_the_limits(self):
        self.build()
        payload, _ = self.call("mt5_risk_status")
        self.assertEqual(payload["limits"]["hard_ceilings"]["risk_per_trade_pct"], 2.0)
        self.assertEqual(payload["autonomy"], "propose")

    def test_positions_separates_foreign_positions(self):
        from mt5trader.fixtures import make_position
        self.build(positions=[make_position(5, "EURUSD", "buy", 0.1, 1.085, 999999)])
        payload, _ = self.call("mt5_positions", {"include_foreign": True})
        self.assertEqual(payload["count"], 0)
        self.assertEqual(len(payload["foreign"]), 1)

    def test_a_bad_timeframe_is_an_error_with_the_list_of_good_ones(self):
        self.build()
        payload, is_error = self.call("mt5_rates", {"symbol": "EURUSD",
                                                    "timeframe": "M7"})
        self.assertTrue(is_error)
        self.assertIn("H1", payload["error"])

    def test_rates_arrive_as_bars_with_a_close_price(self):
        self.build()
        payload, is_error = self.call("mt5_rates", {"symbol": "EURUSD",
                                                    "timeframe": "H1", "count": 30})
        self.assertFalse(is_error)
        self.assertEqual(payload["bars"], 30)
        first = payload["rates"][0]
        for field in ("time", "open", "high", "low", "close", "tick_volume", "spread"):
            self.assertIn(field, first, field)
        self.assertIsInstance(first["close"], float)
        self.assertIsInstance(first["tick_volume"], int)

    def test_size_refuses_without_a_risk_budget(self):
        self.build()
        payload, is_error = self.call("mt5_size", {"symbol": "EURUSD", "side": "buy",
                                                   "stop": 1.08412})
        self.assertTrue(is_error)
        self.assertIn("risk_money", payload["error"])

    def test_size_reports_the_minimum_possible_risk_when_a_budget_is_too_small(self):
        self.build()
        payload, _ = self.call("mt5_size", {"symbol": "BIG", "side": "buy",
                                            "entry": 100.5, "stop": 99.5,
                                            "risk_money": 3})
        self.assertIsNone(payload["lots"])
        self.assertAlmostEqual(payload["minimum_possible_risk"], 5.0, places=2)


class PlanningTools(ServerTestCase):
    def test_validate_passes_in_propose_mode_and_sends_nothing(self):
        self.build()
        payload, is_error = self.call("mt5_validate", harness.clean_plan())
        self.assertFalse(is_error)
        self.assertEqual(payload["verdict"], "PASS", payload["blocking"])
        self.assertFalse(payload["execution_gate"]["can_send"])
        self.assertEqual(self.h.fake.sent, [])

    def test_plan_stores_a_plan_even_in_propose_mode(self):
        """Propose mode is the mode whose whole job is producing a plan."""
        self.build()
        payload, _ = self.call("mt5_plan", harness.clean_plan())
        self.assertTrue(payload["stored"])
        self.assertIsNotNone(payload["plan_id"])
        self.assertTrue(self.h.store.load_plan(payload["plan_id"]))
        self.assertIn("propose", payload["next"])

    def test_validate_reports_every_failed_check(self):
        self.build()
        payload, _ = self.call("mt5_validate", harness.clean_plan(risk_percent=9.0))
        self.assertEqual(payload["verdict"], "BLOCK")
        self.assertIn("risk_per_trade", payload["failed_checks"])
        self.assertTrue(payload["blocking"])

    def test_a_stored_plan_keeps_its_fingerprint(self):
        self.build()
        payload, _ = self.call("mt5_plan", harness.clean_plan())
        stored = self.h.store.load_plan(payload["plan_id"])
        self.assertEqual(stored["fingerprint"], payload["fingerprint"])


class OrderGate(ServerTestCase):
    def test_propose_mode_refuses_to_send_and_says_why(self):
        self.build(env=harness.PROPOSE)
        payload, _ = self.call("mt5_order", harness.clean_plan())
        self.assertFalse(payload["sent"])
        self.assertEqual(payload["verdict"], "PASS")
        self.assertIn("propose mode", payload["message"])
        self.assertEqual(self.h.fake.sent, [])

    def test_auto_mode_needs_a_confirmation(self):
        self.build(env=harness.CONFIRM)
        payload, _ = self.call("mt5_order", harness.clean_plan())
        self.assertFalse(payload["sent"])
        self.assertTrue(payload["needs_confirmation"])
        self.assertEqual(self.h.fake.sent, [])

    def test_auto_mode_sends_with_a_confirmation(self):
        self.build(env=harness.CONFIRM)
        payload, _ = self.call("mt5_order", dict(harness.clean_plan(), confirm=True))
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(len(self.h.fake.sent), 1)

    def test_full_autonomy_sends_on_the_kernel_alone(self):
        self.build(env=harness.AUTONOMOUS)
        payload, _ = self.call("mt5_order", harness.clean_plan())
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(len(self.h.fake.positions), 1)
        self.assertEqual(self.h.fake.positions[0]["magic"], self.h.settings.magic)

    def test_a_blocked_plan_is_never_sent_and_has_no_override(self):
        self.build(env=harness.AUTONOMOUS)
        payload, _ = self.call("mt5_order", dict(harness.clean_plan(risk_percent=9.0),
                                                 confirm=True, force=True))
        self.assertFalse(payload["sent"])
        self.assertIn("blocked by the risk kernel", payload["message"])
        self.assertEqual(self.h.fake.sent, [])

    def test_a_plan_id_whose_fingerprint_changed_is_refused(self):
        self.build(env=harness.AUTONOMOUS)
        plan, _ = self.call("mt5_plan", harness.clean_plan())
        payload, _ = self.call("mt5_order", dict(harness.clean_plan(risk_percent=0.4),
                                                 plan_id=plan["plan_id"], confirm=True))
        self.assertFalse(payload["sent"])
        self.assertIn("no longer matches", payload["message"])
        self.assertEqual(self.h.fake.sent, [])

    def test_a_plan_id_that_still_matches_is_sent(self):
        self.build(env=harness.AUTONOMOUS)
        plan, _ = self.call("mt5_plan", harness.clean_plan())
        payload, _ = self.call("mt5_order", dict(harness.clean_plan(),
                                                 plan_id=plan["plan_id"], confirm=True))
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(len(self.h.fake.sent), 1)

    def test_an_unknown_plan_id_is_an_error(self):
        self.build(env=harness.AUTONOMOUS)
        payload, is_error = self.call("mt5_order", dict(harness.clean_plan(),
                                                        plan_id="plan-nope"))
        self.assertTrue(is_error)
        self.assertIn("no stored plan", payload["error"])

    def test_the_brokers_own_preflight_refusal_stops_the_send(self):
        """order_check is the broker's validation. If it says no, nothing is placed
        and the model gets told so before anything reaches the market."""
        self.build(env=harness.AUTONOMOUS)
        self.h.fake.script_check = [10019]
        payload, _ = self.call("mt5_order", harness.clean_plan())
        self.assertFalse(payload["sent"])
        self.assertIn("order_check refused", payload["message"])
        self.assertEqual(self.h.fake.sent, [])

    def test_an_ambiguous_send_tells_the_model_to_reconcile_and_not_to_resend(self):
        self.build(env=harness.AUTONOMOUS)
        self.h.fake.script_send = [10012]
        payload, _ = self.call("mt5_order", harness.clean_plan())
        self.assertEqual(payload["status"], "ambiguous")
        self.assertIn("mt5_reconcile", payload["next"])
        self.assertIn("Do not send again", payload["next"])
        self.assertEqual(len(self.h.store.unresolved_intents()), 1)

    def test_an_ambiguous_send_blocks_the_next_order(self):
        self.build(env=harness.AUTONOMOUS)
        self.h.fake.script_send = [10012]
        self.call("mt5_order", harness.clean_plan())
        payload, _ = self.call("mt5_order", harness.clean_plan())
        self.assertFalse(payload["sent"])
        self.assertIn("unresolved_intents", payload["failed_checks"])

    def test_reconciling_clears_the_block(self):
        import time
        from mt5trader.fixtures import make_deal
        self.build(env=harness.AUTONOMOUS)
        self.h.fake.script_send = [10012]
        sent, _ = self.call("mt5_order", harness.clean_plan())
        self.h.fake.deals.append(make_deal(
            1, "EURUSD", sent["order"]["volume"], sent["order"]["price"],
            self.h.settings.magic, entry=0, position_id=42, when=int(time.time())))
        payload, _ = self.call("mt5_reconcile", {})
        self.assertEqual(payload["results"][0]["status"], "executed")
        self.assertEqual(self.h.store.unresolved_intents(), [])

    def test_a_sent_order_leaves_an_intent_and_a_journal_entry(self):
        self.build(env=harness.AUTONOMOUS)
        self.call("mt5_order", harness.clean_plan())
        kinds = [e["kind"] for e in self.h.store.events()]
        self.assertIn("order_intent", kinds)
        self.assertIn("order_sent", kinds)


class GuardTools(ServerTestCase):
    def test_the_kill_switch_blocks_and_flatten_separately_goes_flat(self):
        from mt5trader.fixtures import make_position
        self.build(env=harness.AUTONOMOUS,
                   positions=[make_position(1, "EURUSD", "buy", 0.1, 1.085,
                                            MAGIC)])
        tripped, _ = self.call("mt5_kill_switch", {"action": "trip",
                                                   "reason": "session stop"})
        self.assertEqual(tripped["kill_switch"], "tripped")
        self.assertTrue(tripped["open_positions"])
        self.assertIn("mt5_flatten", tripped["next"])

        blocked, _ = self.call("mt5_order", harness.clean_plan())
        self.assertFalse(blocked["sent"])
        self.assertIn("kill_switch", blocked["failed_checks"])

        flat, _ = self.call("mt5_flatten", {})
        self.assertEqual(flat["closed"], 1)
        self.assertEqual(flat["remaining"], 0)

    def test_releasing_the_switch_needs_the_environment_switch(self):
        self.build(env=harness.AUTONOMOUS)
        self.call("mt5_kill_switch", {"action": "trip", "reason": "t"})
        payload, is_error = self.call("mt5_kill_switch", {"action": "release"})
        self.assertTrue(is_error)
        self.assertIn("MT5_ALLOW_KILL_RELEASE", payload["error"])

    def test_journal_lists_what_was_attempted(self):
        self.build(env=harness.AUTONOMOUS)
        self.h.fake.script_send = [10018]
        self.call("mt5_order", harness.clean_plan())
        payload, _ = self.call("mt5_journal", {"kind": "order_rejected"})
        self.assertEqual(len(payload["events"]), 1)

    def test_flatten_with_scope_all_needs_the_switch(self):
        self.build()
        payload, is_error = self.call("mt5_flatten", {"scope": "all"})
        self.assertTrue(is_error)
        self.assertIn("MT5_ALLOW_FLATTEN", payload["error"])


if __name__ == "__main__":
    unittest.main()

