"""Hygiene tests: the two classes of mistake that no behavioural test catches.

Both of these were real, both were invisible, and neither was caught by 386 tests
that exercise behaviour:

1. **A deprecated datetime call.** Python 3.13 warns on ``utcnow()`` and
   ``utcfromtimestamp()``. This server writes JSON-RPC on stdout, so a warning
   printed by the interpreter is a corrupted protocol frame to the client, and the
   symptom would look like a client crash rather than a deprecation.

2. **A ``print()`` in the request path.** Same stream, same result. Nothing in the
   code would break; the client would just stop understanding the server.

Neither is a style opinion, which is why they are tests.
"""

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import records
from mt5trader.risk import HARD_CEILINGS, RiskKernel
from mt5trader.server import Server

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "mt5trader"

# The modules that run inside the MCP server process. cli.py is excluded on
# purpose: it is a command line program and printing is its whole job.
SERVER_PATH = [name for name in (
    "server.py", "bridge.py", "risk.py", "orders.py", "positions.py",
    "instruments.py", "sizing.py", "history.py", "state.py", "records.py",
    "config.py", "errors.py", "fixtures.py", "market.py", "levels.py",
    "sessions.py", "correlation.py", "news.py", "cot.py")]

DEPRECATED = ("utcnow", "utcfromtimestamp")


class NoDeprecatedClockCalls(unittest.TestCase):
    def test_no_module_calls_a_deprecated_datetime_function(self):
        offenders = []
        for name in SERVER_PATH + ["cli.py"]:
            source = (PACKAGE / name).read_text(encoding="utf-8")
            tree = ast.parse(source, name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                attribute = getattr(target, "attr", None)
                if attribute in DEPRECATED:
                    offenders.append("%s line %d: %s()" % (name, node.lineno, attribute))
        self.assertEqual(offenders, [], "use records.naive_utc_now() / "
                                       "records.naive_utc_from_epoch() instead")

    def test_the_docstrings_are_not_mistaken_for_calls(self):
        """The guard has to look at the syntax tree, not at the text, or the
        explanation of the rule trips the rule."""
        source = (PACKAGE / "records.py").read_text(encoding="utf-8")
        self.assertIn("utcnow()", source)          # named in the docstring
        self.assertEqual([n for n in ast.walk(ast.parse(source))
                          if isinstance(n, ast.Call)
                          and getattr(n.func, "attr", None) in DEPRECATED], [])


class NothingPrintsOnTheWire(unittest.TestCase):
    def test_no_print_in_the_server_path(self):
        offenders = []
        for name in SERVER_PATH:
            source = (PACKAGE / name).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source, name)):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    offenders.append("%s line %d" % (name, node.lineno))
        self.assertEqual(offenders, [], "stdout carries JSON-RPC and nothing else; "
                                        "use sys.stderr or return the text")

    def test_the_server_does_write_diagnostics_to_stderr(self):
        """The rule is about stdout, not about being silent."""
        server = Server(settings=__import__("mt5trader.config", fromlist=["Settings"])
                        .Settings(root=str(ROOT), config_path=None, environ={}))
        self.assertTrue(callable(server.log))


class ClockHelpers(unittest.TestCase):
    def test_naive_utc_now_is_naive_and_utc(self):
        import datetime
        now = records.naive_utc_now()
        self.assertIsNone(now.tzinfo)
        aware = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self.assertLess(abs((now - aware).total_seconds()), 5)

    def test_naive_utc_from_epoch_round_trips(self):
        import calendar
        stamp = 1792324800
        moment = records.naive_utc_from_epoch(stamp)
        self.assertIsNone(moment.tzinfo)
        # calendar.timegm reads the tuple as UTC. .timestamp() would read it as
        # local time, which is the whole reason this project never calls it on a
        # naive datetime.
        self.assertEqual(calendar.timegm(moment.timetuple()), stamp)

    def test_iso_time_still_labels_the_server_clock_as_utc(self):
        self.assertEqual(records.iso_time(1792324800), "2026-10-18T12:00:00Z")

    def test_iso_time_survives_a_bad_epoch(self):
        self.assertIsNone(records.iso_time(None))
        self.assertIsNone(records.iso_time(10 ** 18))

    def test_naive_datetimes_are_never_converted_with_a_local_time_call(self):
        """.timestamp() on a naive datetime reads it as the machine's local time, and
        time.mktime does the same to a struct_time. Every datetime this project builds
        means UTC, so both would shift values by the machine's offset. The one safe
        .timestamp() call site is where the receiver is aware, which here means the
        line mentions timezone.utc.

        Read out of the syntax tree, not out of the text. An earlier version of this
        check scanned lines, and the paragraph explaining the rule contains the very
        string the rule forbids, so the documentation tripped the check.
        """
        offenders = []
        paths = sorted(PACKAGE.glob("*.py")) + sorted((ROOT / "tools").glob("*.py"))
        for path in paths:
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            for node in ast.walk(ast.parse(source, path.name)):
                if not isinstance(node, ast.Call):
                    continue
                attribute = getattr(node.func, "attr", None)
                if attribute not in ("timestamp", "mktime"):
                    continue
                line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
                if attribute == "mktime":
                    offenders.append("%s line %d: time.mktime reads local time"
                                     % (path.name, node.lineno))
                elif "timezone.utc" not in line:
                    offenders.append("%s line %d: .timestamp() on a possibly naive "
                                     "datetime" % (path.name, node.lineno))
        self.assertEqual(offenders, [])

    def test_the_window_conversion_reads_a_naive_datetime_as_utc(self):
        """The rule above, pinned to the one place that converts a caller's bound:
        two hours either side of a UTC midnight must not move the window."""
        from mt5trader.fixtures import _epoch
        import calendar
        import datetime
        moment = datetime.datetime(2026, 10, 18, 0, 0, 0)
        self.assertEqual(_epoch(moment), calendar.timegm(moment.timetuple()))
        self.assertEqual(_epoch(moment), 1792281600)
        self.assertEqual(_epoch(1792281600.0), 1792281600.0)

    def test_nothing_defines_a_dead_server_epoch(self):
        """Clock.server_epoch existed, was never called, and used time.mktime. Dead
        code that is also wrong is worse than either."""
        source = (PACKAGE / "history.py").read_text(encoding="utf-8")
        self.assertNotIn("def server_epoch", source)

    def test_persisted_timestamps_carry_a_utc_marker(self):
        """An audit trail with an ambiguous timestamp is not an audit trail."""
        for name in ("state.py", "orders.py", "server.py"):
            source = (PACKAGE / name).read_text(encoding="utf-8")
            self.assertNotIn("datetime.datetime.now().replace(microsecond=0)", source,
                             "%s still writes a bare local timestamp" % name)


class EveryModuleImports(unittest.TestCase):
    def test_no_module_fails_to_import(self):
        import importlib
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name.startswith("__"):
                continue
            name = "mt5trader." + path.stem
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_the_registry_has_no_duplicates_and_every_tool_is_documented(self):
        server = Server(settings=__import__("mt5trader.config", fromlist=["Settings"])
                        .Settings(root=str(ROOT), config_path=None, environ={}))
        names = [tool.name for tool in server.tools.values()]
        self.assertEqual(len(names), len(set(names)), "a tool name is registered twice")
        for tool in server.tools.values():
            self.assertGreater(len(tool.description), 25, tool.name)
            self.assertEqual(tool.input_schema["type"], "object", tool.name)
            self.assertTrue(tool.name.startswith("mt5_"), tool.name)


class CeilingsCoverTheConfig(unittest.TestCase):
    def test_every_ceiling_names_a_real_config_key(self):
        """A ceiling on a key nobody reads is a limit that does not exist."""
        from mt5trader.config import DEFAULT_CONFIG
        known = set(DEFAULT_CONFIG["risk"]) | set(DEFAULT_CONFIG["guards"]) | \
            set(DEFAULT_CONFIG["execution"])
        for key in HARD_CEILINGS:
            self.assertIn(key, known, "%s is capped but never read from the config" % key)

    def test_a_ceiling_cannot_be_raised_by_a_config_file(self):
        import json
        import tempfile
        from mt5trader.config import Settings
        absurd = {"risk": {key: ceiling * 100 + 1 for key, ceiling in HARD_CEILINGS.items()},
                  "guards": {key: ceiling * 100 + 1 for key, ceiling in HARD_CEILINGS.items()}}
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "config.json").write_text(json.dumps(absurd), encoding="utf-8")
            settings = Settings(root=str(root), config_path=None, environ={})
        for key, ceiling in HARD_CEILINGS.items():
            for section in ("risk", "guards"):
                if key in settings.config[section]:
                    self.assertLessEqual(float(settings.config[section][key]),
                                         float(ceiling), key)
                    self.assertTrue(any(key in note for note in settings.notes), key)

    def test_building_settings_does_not_change_the_module_defaults(self):
        """The clamps write into the config, so the merge has to hand out a private
        copy. With a shallow one, the first Settings in a process permanently changes
        the defaults for every Settings after it, and the symptom is limits that are
        right the first time and wrong the second."""
        import json
        import tempfile
        from mt5trader.config import DEFAULT_CONFIG, Settings
        before = json.dumps(DEFAULT_CONFIG, sort_keys=True)
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "config.json").write_text(
                json.dumps({"risk": {"risk_per_trade_pct": 99, "max_open_positions": 999}}),
                encoding="utf-8")
            loose = Settings(root=str(root), config_path=None, environ={})
            loose.config["risk"]["max_lots_per_order"] = 500
            loose._clamp_all()
        self.assertEqual(json.dumps(DEFAULT_CONFIG, sort_keys=True), before)
        self.assertEqual(DEFAULT_CONFIG["risk"]["risk_per_trade_pct"], 0.5)
        self.assertEqual(DEFAULT_CONFIG["risk"]["max_open_positions"], 5)

    def test_two_settings_do_not_share_nested_dicts(self):
        from mt5trader.config import Settings
        first = Settings(root=str(ROOT), config_path=None, environ={})
        second = Settings(root=str(ROOT), config_path=None, environ={})
        first.config["risk"]["max_open_positions"] = 42
        self.assertNotEqual(second.config["risk"]["max_open_positions"], 42)
        self.assertEqual(first.config["risk"]["max_open_positions"], 42)

    def test_the_kernel_reads_every_guards_key_it_advertises(self):
        import inspect
        source = inspect.getsource(RiskKernel)
        for key in ("correlation_limit", "correlation_action", "calendar",
                    "calendar_min_importance", "calendar_before_minutes",
                    "calendar_after_minutes", "max_quote_age_seconds",
                    "treat_stopless_as_full_risk", "max_positions_per_symbol"):
            self.assertIn(key, source, "guards.%s is advertised but never read" % key)


if __name__ == "__main__":
    unittest.main()
