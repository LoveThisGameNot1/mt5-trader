"""Structural levels, read from the terminal's own history.

The point of this module is to take "where might this turn" out of the model's
imagination and put it into numbers that a human can check against their chart.
Every level here is either a plain extreme of a completed period or an arithmetic
band, and each one comes back with its distance from the current price in price,
in points and in ATR, so a model can say "the prior day low is 0.3 ATR away"
instead of "there is support below".

Levels are labelled with the date of the bar they come from rather than "today"
or "yesterday", because on a Sunday the newest daily bar belongs to Friday and a
label of "today's open" would be a lie that reads as a fact.
"""

from . import market, records

# How many ATRs away a level stops being worth mentioning.
DEFAULT_REACH_ATR = 2.0


def _distance(price, level):
    return round(price - level, 8) if price is not None else None


def _round_number_steps(price):
    """The steps a human actually reads off a chart for this price magnitude.

    Derived from the order of magnitude rather than from a table, so it works the
    same for 1.1234, 4164, 81920 and 39450: two orders of magnitude below the
    price, and five times that.
    """
    if not price:
        return []
    import math
    order = math.floor(math.log10(abs(price)))
    step = 10.0 ** (order - 2)
    return [step, step * 5, step * 10]


def _instrument_point(instrument):
    if instrument is not None and getattr(instrument, "point", None):
        return instrument.point, instrument.digits
    return None, None


def compute(bridge, symbol, instrument=None, clock=None, reach_atr=DEFAULT_REACH_ATR,
            atr_period=14):
    """Every level this terminal can justify, plus the ones nearest the price."""
    d1 = market.bars(bridge, symbol, "D1", 90, use_cache=True)
    w1 = market.bars(bridge, symbol, "W1", 30, use_cache=True)
    mn1 = market.bars(bridge, symbol, "MN1", 18, use_cache=True)
    h4 = market.bars(bridge, symbol, "H4", 120, use_cache=True)
    h1 = market.bars(bridge, symbol, "H1", 200, use_cache=True)

    point, digits = _instrument_point(instrument)
    if instrument is not None and instrument.has_quotes:
        price = instrument.mid
    elif d1:
        price = d1[-1]["close"]
    else:
        price = None

    atrs = {"D1": market.atr(d1, atr_period), "H4": market.atr(h4, atr_period),
            "H1": market.atr(h1, atr_period)}
    daily_atr = atrs["D1"]

    levels = []

    def add(label, bar, field, source):
        if bar is None:
            return
        value = records.fnum(bar.get(field), None)
        if value is None:
            return
        entry = {
            "level": label,
            "price": round(value, digits) if digits is not None else value,
            "source": source,
            "date": records.iso_time(bar.get("time")),
        }
        if price is not None:
            entry["distance"] = _distance(price, value)
            if point:
                entry["distance_points"] = round((price - value) / point, 1)
            if daily_atr:
                entry["distance_atr"] = round((price - value) / daily_atr, 3)
        levels.append(entry)

    if len(d1) >= 1:
        add("last completed daily high", d1[-1], "high", "D1")
        add("last completed daily low", d1[-1], "low", "D1")
        add("last completed daily close", d1[-1], "close", "D1")
    if len(d1) >= 2:
        add("previous daily high", d1[-2], "high", "D1")
        add("previous daily low", d1[-2], "low", "D1")
    if len(d1) >= 5:
        window = d1[-5:]
        add("5-day high", {"high": max(b["high"] for b in window),
                           "time": window[-1]["time"]}, "high", "D1 x5")
        add("5-day low", {"low": min(b["low"] for b in window),
                          "time": window[-1]["time"]}, "low", "D1 x5")
    if len(w1) >= 1:
        add("last completed weekly high", w1[-1], "high", "W1")
        add("last completed weekly low", w1[-1], "low", "W1")
        add("last completed weekly open", w1[-1], "open", "W1")
    if len(mn1) >= 1:
        add("last completed monthly high", mn1[-1], "high", "MN1")
        add("last completed monthly low", mn1[-1], "low", "MN1")

    if price is not None and daily_atr:
        for label, sign in (("price + 1 ATR (D1)", 1.0), ("price - 1 ATR (D1)", -1.0)):
            levels.append({
                "level": label,
                "price": round(price + sign * daily_atr, digits or 8),
                "source": "ATR band",
                "date": None,   # arithmetic, not a bar, so it has no date
                "distance": round(-sign * daily_atr, 8),
                "distance_atr": -sign,
            })

    reach = (daily_atr * reach_atr) if daily_atr else None

    # Round numbers are always reported, whether or not they are near: they are the
    # grid a human reads off a chart, and 'the nearest round number is 0.4 ATR away'
    # is a useful answer even when none is close. The ATR reach filters the *nearest*
    # list, not this one.
    round_numbers = []
    if price is not None:
        for step in _round_number_steps(price):
            if not step:
                continue
            below = (price // step) * step
            for candidate in (below, below + step):
                distance = abs(candidate - price)
                round_numbers.append({
                    "price": round(candidate, digits) if digits is not None else candidate,
                    "step": step,
                    "distance_points": (round((price - candidate) / point, 1) if point else None),
                    "distance_atr": (round((price - candidate) / daily_atr, 3)
                                     if daily_atr else None),
                    "within_reach": bool(reach is not None and distance <= reach),
                })
        seen, unique = set(), []
        for item in sorted(round_numbers, key=lambda x: abs(x["price"] - price)):
            key = round(item["price"], digits or 8)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        round_numbers = unique[:6]

    if price is not None and reach:
        nearby = [lvl for lvl in levels if abs(lvl.get("distance") or 0) <= reach]
        for item in round_numbers:
            if item["within_reach"]:
                nearby.append({
                    "level": "round number %.6g" % item["step"],
                    "price": item["price"],
                    "source": "round number",
                    "date": None,
                    "distance": (round(price - item["price"], 8) if price is not None else None),
                    "distance_points": item["distance_points"],
                    "distance_atr": item["distance_atr"],
                })
    else:
        nearby = list(levels)
    nearby.sort(key=lambda lvl: abs(lvl.get("distance") or 0))

    return {
        "symbol": symbol,
        "price": price,
        "digits": digits,
        "point": point,
        "atr": {name: (round(value, digits or 8) if value else None)
                for name, value in atrs.items()},
        "atr_source": "mean true range of closed bars, not Wilder's smoothing",
        "levels": levels,
        "nearest": nearby[:6],
        "round_numbers": round_numbers[:8],
        "round_number_rule": ("steps of two orders of magnitude below the price, and "
                              "five times that; a human reads these off a chart"),
        "reach_atr": reach_atr,
        "recent_bars": {
            "D1": len(d1), "W1": len(w1), "MN1": len(mn1), "H4": len(h4), "H1": len(h1),
        },
        "caveat": ("levels are labelled with the date of the bar they come from, not "
                   "with 'today': on a Monday the newest completed daily bar belongs "
                   "to Friday. The forming bar is never used."),
    }
