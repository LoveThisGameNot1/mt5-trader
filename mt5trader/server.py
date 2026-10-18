"""The MCP server: newline-delimited JSON-RPC 2.0 over stdio.

Deliberately dependency-free. The official MCP SDK is a moving target, and this
server has to work identically under Claude Code, the Codex CLI and opencode,
which are on different SDK versions. The wire format is stable and small:
``initialize``, ``tools/list``, ``tools/call``, and notifications.

Two rules that matter for a stdio server:

* **stdout carries protocol traffic and nothing else.** A stray ``print`` breaks
  the client's parser in a way that looks like a crash on the client side. All
  diagnostics go to stderr, or come back as a tool result.
* **A tool never raises out of the handler.** An exception becomes an
  ``isError`` result with the message in it, because the model can act on an
  error message and cannot act on a traceback it never sees.

The MT5 terminal is connected lazily, on the first tool call that needs it. The
server therefore starts and answers ``mt5_status`` even when MetaTrader 5 is
closed, which is what makes the doctor skill possible.
"""

import json
import sys
import traceback

from . import __version__, errors, history, instruments, orders, positions, records
from .bridge import Mt5Bridge, Mt5Error
from .config import Settings, load_settings
from .risk import BLOCK, PASS, RiskKernel
from .state import Store

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mt5-trader"

STR = {"type": "string"}
NUM = {"type": "number"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
LIST = {"type": "array", "items": {"type": "string"}}


def schema(properties, required=None, **extra):
    out = {"type": "object", "properties": properties, "required": required or []}
    out.update(extra)
    return out


class Tool:
    def __init__(self, name, description, input_schema, handler, mutating=False,
                 needs_terminal=True):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.mutating = mutating
        self.needs_terminal = needs_terminal

    def as_dict(self):
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


class Server:
    def __init__(self, settings=None, bridge=None, store=None):
        self.settings = settings or load_settings()
        self.settings.ensure_dirs()
        self.store = store or Store(self.settings.state_dir, self.settings.journal_dir)
        self.bridge = bridge or Mt5Bridge(self.settings)
        self.kernel = RiskKernel(self.settings, self.store, self.bridge)
        self.tools = {}
        self._register()

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def log(self, message):
        sys.stderr.write("[%s] %s\n" % (SERVER_NAME, message))
        sys.stderr.flush()

    def text(self, payload, is_error=False):
        body = payload if isinstance(payload, str) else records.dumps(payload)
        return {"content": [{"type": "text", "text": body}], "isError": is_error}

    def fail(self, message):
        return self.text({"error": message}, is_error=True)

    def call_tool(self, name, arguments):
        tool = self.tools.get(name)
        if tool is None:
            return self.fail("unknown tool %r. Available: %s"
                             % (name, ", ".join(sorted(self.tools))))
        try:
            if tool.needs_terminal:
                self.bridge.connect()
            payload = tool.handler(arguments or {})
            if isinstance(payload, dict) and payload.get("__error__"):
                return self.text({k: v for k, v in payload.items() if k != "__error__"},
                                 is_error=True)
            return self.text(payload, is_error=bool(
                isinstance(payload, dict) and payload.get("status") in
                ("rejected", "refused", "failed")))
        except Mt5Error as exc:
            return self.fail("%s. %s" % (exc, exc.as_dict().get("last_error_text") or ""))
        except PermissionError as exc:
            return self.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - the model must see this, not a traceback
            self.log("tool %s failed: %s\n%s" % (name, exc, traceback.format_exc()))
            return self.fail("%s: %s" % (type(exc).__name__, exc))

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def add(self, name, description, input_schema, handler, mutating=False,
            needs_terminal=True):
        self.tools[name] = Tool(name, description, input_schema, handler, mutating,
                                needs_terminal)

    def _register(self):
        # ---- diagnostics -------------------------------------------------
        self.add(
            "mt5_status",
            "Diagnose the whole stack before anything else: is the MetaTrader5 "
            "python module importable, is the terminal running and connected, is "
            "Algo Trading on, is the account demo or real, what is the server time "
            "offset, which symbols does it expose, and which environment switches "
            "gate trading. Answers even when the terminal is closed. Call this "
            "first in every session.",
            schema({}), self.tool_status, needs_terminal=False)

        self.add(
            "mt5_clock",
            "The broker's clock. MT5 times are server time presented as UTC, and a "
            "daily loss limit has to be measured against server midnight. Reports "
            "the measured offset, how it was measured, and the current server, UTC "
            "and local time side by side.",
            schema({}), self.tool_clock)

        self.add(
            "mt5_account",
            "Account balance, equity, margin, free margin, margin level, leverage, "
            "netting or hedging mode, currency, server name, and today's anchored "
            "numbers (day start equity, day PnL in money and percent, orders sent "
            "today).",
            schema({}), self.tool_account)

        self.add(
            "mt5_symbols",
            "List or search the terminal's symbols. Use it to find the broker's "
            "real name for an instrument: brokers decorate symbols (EURUSD.pro, "
            "XAUUSD.m, US30.cash) and the decoration cannot be guessed.",
            schema({"search": dict(STR, description="substring, case insensitive"),
                    "limit": dict(INT, description="default 100")}),
            self.tool_symbols)

        self.add(
            "mt5_symbol",
            "Full trading metadata for one symbol, read fresh: digits, point, tick "
            "size, tick value for loss and profit, contract size, volume minimum, "
            "maximum and step, the broker's stop and freeze levels in points, "
            "trade mode, execution mode, the filling modes this symbol accepts, "
            "swap, and live bid/ask/spread. Every sizing and stop decision must be "
            "based on this, never on a pip assumption.",
            schema({"symbol": dict(STR, description="symbol or alias")}, ["symbol"]),
            self.tool_symbol)

        self.add(
            "mt5_quote",
            "Live bid, ask, spread in points and the tick timestamp for several "
            "symbols at once. Cheaper than calling mt5_symbol per instrument when "
            "only prices are needed.",
            schema({"symbols": dict(type="array", items=STR)}, ["symbols"]),
            self.tool_quote)

        self.add(
            "mt5_rates",
            "OHLC candles for a symbol. Timeframes: M1, M2, M3, M4, M5, M6, M10, "
            "M12, M15, M20, M30, H1, H2, H3, H4, H6, H8, H12, D1, W1, MN1. Give "
            "either count (most recent) or from/to ISO timestamps.",
            schema({"symbol": dict(STR), "timeframe": dict(STR, description="default H1"),
                    "count": dict(INT, description="default 100, max 5000"),
                    "from": dict(STR, description="ISO timestamp, server time"),
                    "to": dict(STR, description="ISO timestamp, server time")},
                   ["symbol"]),
            self.tool_rates)

        # ---- position and history ---------------------------------------
        self.add(
            "mt5_positions",
            "Open positions with the kernel's own numbers attached: the money at "
            "risk to the stop, the risk left now, and the current R multiple. "
            "Separates this kernel's positions from anything else on the account "
            "so a manual trade or a second EA is never mistaken for ours.",
            schema({"symbol": dict(STR, description="optional filter"),
                    "include_foreign": dict(BOOL, description="also list positions "
                                                              "with another magic")}),
            self.tool_positions)

        self.add(
            "mt5_orders",
            "Pending orders, with type, trigger price, stop loss, take profit and "
            "expiry.",
            schema({}), self.tool_orders)

        self.add(
            "mt5_history",
            "Closed deals in a window, summed properly: profit, swap, commission "
            "and fee as four separate columns plus the total. Also the round trips "
            "(position by position) and the close reason, so a stop-out is "
            "distinguishable from a take profit. Defaults to the current server day.",
            schema({"days": dict(NUM, description="look back N days, default 1, max 365"),
                    "symbol": dict(STR), "magic_only": dict(BOOL, description="default true")}),
            self.tool_history)

        self.add(
            "mt5_stats",
            "Performance over a window: closed positions, win rate, profit factor, "
            "expectancy, average win and loss, best and worst, the longest losing "
            "streak, average holding time, a breakdown by close reason, and a "
            "realised equity curve. Built from round trips, not from raw deals.",
            schema({"days": dict(NUM, description="default 30, max 365"),
                    "symbol": dict(STR), "magic_only": dict(BOOL, description="default true")}),
            self.tool_stats)

        self.add(
            "mt5_journal",
            "The kernel's own audit trail: intents, sends, rejections, ambiguous "
            "results, reconciliations, kill switch events, limit breaches. Read "
            "this when something is unexplained - it records what was attempted, "
            "not just what succeeded.",
            schema({"limit": dict(INT, description="default 100"),
                    "kind": dict(STR, description="filter by event kind")}),
            self.tool_journal)

        # ---- risk --------------------------------------------------------
        self.add(
            "mt5_risk_status",
            "The complete risk picture and the limits actually in force: every "
            "effective limit with the hard ceiling it was clamped against, money "
            "at risk now across all positions, positions with no stop loss, the "
            "day's numbers, kill switch state, unresolved intents, and the "
            "autonomy level with what it permits. Call this before planning "
            "anything.",
            schema({}), self.tool_risk_status)

        self.add(
            "mt5_reconcile",
            "Resolve intents whose outcome MT5 never confirmed, by searching the "
            "deal history, the open positions and the pending orders for a match. "
            "Run this whenever a send came back ambiguous, and before opening new "
            "exposure. Unresolved intents block new orders on purpose.",
            schema({"all": dict(BOOL, description="default true; false reconciles only "
                                                  "the newest intent")}),
            self.tool_reconcile, mutating=True)

        # ---- planning ----------------------------------------------------
        self.add(
            "mt5_size",
            "Size a position from money at risk and never round up: the largest "
            "valid volume whose loss at the stop stays inside the allowance. Works "
            "for forex, indices, gold and crypto because it uses the symbol's own "
            "tick size and tick value. If the smallest tradeable size is already "
            "too big, it says so and reports what that minimum would cost.",
            schema({"symbol": dict(STR),
                    "side": dict(STR, description="buy or sell"),
                    "entry": dict(NUM, description="defaults to the current price"),
                    "stop": dict(NUM, description="required: risk cannot be priced without it"),
                    "target": dict(NUM),
                    "risk_money": dict(NUM, description="money to risk"),
                    "risk_percent": dict(NUM, description="percent of equity to risk")},
                   ["symbol", "side", "stop"]),
            self.tool_size)

        self.add(
            "mt5_validate",
            "Run the full risk kernel on a plan without sending anything: returns "
            "PASS or BLOCK, every single check with the value it measured and the "
            "limit it compared against, the normalised order, and a fingerprint. "
            "In propose mode this is the finish line - hand the result to the "
            "human. Never conclude an order is fine without this.",
            schema(self._plan_properties(), ["symbol", "side"]),
            self.tool_validate)

        self.add(
            "mt5_plan",
            "Validate a plan and store it, so the human can read it, or so it can "
            "be executed later by plan_id. Returns the plan id and the full "
            "verdict. Stored plans are immutable: any edit changes the "
            "fingerprint and invalidates the plan.",
            schema(self._plan_properties(), ["symbol", "side"]),
            self.tool_plan, mutating=True)

        # ---- execution ---------------------------------------------------
        self.add(
            "mt5_order",
            "Place an order. Runs the risk kernel first and refuses on any BLOCK "
            "reason; there is no override. With MT5_AUTONOMY=auto each call needs "
            "confirm=true and a plan_id whose fingerprint still matches; with full "
            "autonomy a passing kernel is enough. On demo unless the server was "
            "started with MT5_ALLOW_LIVE=1. If the result comes back ambiguous, "
            "stop and call mt5_reconcile - do not send again.",
            schema(dict(self._plan_properties(),
                        plan_id=dict(STR, description="a plan from mt5_plan; its fingerprint "
                                                       "must still match"),
                        confirm=dict(BOOL, description="required when autonomy=auto")),
                   ["symbol", "side"]),
            self.tool_order, mutating=True)

        self.add(
            "mt5_modify",
            "Change the stop loss and/or take profit of an open position. MT5's "
            "modify is absolute, so whatever you leave out is preserved from the "
            "position; clearing needs clear_sl or clear_tp explicitly. The result "
            "is verified by reading the position back, and a change the broker "
            "accepted but did not apply is reported as drifted, not as done.",
            schema({"ticket": dict(INT),
                    "sl": dict(NUM), "tp": dict(NUM),
                    "clear_sl": dict(BOOL), "clear_tp": dict(BOOL),
                    "allow_foreign": dict(BOOL, description="default false")}, ["ticket"]),
            self.tool_modify, mutating=True)

        self.add(
            "mt5_break_even",
            "Move the stop to the entry price, optionally offset by points. Refuses "
            "while the position is not in profit, because a break-even stop on a "
            "loser is just a closer stop loss.",
            schema({"ticket": dict(INT),
                    "offset_points": dict(NUM, description="default 0"),
                    "require_profit": dict(BOOL, description="default true")}, ["ticket"]),
            self.tool_break_even, mutating=True)

        self.add(
            "mt5_trail",
            "One trailing step: move the stop to distance_points behind the current "
            "price, never backwards, and only if it improves by at least "
            "step_points. Deliberately one step per call, so the kernel's rate "
            "limits stay meaningful - calling this in a loop from inside the model "
            "is what the limits exist to catch.",
            schema({"ticket": dict(INT),
                    "distance_points": dict(NUM, description="gap in points"),
                    "step_points": dict(NUM, description="minimum improvement, default 0")},
                   ["ticket", "distance_points"]),
            self.tool_trail, mutating=True)

        self.add(
            "mt5_close",
            "Close a position, fully or in part, with an opposite market order that "
            "carries position=ticket. A partial close that would leave a remainder "
            "under the symbol's minimum volume is closed in full instead, and told "
            "to you as such. Verified by reading the position back.",
            schema({"ticket": dict(INT),
                    "volume": dict(NUM, description="default: the whole position"),
                    "comment": dict(STR),
                    "allow_foreign": dict(BOOL, description="default false")}, ["ticket"]),
            self.tool_close, mutating=True)

        self.add(
            "mt5_cancel",
            "Cancel a pending order by ticket.",
            schema({"ticket": dict(INT)}, ["ticket"]),
            self.tool_cancel, mutating=True)

        self.add(
            "mt5_flatten",
            "Close everything and verify it is flat. scope=ours closes only this "
            "kernel's positions; scope=all also closes anything else on the symbol "
            "and needs MT5_ALLOW_FLATTEN=1 in the server environment. This is the "
            "'stop now' button: after a kill switch, this is what you call.",
            schema({"symbol": dict(STR, description="optional, default every symbol"),
                    "side": dict(STR, description="buy or sell, optional"),
                    "scope": dict(STR, description="ours (default) or all")},
                   []),
            self.tool_flatten, mutating=True)

        self.add(
            "mt5_kill_switch",
            "Trip or read the kill switch. Tripping is always allowed and blocks "
            "every future order including a passing risk check. Clearing it needs "
            "MT5_ALLOW_KILL_RELEASE=1 in the server environment, because stopping is "
            "a decision the model may take and resuming is the human's.",
            schema({"action": dict(STR, description="trip, status or release"),
                    "reason": dict(STR)}, ["action"]),
            self.tool_kill_switch, mutating=True)

    def _plan_properties(self):
        return {
            "symbol": dict(STR, description="symbol or alias"),
            "side": dict(STR, description="buy or sell"),
            "kind": dict(STR, description="market (default) or pending"),
            "type": dict(STR, description="limit, stop or stop_limit for pending orders"),
            "volume": dict(NUM, description="lots; omit to size from risk"),
            "risk_money": dict(NUM, description="money to risk, alternative to volume"),
            "risk_percent": dict(NUM, description="percent of equity to risk"),
            "price": dict(NUM, description="entry for a pending order, otherwise the "
                                           "current price is used"),
            "stop": dict(NUM, description="stop loss price"),
            "target": dict(NUM, description="take profit price"),
            "stop_limit_price": dict(NUM, description="for stop_limit orders only"),
            "deviation": dict(INT, description="slippage tolerance in points"),
            "filling": dict(STR, description="auto (default), fok, ioc or return"),
            "comment": dict(STR),
            "time_in_force": dict(STR, description="gtc (default), day or specified"),
            "allow_no_sl": dict(BOOL, description="explicitly accept a position with no "
                                                  "stop; guards.require_sl overrides"),
        }

    # ------------------------------------------------------------------
    # handlers: diagnostics
    # ------------------------------------------------------------------
    def tool_status(self, args):
        self.settings.ensure_dirs()
        status = self.bridge.status()
        account = status.get("account") or {}
        terminal = status.get("terminal") or {}
        try:
            clock = self.kernel.clock()
            clock_info = clock.describe()
        except Mt5Error:
            clock_info = None
        real = records.inum(account.get("trade_mode"), None) == 2
        schema = self._schema_probe(account)
        gate = {
            "autonomy": self.settings.autonomy,
            "can_place_orders": self.settings.at_least("auto"),
            "live_account": real,
            "live_orders_allowed": self.settings.allow_live,
            "flatten_foreign_allowed": self.settings.allow_flatten,
            "risk_edit_allowed": self.settings.allow_risk_edit,
            "kill_release_allowed": self.settings.allow_kill_release,
        }
        verdict = "ready"
        problems = []
        if not status.get("connected"):
            verdict = "not connected"
            problems.append("the terminal is not answering. Start MetaTrader 5, log "
                            "in, and enable Algo Trading in the toolbar.")
        if not terminal.get("trade_allowed"):
            problems.append("the Algo Trading button in the terminal is off, so no "
                            "order can be sent.")
        if real and not self.settings.allow_live:
            problems.append("this is a REAL account and MT5_ALLOW_LIVE is not set; it is "
                            "read-only here.")
        if self.settings.autonomy == "propose":
            problems.append("autonomy is propose, so orders can only be planned.")
        if real and self.settings.allow_live and self.settings.autonomy == "full":
            problems.append("LIVE account with full autonomy: orders will be sent "
                            "without a per-order confirmation.")
        if schema is not None and not schema.get("ok"):
            problems.append("the terminal does not report the symbol fields the kernel "
                            "needs (%s), so no position can be sized."
                            % ", ".join(schema.get("required_missing") or []))
        return {
            "server": {"name": SERVER_NAME, "version": __version__,
                       "protocol": PROTOCOL_VERSION},
            "module": {"version": status.get("module_version"),
                       "author": status.get("module_author")},
            "verdict": verdict,
            "connected": status.get("connected"),
            "terminal": terminal,
            "account": account,
            "clock": clock_info,
            "gates": gate,
            "symbol_schema": schema,
            "settings": self.settings.summary(),
            "last_error": status.get("last_error"),
            "bridge_stats": status.get("stats"),
            "problems": problems,
            "next": ("call mt5_risk_status to see the limits actually in force"
                     if status.get("connected") else
                     "fix the connection, then call mt5_status again"),
        }

    def _schema_probe(self, account):
        """Assert the kernel's assumptions about symbol_info field names against
        the live terminal.

        A renamed field reads as None, a None falls back to a default, and the
        default for a tick value is zero. A zero tick value does not raise: it
        sizes every position at nothing and looks like a quiet symbol. So the
        names are checked rather than trusted.
        """
        if not account:
            return None
        probe = None
        wanted = (self.settings.config.get("symbols") or [None])[0]
        if wanted:
            try:
                probe = self.bridge.call("symbol_info", self.settings.symbol(wanted))
            except Mt5Error:
                probe = None
        if probe is None:
            try:
                first = self.bridge.call("symbols_get") or ()
                probe = first[0] if first else None
            except Mt5Error:
                probe = None
        if probe is None:
            return {"ok": None, "meaning": "no symbol available to probe"}
        return instruments.check_schema(probe)

    def tool_clock(self, args):
        clock = self.kernel.clock()
        return {"clock": clock.describe(),
                "day_start": clock.day_start().isoformat(sep=" "),
                "day_key": clock.day_key()}

    def tool_account(self, args):
        account = self.kernel.account()
        day = self.kernel.day_state(account)
        return {
            "account": records.as_dict(account),
            "money": {
                "balance": records.fnum(records.get(account, "balance"), None),
                "equity": records.fnum(records.get(account, "equity"), None),
                "profit": records.fnum(records.get(account, "profit"), None),
                "margin": records.fnum(records.get(account, "margin"), None),
                "margin_free": records.fnum(records.get(account, "margin_free"), None),
                "margin_level": records.fnum(records.get(account, "margin_level"), None),
                "margin_so_call": records.fnum(records.get(account, "margin_so_call"), None),
                "margin_so_so": records.fnum(records.get(account, "margin_so_so"), None),
            },
            "mode": positions.account_margin_mode(self.bridge),
            "today": day,
            "live_account": self.kernel.is_live_account(account),
        }

    def tool_symbols(self, args):
        search = str(args.get("search") or "").upper()
        limit = int(args.get("limit") or 100)
        found = self.bridge.call("symbols_get") or ()
        names = []
        for info in found:
            name = str(records.get(info, "name", ""))
            if search and search not in name.upper():
                continue
            names.append(name)
            if len(names) >= limit:
                break
        return {"total_on_terminal": len(found), "returned": len(names),
                "filter": search or None,
                "symbols": names,
                "note": "pass one of these to mt5_symbol to get its trading metadata"}

    def tool_symbol(self, args):
        instrument, notes = instruments.load(self.bridge, self.settings, args["symbol"])
        return {"instrument": instrument.summary(), "notes": notes,
                "path": instrument.path,
                "stop_distance_price": instrument.min_stop_distance,
                "freeze_distance_price": instrument.freeze_distance}

    def tool_quote(self, args):
        out = []
        for name in args.get("symbols") or []:
            instrument, notes = instruments.load(self.bridge, self.settings, name)
            out.append({
                "symbol": instrument.symbol,
                "bid": instrument.bid, "ask": instrument.ask,
                "spread_points": instrument.spread_points,
                "tick_time": records.iso_time(instrument.tick_time),
                "has_quotes": instrument.has_quotes,
                "market_open": instrument.market_open,
                "value_per_point_per_lot": round(instrument.value_per_point_per_lot, 6),
                "notes": notes,
            })
        return {"quotes": out}

    def tool_rates(self, args):
        instrument, _ = instruments.load(self.bridge, self.settings, args["symbol"])
        timeframe = str(args.get("timeframe") or "H1").upper()
        code = TIMEFRAMES.get(timeframe)
        if code is None:
            return {"__error__": True,
                    "error": "unknown timeframe %r. One of: %s"
                             % (timeframe, ", ".join(sorted(TIMEFRAMES)))}
        if args.get("from"):
            import datetime
            bars = self.bridge.call("copy_rates_range", instrument.symbol, code,
                                    _to_dt(args["from"]),
                                    _to_dt(args.get("to") or datetime.datetime.utcnow()))
        else:
            count = int(args.get("count") or 100)
            count = max(1, min(count, 5000))
            bars = self.bridge.call("copy_rates_from_pos", instrument.symbol, code, 0, count)
        rows = [records.as_dict(bar, ["time", "open", "high", "low", "close", "tick_volume",
                                      "real_volume", "spread"])
                for bar in (bars if bars is not None else ())]
        for row in rows:
            row["time"] = records.iso_time(row.get("time"))
        return {"symbol": instrument.symbol, "timeframe": timeframe, "bars": len(rows),
                "rates": rows}

    # ------------------------------------------------------------------
    # handlers: positions and history
    # ------------------------------------------------------------------
    def tool_positions(self, args):
        include_foreign = bool(args.get("include_foreign"))
        ours = positions.positions(self.bridge, self.settings, symbol=args.get("symbol"))
        out = []
        for position in ours:
            instrument = None
            try:
                instrument, _ = instruments.load(self.bridge, self.settings,
                                                 str(records.get(position, "symbol")))
            except Exception:  # noqa: BLE001
                pass
            out.append(positions.describe(position, instrument))
        payload = {"count": len(out), "positions": out}
        if include_foreign:
            foreign = positions.positions(self.bridge, None, symbol=args.get("symbol"),
                                          magic_only=False)
            payload["foreign"] = [
                positions.describe(p) for p in foreign
                if records.inum(records.get(p, "magic"), 0) != self.settings.magic]
        payload["mode"] = positions.account_margin_mode(self.bridge)
        payload["open_risk"] = self.kernel.open_risk()
        return payload

    def tool_orders(self, args):
        pending = self.kernel.pending_orders()
        return {"count": len(pending),
                "orders": [records.as_dict(o, ["ticket", "symbol", "type", "type_filling",
                                               "type_time", "volume_current", "price_open",
                                               "sl", "tp", "price_current", "time_setup",
                                               "time_expiration", "magic", "comment", "state"])
                           for o in pending]}

    def tool_history(self, args):
        days = min(float(args.get("days") or 1), 365)
        import datetime
        date_from = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        if args.get("symbol"):
            instrument, _ = instruments.load(self.bridge, self.settings, args["symbol"])
            symbol = instrument.symbol
        else:
            symbol = None
        magic_only = args.get("magic_only", True)
        deal_list = history.deals(self.bridge, date_from)
        if magic_only:
            deal_list = [d for d in deal_list
                         if records.inum(records.get(d, "magic"), 0) == self.settings.magic]
        if symbol:
            deal_list = [d for d in deal_list if str(records.get(d, "symbol")) == symbol]
        summary = history.realized_summary(deal_list)
        trips = history.round_trips(deal_list)
        return {
            "window_days": days, "since": date_from.replace(microsecond=0).isoformat(),
            "symbol": symbol, "magic_only": magic_only,
            "summary": summary,
            "closed_positions": len(trips),
            "round_trips": trips[-100:],
            "clock": self.kernel.clock().describe(),
            "caveat": ("MT5 timestamps are broker server time presented as UTC; the "
                       "day window here is UTC based, so an off-by-hours reading is "
                       "possible near midnight server time."),
        }

    def tool_stats(self, args):
        days = min(float(args.get("days") or 30), 365)
        import datetime
        date_from = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        deal_list = history.deals(self.bridge, date_from)
        if args.get("magic_only", True):
            deal_list = [d for d in deal_list
                         if records.inum(records.get(d, "magic"), 0) == self.settings.magic]
        if args.get("symbol"):
            instrument, _ = instruments.load(self.bridge, self.settings, args["symbol"])
            deal_list = [d for d in deal_list
                         if str(records.get(d, "symbol")) == instrument.symbol]
        out = history.stats(deal_list)
        out["window_days"] = days
        out["since"] = date_from.replace(microsecond=0).isoformat()
        out["magic_only"] = args.get("magic_only", True)
        return out

    def tool_journal(self, args):
        limit = int(args.get("limit") or 100)
        return {"events": self.store.events(limit=limit, kind=args.get("kind")),
                "journal_dir": str(self.store.journal_dir)}

    # ------------------------------------------------------------------
    # handlers: risk
    # ------------------------------------------------------------------
    def tool_risk_status(self, args):
        return self.kernel.report()

    def tool_reconcile(self, args):
        if args.get("all", True):
            results = orders.reconcile_all(self.bridge, self.store, self.settings)
        else:
            unresolved = self.store.unresolved_intents()
            if not unresolved:
                return {"reconciled": 0, "note": "nothing unresolved"}
            results = [orders.reconcile(self.bridge, self.settings, unresolved[0])]
        return {"reconciled": len(results), "results": results}

    # ------------------------------------------------------------------
    # handlers: planning
    # ------------------------------------------------------------------
    def tool_size(self, args):
        instrument, notes = instruments.load(self.bridge, self.settings, args["symbol"])
        side = str(args["side"]).lower()
        entry = records.fnum(args.get("entry"), None) or instrument.entry_price(side)
        from . import sizing
        if args.get("risk_percent") is not None:
            account = self.kernel.account()
            out = sizing.size_for_percent(instrument, records.fnum(
                records.get(account, "equity"), 0.0), float(args["risk_percent"]),
                entry, records.fnum(args["stop"]))
        elif args.get("risk_money") is not None:
            out = sizing.size_for_money(instrument, float(args["risk_money"]), entry,
                                        records.fnum(args["stop"]))
        else:
            return {"__error__": True, "error": "give risk_money or risk_percent"}
        out["symbol"] = instrument.symbol
        out["side"] = side
        out["entry"] = entry
        out["stop"] = records.fnum(args.get("stop"))
        if args.get("target") is not None:
            out["reward_risk"] = sizing.reward_risk(entry, records.fnum(args["stop"]),
                                                   records.fnum(args["target"]))
        out["instrument"] = {
            "value_per_point_per_lot": round(instrument.value_per_point_per_lot, 6),
            "tick_size": instrument.tick_size,
            "tick_value_loss": instrument.tick_value_loss,
            "volume_min": instrument.volume_min,
            "volume_step": instrument.volume_step,
            "volume_max": instrument.volume_max,
            "trade_stops_level_points": instrument.trade_stops_level,
        }
        out["notes"] = notes
        return out

    def tool_validate(self, args):
        verdict = self.kernel.check(args)
        return self._verdict_payload(verdict)

    def tool_plan(self, args):
        verdict = self.kernel.check(args)
        payload = self._verdict_payload(verdict)
        if verdict["verdict"] != PASS:
            payload["stored"] = False
            return payload
        from . import state as state_module
        plan = {
            "plan_id": state_module.new_id("plan"),
            "created_at": _now(),
            "request": {k: v for k, v in args.items()},
            "order": verdict["order"],
            "fingerprint": verdict["fingerprint"],
            "verdict": verdict["verdict"],
            "limits": verdict["limits"],
            "day": verdict["day"],
            "warnings": verdict["warnings"],
        }
        self.store.save_plan(plan)
        self.store.event("plan_created", {"plan_id": plan["plan_id"],
                                          "symbol": plan["order"]["symbol"],
                                          "fingerprint": plan["fingerprint"]})
        payload["plan_id"] = plan["plan_id"]
        payload["stored"] = True
        payload["next"] = ("execute it with mt5_order and this plan_id"
                           if self.settings.at_least("auto") else
                           "autonomy is propose: show this plan to the human")
        return payload

    # ------------------------------------------------------------------
    # handlers: execution
    # ------------------------------------------------------------------
    def tool_order(self, args):
        plan_id = args.get("plan_id")
        stored = None
        if plan_id:
            stored = self.store.load_plan(plan_id)
            if stored is None:
                return {"__error__": True,
                        "error": "no stored plan %s. Create one with mt5_plan, or drop "
                                 "plan_id and pass the order fields directly." % plan_id}
            merged = dict(stored.get("request") or {})
            merged.update({k: v for k, v in args.items()
                           if k not in ("plan_id", "confirm") and v is not None})
            args = merged

        verdict = self.kernel.check(args)
        if verdict["verdict"] != PASS:
            payload = self._verdict_payload(verdict)
            payload["sent"] = False
            payload["message"] = ("blocked by the risk kernel. A block is final: there "
                                  "is no override, and fixing the plan means a new "
                                  "check.")
            return payload

        if not self.settings.at_least("auto"):
            payload = self._verdict_payload(verdict)
            payload["sent"] = False
            payload["message"] = (
                "the plan passes the kernel, but this server runs in propose mode, so "
                "no order tool can send. Hand the verdict and the order to the human. "
                "Sending needs MT5_AUTONOMY=auto or full in the environment that "
                "launched the server.")
            return payload

        if stored is not None and stored.get("fingerprint") != verdict["fingerprint"]:
            payload = self._verdict_payload(verdict)
            payload["sent"] = False
            payload["message"] = ("the plan %s no longer matches this order (plan "
                                  "fingerprint %s, order fingerprint %s). A changed "
                                  "volume, price or stop invalidates a plan on purpose; "
                                  "make a new one." % (plan_id, stored.get("fingerprint"),
                                                       verdict["fingerprint"]))
            return payload

        if self.settings.autonomy == "auto" and not args.get("confirm"):
            payload = self._verdict_payload(verdict)
            payload["sent"] = False
            payload["needs_confirmation"] = True
            payload["message"] = ("the kernel passed. Autonomy is 'auto', so this needs "
                                  "confirm=true and (for a stored plan) plan_id=%s to be "
                                  "sent." % (plan_id or "<new plan>"))
            return payload

        sent = self._execute(verdict)
        sent["verdict"] = verdict["verdict"]
        sent["fingerprint"] = verdict["fingerprint"]
        sent["order"] = verdict["order"]
        return sent

    def _execute(self, verdict):
        spec = dict(verdict["order"])
        spec["plan_id"] = verdict.get("plan_id")
        spec["_clock"] = self.kernel.clock()
        instrument = instruments.load(self.bridge, self.settings, spec["symbol"])[0]
        request = orders.build_request(self.bridge, self.settings, spec, instrument)
        pre = orders.preflight(self.bridge, request)
        if pre.get("available") and pre.get("ok") is False:
            return {"sent": False, "preflight": pre,
                    "message": "the broker's own order_check refused this before it was "
                               "sent: %s (%s). Nothing was placed."
                               % (pre.get("comment"), pre.get("explanation", {}).get("name"))}
        outcome = orders.send(self.bridge, self.store, spec, instrument, request,
                              self.settings, clock=self.kernel.clock())
        self.kernel.note_order_sent(spec)
        outcome["preflight"] = pre
        outcome["request"] = {k: v for k, v in request.items() if k != "expiration"}
        if outcome.get("status") == "ambiguous":
            outcome["next"] = ("call mt5_reconcile now. Do not send again: the order may "
                               "already be live, and new exposure is blocked until this "
                               "is resolved.")
        else:
            outcome["positions"] = [
                positions.describe(p, instrument)
                for p in positions.positions(self.bridge, self.settings, spec["symbol"])]
        return outcome

    def tool_modify(self, args):
        out = positions.modify(self.bridge, self.settings, args["ticket"],
                               sl=args.get("sl"), tp=args.get("tp"),
                               allow_foreign=bool(args.get("allow_foreign")),
                               clear_sl=bool(args.get("clear_sl")),
                               clear_tp=bool(args.get("clear_tp")))
        if out.get("status") == "done" and records.fnum(args.get("sl"), None) is not None:
            self.store.event("stop_modified", {"ticket": args["ticket"], "sl": args.get("sl")})
        return out

    def tool_break_even(self, args):
        return positions.break_even(self.bridge, self.settings, args["ticket"],
                                    offset_points=args.get("offset_points") or 0,
                                    require_profit=args.get("require_profit", True))

    def tool_trail(self, args):
        return positions.trail(self.bridge, self.settings, args["ticket"],
                               args["distance_points"], step_points=args.get("step_points") or 0)

    def tool_close(self, args):
        out = positions.close(self.bridge, self.settings, args["ticket"],
                              volume=args.get("volume"), comment=args.get("comment") or
                              "mt5trader close",
                              allow_foreign=bool(args.get("allow_foreign")))
        self.store.event("position_closed", {"ticket": args["ticket"],
                                             "status": out.get("status"),
                                             "volume": out.get("closed_volume")})
        return out

    def tool_cancel(self, args):
        return positions.cancel_pending(self.bridge, self.settings, args["ticket"])

    def tool_flatten(self, args):
        return positions.close_all(self.bridge, self.settings, symbol=args.get("symbol"),
                                   side=args.get("side"), scope=args.get("scope") or "ours")

    def tool_kill_switch(self, args):
        action = str(args.get("action") or "status").lower()
        if action == "trip":
            reason = args.get("reason") or "tripped by the model"
            self.kernel.trip(reason)
            remaining = self.kernel.positions()
            return {"kill_switch": "tripped", "reason": reason,
                    "open_positions": len(remaining),
                    "next": ("positions are still open. The kill switch blocks new "
                             "orders; it does not close anything. Call mt5_flatten to "
                             "go flat." if remaining else "already flat")}
        if action == "release":
            state = self.kernel.release(args.get("reason") or "released")
            return {"kill_switch": "released", "state": state}
        report = self.kernel.report()
        return {"kill_switch": report["kill_switch"], "day_counters": report["day_counters"],
                "note": "use action=trip to stop everything, action=release to clear it "
                        "(needs MT5_ALLOW_KILL_RELEASE=1)"}

    # ------------------------------------------------------------------
    def _verdict_payload(self, verdict):
        payload = dict(verdict)
        payload["summary"] = _verdict_summary(verdict)
        return payload

    # ------------------------------------------------------------------
    # protocol
    # ------------------------------------------------------------------
    def handle(self, message):
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": message.get("params", {}).get("protocolVersion")
                or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"listChanged": False, "subscribe": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": _INSTRUCTIONS,
            }}
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"tools": [t.as_dict() for t in self.tools.values()]}}
        if method == "tools/call":
            params = message.get("params") or {}
            result = self.call_tool(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}}
        if method == "logging/setLevel":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "method not found: %s" % method}}

    def serve(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        self.log("ready: autonomy=%s live=%s" % (self.settings.autonomy,
                                                 self.settings.allow_live))
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self.log("dropped an unparseable line: %s" % exc)
                continue
            response = self.handle(message)
            if response is None:
                continue
            stdout.write(json.dumps(records.to_jsonable(response), ensure_ascii=False) + "\n")
            stdout.flush()
        self.bridge.shutdown()
        return 0


_INSTRUCTIONS = (
    "MetaTrader 5 trading over a locally running terminal. Work in this order: "
    "mt5_status, then mt5_risk_status, then market data, then mt5_validate or "
    "mt5_plan, and only then mt5_order. The risk kernel is the authority: a BLOCK "
    "is final and there is no override, so read the failed checks and change the "
    "plan rather than retrying. Never send an order twice after an ambiguous "
    "result - reconcile first. Stops and sizes come from the symbol's own "
    "metadata through mt5_symbol, never from pip arithmetic."
)


def _verdict_summary(verdict):
    order = verdict.get("order") or {}
    return {
        "verdict": verdict.get("verdict"),
        "headline": ("%s %s %.4f lots on %s" % (order.get("type"), order.get("side"),
                                                order.get("volume") or 0,
                                                order.get("symbol"))
                     if order else "no order"),
        "risk_money": order.get("risk_money"),
        "reward_risk": order.get("reward_risk"),
        "fingerprint": verdict.get("fingerprint"),
        "failed_checks": verdict.get("failed_checks"),
        "blocking": verdict.get("blocking"),
        "warnings": verdict.get("warnings"),
    }


def _to_dt(value):
    import datetime
    if isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.fromisoformat(str(value).replace("Z", ""))


def _now():
    import datetime
    return datetime.datetime.now().replace(microsecond=0).isoformat()


TIMEFRAMES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12,
    "M15": 15, "M20": 20, "M30": 30,
    "H1": 1 | 0x4000, "H2": 2 | 0x4000, "H3": 3 | 0x4000, "H4": 4 | 0x4000,
    "H6": 6 | 0x4000, "H8": 8 | 0x4000, "H12": 12 | 0x4000,
    "D1": 24 | 0x4000, "W1": 1 | 0x8000, "MN1": 1 | 0xC000,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = argv[0] if argv else None
    server = Server(settings=Settings(root=root) if root else load_settings())
    return server.serve()


if __name__ == "__main__":
    sys.exit(main())
