"""Symbol metadata, and the normalisation every order has to go through.

This module exists because "1 lot = 100k = $10 per pip" is only true for a
handful of instrument types. On a real broker account the same code has to size
a EURUSD trade, an XAUUSD trade, a US30 CFD and a BTCUSD contract, and the four
have different ``trade_tick_size``, ``trade_tick_value``, ``contract_size``,
``volume_step`` and ``trade_stops_level``. Everything the kernel needs comes
from the terminal's own metadata, never from an assumption about pips.

The three normalisations MT5 will silently punish you for skipping:

**Volume**  Valid sizes are not "any multiple of step". They are
            ``volume_min + n * volume_step``. Sending 0.25 when the step is 0.1
            and the minimum is 0.01 is a rejection, not a rounding.
            Floats make it worse: 0.3 / 0.1 is 2.9999999999999996, so the
            arithmetic runs through Decimal, not float.

**Price**   Must be rounded to the symbol's ``digits``, half away from zero,
            which is what MQL's NormalizeDouble does. Python's round() is
            banker's rounding and will disagree with the terminal.

**Stops**   ``trade_stops_level`` and ``trade_freeze_level`` are in *points*,
            not price and not pips, and ``point`` is not always ``tick_size``.
            A stop closer than the stop level is rejected. A stop inside the
            freeze level cannot be modified at all (retcode 10029). Both are
            checked here, before a request is ever built.
"""

import decimal

from . import records

# The python module ships ORDER_FILLING_* but not the SYMBOL_FILLING_* bitmask,
# so the two bits of symbol_info.filling_mode are named here. Bit 1 = FOK
# allowed, bit 2 = IOC allowed. RETURN needs no bit: it is available for market
# and instant execution where the remaining volume goes back to the book.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_FILLING_BOC = 4

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
ORDER_FILLING_BOC = 3

TRADE_MODE_DISABLED = 0
TRADE_MODE_LONGONLY = 1
TRADE_MODE_SHORTONLY = 2
TRADE_MODE_CLOSEONLY = 3
TRADE_MODE_FULL = 4

EXECUTION_REQUEST = 0
EXECUTION_INSTANT = 1
EXECUTION_MARKET = 2
EXECUTION_EXCHANGE = 3

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

# The fields the kernel reads out of symbol_info, under their real MetaTrader 5
# names. Note the "trade_" prefix: the tick size and the tick values are
# ``trade_tick_size`` and ``trade_tick_value_loss``, not ``tick_size`` and
# ``tick_value``. Reading a name that does not exist yields None, and a None that
# falls back to a default turns into a zero, and a zero tick value looks exactly
# like a symbol nobody can price. So the names are asserted against the live
# terminal instead of trusted: see :func:`check_schema` and the schema block in
# mt5_status.
EXPECTED_SYMBOL_FIELDS = (
    "name", "digits", "point", "trade_tick_size", "trade_tick_value",
    "trade_tick_value_loss", "trade_tick_value_profit", "contract_size",
    "volume_min", "volume_max", "volume_step", "trade_stops_level",
    "trade_freeze_level", "trade_mode", "trade_execution", "filling_mode",
    "trade_calc_mode", "currency_base", "currency_profit", "currency_margin",
)

# Without these, a position cannot be sized at all: there is either no price
# increment to count in, or no money attached to it.
REQUIRED_SYMBOL_FIELDS = (
    "digits", "point", "trade_tick_size", "trade_tick_value_loss",
    "volume_min", "volume_step",
)

_MISSING = object()


def check_schema(info):
    """Which of the fields the kernel depends on the terminal actually exposes.

    Run against a live symbol, this is the only thing that catches a terminal
    build that renamed a field. It is cheap and it is the difference between a
    loud failure and a silent zero.
    """
    present, missing = [], []
    for name in EXPECTED_SYMBOL_FIELDS:
        value = records.get(info, name, _MISSING)
        if value is _MISSING or value is None:
            missing.append(name)
        else:
            present.append(name)
    required_missing = [n for n in REQUIRED_SYMBOL_FIELDS if n in missing]
    return {
        "symbol": records.get(info, "name"),
        "present": present,
        "missing": missing,
        "required_missing": required_missing,
        "ok": not required_missing,
        "meaning": ("the kernel can read every field it needs"
                    if not required_missing else
                    "the kernel cannot size a position on this terminal: %s is/are "
                    "not being reported. Sizing would silently divide by a zero tick "
                    "value." % ", ".join(required_missing)),
    }

FILLING_NAMES = {
    ORDER_FILLING_FOK: "FOK",
    ORDER_FILLING_IOC: "IOC",
    ORDER_FILLING_RETURN: "RETURN",
    ORDER_FILLING_BOC: "BOC",
}

TRADE_MODE_NAMES = {
    TRADE_MODE_DISABLED: "disabled",
    TRADE_MODE_LONGONLY: "long-only",
    TRADE_MODE_SHORTONLY: "short-only",
    TRADE_MODE_CLOSEONLY: "close-only",
    TRADE_MODE_FULL: "full",
}


class InstrumentError(ValueError):
    """The symbol does not exist, or a value cannot be made valid for it."""


def _dec(value):
    return decimal.Decimal(str(value))


def round_price(price, digits):
    """Half away from zero, the way NormalizeDouble does it."""
    if price is None:
        return None
    quant = decimal.Decimal(1).scaleb(-int(digits))
    return float(_dec(price).quantize(quant, rounding=decimal.ROUND_HALF_UP))


class Instrument:
    """One symbol's trading metadata, frozen at the moment it was read.

    Metadata changes intraday (a broker widens trade_stops_level during news,
    spread floats, the market closes). Anything that decides an order must be
    read fresh, which is why the kernel builds a new Instrument per order
    rather than caching one per session.
    """

    def __init__(self, info, tick=None, account_currency=None, account_leverage=None):
        r = records.get
        self.info = info
        self.tick = tick
        self.account_currency = account_currency
        self.account_leverage = account_leverage

        self.symbol = r(info, "name")
        self.description = r(info, "description", "")
        self.path = r(info, "path", "")

        self.digits = records.inum(r(info, "digits"), 5)
        self.point = records.fnum(r(info, "point"), 10 ** -self.digits)
        # Real MetaTrader 5 names. See EXPECTED_SYMBOL_FIELDS above for why these
        # are not called tick_size and tick_value.
        self.tick_size = records.fnum(r(info, "trade_tick_size"), None)
        self.tick_size_reported = self.tick_size is not None
        if self.tick_size is None:
            self.tick_size = self.point
        self.tick_value = records.fnum(r(info, "trade_tick_value"), 0.0)
        self.tick_value_profit = records.fnum(r(info, "trade_tick_value_profit"),
                                             self.tick_value)
        # Risk must use the loss side: on a cross-currency symbol the two tick
        # values differ, and the loss one is the one that bites.
        self.tick_value_loss = records.fnum(r(info, "trade_tick_value_loss"),
                                           self.tick_value)
        self.contract_size = records.fnum(r(info, "contract_size"), 0.0)

        self.volume_min = records.fnum(r(info, "volume_min"), 0.01)
        self.volume_max = records.fnum(r(info, "volume_max"), 100.0)
        self.volume_step = records.fnum(r(info, "volume_step"), 0.01)
        self.volume_limit = records.fnum(r(info, "volume_limit"), 0.0)

        self.trade_stops_level = records.inum(r(info, "trade_stops_level"), 0) or 0
        self.trade_freeze_level = records.inum(r(info, "trade_freeze_level"), 0) or 0

        self.trade_mode = records.inum(r(info, "trade_mode"), TRADE_MODE_DISABLED)
        self.trade_execution = records.inum(r(info, "trade_execution"), EXECUTION_MARKET)
        self.filling_mode = records.inum(r(info, "filling_mode"), 0) or 0
        self.expiration_mode = records.inum(r(info, "expiration_mode"), 0) or 0
        self.order_mode = records.inum(r(info, "order_mode"), 0) or 0
        self.order_gtc_mode = records.inum(r(info, "order_gtc_mode"), 0) or 0

        self.calc_mode = records.inum(r(info, "trade_calc_mode"), 0)
        self.currency_base = r(info, "currency_base", "")
        self.currency_profit = r(info, "currency_profit", "")
        self.currency_margin = r(info, "currency_margin", "")
        self.swap_long = records.fnum(r(info, "swap_long"), 0.0)
        self.swap_short = records.fnum(r(info, "swap_short"), 0.0)
        self.swap_mode = records.inum(r(info, "swap_mode"), 0)
        self.spread_float = bool(r(info, "spread_float", False))
        self.margin_initial = records.fnum(r(info, "margin_initial"), 0.0)

    # -- prices ------------------------------------------------------------
    @property
    def bid(self):
        return records.fnum(r_get(self.tick, "bid"))

    @property
    def ask(self):
        return records.fnum(r_get(self.tick, "ask"))

    @property
    def last(self):
        return records.fnum(r_get(self.tick, "last"))

    @property
    def tick_time(self):
        value = r_get(self.tick, "time")
        return int(value) if value else None

    @property
    def has_quotes(self):
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    @property
    def mid(self):
        if not self.has_quotes:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread_price(self):
        if not self.has_quotes:
            return None
        return round_price(self.ask - self.bid, self.digits)

    @property
    def spread_points(self):
        """Spread in points. This is the unit trade_stops_level and the
        executor's max_spread_points are both expressed in."""
        spread = self.spread_price
        if spread is None or not self.point:
            return None
        return int(round(spread / self.point))

    def entry_price(self, side):
        """What a market order will actually be filled at: ask to buy, bid to
        sell. Using the mid here is a classic sizing bug."""
        return self.ask if side == "buy" else self.bid

    def exit_price(self, side):
        return self.bid if side == "buy" else self.ask

    # -- per-lot money math ------------------------------------------------
    @property
    def pricing_ok(self):
        """Whether the terminal reported enough to price risk at all.

        A zero tick value is not a small risk, it is an unpriceable one, and the
        only honest answer is to refuse rather than to size something.
        """
        return bool(self.tick_value_loss) and bool(self.tick_size) and self.tick_size > 0

    @property
    def value_per_price_unit_per_lot(self):
        """Account-currency value of a one-unit price move, for one lot.

        On EURUSD with tick_size 0.00001 and tick_value 1.0 that is 100000 --
        i.e. $10 per pip, arrived at from metadata rather than from a 100k
        contract-size assumption.
        """
        if not self.tick_size:
            return 0.0
        return self.tick_value_loss / self.tick_size

    @property
    def value_per_point_per_lot(self):
        if not self.tick_size:
            return 0.0
        return self.value_per_price_unit_per_lot * self.point

    def value_per_point(self, lots):
        return self.value_per_point_per_lot * float(lots)

    def risk_money(self, lots, entry, stop):
        """Loss in account currency if *stop* is hit on *lots*.

        Prices are decimal quantities, so the subtraction runs through Decimal
        after rounding to the symbol's digits. In binary floating point
        1.08512 - 1.08412 is 0.001000000000000112, which is enough to make a
        hundred-point stop look like a hundred and one and floor a one-lot
        position down to 0.99 for no reason anyone could explain to a user.
        """
        if entry is None or stop is None:
            return None
        entry_d = _dec(round_price(entry, self.digits))
        stop_d = _dec(round_price(stop, self.digits))
        distance = abs(entry_d - stop_d)
        if distance <= 0:
            return None
        tick = _dec(self.tick_size)
        if tick <= 0:
            return None
        ticks = distance / tick
        return float(ticks * _dec(self.tick_value_loss) * _dec(lots))

    def price_for_money(self, lots, money):
        """How far price has to move for *lots* to make or lose *money*."""
        per_tick = _dec(self.tick_value_loss) * _dec(lots)
        if per_tick <= 0:
            return None
        ticks = _dec(money) / per_tick
        return float(ticks * _dec(self.tick_size))

    def points_to_price(self, points):
        return float(points) * self.point

    def price_to_points(self, price_distance):
        if not self.point:
            return None
        return float(price_distance) / self.point

    def money_to_points(self, lots, money):
        distance = self.price_for_money(lots, money)
        return None if distance is None else self.price_to_points(distance)

    @property
    def pip_size(self):
        """Display only. A pip is not a trading unit in MT5 -- nothing in the
        order path may use this."""
        if self.digits in (3, 5):
            return self.point * 10
        return self.point

    # -- state -------------------------------------------------------------
    @property
    def market_open(self):
        return self.trade_mode != TRADE_MODE_DISABLED

    @property
    def new_positions_allowed(self):
        return self.trade_mode in (TRADE_MODE_FULL, TRADE_MODE_LONGONLY, TRADE_MODE_SHORTONLY)

    def side_allowed(self, side):
        if self.trade_mode == TRADE_MODE_FULL:
            return True
        if self.trade_mode == TRADE_MODE_LONGONLY:
            return side == "buy"
        if self.trade_mode == TRADE_MODE_SHORTONLY:
            return side == "sell"
        return False

    @property
    def min_stop_distance(self):
        """Broker minimum distance between price and stop, in price terms."""
        return self.trade_stops_level * self.point

    @property
    def freeze_distance(self):
        return self.trade_freeze_level * self.point

    def filling_candidates(self, preferred="auto"):
        """The filling modes to try, best first.

        FOK and IOC come from the symbol's bitmask. RETURN is appended for
        market and instant execution, where the unfilled remainder goes back to
        the book rather than being rejected -- that is what makes a market order
        on a variable-volume symbol succeed at all. Getting this wrong is
        retcode 10030, "Unsupported filling mode", which is the most common
        rejection in MT5 automation after stopped-out sizing.
        """
        allowed = []
        if preferred in ("fok", "ioc", "return", "boc"):
            explicit = {"fok": ORDER_FILLING_FOK, "ioc": ORDER_FILLING_IOC,
                        "return": ORDER_FILLING_RETURN, "boc": ORDER_FILLING_BOC}[preferred]
            allowed.append(explicit)
        else:
            if self.filling_mode & SYMBOL_FILLING_IOC:
                allowed.append(ORDER_FILLING_IOC)
            if self.filling_mode & SYMBOL_FILLING_FOK:
                allowed.append(ORDER_FILLING_FOK)
        if self.trade_execution in (EXECUTION_MARKET, EXECUTION_INSTANT, EXECUTION_REQUEST):
            if ORDER_FILLING_RETURN not in allowed:
                allowed.append(ORDER_FILLING_RETURN)
        if self.trade_execution == EXECUTION_EXCHANGE and self.filling_mode & SYMBOL_FILLING_BOC:
            if ORDER_FILLING_BOC not in allowed:
                allowed.append(ORDER_FILLING_BOC)
        if not allowed:
            allowed = [ORDER_FILLING_RETURN, ORDER_FILLING_IOC, ORDER_FILLING_FOK]
        return allowed

    # -- normalisation -----------------------------------------------------
    def normalize_volume(self, lots, allow_round_down=True):
        """Nearest valid size at or below *lots*.

        Valid sizes are volume_min + n * volume_step, not n * volume_step, and
        the comparison runs through Decimal so 0.3 does not become 0.29.
        """
        if lots is None:
            raise InstrumentError("no volume to normalise for %s" % self.symbol)
        lots = records.fnum(lots, None)
        if lots is None or lots <= 0:
            raise InstrumentError("volume must be a positive number, got %r" % lots)
        minimum, step, maximum = _dec(self.volume_min), _dec(self.volume_step), _dec(self.volume_max)
        value = _dec(lots)
        if step > 0:
            steps = (value - minimum) / step
            if allow_round_down:
                n = int(steps.to_integral_value(rounding=decimal.ROUND_FLOOR))
            else:
                n = int(steps.to_integral_value(rounding=decimal.ROUND_HALF_UP))
            value = minimum + (n * step)
            if value > maximum:
                n = int(((maximum - minimum) / step).to_integral_value(
                    rounding=decimal.ROUND_FLOOR))
                value = minimum + (n * step)
        elif value > maximum:
            value = maximum
        if value < minimum:
            raise InstrumentError(
                "%.4f is below the minimum volume %.4f for %s"
                % (float(value), self.volume_min, self.symbol))
        return float(value.quantize(step if step > 0 else _dec("0.01")))

    def max_volume_available(self):
        """The largest single order the symbol accepts."""
        return self.volume_max

    def normalize_price(self, price):
        if price is None:
            return None
        return round_price(price, self.digits)

    def normalize_stops(self, side, entry, sl=None, tp=None, current=None,
                        min_points=0, clamp=False):
        """Validate and round SL/TP. Returns (sl, tp, notes).

        The side of the book a stop may sit on is measured against the price the
        broker will compare it with, not against the entry. For a new order that
        is the order price, so pass nothing and the entry is used. For moving the
        stop of an open position it is the *current* price: a trailing stop on a
        winner sits above the entry, which is entirely legal, and validating it
        against the entry would refuse every trail that worked.

        Raises when a stop is unusable and *clamp* is False, because silently
        moving a stop changes the risk the plan was sized for. When *clamp* is
        True the stop is pushed out to the minimum distance and the change is
        reported instead.
        """
        notes = []
        entry = records.fnum(entry, None)
        if entry is None:
            raise InstrumentError("no entry price to validate stops against")
        baseline = records.fnum(current, None)
        if baseline is None:
            baseline = entry
        floor = max(self.min_stop_distance, float(min_points or 0) * self.point)

        out = []
        for label, raw in (("sl", sl), ("tp", tp)):
            price = records.fnum(raw, None)
            if price is None:
                out.append(None)
                continue
            if side == "buy" and label == "sl" and price >= baseline:
                raise InstrumentError(
                    "a buy stop loss must sit below the price (%s >= %s)"
                    % (price, baseline))
            if side == "sell" and label == "sl" and price <= baseline:
                raise InstrumentError(
                    "a sell stop loss must sit above the price (%s <= %s)"
                    % (price, baseline))
            if side == "buy" and label == "tp" and price <= baseline:
                raise InstrumentError(
                    "a buy take profit must sit above the price (%s <= %s)"
                    % (price, baseline))
            if side == "sell" and label == "tp" and price >= baseline:
                raise InstrumentError(
                    "a sell take profit must sit below the price (%s >= %s)"
                    % (price, baseline))
            if floor > 0 and abs(baseline - price) < floor:
                if not clamp:
                    raise InstrumentError(
                        "%s at %s is %.5f from the price, the broker's minimum is %.5f "
                        "(trade_stops_level=%d points)"
                        % (label, price, abs(baseline - price), floor,
                           self.trade_stops_level))
                price = baseline - floor if (side == "buy") == (label == "sl") else baseline + floor
                notes.append("%s clamped to the broker's minimum distance" % label)
            out.append(self.normalize_price(price))
        return out[0], out[1], notes

    def check_freeze(self, sl=None, tp=None, current=None):
        """A stop inside the freeze level cannot be modified (retcode 10029)."""
        if not self.trade_freeze_level:
            return None
        reference = records.fnum(current, None)
        if reference is None:
            return None
        for label, value in (("sl", sl), ("tp", tp)):
            price = records.fnum(value, None)
            if price is None:
                continue
            if abs(reference - price) < self.freeze_distance:
                return ("%s is inside the freeze level (%.5f < %.5f); the terminal "
                        "will refuse the change until price moves away"
                        % (label, abs(reference - price), self.freeze_distance))
        return None

    def stop_distance_price(self, stop_points):
        return float(stop_points) * self.point

    # -- serialisation -----------------------------------------------------
    def summary(self):
        return {
            "symbol": self.symbol,
            "description": self.description,
            "digits": self.digits,
            "point": self.point,
            "tick_size": self.tick_size,
            "tick_size_reported": self.tick_size_reported,
            "tick_value": self.tick_value,
            "tick_value_loss": self.tick_value_loss,
            "tick_value_profit": self.tick_value_profit,
            "pricing_ok": self.pricing_ok,
            "contract_size": self.contract_size,
            "volume_min": self.volume_min,
            "volume_max": self.volume_max,
            "volume_step": self.volume_step,
            "trade_stops_level_points": self.trade_stops_level,
            "trade_freeze_level_points": self.trade_freeze_level,
            "trade_mode": TRADE_MODE_NAMES.get(self.trade_mode, self.trade_mode),
            "trade_execution": self.trade_execution,
            "filling_modes": [FILLING_NAMES.get(f, f) for f in self.filling_candidates()],
            "currency_base": self.currency_base,
            "currency_profit": self.currency_profit,
            "currency_margin": self.currency_margin,
            "value_per_point_per_lot": self.value_per_point_per_lot,
            "value_per_price_unit_per_lot": self.value_per_price_unit_per_lot,
            "pip_size_display_only": self.pip_size,
            "bid": self.bid,
            "ask": self.ask,
            "spread_points": self.spread_points,
            "spread_float": self.spread_float,
            "swap_long": self.swap_long,
            "swap_short": self.swap_short,
            "market_open": self.market_open,
            "new_positions_allowed": self.new_positions_allowed,
            "tick_time": self.tick_time,
        }


def r_get(record, name, default=None):
    """Small indirection so tick reads stay short and never raise."""
    return records.get(record, name, default)


def build(info, tick=None, account_currency=None, account_leverage=None):
    if info is None:
        raise InstrumentError("no symbol metadata")
    return Instrument(info, tick=tick, account_currency=account_currency,
                      account_leverage=account_leverage)


def resolve_symbol(bridge, settings, name):
    """Find the broker's real symbol name for a user-facing one.

    Brokers decorate symbol names (EURUSD.pro, XAUUSD.m, US30.cash) and the
    decoration is not guessable. Order of preference: an explicit alias from the
    config, the name exactly as given, the name plus the configured suffix, then
    a case-insensitive search over the terminal's own symbol list.
    """
    name = str(name or "").strip()
    if not name:
        raise InstrumentError("no symbol name given")
    alias = settings.config.get("symbol_aliases", {}).get(name)
    candidates = []
    if alias:
        candidates.append(alias)
    candidates.append(name)
    if settings.symbol_suffix:
        candidates.append(name + settings.symbol_suffix)
    seen = set()
    for candidate in candidates:
        if candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        info = bridge.call("symbol_info", candidate)
        if info is not None:
            return candidate, info
    needle = name.upper()
    for info in (bridge.call("symbols_get") or ()):
        full = str(records.get(info, "name", ""))
        if full.upper() == needle or full.upper().split(".")[0] == needle or \
                full.upper().startswith(needle):
            return full, info
    available = sorted(str(records.get(i, "name", "")) for i in (bridge.call("symbols_get") or ()))
    hint = ", ".join(available[:40]) if available else "the terminal returned no symbols"
    raise InstrumentError(
        "no symbol matching %r in this terminal (tried %s). Available: %s"
        % (name, ", ".join(seen), hint))


def load(bridge, settings, name, ensure_selected=True):
    """Resolve, select if needed, and return (instrument, notes)."""
    notes = []
    full, info = resolve_symbol(bridge, settings, name)
    if full != name and full.upper() != name.upper():
        notes.append("resolved %s to %s" % (name, full))
    if ensure_selected and not records.get(info, "visible", True):
        if bridge.call("symbol_select", full, True):
            notes.append("%s was not in Market Watch and was added" % full)
            info = bridge.call("symbol_info", full) or info
    tick = bridge.call("symbol_info_tick", full)
    account = bridge.call("account_info")
    instrument = build(info, tick=tick,
                       account_currency=records.get(account, "currency"),
                       account_leverage=records.get(account, "leverage"))
    if not instrument.has_quotes:
        notes.append("%s has no live quotes; the market is probably closed" % full)
    return instrument, notes
