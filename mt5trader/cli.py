"""Command line entry points.

``serve``    run the MCP server on stdio. This is what the hosts launch.
``doctor``   the same diagnosis as the mt5_status tool, for a terminal.
``config``   show the effective configuration, every clamp that was applied, and
             the hard ceilings. Run this after editing config.json: a value above
             a ceiling is silently lowered, and this is where you see it.
``tools``    list the tool names and what they do, for wiring up a host.
``selftest`` run the offline checks with no terminal attached.
``version``  print the version.
"""

import argparse
import sys

from . import __version__, records
from .bridge import Mt5Bridge, Mt5Error
from .config import HARD_CEILINGS, Settings
from .risk import RiskKernel
from .server import Server
from .state import Store


def cmd_serve(args):
    settings = Settings(root=args.root)
    return Server(settings=settings).serve()


def cmd_doctor(args):
    server = Server(settings=Settings(root=args.root))
    payload = server.tool_status({})
    if args.json:
        print(records.dumps(payload))
        return 0
    print("mt5-trader %s  (protocol %s)" % (__version__, payload["server"]["protocol"]))
    print("=" * 72)
    print("MetaTrader5 module : %s" % (payload["module"]["version"] or "NOT IMPORTABLE"))
    print("terminal connected : %s" % payload["connected"])
    terminal = payload["terminal"] or {}
    account = payload["account"] or {}
    if terminal:
        print("terminal           : build %s, %s" % (terminal.get("build"),
                                                     terminal.get("name") or ""))
        print("algo trading       : %s" % ("ON" if terminal.get("trade_allowed") else "OFF"))
        print("connected to server: %s" % terminal.get("connected"))
    if account:
        print("account            : %s @ %s (%s), leverage 1:%s"
              % (account.get("login"), account.get("server"),
                 {0: "demo", 1: "contest", 2: "REAL"}.get(account.get("trade_mode"), "?"),
                 account.get("leverage")))
        print("equity / balance   : %s / %s %s" % (account.get("equity"),
                                                   account.get("balance"),
                                                   account.get("currency") or ""))
    clock = payload.get("clock")
    if clock:
        print("server time        : %s (offset %+.2fh, from %s)"
              % (clock["server_now"], clock["server_offset_hours"], clock["source"]))
    print("autonomy           : %s" % payload["gates"]["autonomy"])
    print("live orders        : %s" % ("allowed" if payload["gates"]["live_orders_allowed"]
                                       else "blocked"))
    print("verdict            : %s" % payload["verdict"].upper())
    if payload["problems"]:
        print("\nproblems")
        for problem in payload["problems"]:
            print("  - %s" % problem)
    if payload["last_error"]:
        print("\nlast error         : %s" % payload["last_error"])
    return 0 if payload["connected"] else 1


def cmd_config(args):
    settings = Settings(root=args.root)
    payload = settings.summary()
    if args.json:
        print(records.dumps(payload))
        return 0
    print("root      : %s" % payload["root"])
    print("config    : %s%s" % (payload["config"],
                                "" if settings.config_path.is_file() else "  (not found, defaults)"))
    print("state dir : %s" % payload["state_dir"])
    print("magic     : %s" % payload["magic"])
    print("autonomy  : %s" % payload["autonomy"])
    print()
    print("effective limits (already clamped against the hard ceilings)")
    limits = RiskKernel(settings, Store(settings.state_dir, settings.journal_dir),
                        Mt5Bridge(settings, autoconnect=False)).limits(10000.0)
    for key in sorted(limits):
        if key == "hard_ceilings":
            continue
        print("  %-28s %s" % (key, limits[key]))
    print()
    print("hard ceilings (code, cannot be raised by config)")
    for key, value in sorted(HARD_CEILINGS.items()):
        print("  %-28s %s" % (key, value))
    if payload["notes"]:
        print()
        print("notes")
        for note in payload["notes"]:
            print("  - %s" % note)
    return 0


def cmd_tools(args):
    server = Server(settings=Settings(root=args.root))
    if args.json:
        print(records.dumps([t.as_dict() for t in server.tools.values()]))
        return 0
    for tool in server.tools.values():
        flag = "WRITE" if tool.mutating else "read "
        print("%s %-18s %s" % (flag, tool.name, tool.description.split(".")[0] + "."))
    print("\n%d tools. %d of them change something on the terminal."
          % (len(server.tools), sum(1 for t in server.tools.values() if t.mutating)))
    return 0


def cmd_selftest(args):
    """Offline consistency checks. No terminal, no network."""
    from . import errors
    problems = []
    settings = Settings(root=args.root, environ={})
    if settings.autonomy != "propose":
        problems.append("a bare environment should default to propose autonomy, got %s"
                        % settings.autonomy)
    if settings.allow_live:
        problems.append("a bare environment must not allow live trading")

    loose = Settings(root=args.root, environ={})
    loose.config["risk"]["risk_per_trade_pct"] = 999
    loose.config["risk"]["max_lots_per_order"] = 999
    loose._clamp_all()
    if loose.config["risk"]["risk_per_trade_pct"] != HARD_CEILINGS["risk_per_trade_pct"]:
        problems.append("risk_per_trade_pct was not clamped to its ceiling")

    from .fixtures import btcusd, eurusd, us30, xauusd
    from . import instruments, sizing
    for info in (eurusd(), us30(), xauusd(), btcusd()):
        tick = {"bid": info["bid"], "ask": info["ask"], "time": int(__import__("time").time())}
        instrument = instruments.build(info, tick=tick)
        stop = instrument.ask - 100 * instrument.point
        try:
            out = sizing.size_for_money(instrument, 100.0, instrument.ask, stop)
        except Exception as exc:  # noqa: BLE001
            problems.append("sizing %s raised %s" % (instrument.symbol, exc))
            continue
        if out.get("lots") is None:
            problems.append("sizing %s refused a 100.00 budget: %s"
                            % (instrument.symbol, out.get("reason")))
            continue
        if out["risk_money"] > 100.0 + 0.01:
            problems.append("sizing %s exceeded the 100.0 budget with %s"
                            % (instrument.symbol, out["risk_money"]))
        if out["lots"] > instrument.volume_max:
            problems.append("sizing %s produced %.4f lots, over the symbol maximum"
                            % (instrument.symbol, out["lots"]))

    for code in (10004, 10012, 10030):
        if errors.classify(code)[1] == errors.FATAL:
            problems.append("retcode %s is classified fatal but must not be: a requote, "
                            "a timeout and a bad filling mode are all recoverable"
                            % code)
    if errors.classify(10012)[1] != errors.AMBIGUOUS:
        problems.append("a timeout must be classified ambiguous, never retryable")

    print("mt5-trader %s selftest" % __version__)
    if problems:
        print("FAILED")
        for problem in problems:
            print("  - %s" % problem)
        return 1
    print("OK: floors clamp, autonomy defaults to propose, sizing stays inside its "
          "budget on forex, indices, gold and crypto, retcode classes are sane.")
    return 0


def cmd_version(args):
    print(__version__)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mt5trader",
        description="MetaTrader 5 trading kernel and MCP server")
    parser.add_argument("--root", help="project directory (default: cwd, or the "
                                       "nearest directory with a config.json)")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    subs = parser.add_subparsers(dest="command")

    for name, handler in (("serve", cmd_serve), ("doctor", cmd_doctor),
                          ("config", cmd_config), ("tools", cmd_tools),
                          ("selftest", cmd_selftest), ("version", cmd_version)):
        sub = subs.add_parser(name, help=(handler.__doc__ or "").strip().split("\n")[0])
        sub.set_defaults(func=handler)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except Mt5Error as exc:
        print("mt5-trader: %s" % exc, file=sys.stderr)
        if exc.code is not None:
            print("  last_error %s: %s" % (exc.code, exc.text), file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print("mt5-trader: %s" % exc, file=sys.stderr)
        return 2
