"""Session and volatility statistics, from the terminal's own hourly bars.

"Should I trade now" is a different question from "what should I trade", and it is
answerable from data the terminal already has. This module turns H1 bars into the
range the market actually delivered at each server hour, groups those hours into
the three sessions a trader names, and says how the current hour compares with the
average day.

Everything is in **server hours**, which is what the terminal's own charts and
clock show. The offset between server time and UTC is measured (see
``history.server_offset``) and used to place the UTC session windows on the server
clock, so a broker at UTC+3 gets London at 10:00-19:00 server, not at 07:00-16:00.
Getting that wrong shifts every session by three hours and makes the numbers look
like noise.
"""

from . import market, records

import datetime

# The conventional windows, in UTC. Converted to server hours at call time.
SESSIONS_UTC = (
    ("Sydney", 21, 6),
    ("Tokyo", 0, 9),
    ("London", 7, 16),
    ("New York", 12, 21),
)

# The window where the two biggest desks overlap, also in UTC.
OVERLAP_UTC = ("London/New York", 12, 16)

DEFAULT_DAYS = 20


def _server_hour(utc_hour, offset_seconds):
    return int((utc_hour + round(offset_seconds / 3600.0))) % 24


def _window_label(hours):
    if len(hours) >= 24:
        return "all day"
    return "%02d:00-%02d:00" % (hours[0], (hours[-1] + 1) % 24)


def _hours_in_window(start, end):
    if start == end:
        return list(range(24))
    if start < end:
        return list(range(start, end))
    return list(range(start, 24)) + list(range(0, end))


def compute(bridge, symbol, instrument=None, clock=None, days=DEFAULT_DAYS,
            timeframe="H1"):
    """Per-server-hour range statistics and the named sessions built from them."""
    needed = int(days) * (24 if timeframe == "H1" else 6)
    rows = market.bars(bridge, symbol, timeframe, min(needed, 5000), use_cache=True)
    if not rows:
        return {"symbol": symbol, "bars": 0,
                "note": "the terminal returned no bars for this symbol; session "
                        "statistics need history"}

    offset = clock.offset if clock is not None else 0
    buckets = {hour: {"bars": 0, "range": 0.0, "move": 0.0} for hour in range(24)}
    for row in rows:
        try:
            moment = datetime.datetime.utcfromtimestamp(row["time"])
        except (OverflowError, OSError, ValueError):
            continue
        hour = moment.hour
        span = row["high"] - row["low"]
        move = abs(row["close"] - row["open"])
        bucket = buckets[hour]
        bucket["bars"] += 1
        bucket["range"] += span
        bucket["move"] += move

    by_hour = []
    total_range = 0.0
    for hour in range(24):
        bucket = buckets[hour]
        avg_range = (bucket["range"] / bucket["bars"]) if bucket["bars"] else None
        if avg_range:
            total_range += avg_range
        by_hour.append({
            "server_hour": hour,
            "bars": bucket["bars"],
            "avg_range": round(avg_range, 8) if avg_range else None,
            "avg_body": round(bucket["move"] / bucket["bars"], 8) if bucket["bars"] else None,
        })
    for item in by_hour:
        item["range_share_pct"] = (
            round(100.0 * item["avg_range"] / total_range, 2)
            if item["avg_range"] and total_range else None)

    sessions = []
    for name, start_utc, end_utc in SESSIONS_UTC:
        hours = _hours_in_window(_server_hour(start_utc, offset),
                                 _server_hour(end_utc, offset))
        spans = [buckets[h]["range"] for h in hours if buckets[h]["bars"]]
        counts = sum(buckets[h]["bars"] for h in hours)
        if not spans or not counts:
            continue
        per_bar = sum(spans) / counts
        sessions.append({
            "session": name,
            # No "share of the day" figure here on purpose: these windows overlap
            # (Tokyo and London, London and New York), so shares would double count
            # and add up to more than a hundred percent. Per-hour range does not.
            "server_hours": _window_label(hours),
            "hours": hours,
            "bars": counts,
            "avg_range_per_hour": round(per_bar, 8),
            "avg_range_over_session": round(per_bar * len(hours), 8),
        })

    now_hour = clock.now.hour if clock is not None else None
    current = None
    if now_hour is not None:
        current = {
            "server_hour": now_hour,
            "hour_avg_range": by_hour[now_hour]["avg_range"],
            "hour_share_pct": by_hour[now_hour]["range_share_pct"],
            "sessions_open": [s["session"] for s in sessions if now_hour in s["hours"]],
            "vs_fair_share": (
                round(by_hour[now_hour]["range_share_pct"] / (100.0 / 24), 2)
                if by_hour[now_hour]["range_share_pct"] else None),
        }
    overlap_hours = _hours_in_window(_server_hour(OVERLAP_UTC[1], offset),
                                     _server_hour(OVERLAP_UTC[2], offset))

    busiest = sorted([h for h in by_hour if h["avg_range"]],
                     key=lambda h: h["avg_range"], reverse=True)[:6]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars_used": len(rows),
        "days_covered": round(len(rows) / 24.0, 1) if timeframe == "H1" else None,
        "server_offset_hours": round(offset / 3600.0, 2),
        "by_server_hour": by_hour,
        "sessions": sessions,
        "current": current,
        "overlap": {"name": OVERLAP_UTC[0], "server_hours": overlap_hours},
        "busiest_server_hours": [h["server_hour"] for h in busiest],
        "quietest_server_hours": [
            h["server_hour"] for h in sorted(
                [h for h in by_hour if h["avg_range"]], key=lambda h: h["avg_range"])[:6]],
        "caveat": ("server-hour statistics from the terminal's own bars. A broker at "
                   "UTC+3 shows London at 10:00-19:00 server, and these numbers follow "
                   "that clock because that is the clock the charts use. A window with "
                   "no bars is reported as null rather than as zero: the market was "
                   "shut, which is not the same as quiet."),
    }
