"""Bars, indicators and the shared arithmetic the data tools sit on.

Everything here reads prices through ``copy_rates_from_pos`` and returns plain
lists of dicts, so the same code sees identical shapes whether the terminal hands
back structseq rows or, when numpy is installed, a numpy structured array (see
``records`` for why that distinction is not cosmetic).

Two rules apply to every indicator in this module:

* **Only closed bars.** The last bar returned by MT5 is the one still forming, and
  an indicator computed on it changes under you between two calls. Every function
  here drops it unless the caller explicitly asks for it.
* **Bars are in server time.** An MQL5/MetaTrader bar timestamp is the broker's
  wall clock, grouped by hour and day the way the broker groups them. Session
  statistics below are therefore *server-hour* statistics, which is what a trader
  looking at the terminal sees, and they are labelled as such.
"""

import math

from . import records

TIMEFRAMES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12,
    "M15": 15, "M20": 20, "M30": 30,
    "H1": 1 | 0x4000, "H2": 2 | 0x4000, "H3": 3 | 0x4000, "H4": 4 | 0x4000,
    "H6": 6 | 0x4000, "H8": 8 | 0x4000, "H12": 12 | 0x4000,
    "D1": 24 | 0x4000, "W1": 1 | 0x8000, "MN1": 1 | 0xC000,
}

# Seconds per bar, used to work out how far back a window reaches.
TIMEFRAME_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360, "M10": 600,
    "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600, "H8": 28800,
    "H12": 43200, "D1": 86400, "W1": 604800, "MN1": 2592000,
}

FIELDS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")

# A short in-process cache. The kernel may ask for the same daily series several
# times inside one verdict (correlation against each open position), and there is
# no reason to make the terminal answer the same question five times.
_CACHE = {}
_CACHE_TTL = 300.0


def timeframe_code(name):
    code = TIMEFRAMES.get(str(name or "").upper())
    if code is None:
        raise ValueError("unknown timeframe %r. One of: %s"
                         % (name, ", ".join(sorted(TIMEFRAMES))))
    return code


def _cache_get(key):
    import time
    hit = _CACHE.get(key)
    if not hit:
        return None
    stamp, value = hit
    if time.time() - stamp > _CACHE_TTL:
        del _CACHE[key]
        return None
    return value


def _cache_put(key, value):
    import time
    _CACHE[key] = (time.time(), value)
    return value


def clear_cache():
    _CACHE.clear()


def bars(bridge, symbol, timeframe="H1", count=200, include_forming=False,
         use_cache=False):
    """Closed bars, oldest first, as plain dicts with float fields.

    ``count`` is how many *closed* bars the caller wants; one more is requested
    from the terminal and the forming bar is dropped, unless include_forming is
    set.
    """
    code = timeframe_code(timeframe)
    key = (symbol, code, int(count), bool(include_forming))
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    want = int(count) + (0 if include_forming else 1)
    raw = bridge.call("copy_rates_from_pos", symbol, code, 0, want)
    if raw is None:
        return []
    rows = []
    for row in raw:
        item = records.as_dict(row, FIELDS)
        if not item or "close" not in item:
            continue
        rows.append({
            "time": records.inum(item.get("time"), 0),
            "open": records.fnum(item.get("open"), 0.0),
            "high": records.fnum(item.get("high"), 0.0),
            "low": records.fnum(item.get("low"), 0.0),
            "close": records.fnum(item.get("close"), 0.0),
            "tick_volume": records.inum(item.get("tick_volume"), 0) or 0,
            "real_volume": records.inum(item.get("real_volume"), 0) or 0,
            "spread": records.inum(item.get("spread"), None),
        })
    if not include_forming and rows:
        rows = rows[:-1]
    if use_cache:
        _cache_put(key, rows)
    return rows


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------
def closes(bars_list):
    return [b["close"] for b in bars_list]


def ema(values, period):
    if not values or len(values) < period or period < 1:
        return None
    factor = 2.0 / (period + 1)
    out = sum(values[:period]) / period
    for value in values[period:]:
        out = value * factor + out * (1 - factor)
    return out


def sma(values, period):
    if not values or len(values) < period or period < 1:
        return None
    return sum(values[-period:]) / period


def true_ranges(bars_list):
    out = []
    for index in range(1, len(bars_list)):
        high, low = bars_list[index]["high"], bars_list[index]["low"]
        previous_close = bars_list[index - 1]["close"]
        out.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return out


def atr(bars_list, period=14):
    """Mean true range, not Wilder's smoothing.

    Wilder's is the usual choice and it is what most charts draw. The mean is used
    here because it is exactly reproducible from a stated window with no seed
    state, which matters more for a number that sets a stop than the smoothing
    does.
    """
    spans = true_ranges(bars_list)
    if len(spans) < period:
        return None
    return sum(spans[-period:]) / period


def returns(values):
    """Simple period-over-period returns."""
    return [(values[i] / values[i - 1] - 1.0)
            for i in range(1, len(values)) if values[i - 1]]


def correlation(left, right):
    """Pearson correlation of two already-aligned series."""
    n = min(len(left), len(right))
    if n < 3:
        return None
    a, b = left[-n:], right[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / math.sqrt(var_a * var_b)


def stdev(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def align_returns(series_by_symbol):
    """Returns aligned by timestamp, so two symbols that do not trade at the same
    hours are not compared bar-for-bar across a gap.

    MT5 bar timestamps are the broker's clock; two symbols on one terminal agree
    on it, but a symbol that closed for a day (an index, a stock CFD) does not. The
    intersection of timestamps is what gets correlated, and the caller is told how
    many points that left.
    """
    common = None
    for rows in series_by_symbol.values():
        stamps = {row["time"] for row in rows}
        common = stamps if common is None else (common & stamps)
    common = sorted(common or [])
    if len(common) < 4:
        return {name: [] for name in series_by_symbol}, []
    aligned = {}
    for name, rows in series_by_symbol.items():
        by_time = {row["time"]: row["close"] for row in rows}
        aligned[name] = returns([by_time[stamp] for stamp in common])
    return aligned, common
