"""Offline fixtures: four instrument types and a stand-in MetaTrader5 module.

The numbers below are the ones that break naive sizing code, which is why they
are the ones used in the tests:

* EURUSD, a 5-digit forex symbol where ``trade_tick_size`` is one point and a pip is
  ten points;
* XAUUSD, gold, where the contract size and the tick value have no pip in them
  at all;
* US30, an index CFD with one digit and a tick value that only makes sense when
  divided by the tick size;
* BTCUSD, a crypto contract where 0.01 lots is a real size;
* "TESTM" with ``volume_min`` 0.1 and ``volume_step`` 0.1, the case where valid
  volumes are ``min + n*step`` and "any multiple of step" gets it wrong.

``FakeMt5`` implements only the surface the kernel actually calls, keeps an
in-memory book of positions, orders and deals, and can be scripted to fail with
a queue of return codes so the retry, rotate-filling and reconciliation paths
get exercised without a broker.
"""

import copy
import datetime
import time


def make_symbol(name, **overrides):
    base = {
        "name": name,
        "description": name,
        "path": "Test\\" + name,
        "digits": 5,
        "point": 0.00001,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
        "trade_tick_value_profit": 1.0,
        "trade_tick_value_loss": 1.0,
        "contract_size": 100000.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0.0,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "trade_mode": 4,
        "trade_execution": 2,
        "filling_mode": 3,
        "expiration_mode": 0,
        "order_mode": 0,
        "order_gtc_mode": 0,
        "trade_calc_mode": 0,
        "currency_base": "EUR",
        "currency_profit": "USD",
        "currency_margin": "EUR",
        "swap_long": -1.2,
        "swap_short": -0.6,
        "swap_mode": 1,
        "spread_float": True,
        "margin_initial": 0.0,
        "visible": True,
        "select": True,
    }
    base.update(overrides)
    # A symbol's profit and loss tick values track its tick value unless they are
    # set explicitly. Forgetting this is how a fixture ends up claiming $1000 a
    # point on a crypto contract, and how a test comes to encode nonsense.
    if "trade_tick_value" in overrides:
        base["trade_tick_value_profit"] = overrides.get(
            "trade_tick_value_profit", overrides["trade_tick_value"])
        base["trade_tick_value_loss"] = overrides.get(
            "trade_tick_value_loss", overrides["trade_tick_value"])
    return base


def eurusd():
    return make_symbol("EURUSD", digits=5, point=0.00001, trade_tick_size=0.00001,
                       bid=1.08500, ask=1.08512)


def eurusd_wide_stops():
    return make_symbol("EURUSDW", trade_stops_level=50, trade_freeze_level=10,
                       bid=1.08500, ask=1.08512)


def fractional_step():
    """volume_min 0.1 with volume_step 0.01: the case where a partial close would
    strand a remainder below the minimum, which the broker refuses."""
    return make_symbol("FHEDGE", volume_min=0.1, volume_step=0.01, volume_max=50.0,
                       bid=1.08500, ask=1.08512)


def freeze_only():
    """No minimum stop distance but a wide freeze level, the case where a stop is
    legal to place but illegal to move."""
    return make_symbol("FRZ", trade_stops_level=0, trade_freeze_level=50,
                       bid=1.08500, ask=1.08512)


def xauusd():
    return make_symbol("XAUUSD", digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=1.0,
                       contract_size=100.0, currency_base="XAU", bid=2400.00, ask=2400.35)


def us30():
    return make_symbol("US30", digits=1, point=0.1, trade_tick_size=0.1, trade_tick_value=0.1,
                       contract_size=1.0, currency_base="USD", currency_profit="USD",
                       volume_min=0.1, volume_step=0.1, bid=39450.0, ask=39453.0,
                       filling_mode=1)


def btcusd():
    return make_symbol("BTCUSD", digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=0.01,
                       contract_size=1.0, currency_base="BTC", currency_profit="USD",
                       bid=63000.00, ask=63025.00)


def testm():
    """volume_min 0.1 with volume_step 0.1: valid sizes are 0.1, 0.2, 0.3 ..."""
    return make_symbol("TESTM", volume_min=0.1, volume_step=0.1, volume_max=5.0,
                       trade_tick_size=0.001, point=0.001, trade_tick_value=0.5, contract_size=10.0,
                       digits=3, bid=10.000, ask=10.020, filling_mode=2,
                       trade_stops_level=20)


def one_lot_minimum():
    """A symbol where the smallest size is already a big position. Used to check
    that sizing reports the minimum possible risk instead of silently going
    below the broker's floor."""
    return make_symbol("BIG", volume_min=1.0, volume_step=1.0, volume_max=50.0,
                       trade_tick_size=1.0, point=1.0, trade_tick_value=5.0, contract_size=1.0,
                       digits=1, bid=100.0, ask=100.5)


def make_account(**overrides):
    base = {
        "login": 5000123,
        "trade_mode": 0,          # demo
        "leverage": 100,
        "limit_orders": 200,
        "margin_so_mode": 0,
        "trade_allowed": True,
        "trade_expert": True,
        "margin_mode": 2,         # hedging
        "currency_digits": 2,
        "fifo_close": False,
        "balance": 10000.0,
        "credit": 0.0,
        "profit": 0.0,
        "equity": 10000.0,
        "margin": 0.0,
        "margin_free": 10000.0,
        "margin_level": 0.0,
        "margin_so_call": 50.0,
        "margin_so_so": 30.0,
        "margin_initial": 0.0,
        "margin_maintenance": 0.0,
        "assets": 10000.0,
        "liabilities": 0.0,
        "commission_blocked": 0.0,
        "name": "Test Trader",
        "server": "TestBroker-Demo",
        "currency": "USD",
        "company": "TestBroker Ltd",
    }
    base.update(overrides)
    return base


def make_terminal(**overrides):
    base = {
        "community_account": False,
        "community_connection": False,
        "connected": True,
        "dlls_allowed": False,
        "trade_allowed": True,
        "tradeapi_disabled": False,
        "email_enabled": False,
        "ftp_enabled": False,
        "notifications_enabled": False,
        "mqid": False,
        "build": 4260,
        "maxbars": 100000,
        "codepage": 1252,
        "ping_last": 12000,
        "community_balance": 0.0,
        "retransmission": 0.0,
        "company": "TestBroker Ltd",
        "name": "MetaTrader 5",
        "language": "English",
        "path": "C:\\Program Files\\TestBroker MT5",
        "data_path": "C:\\Users\\test\\AppData\\Roaming\\MetaQuotes\\Terminal\\ABCD",
        "commondata_path": "C:\\Users\\test\\AppData\\Roaming\\MetaQuotes\\Terminal\\Common",
    }
    base.update(overrides)
    return base


def make_deal(ticket, symbol, volume, price, magic, entry=0, profit_value=0.0,
              when=None, position_id=None, swap=0.0, commission=0.0, reason=0):
    when = when or int(time.time())
    return {
        "ticket": ticket,
        "order": ticket,
        "time": when,
        "time_msc": when * 1000,
        "type": 0 if entry == 0 else 1,
        "entry": entry,
        "magic": magic,
        "position_id": position_id if position_id is not None else ticket,
        "reason": reason,
        "volume": float(volume),
        "price": float(price),
        "commission": commission,
        "swap": swap,
        "profit": profit_value,
        "fee": 0.0,
        "symbol": symbol,
        "comment": "",
        "external_id": "",
    }


def make_position(ticket, symbol, side, volume, price_open, magic, sl=0.0, tp=0.0,
                  price_current=None, profit=0.0, when=None):
    return {
        "ticket": ticket,
        "time": when or int(time.time()),
        "time_msc": (when or int(time.time())) * 1000,
        "time_update": when or int(time.time()),
        "time_update_msc": (when or int(time.time())) * 1000,
        "type": 0 if side == "buy" else 1,
        "magic": magic,
        "identifier": ticket,
        "reason": 3,
        "volume": float(volume),
        "price_open": float(price_open),
        "sl": float(sl or 0.0),
        "tp": float(tp or 0.0),
        "price_current": float(price_current if price_current is not None else price_open),
        "swap": 0.0,
        "profit": float(profit),
        "symbol": symbol,
        "comment": "",
        "external_id": "",
    }


class FakeMt5:
    """A stand-in for the MetaTrader5 module.

    Scripted failures:

        fake.script_send = [10004, 10009]     # first attempt requotes, then fills
        fake.script_check = 10019             # order_check refuses
        fake.trade_allowed = False            # Algo Trading off
    """

    __version__ = "5.0.6180-fake"
    __author__ = "fixtures"

    def __init__(self, symbols=None, account=None, terminal=None,
                 positions=None, orders=None, deals=None):
        self.symbols = {s["name"]: s for s in (symbols or [eurusd(), xauusd(), us30(),
                                                           btcusd(), testm(),
                                                           one_lot_minimum()])}
        self.account = account or make_account()
        self.terminal = terminal or make_terminal()
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.deals = list(deals or [])
        self.script_send = []
        self.script_check = None
        self.sent = []
        self.initialized = False
        self.trade_allowed = True
        self._last_error = (1, "Success")
        self._ticket = 900000
        self._tick_time = None

    # -- module surface ----------------------------------------------------
    def initialize(self, **kwargs):
        self.initialized = True
        self._last_error = (1, "Success")
        return True

    def shutdown(self):
        self.initialized = False
        return True

    def last_error(self):
        return self._last_error

    def version(self):
        return (500, 4260, "01 Jan 2026")

    def terminal_info(self):
        info = dict(self.terminal)
        info["trade_allowed"] = self.trade_allowed
        return info

    def account_info(self):
        return dict(self.account) if self.account else None

    def symbols_get(self, group=None):
        return tuple(copy.deepcopy(s) for s in self.symbols.values())

    def symbol_info(self, name):
        found = self.symbols.get(name)
        if found is None:
            self._last_error = (-4, "no such symbol")
            return None
        return copy.deepcopy(found)

    def symbol_select(self, name, enable):
        if name in self.symbols:
            self.symbols[name]["visible"] = bool(enable)
            return True
        return False

    def symbol_info_tick(self, name):
        found = self.symbols.get(name)
        if found is None:
            return None
        return {
            "time": int(self._tick_time or time.time()),
            "bid": found.get("bid"),
            "ask": found.get("ask"),
            "last": found.get("bid"),
            "volume": 10,
            "time_msc": int((self._tick_time or time.time()) * 1000),
            "flags": 6,
            "volume_real": 1.0,
        }

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        """Synthetic bars, as plain dicts.

        The real package returns a numpy structured array here when numpy is
        installed, and one row of that is a numpy.void whose fields are invisible
        to dir() and getattr(). That shape is covered by the records tests rather
        than by this fake; what matters here is that the tool path produces bars
        with a close price.
        """
        found = self.symbols.get(symbol)
        if found is None:
            return None
        base = found.get("bid") or 100.0
        step_seconds = int(timeframe) if int(timeframe) < 10000 else 3600
        spot = int(self._tick_time or time.time())
        spread_points = 1
        if found.get("ask") and found.get("point"):
            spread_points = int(round((found["ask"] - found["bid"]) / found["point"]))
        out = []
        for index in range(int(count)):
            close = base + (index - int(count)) * base * 0.0001
            out.append({
                "time": spot - (int(count) - index) * step_seconds * 60,
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "tick_volume": 100 + index,
                "spread": spread_points,
                "real_volume": 0,
            })
        return out

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        return self.copy_rates_from_pos(symbol, timeframe, 0, 50)

    def positions_get(self, ticket=None, symbol=None):
        out = []
        for position in self.positions:
            if ticket is not None and position["ticket"] != int(ticket):
                continue
            if symbol is not None and position["symbol"] != symbol:
                continue
            out.append(copy.deepcopy(position))
        return tuple(out)

    def orders_get(self, ticket=None, symbol=None):
        out = []
        for order in self.orders:
            if ticket is not None and order["ticket"] != int(ticket):
                continue
            if symbol is not None and order["symbol"] != symbol:
                continue
            out.append(copy.deepcopy(order))
        return tuple(out)

    def history_deals_get(self, date_from=None, date_to=None, position=None, ticket=None):
        out = []
        for deal in self.deals:
            if position is not None and deal.get("position_id") != int(position):
                continue
            if date_from is not None:
                start = date_from.timestamp() if isinstance(date_from, datetime.datetime) else float(date_from)
                if deal["time"] < start - 86400 * 400:
                    continue
            if date_to is not None:
                end = date_to.timestamp() if isinstance(date_to, datetime.datetime) else float(date_to)
                if deal["time"] > end + 86400:
                    continue
            out.append(copy.deepcopy(deal))
        return tuple(out)

    def order_calc_margin(self, action, symbol, volume, price):
        found = self.symbols.get(symbol)
        if not found:
            return None
        leverage = self.account.get("leverage") or 1
        return float(volume) * found["contract_size"] * float(price) / float(leverage)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        found = self.symbols.get(symbol)
        if not found:
            return None
        per_unit = found["trade_tick_value_loss"] / found["trade_tick_size"]
        move = (float(price_close) - float(price_open)) if action == 0 else \
            (float(price_open) - float(price_close))
        return move * per_unit * float(volume)

    def order_check(self, request):
        if self.script_check:
            code = self.script_check.pop(0) if isinstance(self.script_check, list) \
                else self.script_check
            return {"retcode": code, "comment": "scripted", "margin": 0.0,
                    "margin_free": self.account["margin_free"], "balance": 0.0,
                    "equity": 0.0, "profit": 0.0}
        found = self.symbols.get(request.get("symbol"))
        if not found:
            return {"retcode": 10013, "comment": "unknown symbol", "margin": 0.0,
                    "margin_free": 0.0, "balance": 0.0, "equity": 0.0, "profit": 0.0}
        margin = self.order_calc_margin(0, request["symbol"], request.get("volume", 0.01),
                                        request.get("price", found["bid"]))
        return {"retcode": 10009, "comment": "Done", "margin": margin,
                "margin_free": self.account["margin_free"],
                "balance": self.account["balance"],
                "equity": self.account["equity"], "profit": 0.0}

    def order_send(self, request):
        self.sent.append(copy.deepcopy(request))
        if self.script_send:
            code = self.script_send.pop(0)
            if code != 10009:
                return {"retcode": code, "comment": "scripted %s" % code, "order": 0,
                        "deal": 0, "volume": 0.0, "price": 0.0,
                        "request_id": 0, "retcode_external": 0}
        self._ticket += 1
        ticket = self._ticket
        action = request.get("action")
        if action == 8:  # TRADE_ACTION_REMOVE needs no symbol
            self.orders = [o for o in self.orders if o["ticket"] != request.get("order")]
            return {"retcode": 10009, "comment": "Done", "order": request.get("order"),
                    "deal": 0, "volume": 0.0, "price": 0.0, "request_id": 1,
                    "retcode_external": 0}
        symbol = request.get("symbol")
        found = self.symbols.get(symbol) if symbol else None
        if found is None:
            return {"retcode": 10013, "comment": "unknown symbol", "order": 0, "deal": 0,
                    "volume": 0.0, "price": 0.0, "request_id": 0, "retcode_external": 0}

        if action == 1 and request.get("position"):
            return self._close(request, ticket, found)
        if action == 6:
            return self._modify(request, found)
        if action == 8:
            self.orders = [o for o in self.orders if o["ticket"] != request.get("order")]
            return self._done(request, ticket, found)
        if action == 5:
            self.orders.append({
                "ticket": ticket, "symbol": request["symbol"], "type": request["type"],
                "type_filling": request.get("type_filling"), "type_time": request.get("type_time"),
                "volume_current": request["volume"], "price_open": request["price"],
                "sl": request.get("sl", 0.0), "tp": request.get("tp", 0.0),
                "price_current": found["bid"], "time_setup": int(time.time()),
                "time_expiration": 0, "magic": request.get("magic", 0),
                "comment": request.get("comment", ""), "state": 1,
            })
            return {"retcode": 10008, "comment": "Placed", "order": ticket, "deal": 0,
                    "volume": 0.0, "price": request["price"], "bid": found["bid"],
                    "ask": found["ask"], "request_id": 1, "retcode_external": 0}
        return self._open(request, ticket, found)

    # -- helpers -----------------------------------------------------------
    def _done(self, request, ticket, found):
        return {"retcode": 10009, "comment": "Done", "order": ticket, "deal": ticket,
                "volume": request.get("volume", 0.0), "price": request.get("price", 0.0),
                "bid": found["bid"], "ask": found["ask"], "request_id": 1,
                "retcode_external": 0}

    def _open(self, request, ticket, found):
        self.positions.append(make_position(
            ticket, request["symbol"],
            "buy" if request["type"] == 0 else "sell",
            request["volume"], request["price"], request.get("magic", 0),
            sl=request.get("sl", 0.0), tp=request.get("tp", 0.0),
            price_current=request["price"]))
        self.deals.append(make_deal(ticket, request["symbol"], request["volume"],
                                    request["price"], request.get("magic", 0),
                                    entry=0, position_id=ticket))
        return self._done(request, ticket, found)

    def _close(self, request, ticket, found):
        target = None
        for position in self.positions:
            if position["ticket"] == request["position"]:
                target = position
                break
        if target is None:
            return {"retcode": 10036, "comment": "position closed", "order": 0,
                    "deal": 0, "volume": 0.0, "price": 0.0, "request_id": 0,
                    "retcode_external": 0}
        volume = float(request["volume"])
        profit = self.order_calc_profit(
            0 if target["type"] == 0 else 1, target["symbol"], volume,
            target["price_open"], request["price"]) or 0.0
        self.deals.append(make_deal(ticket, target["symbol"], volume, request["price"],
                                    target["magic"], entry=1, profit_value=profit,
                                    position_id=target["ticket"]))
        if volume >= target["volume"] - 1e-9:
            self.positions = [p for p in self.positions if p["ticket"] != target["ticket"]]
        else:
            for position in self.positions:
                if position["ticket"] == target["ticket"]:
                    position["volume"] = round(position["volume"] - volume, 8)
        self.account["equity"] = self.account.get("equity", 0.0) + profit
        self.account["balance"] = self.account.get("balance", 0.0) + profit
        return self._done(request, ticket, found)

    def _modify(self, request, found):
        for position in self.positions:
            if position["ticket"] == request["position"]:
                position["sl"] = request.get("sl", 0.0)
                position["tp"] = request.get("tp", 0.0)
                return {"retcode": 10009, "comment": "Done", "order": 0, "deal": 0,
                        "volume": 0.0, "price": 0.0, "request_id": 0,
                        "retcode_external": 0}
        return {"retcode": 10036, "comment": "position closed", "order": 0, "deal": 0,
                "volume": 0.0, "price": 0.0, "request_id": 0, "retcode_external": 0}

    # -- convenience -------------------------------------------------------
    def deal_time_offset(self, seconds):
        self._tick_time = time.time() + seconds



