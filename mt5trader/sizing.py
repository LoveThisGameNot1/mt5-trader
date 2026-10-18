"""Position sizing and the money math behind it.

One rule drives everything here: **floor, never round up**. A position sized to
0.037 lots when the step is 0.01 becomes 0.03. Rounding it to 0.04 would put on
more risk than the plan authorised, and on a hundred trades that is the
difference between a strategy and a blowup. Every function therefore returns the
largest size that does not exceed the instruction, and says so when the largest
valid size is still too big or too small to be usable.

Everything is derived from the instrument's own ``tick_size`` and
``tick_value_loss``, so the same code sizes:

    EURUSD    tick 0.00001, tick value $1.00   -> $10 per pip per lot
    XAUUSD    tick 0.01,    tick value $1.00   -> $100 per $1 move per lot
    US30      tick 1.0,     tick value $1.00   -> $1 per index point per lot
    BTCUSD    tick 0.01,    tick value $0.01   -> $1 per $1 move per lot

with no pip assumptions and no contract-size constants.
"""

from . import records
from .instruments import InstrumentError


def _result(lots=None, risk=None, reason=None, **extra):
    out = {
        "lots": lots,
        "risk_money": round(risk, 2) if risk is not None else None,
        "reason": reason,
    }
    out.update(extra)
    return out


def risk_per_lot(instrument, entry, stop):
    """Loss in account currency for one lot if *stop* is hit."""
    return instrument.risk_money(1.0, entry, stop)


def size_for_money(instrument, risk_money, entry, stop):
    """Largest valid size whose loss at *stop* stays inside *risk_money*.

    Returns a dict. ``lots`` is None when even the minimum volume would exceed
    the allowance, in which case ``minimum_possible_risk`` says what the
    smallest tradeable size would cost -- the number the model needs in order to
    tell the user something actionable instead of "too small".
    """
    risk_money = records.fnum(risk_money, None)
    if risk_money is None or risk_money <= 0:
        return _result(reason="the risk allowance must be a positive number, got %r"
                              % risk_money)
    entry = records.fnum(entry, None)
    stop = records.fnum(stop, None)
    if entry is None or stop is None:
        return _result(reason="sizing needs both an entry and a stop price")
    if abs(entry - stop) <= 0:
        return _result(reason="entry and stop are the same price; the risk is undefined")

    per_lot = risk_per_lot(instrument, entry, stop)
    if not per_lot or per_lot <= 0:
        return _result(
            reason="cannot price the risk: %s reports tick_size=%s and "
                   "tick_value_loss=%s, which gives no money per price unit"
                   % (instrument.symbol, instrument.tick_size, instrument.tick_value_loss))

    raw = risk_money / per_lot
    minimum_risk = per_lot * instrument.volume_min
    if raw < instrument.volume_min:
        return _result(
            lots=None,
            reason="%.4f lots would be needed to risk %.2f, but the smallest size "
                   "on %s is %.4f lots, which risks %.2f"
                   % (raw, risk_money, instrument.symbol, instrument.volume_min,
                      minimum_risk),
            needed_lots=round(raw, 6),
            minimum_possible_risk=round(minimum_risk, 2),
        )
    try:
        lots = instrument.normalize_volume(raw)
    except InstrumentError as exc:
        return _result(reason=str(exc))

    actual = per_lot * lots
    if actual > risk_money * 1.0000001:
        return _result(
            lots=None,
            reason="no valid size fits: %.4f lots is the floor for the step but it "
                   "risks %.2f against an allowance of %.2f"
                   % (lots, actual, risk_money))

    stop_points = instrument.price_to_points(abs(entry - stop))
    out = _result(
        lots=lots,
        risk=actual,
        requested_lots=round(raw, 6),
        risk_per_lot=round(per_lot, 2),
        stop_distance=round(abs(entry - stop), instrument.digits),
        stop_points=round(stop_points, 1) if stop_points else None,
        value_per_point=round(instrument.value_per_point(lots), 4),
        budget=round(risk_money, 2),
    )
    if raw > instrument.volume_max:
        out["capped_at_max"] = True
        out["note"] = (
            "the exact size would have been %.4f lots, above the symbol maximum of "
            "%.4f, so this position is smaller than the risk allowance allows and "
            "therefore risks less than the budget" % (raw, instrument.volume_max))
    return out


def size_for_percent(instrument, equity, percent, entry, stop):
    """Size from a percentage of equity. Equity, not balance: a position is
    liquidated against equity, so that is what risk scales with."""
    equity = records.fnum(equity, None)
    percent = records.fnum(percent, None)
    if equity is None or equity <= 0:
        return _result(reason="equity must be positive to size from a percentage")
    if percent is None or percent <= 0:
        return _result(reason="the risk percentage must be positive")
    money = equity * percent / 100.0
    out = size_for_money(instrument, money, entry, stop)
    out["equity"] = round(equity, 2)
    out["risk_percent"] = percent
    out["risk_money"] = round(money, 2) if out.get("lots") else out.get("risk_money")
    out["actual_risk_percent"] = (
        round(100.0 * out["risk_money"] / equity, 4) if out.get("lots") else None)
    return out


def reward_risk(entry, stop, tp):
    """R multiple of a take profit. None when the target is on the wrong side."""
    entry, stop, tp = (records.fnum(entry), records.fnum(stop), records.fnum(tp))
    if None in (entry, stop, tp):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return (tp - entry) / (entry - stop)


def r_now(side, entry, stop, price):
    """How many R the current price is worth. Negative means underwater."""
    entry, stop, price = (records.fnum(entry), records.fnum(stop), records.fnum(price))
    if None in (entry, stop, price):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    move = (price - entry) if side == "buy" else (entry - price)
    return move / risk


def margin_required(bridge, instrument, side, lots, price):
    """Real margin from the terminal, not leverage arithmetic.

    ``order_calc_margin`` accounts for the symbol's margin calculation mode,
    the account's hedging mode and any per-symbol margin rate, none of which
    are visible from leverage alone.
    """
    action = 0 if side == "buy" else 1  # ORDER_TYPE_BUY / ORDER_TYPE_SELL
    try:
        value = bridge.call("order_calc_margin", action, instrument.symbol, float(lots),
                            float(price))
    except Exception:  # noqa: BLE001 - a missing answer falls back to the estimate
        value = None
    if value is not None:
        return records.fnum(value, None), "terminal"
    leverage = records.fnum(instrument.account_leverage, None)
    if leverage and price:
        notional = float(lots) * instrument.contract_size * float(price)
        return notional / float(leverage), "estimated from leverage"
    return None, "unavailable"


def projected_profit(bridge, instrument, side, lots, entry, exit_price):
    action = 0 if side == "buy" else 1
    try:
        value = bridge.call("order_calc_profit", action, instrument.symbol, float(lots),
                            float(entry), float(exit_price))
    except Exception:  # noqa: BLE001
        value = None
    if value is not None:
        return records.fnum(value, None), "terminal"
    return instrument.risk_money(lots, entry, exit_price) * (
        1 if (side == "buy") == (exit_price > entry) else -1), "estimated"


def max_lots_for_margin(instrument, free_margin, side, price, reserve_fraction=0.0):
    """Largest size that still leaves the account above its margin floor."""
    free_margin = records.fnum(free_margin, None)
    if free_margin is None or free_margin <= 0:
        return None
    usable = free_margin * (1.0 - float(reserve_fraction))
    leverage = records.fnum(instrument.account_leverage, None)
    if leverage and price and instrument.contract_size:
        notional_per_lot = instrument.contract_size * float(price)
        if notional_per_lot <= 0:
            return None
        return usable * leverage / notional_per_lot
    return None


def check_margin_fit(bridge, instrument, side, lots, price, free_margin,
                     reserve_fraction=0.25):
    """Does this order fit, with room left afterwards?

    A trade that fits exactly and leaves no free margin is a stop-out waiting
    for a bad tick, so the default reserves a quarter of the free margin.
    """
    margin, source = margin_required(bridge, instrument, side, lots, price)
    out = {
        "margin_required": round(margin, 2) if margin is not None else None,
        "margin_source": source,
        "free_margin": round(records.fnum(free_margin, 0.0), 2),
        "reserve_fraction": reserve_fraction,
    }
    if margin is None:
        out["fits"] = None
        out["note"] = "the terminal gave no margin figure; treat this as unverified"
        return out
    reserve = out["free_margin"] * float(reserve_fraction)
    out["reserve_kept"] = round(reserve, 2)
    out["margin_after"] = round(out["free_margin"] - margin, 2)
    out["fits"] = margin <= (out["free_margin"] - reserve)
    if not out["fits"]:
        cap = max_lots_for_margin(instrument, out["free_margin"], side, price,
                                  reserve_fraction)
        out["max_lots_for_this_free_margin"] = round(cap, 4) if cap else None
    return out


def stop_distance(lots, instrument, money):
    """The stop distance in price terms that makes *lots* risk exactly *money*.
    Useful for turning a fixed stop budget into a level when the level is what
    the strategy decides and the size is what the account decides."""
    distance = instrument.price_for_money(lots, money)
    if distance is None:
        return None
    return round(distance, instrument.digits)
