"""The risk kernel.

Every path to an order goes through :meth:`RiskKernel.check`. It is a plain
function of (plan, account, positions, state, clock) and returns a verdict, which
is what makes the whole safety story testable without a terminal.

Design decisions worth stating out loud, because they are the ones that decide
whether this thing protects an account or just appears to:

**Fail closed.** If a number the kernel needs is missing -- no margin figure, no
quote, no equity -- the order is blocked, not waved through. "I could not verify
this is safe" and "this is safe" must never produce the same verdict.

**Measure the whole account.** A daily loss limit that only counts realised
profit misses floating losses, and a limit that only counts ``profit`` misses
swap, commission and fee. Here the day's number is equity against the equity at
server midnight, so open positions are already inside it.

**A stop-less position is not a small risk, it is an unknown one.** By default a
position without a stop loss is treated as consuming the entire remaining daily
allowance, so the next order is blocked until it has one. That is the only
honest way to count a risk nobody has bounded.

**The ceilings are code.** Limits live in the config file, but every value is
clamped against ``config.HARD_CEILINGS`` when the process starts. Editing the
config can tighten the kernel, never loosen it, and no tool exists that writes a
ceiling. The autonomy level is not in the config file at all: it is an
environment variable read once at startup, so it is set by whoever launched the
server and not by anything the model can do at runtime.

**Unreconciled intents block new exposure.** An MT5 timeout may or may not have
placed an order. Until that is resolved against the deal history, no new
position may be opened. Closing is always allowed.
"""

import datetime
import hashlib
import json
import time

from . import correlation, history, news, records
from .config import HARD_CEILINGS
from .instruments import InstrumentError, load as load_instrument

PASS = "PASS"
BLOCK = "BLOCK"

REAL_ACCOUNT_TRADE_MODE = 2


class PlanError(ValueError):
    """The plan cannot be turned into an order at all."""


def fingerprint(order_spec):
    """Stable hash of the fields that define an order.

    The executor accepts only a plan whose fingerprint still matches, so a
    volume, price or stop edited between planning and execution invalidates the
    plan. Same idea as a signed order ticket, with the hash kept short enough to
    read out loud.
    """
    core = {
        "symbol": order_spec.get("symbol"),
        "side": order_spec.get("side"),
        "kind": order_spec.get("kind"),
        "type": order_spec.get("type"),
        "volume": order_spec.get("volume"),
        "price": order_spec.get("price"),
        "stop": order_spec.get("stop"),
        "target": order_spec.get("target"),
        "magic": order_spec.get("magic"),
        "deviation": order_spec.get("deviation"),
        "filling": order_spec.get("filling"),
        "expiration": order_spec.get("expiration"),
        "stop_distance": order_spec.get("stop_distance"),
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class RiskKernel:
    def __init__(self, settings, store, bridge):
        self.settings = settings
        self.store = store
        self.bridge = bridge
        self._clock = None

    # ------------------------------------------------------------------
    # time
    # ------------------------------------------------------------------
    def clock(self, symbol_hint=None):
        """The broker clock, cached -- except when the offset could not be
        measured. A closed market produces an unmeasurable offset, so it is
        re-measured on every call and self-corrects as soon as anything trades."""
        if self._clock is None or self._clock.source in ("stale_tick", "fallback_local"):
            self._clock = history.Clock.measure(self.bridge, symbol_hint)
        return self._clock

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def account(self):
        return self.bridge.require("account_info", what="account")

    def terminal(self):
        return self.bridge.call("terminal_info")

    def is_live_account(self, account=None):
        account = account if account is not None else self.account()
        return records.inum(records.get(account, "trade_mode"), -1) == REAL_ACCOUNT_TRADE_MODE

    def positions(self, magic_only=True, symbol=None):
        raw = self.bridge.call("positions_get")
        out = []
        for position in (raw or ()):
            if magic_only and records.inum(records.get(position, "magic"), 0) != self.settings.magic:
                continue
            if symbol and str(records.get(position, "symbol")) != str(symbol):
                continue
            out.append(position)
        return out

    def pending_orders(self, magic_only=True):
        raw = self.bridge.call("orders_get")
        out = []
        for order in (raw or ()):
            if magic_only and records.inum(records.get(order, "magic"), 0) != self.settings.magic:
                continue
            out.append(order)
        return out

    def foreign_positions(self):
        """Positions on this account that this kernel does not own. Kept visible
        so a manual trade or another EA is never mistaken for ours."""
        return self.positions(magic_only=False) and [
            records.as_dict(p, ["ticket", "symbol", "type", "volume", "price_open",
                                "sl", "tp", "profit", "magic", "comment"])
            for p in self.positions(magic_only=False)
            if records.inum(records.get(p, "magic"), 0) != self.settings.magic
        ]

    # ------------------------------------------------------------------
    # state: day anchors, counters, cooldowns, kill switch
    # ------------------------------------------------------------------
    def ingest_deals(self, clock=None):
        """Walk closing deals newer than the stored cursor and fold them into the
        day counters: realised total, loss streak, last loss time.

        Done by cursor rather than by re-summing the day, because the loss
        cooldown needs to know *when* the last loss happened, and a stop-out is
        not something the model reported.
        """
        clock = clock or self.clock()
        state = self.store.load()
        cursor = records.inum(state.get("deal_cursor"), 0) or 0
        day_start = clock.day_start().isoformat(sep=" ")
        from_dt = records.naive_utc_from_epoch(
            datetime.datetime.now(datetime.timezone.utc).timestamp() - 30 * 86400)
        deal_list = history.deals_for_magic(self.bridge, self.settings.magic, from_dt)
        newest = cursor
        losses = 0
        for deal in deal_list:
            stamp = records.inum(records.get(deal, "time"), 0) or 0
            if stamp <= cursor:
                continue
            newest = max(newest, stamp)
            if records.inum(records.get(deal, "entry"), -1) in history.CLOSING_ENTRIES:
                if history.deal_money(deal) < 0:
                    losses += 1
        changed = newest != cursor
        if changed:
            def apply(current):
                current["deal_cursor"] = newest
                if losses:
                    current["last_loss_ts"] = newest
                    current["loss_streak"] = (records.inum(current.get("loss_streak"), 0) or 0) + losses
                else:
                    current["loss_streak"] = 0
                return current
            self.store.mutate(apply)
        return {"new_deals": changed, "lost_deals": losses, "cursor": newest,
                "day_start": day_start}

    def day_state(self, account=None, clock=None):
        """Anchor the trading day and return the counters the checks need."""
        account = account if account is not None else self.account()
        clock = clock or self.clock()
        day = clock.day_key()
        equity = records.fnum(records.get(account, "equity"), 0.0) or 0.0
        balance = records.fnum(records.get(account, "balance"), 0.0) or 0.0

        def apply(state):
            if state.get("day") != day:
                state["day"] = day
                state["day_start_equity"] = equity
                state["day_start_balance"] = balance
                state["orders_today"] = 0
                state["order_times"] = []
                state["loss_streak"] = 0
            if state.get("day_start_equity") in (None,):
                state["day_start_equity"] = equity
            peak = records.fnum(state.get("peak_equity"), None)
            if peak is None or equity > peak:
                state["peak_equity"] = equity
            return state

        state = self.store.mutate(apply)
        start_equity = records.fnum(state.get("day_start_equity"), equity) or equity
        day_pnl = equity - start_equity
        day_pct = (day_pnl / start_equity * 100.0) if start_equity else 0.0
        return {
            "day": day,
            "day_start_equity": round(start_equity, 2),
            "equity": round(equity, 2),
            "balance": round(balance, 2),
            "day_pnl": round(day_pnl, 2),
            "day_pnl_percent": round(day_pct, 3),
            "peak_equity": round(records.fnum(state.get("peak_equity"), equity) or equity, 2),
            "orders_today": records.inum(state.get("orders_today"), 0) or 0,
            "order_times": list(state.get("order_times") or []),
            "last_order_ts": records.inum(state.get("last_order_ts"), None),
            "last_loss_ts": records.inum(state.get("last_loss_ts"), None),
            "loss_streak": records.inum(state.get("loss_streak"), 0) or 0,
            "kill_switch": bool(state.get("kill_switch")),
            "kill_reason": state.get("kill_reason"),
            "free_margin": records.fnum(records.get(account, "margin_free"), 0.0) or 0.0,
            "margin_level": records.fnum(records.get(account, "margin_level"), None),
        }

    def note_order_sent(self, order_spec, now=None):
        now = now or time.time()
        def apply(state):
            state["orders_today"] = (records.inum(state.get("orders_today"), 0) or 0) + 1
            times = [t for t in (state.get("order_times") or []) if now - float(t) < 90]
            times.append(float(now))
            state["order_times"] = times
            state["last_order_ts"] = int(now)
            return state
        return self.store.mutate(apply)

    def trip(self, reason, clock=None):
        clock = clock or self.clock()
        def apply(state):
            state["kill_switch"] = True
            state["kill_reason"] = reason
            state["kill_since"] = clock.now.replace(microsecond=0).isoformat()
            days = list(state.get("tripped_days") or [])
            if clock.day_key() not in days:
                days.append(clock.day_key())
            state["tripped_days"] = days[-30:]
            return state
        state = self.store.mutate(apply)
        self.store.event("kill_switch_tripped", {"reason": reason})
        return state["kill_reason"]

    def release(self, reason="released by the operator"):
        if not self.settings.allow_kill_release:
            raise PermissionError(
                "releasing the kill switch needs MT5_ALLOW_KILL_RELEASE=1 in the "
                "environment of the server process. Tripping it is always allowed; "
                "clearing it is a human decision.")
        def apply(state):
            state["kill_switch"] = False
            state["kill_reason"] = None
            state["kill_since"] = None
            return state
        state = self.store.mutate(apply)
        self.store.event("kill_switch_released", {"reason": reason})
        return state

    # ------------------------------------------------------------------
    # limits
    # ------------------------------------------------------------------
    def limits(self, equity, day_state=None):
        risk = self.settings.risk
        day_state = day_state or {}
        per_trade_pct = records.fnum(risk.get("risk_per_trade_pct"), 0.5)
        per_trade_usd = records.fnum(risk.get("risk_per_trade_usd"), None)
        money_from_pct = equity * per_trade_pct / 100.0
        cap = money_from_pct
        if per_trade_usd:
            cap = min(cap, per_trade_usd)
        return {
            "equity": round(equity, 2),
            "per_trade_pct": per_trade_pct,
            "per_trade_money": round(cap, 2),
            "per_trade_money_from_pct": round(money_from_pct, 2),
            "per_trade_money_cap_usd": per_trade_usd,
            "total_risk_pct": records.fnum(risk.get("total_risk_pct"), 2.0),
            "total_risk_money": round(equity * records.fnum(risk.get("total_risk_pct"), 2.0) / 100.0, 2),
            "daily_loss_pct": records.fnum(risk.get("daily_loss_pct"), 2.0),
            "daily_loss_money": round(equity * records.fnum(risk.get("daily_loss_pct"), 2.0) / 100.0, 2),
            "max_drawdown_pct": records.fnum(risk.get("max_drawdown_pct"), 10.0),
            "max_open_positions": records.inum(risk.get("max_open_positions"), 5),
            "max_positions_per_symbol": records.inum(
                self.settings.guards.get("max_positions_per_symbol"), 1),
            "orders_per_day": records.inum(risk.get("orders_per_day"), 10),
            "orders_per_minute": records.inum(risk.get("orders_per_minute"), 3),
            "max_lots_per_order": records.fnum(risk.get("max_lots_per_order"), 2.0),
            "loss_cooldown_seconds": records.inum(risk.get("loss_cooldown_seconds"), 300),
            "duplicate_window_seconds": records.inum(risk.get("duplicate_window_seconds"), 120),
            "hard_ceilings": HARD_CEILINGS,
        }

    def open_risk(self, positions=None):
        """Total money at risk across our open positions, and the ones that have
        no stop at all."""
        positions = positions if positions is not None else self.positions()
        total = 0.0
        per_symbol = {}
        unbounded = []
        detail = []
        for position in positions:
            symbol = str(records.get(position, "symbol"))
            volume = records.fnum(records.get(position, "volume"), 0.0) or 0.0
            entry = records.fnum(records.get(position, "price_open"), None)
            stop = records.fnum(records.get(position, "sl"), 0.0) or 0.0
            if not stop:
                unbounded.append({"ticket": records.get(position, "ticket"), "symbol": symbol,
                                  "volume": volume})
                continue
            try:
                instrument, _ = load_instrument(self.bridge, self.settings, symbol)
            except Exception:  # noqa: BLE001
                unbounded.append({"ticket": records.get(position, "ticket"), "symbol": symbol,
                                  "volume": volume, "why": "symbol metadata unavailable"})
                continue
            money = instrument.risk_money(volume, entry, stop)
            current = records.fnum(records.get(position, "price_current"), None)
            remaining = instrument.risk_money(volume, current, stop) if current else money
            if money is not None:
                total += money
                per_symbol[symbol] = round(per_symbol.get(symbol, 0.0) + money, 2)
                detail.append({
                    "ticket": records.get(position, "ticket"),
                    "symbol": symbol,
                    "volume": volume,
                    "risk_at_entry": round(money, 2),
                    "risk_now": round(remaining, 2) if remaining is not None else None,
                })
        return {"total": round(total, 2), "per_symbol": per_symbol,
                "unbounded": unbounded, "detail": detail}

    # ------------------------------------------------------------------
    # normalisation of a plan into an order spec
    # ------------------------------------------------------------------
    def normalize(self, plan, instrument=None, clock=None, equity=None):
        """Turn a loose plan into an exact order spec, or raise PlanError."""
        from . import sizing

        clock = clock or self.clock()
        symbol_in = plan.get("symbol")
        if instrument is None:
            try:
                instrument, notes = load_instrument(self.bridge, self.settings, symbol_in)
            except InstrumentError as exc:
                raise PlanError(str(exc))
        else:
            notes = []

        side = str(plan.get("side", "")).strip().lower()
        if side in ("long", "buy"):
            side = "buy"
        elif side in ("short", "sell"):
            side = "sell"
        else:
            raise PlanError("side must be buy or sell, got %r" % plan.get("side"))

        price = records.fnum(plan.get("price"), None)
        stop = records.fnum(plan.get("stop", plan.get("sl")), None)
        target = records.fnum(plan.get("target", plan.get("tp")), None)

        kind = str(plan.get("kind", "market")).strip().lower()
        if kind not in ("market", "pending"):
            raise PlanError("kind must be market or pending")
        order_type = str(plan.get("type", "")).strip().lower()
        if kind == "market":
            order_type = "market"
        else:
            if not order_type:
                entry_reference = price or instrument.entry_price(side)
                current = instrument.entry_price(side)
                if current is not None and entry_reference is not None:
                    if side == "buy":
                        order_type = "limit" if entry_reference < current else "stop"
                    else:
                        order_type = "limit" if entry_reference > current else "stop"
                else:
                    order_type = "limit"
            if order_type not in ("limit", "stop", "stop_limit"):
                raise PlanError("unknown pending order type %r" % order_type)
            if order_type == "stop_limit" and not records.fnum(plan.get("stop_limit_price"), None):
                raise PlanError("a stop_limit order needs a stop_limit_price")

        reference = price
        if reference is None and kind == "market":
            reference = instrument.entry_price(side) if instrument.has_quotes else None
        if reference is None:
            if kind == "market" and not instrument.has_quotes:
                raise PlanError(
                    "%s has no live bid or ask, so a market order cannot be priced. "
                    "Either the session is closed or the symbol is not subscribed in "
                    "Market Watch. Wait for the session, or use a pending order, which "
                    "needs a price you supply rather than a live quote."
                    % instrument.symbol)
            raise PlanError(
                "no price to work from: %s has no quotes and the plan gave no price"
                % instrument.symbol)
        reference = instrument.normalize_price(reference)

        min_points = records.inum(self.settings.guards.get("min_stop_distance_points"), 0) or 0
        try:
            stop_norm, target_norm, stop_notes = instrument.normalize_stops(
                side, reference, stop, target, min_points=min_points)
        except InstrumentError as exc:
            raise PlanError(str(exc))

        volume = records.fnum(plan.get("volume", plan.get("lots")), None)
        sizing_out = None
        if volume is None:
            if stop_norm is None:
                raise PlanError(
                    "a plan with no stop loss cannot be sized from risk: there is no "
                    "distance to divide the allowance by. Give an explicit volume, or "
                    "give a stop.")
            risk_money = records.fnum(plan.get("risk_money"), None)
            risk_percent = records.fnum(plan.get("risk_percent"), None)
            if risk_percent is not None and equity:
                sizing_out = sizing.size_for_percent(instrument, equity, risk_percent,
                                                     reference, stop_norm)
            elif risk_money is not None:
                sizing_out = sizing.size_for_money(instrument, risk_money, reference, stop_norm)
            else:
                raise PlanError("give either a volume, a risk_money or a risk_percent")
            if not sizing_out.get("lots"):
                raise PlanError("cannot size the position: %s" % sizing_out.get("reason"))
            volume = sizing_out["lots"]
        try:
            volume = instrument.normalize_volume(volume)
        except InstrumentError as exc:
            raise PlanError(str(exc))

        spec = {
            "symbol": instrument.symbol,
            "side": side,
            "kind": kind,
            "type": order_type,
            "volume": volume,
            "price": reference,
            "stop": stop_norm,
            "target": target_norm,
            "stop_distance": (round(abs(reference - stop_norm), instrument.digits)
                              if stop_norm else None),
            "stop_points": (round(instrument.price_to_points(abs(reference - stop_norm)), 1)
                            if stop_norm else None),
            "risk_money": (round(instrument.risk_money(volume, reference, stop_norm), 2)
                           if stop_norm else None),
            "reward_risk": (round(sizing.reward_risk(reference, stop_norm, target_norm), 3)
                            if (stop_norm and target_norm) else None),
            "magic": records.inum(plan.get("magic"), self.settings.magic),
            "deviation": records.inum(plan.get("deviation"),
                                      self.settings.execution.get("deviation_points")),
            "filling": str(plan.get("filling") or self.settings.execution.get("filling") or "auto"),
            "comment": str(plan.get("comment") or "mt5trader")[:31],
            "time_in_force": str(plan.get("time_in_force") or "gtc").lower(),
            "expiration": plan.get("expiration"),
            "stop_limit_price": records.fnum(plan.get("stop_limit_price"), None),
            "allow_no_sl": bool(plan.get("allow_no_sl")),
            "notes": list(notes) + list(stop_notes),
            "sizing": sizing_out,
            "instrument": instrument,
        }
        spec["fingerprint"] = fingerprint(spec)
        return spec

    # ------------------------------------------------------------------
    # the check
    # ------------------------------------------------------------------
    def check(self, plan, now=None, instrument=None):
        clock = self.clock(plan.get("symbol"))
        now_ts = now if isinstance(now, (int, float)) else time.time()
        account = self.account()
        terminal = self.terminal()
        day = self.day_state(account, clock)
        limits = self.limits(day["equity"], day)
        checks = []

        def record(code, ok, detail, **values):
            """Add a check. Re-recording a code replaces its earlier entry, so a
            check that is evaluated again later (the kill switch, after a limit
            trips it) reports one line, not two."""
            entry = {"check": code, "ok": bool(ok), "detail": detail}
            entry.update(records.to_jsonable(values))
            for index, existing in enumerate(checks):
                if existing["check"] == code:
                    checks[index] = entry
                    return bool(ok)
            checks.append(entry)
            return bool(ok)

        blocking = []
        warnings = []

        # 1. environment gates, which no plan can influence -----------------
        # Note what is NOT here: the autonomy level. Autonomy decides who may
        # *send*, not whether a plan is sound, so it is reported as an execution
        # gate on the verdict instead of as a failing check. Putting it here made
        # propose mode unable to validate anything, which is the one mode whose
        # entire job is validating.
        live = self.is_live_account(account)
        record("live_account_guard", not live or self.settings.allow_live,
               "real account" if live else "demo/contest account", live=live)
        if live and not self.settings.allow_live:
            blocking.append(
                "live_account_guard: this is a REAL account and the server was not "
                "started with MT5_ALLOW_LIVE=1. Real accounts are read-only here.")

        trade_allowed = bool(records.get(terminal, "trade_allowed", False))
        record("terminal_autotrading", trade_allowed,
               "the terminal's Algo Trading switch" ,
               connected=bool(records.get(terminal, "connected", False)))
        if not trade_allowed:
            blocking.append(
                "terminal_autotrading: automated trading is switched off - the Algo "
                "Trading button in the terminal toolbar is not lit, or the terminal is "
                "not connected. Nothing can be sent until that button is on.")

        account_trade_allowed = bool(records.get(account, "trade_allowed", False))
        record("account_trading", account_trade_allowed, "the account allows trading",
               trade_expert=bool(records.get(account, "trade_expert", False)))
        if not account_trade_allowed:
            blocking.append("account_trading: the account has trading disabled (investor "
                            "password, or disabled by the broker).")

        # 2. kill switch and unresolved intents -----------------------------
        record("kill_switch", not day["kill_switch"],
               day["kill_reason"] or "not tripped")
        if day["kill_switch"]:
            blocking.append("kill_switch: tripped (%s). Clearing it needs "
                            "MT5_ALLOW_KILL_RELEASE=1 and an operator." % day["kill_reason"])

        unresolved = self.store.unresolved_intents()
        record("unresolved_intents", not unresolved,
               "%d intent(s) whose outcome MT5 never confirmed" % len(unresolved),
               intents=[i.get("intent_id") for i in unresolved[:5]])
        if unresolved:
            blocking.append(
                "unresolved_intents: %s. An order may already exist. Reconcile them "
                "first; opening more exposure on top of an unknown position is how "
                "a doubled position happens." % ", ".join(
                    i.get("intent_id", "?") for i in unresolved[:5]))

        # 3. the instrument -------------------------------------------------
        try:
            spec = self.normalize(plan, instrument=instrument, clock=clock,
                                  equity=day["equity"])
        except PlanError as exc:
            record("plan", False, str(exc))
            return self._verdict(BLOCK, checks, blocking + ["plan: %s" % exc], warnings,
                                 None, day, limits, clock)

        instrument = spec["instrument"]
        record("plan", True, "%s %s %.4f lots at %s (stop %s, target %s)"
               % (spec["type"], spec["side"], spec["volume"], spec["price"],
                  spec["stop"], spec["target"]),
               fingerprint=spec["fingerprint"])
        warnings.extend(spec["notes"])

        record("market_open", instrument.market_open,
               "trade mode: %s" % instruments_module_trade_mode(instrument))
        if not instrument.market_open:
            blocking.append("market_open: %s is disabled for trading at the broker."
                            % instrument.symbol)

        record("side_allowed", instrument.side_allowed(spec["side"]),
               "%s allows %s orders" % (instrument.symbol, spec["side"]))
        if not instrument.side_allowed(spec["side"]):
            blocking.append("side_allowed: %s is %s at the broker"
                            % (instrument.symbol, instruments_module_trade_mode(instrument)))

        allowed_sides = [str(s).lower() for s in (self.settings.guards.get("allowed_sides")
                                                 or ["buy", "sell"])]
        record("config_side", spec["side"] in allowed_sides,
               "guards.allowed_sides=%s" % allowed_sides)
        if spec["side"] not in allowed_sides:
            blocking.append("config_side: %s is not in guards.allowed_sides" % spec["side"])

        blocked_symbols = {str(s).upper() for s in (self.settings.guards.get("blocked_symbols")
                                                    or [])}
        record("blocked_symbol", spec["symbol"].upper() not in blocked_symbols,
               "guards.blocked_symbols=%s" % sorted(blocked_symbols))
        if spec["symbol"].upper() in blocked_symbols:
            blocking.append("blocked_symbol: %s is on guards.blocked_symbols" % spec["symbol"])

        # 4. conditions at entry -------------------------------------------
        if spec["kind"] == "market":
            record("quotes", instrument.has_quotes,
                   "bid %s / ask %s (tick %s)" % (
                       instrument.bid, instrument.ask,
                       records.iso_time(instrument.tick_time)),
                   has_quotes=instrument.has_quotes, tick_time=instrument.tick_time)
            if not instrument.has_quotes:
                blocking.append(
                    "quotes: %s has no live bid or ask, so a market order has no price "
                    "to be sent at. Either the session is closed or the symbol is not "
                    "subscribed in Market Watch. A pending order does not need quotes; "
                    "wait for the session, or use one." % instrument.symbol)

            # A quote being present is not the same as it being current. A Friday
            # tick still has a bid and an ask and still shows a one-point spread, so
            # without this the kernel is happy on a Sunday and the order comes back
            # MARKET_CLOSED after a pointless round trip.
            max_age = records.inum(self.settings.guards.get("max_quote_age_seconds"), 900) or 0
            age = None
            if instrument.tick_time:
                age = int((time.time() + clock.offset) - instrument.tick_time)
            fresh = age is not None and age <= max_age
            record("quote_freshness", fresh,
                   ("the last tick is %d seconds old" % age if age is not None
                    else "the terminal reported no tick timestamp"),
                   age_seconds=age, max_age_seconds=max_age,
                   tick_time=records.iso_time(instrument.tick_time))
            if not fresh:
                if age is None:
                    blocking.append(
                        "quote_freshness: %s has no tick timestamp, so there is no way "
                        "to tell whether the quote is current. Failing closed."
                        % instrument.symbol)
                else:
                    blocking.append(
                        "quote_freshness: the last tick on %s is %.1f hours old, so the "
                        "market is closed or the symbol has stopped trading. A market "
                        "order would be refused with MARKET_CLOSED. Use a pending order, "
                        "which needs no live quote, or wait for the session."
                        % (instrument.symbol, age / 3600.0))

            spread = instrument.spread_points
            allowed_spread = records.inum(self.settings.execution.get("max_spread_points"), 30)
            spread_ok = spread is not None and spread <= allowed_spread
            record("spread", spread_ok,
                   ("%s of %s points allowed" % (spread, allowed_spread)
                    if spread is not None else
                    "no spread to measure: %s has no live quote" % instrument.symbol),
                   spread_points=spread, limit_points=allowed_spread)
            if spread is not None and not spread_ok:
                blocking.append(
                    "spread: %s is %s points wide, over the %s point limit. Waiting is "
                    "cheaper than entering here." % (instrument.symbol, spread, allowed_spread))
        else:
            record("quotes", True, "not checked for a pending order")
            record("quote_freshness", True,
                   "not checked for a pending order: it rests until the price arrives")
            record("spread", True, "not checked for a pending order", spread_points=None)

        hour = clock.now.hour
        blocked_hours = _blocked_hours(self.settings.guards.get("blocked_server_hours"))
        record("server_hour", hour not in blocked_hours,
               "server hour %02d, blocked=%s" % (hour, sorted(blocked_hours)), hour=hour)
        if hour in blocked_hours:
            blocking.append("server_hour: %02d:00 server time is inside "
                            "guards.blocked_server_hours" % hour)

        blackout = _in_blackout(clock.now.time(), self.settings.guards.get("news_blackout"))
        record("news_blackout", not blackout,
               blackout or "not inside a configured blackout window")
        if blackout:
            blocking.append("news_blackout: %s" % blackout)

        self._check_event_blackout(record, blocking, warnings, instrument, clock)

        # 5. size and money -------------------------------------------------
        order_volume = spec["volume"]
        ceiling_lots = records.fnum(HARD_CEILINGS["max_lots_per_order"], 10.0)
        allowed_lots = min(records.fnum(limits["max_lots_per_order"], 2.0) or 0, ceiling_lots)
        record("max_lots", order_volume <= allowed_lots,
               "%.4f lots against a limit of %.4f" % (order_volume, allowed_lots),
               volume=order_volume, limit=allowed_lots)
        if order_volume > allowed_lots:
            blocking.append("max_lots: %.4f lots is over the %.4f lot limit per order"
                            % (order_volume, allowed_lots))

        per_trade = records.fnum(limits["per_trade_money"], 0.0)
        actual_risk = records.fnum(spec["risk_money"], None)
        if spec["stop"]:
            record("risk_per_trade", actual_risk is not None and actual_risk <= per_trade + 0.01,
                   "risking %s of the %s allowance" % (actual_risk, round(per_trade, 2)),
                   risk=actual_risk, limit=round(per_trade, 2))
            if actual_risk is None:
                blocking.append("risk_per_trade: the risk could not be priced")
            elif actual_risk > per_trade + 0.01:
                blocking.append("risk_per_trade: risking %s against an allowance of %s. "
                                "Reduce the size or widen the stop ratio."
                                % (round(actual_risk, 2), round(per_trade, 2)))
            if per_trade and actual_risk is not None:
                share = 100.0 * actual_risk / per_trade
                if share < 25:
                    warnings.append("risk_per_trade: this uses only %.0f%% of the "
                                    "per-trade allowance; a bigger stop or a bigger size "
                                    "may be what was intended" % share)
        else:
            exempt = bool(spec.get("allow_no_sl"))
            ok = (not bool(self.settings.guards.get("require_sl", True))) or exempt
            record("risk_per_trade", ok,
                   "no stop loss on the order%s" % (" (explicitly accepted)" if exempt else ""),
                   risk=None, limit=round(per_trade, 2))
            if not ok:
                blocking.append(
                    "risk_per_trade: no stop loss. guards.require_sl is on, and a "
                    "position with no stop has no computable risk, which means no "
                    "limit above can protect it.")
            elif exempt:
                warnings.append(
                    "risk_per_trade: no stop loss, explicitly accepted with allow_no_sl. "
                    "Its risk is unbounded and cannot be measured against any limit.")

        min_rr = records.fnum(self.settings.guards.get("min_reward_risk"), 0.0) or 0.0
        if spec["target"] and spec["stop"]:
            rr = records.fnum(spec["reward_risk"], 0.0) or 0.0
            record("reward_risk", rr >= min_rr,
                   "R:R %.2f against a minimum of %.2f" % (rr, min_rr), rr=rr, min_rr=min_rr)
            if rr < min_rr:
                blocking.append("reward_risk: %.2f R against a minimum of %.2f R"
                                % (rr, min_rr))
        else:
            record("reward_risk", True, "no target given, not checked", rr=None)

        # 6. portfolio ------------------------------------------------------
        positions = self.positions()
        open_risk = self.open_risk(positions)
        record("open_positions", len(positions) < (limits["max_open_positions"] or 999),
               "%d open against a limit of %s" % (len(positions), limits["max_open_positions"]),
               count=len(positions), limit=limits["max_open_positions"])
        if len(positions) >= (limits["max_open_positions"] or 999):
            blocking.append("open_positions: %d positions open, the limit is %s"
                            % (len(positions), limits["max_open_positions"]))

        same_symbol = [p for p in positions
                       if str(records.get(p, "symbol")) == spec["symbol"]]
        per_symbol_cap = limits["max_positions_per_symbol"] or 1
        record("positions_per_symbol",
               len(same_symbol) < per_symbol_cap,
               "%d on %s against a limit of %d" % (len(same_symbol), spec["symbol"],
                                                   per_symbol_cap),
               count=len(same_symbol), limit=per_symbol_cap)
        if len(same_symbol) >= per_symbol_cap:
            blocking.append(
                "positions_per_symbol: already %d position(s) on %s and the limit is %d. "
                "Adding to a losing position is the pattern this check exists to stop."
                % (len(same_symbol), spec["symbol"], per_symbol_cap))

        total_risk_limit = records.fnum(limits["total_risk_money"], 0.0) or 0.0
        projected = open_risk["total"] + (actual_risk or 0.0)
        record("total_risk", projected <= total_risk_limit,
               "open %s + new %s against %s" % (open_risk["total"],
                                                round(actual_risk or 0.0, 2),
                                                round(total_risk_limit, 2)),
               projected=round(projected, 2), limit=round(total_risk_limit, 2))
        if projected > total_risk_limit:
            blocking.append(
                "total_risk: %s already at risk plus %s more, against a portfolio "
                "allowance of %s" % (round(open_risk["total"], 2),
                                     round(actual_risk or 0.0, 2),
                                     round(total_risk_limit, 2)))
        if open_risk["unbounded"]:
            treat_unbounded = self.settings.guards.get("treat_stopless_as_full_risk", True)
            detail = ", ".join("%s #%s" % (u["symbol"], u.get("ticket")) for u in open_risk["unbounded"])
            record("stopless_positions", not treat_unbounded,
                   "positions with no stop loss: %s" % detail,
                   tickets=[u.get("ticket") for u in open_risk["unbounded"]])
            if treat_unbounded:
                blocking.append(
                    "stopless_positions: %s has no stop loss, so its risk is unknown "
                    "and is treated as consuming the whole remaining allowance. Give "
                    "it a stop before opening anything else." % detail)
        else:
            record("stopless_positions", True, "every position has a stop loss")

        self._check_portfolio_correlation(record, blocking, warnings, spec, positions)

        # 7. the day --------------------------------------------------------
        loss_limit = records.fnum(limits["daily_loss_money"], 0.0) or 0.0
        record("daily_loss", day["day_pnl"] > -abs(loss_limit),
               "day PnL %s against a floor of -%s (server day %s)"
               % (day["day_pnl"], round(loss_limit, 2), day["day"]),
               day_pnl=day["day_pnl"], limit=-round(loss_limit, 2))
        if day["day_pnl"] <= -abs(loss_limit):
            reason = ("daily loss limit hit: %s today against a floor of -%s"
                      % (day["day_pnl"], round(loss_limit, 2)))
            blocking.append("daily_loss: " + reason)
            self.trip(reason, clock)
            record("kill_switch", False, "tripped by the daily loss limit: %s" % reason)
            blocking.append(
                "kill_switch: now tripped. Every further order is blocked until an "
                "operator clears it; there is no model-side release.")

        dd_limit = records.fnum(limits["max_drawdown_pct"], 0.0) or 0.0
        peak = day["peak_equity"] or day["equity"]
        drawdown = (peak - day["equity"]) / peak * 100.0 if peak else 0.0
        record("drawdown", drawdown < dd_limit,
               "%.2f%% below the peak equity of %s against a limit of %.2f%%"
               % (drawdown, peak, dd_limit), drawdown_pct=round(drawdown, 3), limit=dd_limit)
        if dd_limit and drawdown >= dd_limit:
            reason = ("drawdown limit hit: %.2f%% below the peak %.2f"
                      % (drawdown, peak))
            blocking.append("drawdown: " + reason)
            self.trip(reason, clock)
            record("kill_switch", False, "tripped by the drawdown limit: %s" % reason)
            blocking.append(
                "kill_switch: now tripped. Every further order is blocked until an "
                "operator clears it; there is no model-side release.")

        orders_today = records.inum(day["orders_today"], 0) or 0
        record("orders_per_day", orders_today < (limits["orders_per_day"] or 999),
               "%d sent today against a limit of %s" % (orders_today, limits["orders_per_day"]),
               count=orders_today, limit=limits["orders_per_day"])
        if orders_today >= (limits["orders_per_day"] or 999):
            blocking.append("orders_per_day: %d orders already sent today, the limit is %s"
                            % (orders_today, limits["orders_per_day"]))

        per_minute = limits["orders_per_minute"] or 3
        recent = [t for t in (day["order_times"] or []) if now_ts - float(t) < 60]
        record("orders_per_minute", len(recent) < per_minute,
               "%d in the last minute against a limit of %d" % (len(recent), per_minute),
               count=len(recent), limit=per_minute)
        if len(recent) >= per_minute:
            blocking.append(
                "orders_per_minute: %d orders in the last minute, the limit is %d. This "
                "check exists to stop a loop that is talking to itself." % (len(recent), per_minute))

        dup_window = limits["duplicate_window_seconds"] or 0
        last_order = records.fnum(day.get("last_order_ts"), None)
        if dup_window and last_order and (now_ts - last_order) < dup_window:
            recently_same = [p for p in positions if str(records.get(p, "symbol")) == spec["symbol"]]
            record("duplicate", not recently_same,
                   "an order went out %.0fs ago and %s is already open"
                   % (now_ts - last_order, spec["symbol"]),
                   seconds_since=round(now_ts - last_order, 1))
            if recently_same:
                blocking.append(
                    "duplicate: an order was sent %.0f seconds ago and there is already a "
                    "position on %s. If the previous call was ambiguous, reconcile it "
                    "instead of sending again." % (now_ts - last_order, spec["symbol"]))
        else:
            record("duplicate", True, "no recent order in the duplicate window")

        cooldown = limits["loss_cooldown_seconds"] or 0
        last_loss = records.fnum(day.get("last_loss_ts"), None)
        if cooldown and last_loss:
            elapsed = now_ts - last_loss
            record("loss_cooldown", elapsed >= cooldown,
                   "%.0fs since the last losing close, cooldown is %ds" % (elapsed, cooldown),
                   seconds_since=round(elapsed), limit=cooldown)
            if elapsed < cooldown:
                blocking.append(
                    "loss_cooldown: a loss closed %.0f seconds ago and the cooldown is "
                    "%d seconds. Re-entering straight after a loss is the single most "
                    "reliable way to turn one bad trade into a bad day."
                    % (elapsed, cooldown))
        else:
            record("loss_cooldown", True, "no cooldown active")

        # 8. margin ---------------------------------------------------------
        from . import sizing
        margin = sizing.check_margin_fit(
            self.bridge, instrument, spec["side"], spec["volume"],
            instrument.entry_price(spec["side"]) or spec["price"],
            day["free_margin"], reserve_fraction=0.25)
        record("margin", margin.get("fits") is not False,
               "needs %s, free %s, keeping %s" % (margin.get("margin_required"),
                                                  margin.get("free_margin"),
                                                  margin.get("reserve_kept")),
               **{k: v for k, v in margin.items() if k not in ("fits", "note")})
        if margin.get("fits") is False:
            blocking.append(
                "margin: needs %s of the %s free margin and would leave less than the "
                "25%% reserve" % (margin.get("margin_required"), margin.get("free_margin")))
        elif margin.get("fits") is None:
            blocking.append("margin: the terminal returned no margin figure, so the "
                            "fit is unverified. This fails closed on purpose.")

        verdict = self._verdict(BLOCK if blocking else PASS, checks, blocking, warnings,
                                spec, day, limits, clock)
        verdict["open_risk"] = open_risk
        verdict["margin"] = margin
        verdict["positions"] = len(positions)
        verdict["foreign_positions"] = self.foreign_positions()
        return verdict

    def _check_portfolio_correlation(self, record, blocking, warnings, spec, positions):
        """Five positions across correlated symbols are one position.

        Correlation is measured on returns, never on price levels: two series that
        both trend up have a price-level correlation near 1 while having nothing to
        do with each other. And it is never measured below a floor of
        observations, because a coefficient from a dozen bars means nothing and a
        gate that fires on noise is worse than no gate.
        """
        limit = records.fnum(self.settings.guards.get("correlation_limit"), 0.8)
        action = str(self.settings.guards.get("correlation_action") or "block").lower()
        if action not in ("block", "warn"):
            action = "block"
        open_symbols = sorted({str(records.get(p, "symbol")) for p in positions
                               if records.get(p, "symbol")})
        open_symbols = [s for s in open_symbols if s != spec["symbol"]]
        if not open_symbols:
            record("portfolio_correlation", True,
                   "nothing else is open, so there is nothing to correlate against")
            return
        if not limit or limit <= 0:
            record("portfolio_correlation", True, "correlation_limit is off")
            return

        configured_points = records.inum(self.settings.guards.get("correlation_min_points"),
                                         correlation.MIN_POINTS) or 0
        min_points = max(correlation.MIN_POINTS, configured_points)
        lookback = records.inum(self.settings.guards.get("correlation_lookback_bars"), 120) or 120
        sides = {str(records.get(p, "symbol")):
                 ("buy" if records.inum(records.get(p, "type"), 1) == 0 else "sell")
                 for p in positions}
        try:
            measured = correlation.exposure(self.bridge, spec["symbol"], open_symbols,
                                            timeframe="D1", count=lookback,
                                            min_points=min_points)
        except Exception as exc:  # noqa: BLE001 - unverifiable is a state, not a crash
            measured = {"compared": [], "points": 0,
                        "unverified": [{"symbol": s, "reason": "could not read bars: %s" % exc}
                                       for s in open_symbols]}

        adds, offsets = [], []
        for item in measured.get("compared") or []:
            if abs(item["r"]) < limit:
                continue
            side_open = sides.get(item["symbol"])
            same = side_open == spec["side"]
            enriched = dict(item, side_open=side_open, same_direction=same,
                            effect=("adds to the same bet" if same
                                    else "the two partly offset each other"))
            (adds if same else offsets).append(enriched)

        unverified = measured.get("unverified") or []
        highest = max((abs(i["r"]) for i in (measured.get("compared") or [])), default=None)
        detail = ("highest |r| against an open position: %s, over %s points"
                  % (highest, measured.get("points")))
        if action == "warn" and (adds or unverified):
            detail += ("; guards.correlation_action is warn, so this is reported and not "
                       "blocked")
        record("portfolio_correlation", not adds and not unverified,
               detail,
               limit=limit, points=measured.get("points"), adds=adds, offsets=offsets,
               unverified=unverified, action=action)
        if adds:
            detail = ", ".join("%s r=%.2f (open %s)" % (i["symbol"], i["r"], i["side_open"])
                               for i in adds)
            weakest = min(abs(i["r"]) for i in adds)
            (blocking if action == "block" else warnings).append(
                "portfolio_correlation: %s. %s would be the same bet as %s at %.2f "
                "correlation or better over %s points, so the money at risk adds up "
                "while the idea does not."
                % (detail, spec["symbol"],
                   "an open position" if len(adds) == 1 else "several open positions",
                   weakest, measured.get("points")))
        for item in offsets:
            warnings.append(
                "portfolio_correlation: %s is open %s at r=%.2f, so this order partly "
                "offsets it rather than adding to it"
                % (item["symbol"], item["side_open"], item["r"]))
        if unverified:
            detail = ", ".join("%s (%s)" % (u.get("symbol"), u.get("reason", "?"))
                               for u in unverified)
            (blocking if action == "block" else warnings).append(
                "portfolio_correlation: %s could not be measured, so the cluster risk "
                "is unverified. This fails closed on purpose: 'I could not check' and "
                "'this is fine' must not look the same." % detail)

    def _check_event_blackout(self, record, blocking, warnings, instrument, clock):
        """A scheduled release, turned into a window rather than a headline.

        The calendar is read from a file a small MQL5 service writes, because the
        Python API cannot reach the terminal's calendar at all. What comes back is a
        window and a reason, never free text from the outside landing in the same
        context that holds the order tools.
        """
        guard = str(self.settings.guards.get("calendar") or "warn").lower()
        if guard not in ("off", "warn", "enforce"):
            guard = "warn"
        if guard == "off":
            record("event_blackout", True, "guards.calendar is off")
            return

        status = news.status(self.settings, self.terminal(), clock)
        fail_closed = guard == "enforce"
        if not status.get("available"):
            detail = status.get("reason") or "no calendar"
            record("event_blackout", False, detail)
            (blocking if fail_closed else warnings).append(
                "event_blackout: %s" % detail)
            return

        if status.get("stale"):
            age = status.get("age_seconds")
            record("event_blackout", False,
                   "the calendar export is %s seconds old" % age,
                   age_seconds=age, path=status.get("path"))
            (blocking if fail_closed else warnings).append(
                "event_blackout: the calendar export is %s seconds old, so the service "
                "in the terminal has stopped and an upcoming release cannot be seen. "
                "Start it in the Navigator under Services." % age)
            return

        events = status.get("events") or []
        min_importance = news.importance_level(
            self.settings.guards.get("calendar_min_importance"))
        currencies = news.currencies_for(instrument)
        now_server = int(time.time() + clock.offset)
        hits = news.active_blackout(
            events, now_server,
            before_minutes=records.inum(self.settings.guards.get("calendar_before_minutes"), 15),
            after_minutes=records.inum(self.settings.guards.get("calendar_after_minutes"), 15),
            min_importance=min_importance, currencies=currencies)
        record("event_blackout", not hits,
               ("inside %d scheduled event window(s)" % len(hits) if hits else
                "no release window open for %s"
                % (", ".join(sorted(currencies)) or "any currency")),
               events=hits, currencies=sorted(currencies), min_importance=min_importance,
               calendar_age_seconds=status.get("age_seconds"))
        if hits:
            detail = "; ".join(
                "%s %s at %s (%s minutes away)"
                % (hit["currency"] or "—", hit["name"], hit["time"],
                   round(hit["starts_in_seconds"] / 60.0)) for hit in hits)
            (blocking if fail_closed else warnings).append(
                "event_blackout: %s. Entering into a scheduled release is not a view on "
                "direction, it is a bet on the spread and on which side the slippage "
                "lands." % detail)

    def _verdict(self, verdict, checks, blocking, warnings, spec, day, limits, clock):
        can_send = self.settings.at_least("auto")
        return {
            "verdict": verdict,
            "blocking": list(blocking),
            "warnings": list(warnings),
            "checks": checks,
            "failed_checks": [c["check"] for c in checks if not c["ok"]],
            "execution_gate": {
                "autonomy": self.settings.autonomy,
                "can_send": can_send,
                "needs_confirm": self.settings.autonomy == "auto",
                "reason": None if can_send else (
                    "the server runs in propose mode, so no order tool can send. "
                    "Planning and validation still work; the verdict and the order go "
                    "to the human. Sending needs MT5_AUTONOMY=auto or full in the "
                    "environment that launched the server."),
            },
            "order": None if spec is None else {
                k: v for k, v in spec.items() if k not in ("instrument",)},
            "fingerprint": spec["fingerprint"] if spec else None,
            "instrument": spec["instrument"].summary() if spec else None,
            "day": day,
            "limits": limits,
            "clock": clock.describe(),
        }

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def report(self):
        """The full risk picture, for the guard skill and for a human."""
        clock = self.clock()
        account = self.account()
        terminal = self.terminal()
        day = self.day_state(account, clock)
        limits = self.limits(day["equity"], day)
        positions = self.positions()
        open_risk = self.open_risk(positions)
        state = self.store.load()
        unresolved = self.store.unresolved_intents()
        return {
            "autonomy": self.settings.autonomy,
            "autonomy_meaning": {
                "propose": "read and plan only; no order tool is available",
                "auto": "orders allowed with an explicit confirm and a fresh plan",
                "full": "orders allowed on a passing risk check",
            }[self.settings.autonomy],
            "allow_live": self.settings.allow_live,
            "allow_flatten": self.settings.allow_flatten,
            "allow_risk_edit": self.settings.allow_risk_edit,
            "allow_kill_release": self.settings.allow_kill_release,
            "magic": self.settings.magic,
            "live_account": self.is_live_account(account),
            "terminal_trade_allowed": bool(records.get(terminal, "trade_allowed", False)),
            "day": day,
            "limits": limits,
            "open_risk": open_risk,
            "open_positions": len(positions),
            "unresolved_intents": [
                {"intent_id": i.get("intent_id"), "symbol": i.get("symbol"),
                 "status": i.get("status"), "sent_at": i.get("sent_at"),
                 "plan_id": i.get("plan_id")} for i in unresolved],
            "kill_switch": {"tripped": bool(state.get("kill_switch")),
                            "reason": state.get("kill_reason"),
                            "since": state.get("kill_since")},
            "day_counters": {
                "orders_today": day["orders_today"],
                "orders_last_minute": len([t for t in (day["order_times"] or [])
                                           if time.time() - float(t) < 60]),
                "loss_streak": day["loss_streak"],
                "last_loss": records.iso_time(day["last_loss_ts"]),
                "last_order": records.iso_time(day["last_order_ts"]),
            },
            "clock": clock.describe(),
            "settings_notes": self.settings.notes,
        }


def _blocked_hours(spec):
    hours = set()
    for item in spec or []:
        if isinstance(item, int):
            hours.add(item % 24)
        elif isinstance(item, str) and ":" in item:
            try:
                hours.add(int(item.split(":")[0]) % 24)
            except ValueError:
                continue
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = int(item[0]) % 24, int(item[1]) % 24
            if start <= end:
                hours.update(range(start, end + 1))
            else:
                hours.update(list(range(start, 24)) + list(range(0, end + 1)))
    return hours


def _in_blackout(moment, spec):
    """Recurring daily windows in server time. Scheduled news needs dates, which
    is why the config takes windows and the caller may pass its own."""
    for window in spec or []:
        try:
            start = _parse_hhmm(window.get("start"))
            end = _parse_hhmm(window.get("end"))
        except (AttributeError, ValueError, TypeError):
            continue
        if start is None or end is None:
            continue
        label = window.get("label") or "configured blackout"
        if start <= end:
            if start <= moment <= end:
                return "%s (%s-%s server)" % (label, window.get("start"), window.get("end"))
        else:  # wraps midnight
            if moment >= start or moment <= end:
                return "%s (%s-%s server, wraps midnight)" % (
                    label, window.get("start"), window.get("end"))
    return None


def _parse_hhmm(value):
    if value is None:
        return None
    parts = str(value).split(":")
    if len(parts) < 2:
        raise ValueError("not a HH:MM value: %r" % value)
    return datetime.time(int(parts[0]) % 24, int(parts[1]) % 60)


def instruments_module_trade_mode(instrument):
    from .instruments import TRADE_MODE_NAMES
    return TRADE_MODE_NAMES.get(instrument.trade_mode, str(instrument.trade_mode))

